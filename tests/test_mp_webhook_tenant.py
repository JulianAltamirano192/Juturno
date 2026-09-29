"""
Tarea 5: el webhook resuelve con qué token consultar el pago en MP según
qué cuenta lo recibió (lectura de "user_id" en el payload → tenant).

El módulo bajo test es app/mp_webhooks; get_payment_details queda
monkeypatcheado para capturar con qué token salió la llamada a MP.
"""

import pytest
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app import mp_webhooks
from app.models import Tenant
from app.mp_crypto import encrypt_token
from tests.test_mp_webhooks import (
    _create_booking,
    _make_payment_details,
    _sign_webhook,
)

PLATFORM_TOKEN = "TEST-token-plataforma"
TENANT_A_TOKEN = "APP_USR-token-negocio-A"
TENANT_B_TOKEN = "APP_USR-token-negocio-B"


@pytest.fixture(autouse=True)
def _webhook_settings(monkeypatch):
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", "test-secret")
    monkeypatch.setattr(mp_webhooks.settings, "MP_ACCESS_TOKEN", PLATFORM_TOKEN)
    from cryptography.fernet import Fernet

    monkeypatch.setattr(
        mp_webhooks.settings,
        "MP_TOKEN_ENCRYPTION_KEY",
        Fernet.generate_key().decode(),
    )


async def _tenant_with_mp(db_session, mp_user_id: str, access_token: str) -> Tenant:
    tenant = Tenant(
        name=f"Tenant MP {mp_user_id}",
        slug=f"t-{mp_user_id}",
        timezone="UTC",
        mp_user_id=mp_user_id,
        mp_access_token_enc=encrypt_token(access_token),
        mp_refresh_token_enc=encrypt_token("TG-refresh"),
    )
    db_session.add(tenant)
    await db_session.commit()
    return tenant


def _patch_payment_fetch(monkeypatch, captured: dict, booking_id: int) -> None:
    """Simula MP devolviendo un pago; captura el access_token usado."""

    async def fake_get_details(
        data_id: str, access_token: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        captured["data_id"] = data_id
        captured["access_token"] = access_token
        return _make_payment_details("approved", f"booking-{booking_id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", fake_get_details)


def _post_webhook(client, data_id: str, mp_user_id: Optional[str]):
    ts = int(datetime.now(timezone.utc).timestamp())
    request_id = f"req-{data_id}"
    signature = _sign_webhook(data_id, request_id, ts, "test-secret")
    payload = {
        "id": f"evt-{data_id}",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    # user_id va a nivel raíz (lo que MP envía en pagos)
    if mp_user_id is not None:
        payload["user_id"] = mp_user_id
    return client.post(
        "/webhooks/mercadopago",
        json=payload,
        headers={"x-signature": signature, "x-request-id": request_id},
    )


# ---------------------------------------------------------------------------
# Resolución de token por tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_uses_tenant_token_when_user_id_matches(
    client, db_session, monkeypatch
):
    booking = await _create_booking(db_session, "wb-tenant-a")
    await _tenant_with_mp(db_session, "vendor-a-id", TENANT_A_TOKEN)
    captured: dict = {}
    _patch_payment_fetch(monkeypatch, captured, booking_id=booking.id)

    res = await _post_webhook(client, "pay-tenant-a", mp_user_id="vendor-a-id")

    assert res.status_code == 200
    assert captured["access_token"] == TENANT_A_TOKEN
    assert captured["data_id"] == "pay-tenant-a"


@pytest.mark.asyncio
async def test_webhook_uses_platform_token_when_user_id_belongs_to_platform(
    client, db_session, monkeypatch
):
    """El pago es de la cuenta de la plataforma (no hay tenant con ese
    user_id) → el fallback de MP usa el token global."""
    booking = await _create_booking(db_session, "wb-platform")
    await _tenant_with_mp(db_session, "vendor-a-id", TENANT_A_TOKEN)
    captured: dict = {}
    _patch_payment_fetch(monkeypatch, captured, booking_id=booking.id)

    res = await _post_webhook(client, "pay-platform", mp_user_id="el-user-id-de-julian")

    assert res.status_code == 200
    assert captured["access_token"] is None  # None → get_payment_details usa el de MP


@pytest.mark.asyncio
async def test_webhook_falls_back_when_tenant_has_no_connected_account(
    client, db_session, monkeypatch
):
    """Tenant existe pero nunca conectó MP → token de plataforma."""
    booking = await _create_booking(db_session, "wb-no-mp")
    captured: dict = {}
    _patch_payment_fetch(monkeypatch, captured, booking_id=booking.id)

    res = await _post_webhook(client, "pay-no-mp", mp_user_id="tenant-sin-mp-user-id")

    assert res.status_code == 200
    assert captured["access_token"] is None


@pytest.mark.asyncio
async def test_webhook_without_user_id_uses_platform_token(
    client, db_session, monkeypatch
):
    """Evento viejo o mal formado sin user_id → token de plataforma."""
    booking = await _create_booking(db_session, "wb-legacy")
    captured: dict = {}
    _patch_payment_fetch(monkeypatch, captured, booking_id=booking.id)

    res = await _post_webhook(client, "pay-legacy", mp_user_id=None)

    assert res.status_code == 200
    assert captured["access_token"] is None


@pytest.mark.asyncio
async def test_two_connected_tenants_webhooks_use_their_own_tokens(
    client, db_session, monkeypatch
):
    """El aislamiento entre tenants: cada webhook resuelve al token
    del tenant DUEÑO del pago, no al de otro."""
    booking = await _create_booking(db_session, "wb-multi")
    await _tenant_with_mp(db_session, "vendor-a-id", TENANT_A_TOKEN)
    await _tenant_with_mp(db_session, "vendor-b-id", TENANT_B_TOKEN)

    seen = {}

    async def fake_get_details(
        data_id: str, access_token: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        seen[data_id] = access_token
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", fake_get_details)

    # Pagos de cada tenant, con su propio user_id
    await _post_webhook(client, "pay-de-A", mp_user_id="vendor-a-id")
    await _post_webhook(client, "pay-de-B", mp_user_id="vendor-b-id")

    assert seen["pay-de-A"] == TENANT_A_TOKEN
    assert seen["pay-de-B"] == TENANT_B_TOKEN
    # Ninguno tocó el token de la plataforma ni el del otro negocio
    assert seen["pay-de-A"] != TENANT_B_TOKEN
    assert seen["pay-de-A"] != PLATFORM_TOKEN
