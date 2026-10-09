import hashlib
import hmac
import json

import pytest

from app import webhooks


@pytest.mark.asyncio
async def test_whatsapp_webhook_handshake(client, monkeypatch):
    """Test: Verificar handshake GET de Meta con META_VERIFY_TOKEN correcto."""
    token = "my-secret-verify-token"
    monkeypatch.setattr(webhooks.settings, "META_VERIFY_TOKEN", token)

    res = await client.get(
        f"/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token={token}&hub.challenge=12345"
    )
    assert res.status_code == 200
    assert res.text == "12345"


@pytest.mark.asyncio
async def test_whatsapp_webhook_handshake_uses_constant_time_compare(
    client, monkeypatch
):
    """The verify token must be compared with hmac.compare_digest, not ==."""
    monkeypatch.setattr(webhooks.settings, "META_VERIFY_TOKEN", "expected-token")
    calls = []
    real_compare = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(webhooks.hmac, "compare_digest", spy)

    res = await client.get(
        "/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=wrong-token&hub.challenge=1"
    )
    assert res.status_code == 403
    assert calls == [(b"wrong-token", b"expected-token")]


@pytest.mark.asyncio
async def test_whatsapp_webhook_handshake_non_ascii_token_returns_403(
    client, monkeypatch
):
    """A non-ASCII token must be rejected with 403, not crash with 500."""
    monkeypatch.setattr(webhooks.settings, "META_VERIFY_TOKEN", "expected-token")

    res = await client.get(
        "/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=%C3%B1and%C3%BA&hub.challenge=1"
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_whatsapp_webhook_valid_signature(client, monkeypatch, caplog):
    """Test: Webhook POST con firma HMAC válida retorna 200, sin loguear el
    teléfono ni el texto del cliente (llegarían a Sentry como breadcrumb)."""
    caplog.set_level("INFO", logger="app.webhooks")
    secret = "meta-app-secret-key"
    monkeypatch.setattr(webhooks.settings, "META_APP_SECRET", secret)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123",
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "5491112345678",
                                    "id": "wamid.1",
                                    "text": {"body": "Hola"},
                                }
                            ]
                        }
                    }
                ],
            }
        ],
    }
    raw_body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    res = await client.post("/webhooks/whatsapp", content=raw_body, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_RECEIVED"
    assert "wamid.1" in caplog.text
    assert "5491112345678" not in caplog.text
    assert "Hola" not in caplog.text


@pytest.mark.asyncio
async def test_whatsapp_webhook_invalid_signature_returns_401(client, monkeypatch):
    """Test: Webhook POST con firma HMAC inválida retorna 401 (Criterio de Done)."""
    secret = "meta-app-secret-key"
    monkeypatch.setattr(webhooks.settings, "META_APP_SECRET", secret)

    payload = {"object": "whatsapp_business_account"}
    raw_body = json.dumps(payload).encode("utf-8")
    headers = {
        "X-Hub-Signature-256": "sha256=invalid_hash_signature_1234567890",
        "Content-Type": "application/json",
    }

    res = await client.post("/webhooks/whatsapp", content=raw_body, headers=headers)
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_whatsapp_webhook_missing_signature_returns_401(client, monkeypatch):
    """Test: Webhook POST sin header de firma cuando META_APP_SECRET está seteado retorna 401."""
    secret = "meta-app-secret-key"
    monkeypatch.setattr(webhooks.settings, "META_APP_SECRET", secret)

    payload = {"object": "whatsapp_business_account"}

    res = await client.post("/webhooks/whatsapp", json=payload)
    assert res.status_code == 401
