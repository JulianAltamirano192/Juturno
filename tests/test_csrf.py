"""CSRF double-submit: the token must survive navigation between panel pages."""

import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.csrf import CSRF_COOKIE_NAME, generate_csrf_token
from app.main import app
from app.models import Tenant
from app.password import hash_password
from app.session import SESSION_MAX_AGE_SECONDS, create_session_token
from tests.conftest import TestingSessionLocal

_FORM_TOKEN = re.compile(r'name="csrf_token" value="([0-9a-f]+)"')
_TOKEN_IN_COOKIE = re.compile(r"csrf_token=[0-9a-f]{64};")


async def _make_tenant(slug: str) -> Tenant:
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="CSRF Biz",
            slug=slug,
            owner_email=f"{slug}@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
    return tenant


@pytest.mark.asyncio
async def test_form_from_another_tab_still_submits():
    """Opening a second panel page must not invalidate the first page's form."""
    tenant = await _make_tenant("csrf-two-tabs")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(
            "juturno_session",
            create_session_token(tenant.id, tenant.session_version),
        )
        tab_a = await client.get("/panel/services/new")
        token_a = _FORM_TOKEN.search(tab_a.text).group(1)

        await client.get("/panel/staff/new")  # second tab

        resp = await client.post(
            "/panel/services/new",
            data={
                "name": "Corte",
                "duration_minutes": "30",
                "price": "1000",
                "deposit_amount": "",
                "csrf_token": token_a,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_invalid_cookie_value_is_replaced():
    """A cookie that is not a token we issued is never echoed into the page."""
    tenant = await _make_tenant("csrf-bad-cookie")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(
            "juturno_session",
            create_session_token(tenant.id, tenant.session_version),
        )
        client.cookies.set(CSRF_COOKIE_NAME, "not-a-token")
        resp = await client.get("/panel/services/new")

    token = _FORM_TOKEN.search(resp.text).group(1)
    assert token != "not-a-token"
    assert len(token) == len(generate_csrf_token())


@pytest.mark.asyncio
async def test_csrf_cookie_lives_as_long_as_the_session():
    """A panel tab left open for hours must still be able to log out: the
    CSRF cookie used to expire after 2 h while the session lasts 14 days,
    so POST /logout answered a bare JSON 403."""
    tenant = await _make_tenant("csrf-cookie-lifetime")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(
            "juturno_session",
            create_session_token(tenant.id, tenant.session_version),
        )
        resp = await client.get("/panel/services/new")

    set_cookie = next(
        h for h in resp.headers.get_list("set-cookie") if h.startswith("csrf_token=")
    )
    assert f"Max-Age={SESSION_MAX_AGE_SECONDS}" in set_cookie


def _csrf_set_cookie(resp) -> str:
    return next(
        h for h in resp.headers.get_list("set-cookie") if h.startswith("csrf_token=")
    )


@pytest.mark.asyncio
async def test_login_page_reuses_existing_token():
    old = generate_csrf_token()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, old)
        resp = await client.get("/login")
    assert _FORM_TOKEN.search(resp.text).group(1) == old


@pytest.mark.asyncio
async def test_login_rotates_token():
    """The pre-login token must not survive into the new session (shared
    browser: a previous user could know it)."""
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="CSRF Login",
            slug="csrf-login-rotate",
            owner_email="csrf-login@test.com",
            password_hash=hash_password("supersecret123"),
            session_version=1,
        )
        session.add(tenant)
        await session.commit()

    old = generate_csrf_token()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, old)
        resp = await client.post(
            "/login",
            data={
                "owner_email": "csrf-login@test.com",
                "password": "supersecret123",
                "csrf_token": old,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    new_cookie = _csrf_set_cookie(resp)
    assert old not in new_cookie
    assert _TOKEN_IN_COOKIE.match(new_cookie)


@pytest.mark.asyncio
async def test_logout_deletes_token():
    tenant = await _make_tenant("csrf-logout-delete")
    token = generate_csrf_token()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(
            "juturno_session",
            create_session_token(tenant.id, tenant.session_version),
        )
        client.cookies.set(CSRF_COOKIE_NAME, token)
        resp = await client.post(
            "/logout", data={"csrf_token": token}, follow_redirects=False
        )
    assert resp.status_code == 303
    assert 'csrf_token=""' in _csrf_set_cookie(resp)
