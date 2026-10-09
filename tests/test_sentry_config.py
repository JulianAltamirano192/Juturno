import json
import logging
from uuid import uuid4

import httpx
import pytest
import sentry_sdk
from fastapi import FastAPI, Request
from sentry_sdk.transport import Transport

from app.main import sentry_options


def test_sentry_events_do_not_include_local_variables():
    """A logged exception must not ship frame locals (MP secrets) to Sentry."""
    events = []

    class CaptureTransport(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.type == "event":
                    events.append(item.payload.json)

    # Generated at runtime so the values never appear in the source context
    # lines that Sentry attaches to each frame.
    secret, token = uuid4().hex, uuid4().hex

    def refresh():
        body = {"client_secret": secret, "refresh_token": token}  # noqa: F841
        raise RuntimeError("boom")

    opts = sentry_options("https://key@o0.ingest.sentry.io/0")
    sentry_sdk.init(**opts, transport=CaptureTransport)
    try:
        try:
            refresh()
        except RuntimeError:
            logging.getLogger("test").exception("refresh failed")
        sentry_sdk.flush()
    finally:
        sentry_sdk.init(dsn="")  # disable Sentry; "" avoids reading SENTRY_DSN

    assert len(events) == 1
    dump = json.dumps(events)
    assert secret not in dump
    assert token not in dump


@pytest.mark.asyncio
async def test_sentry_request_events_do_not_include_secrets_or_pii():
    """Neither error events nor transactions may ship the tenant API key,
    the request body (client phone numbers) or the query string (OAuth code,
    verify token)."""
    events = []

    class CaptureTransport(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.type in ("event", "transaction"):
                    events.append(item.payload.json)

    api_key, phone, code = uuid4().hex, uuid4().hex, uuid4().hex

    opts = sentry_options("https://key@o0.ingest.sentry.io/0")
    # Sample every transaction: they carry request data too.
    opts["traces_sample_rate"] = 1.0
    sentry_sdk.init(**opts, transport=CaptureTransport)
    try:
        app = FastAPI()

        @app.post("/boom")
        async def boom(request: Request):
            await request.json()
            raise RuntimeError("boom")

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            res = await client.post(
                f"/boom?code={code}",
                headers={"X-Tenant-API-Key": api_key},
                json={"client_phone": phone},
            )
        sentry_sdk.flush()
    finally:
        sentry_sdk.init(dsn="")  # disable Sentry; "" avoids reading SENTRY_DSN

    assert res.status_code == 500
    assert sorted(e.get("type", "event") for e in events) == [
        "event",
        "transaction",
    ]
    dump = json.dumps(events)
    assert api_key not in dump
    assert phone not in dump
    assert code not in dump


def test_sentry_drops_uvicorn_access_log_breadcrumbs():
    """uvicorn's access log line carries the full query string (OAuth code,
    verify token) and would ride along as a breadcrumb on the next error."""
    events = []

    class CaptureTransport(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.type == "event":
                    events.append(item.payload.json)

    code = uuid4().hex
    sentry_sdk.init(
        **sentry_options("https://key@o0.ingest.sentry.io/0"),
        transport=CaptureTransport,
    )
    try:
        logging.getLogger("uvicorn.access").info(
            '127.0.0.1 - "GET /mp/connect/callback?code=%s HTTP/1.1" 302', code
        )
        logging.getLogger("test").info("kept breadcrumb")
        logging.getLogger("test").error("boom")
        sentry_sdk.flush()
    finally:
        sentry_sdk.init(dsn="")  # disable Sentry; "" avoids reading SENTRY_DSN

    assert len(events) == 1
    dump = json.dumps(events)
    assert "kept breadcrumb" in dump
    assert code not in dump
