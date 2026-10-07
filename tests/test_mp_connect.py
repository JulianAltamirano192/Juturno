"""
Tests del flujo OAuth de Mercado Pago (Tarea 2 del plan MP por tenant).

Red: todas las llamadas a MP se monkeypatchean. Redis es real (corre en
el stack local) — el state se guarda y se consume de verdad.
"""

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app import mp_connect
from app.auth import hash_api_key
from app.models import ApiKey, Tenant
from app.mp_crypto import decrypt_token, encrypt_token

TEST_FERNET_KEY = Fernet.generate_key().decode()
FAKE_TOKEN_RESPONSE = {
    "access_token": "APP_USR-access-oauth-del-vendedor",
    "refresh_token": "TG-refresh-oauth-del-vendedor",
    "expires_in": 15552000,  # 180 días, como documenta MP
    "user_id": 1234567890,
    "token_type": "Bearer",
    "scope": "offline_access read write",
}
FAKE_USER_PROFILE = {"id": 1234567890, "nickname": "negocio.demo"}


@pytest.fixture(autouse=True)
def _mp_oauth_settings(monkeypatch):
    """Config OAuth de MP válida + clave Fernet para cifrar tokens."""
    monkeypatch.setattr(
        mp_connect.settings, "MP_MARKETPLACE_CLIENT_ID", "1234567890123456"
    )
    monkeypatch.setattr(
        mp_connect.settings, "MP_MARKETPLACE_CLIENT_SECRET", "app-secret"
    )
    monkeypatch.setattr(
        mp_connect.settings,
        "MP_MARKETPLACE_REDIRECT_URL",
        "https://api.juturno.com/mp/connect/callback",
    )
    monkeypatch.setattr(mp_connect.settings, "MP_TOKEN_ENCRYPTION_KEY", TEST_FERNET_KEY)


async def _tenant_with_api_key(db_session):
    tenant = Tenant(name="Negocio Demo", slug="demo", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()
    raw_key = f"test-key-tenant-{tenant.id}"
    db_session.add(ApiKey(tenant_id=tenant.id, key_hash=hash_api_key(raw_key)))
    await db_session.commit()
    return tenant, {"X-Tenant-API-Key": raw_key}


def _patch_mp_exchange(monkeypatch, token_resp=None, profile=None):
    async def fake_exchange(code: str):
        return token_resp if token_resp is not None else FAKE_TOKEN_RESPONSE

    async def fake_profile(access_token: str):
        return (
            (
                str(FAKE_USER_PROFILE["id"]),
                FAKE_USER_PROFILE["nickname"],
            )
            if profile is None
            else profile
        )

    monkeypatch.setattr(mp_connect, "_exchange_code_for_tokens", fake_exchange)
    monkeypatch.setattr(mp_connect, "_fetch_mp_profile", fake_profile)


# ---------------------------------------------------------------------------
# GET /mp/connect/start (removed: a bare API-key link can't be bound to the
# browser that completes the callback — the panel flow replaces it)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_connect_start_is_removed(client, db_session):
    _tenant, headers = await _tenant_with_api_key(db_session)
    res = await client.get("/mp/connect/start", headers=headers)
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# GET /mp/connect/callback
# ---------------------------------------------------------------------------

STATE_COOKIE = "mp_oauth_state"


async def _started_state(tenant: Tenant) -> str:
    """State registrado como lo hace POST /panel/mp/connect/start."""
    state = f"state-test-{tenant.id}"
    await mp_connect._store_state(state, tenant.id)
    return state


@pytest.mark.asyncio
async def test_callback_completes_connection(client, db_session, monkeypatch):
    """Flujo feliz: state válido + cookie del navegador → credenciales cifradas."""
    tenant, _headers = await _tenant_with_api_key(db_session)
    _patch_mp_exchange(monkeypatch)
    state = await _started_state(tenant)

    res = await client.get(
        "/mp/connect/callback",
        params={"code": "TG-code-ok", "state": state},
        cookies={STATE_COOKIE: state},
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert res.headers["location"].endswith("/panel/settings?mp=connected")
    assert "oauth-del-vendedor" not in res.text

    # En DB: ciphertext, no texto plano, y expiración seteada
    raw = await db_session.execute(
        text(
            "SELECT mp_user_id, mp_alias, mp_access_token_enc, "
            "mp_refresh_token_enc, mp_token_expires_at "
            "FROM tenant WHERE id = :tid"
        ).bindparams(tid=tenant.id)
    )
    row = raw.one()
    assert row.mp_user_id == "1234567890"
    assert row.mp_alias == "negocio.demo"
    assert "oauth-del-vendedor" not in row.mp_access_token_enc
    assert decrypt_token(row.mp_access_token_enc) == FAKE_TOKEN_RESPONSE["access_token"]
    assert (
        decrypt_token(row.mp_refresh_token_enc) == FAKE_TOKEN_RESPONSE["refresh_token"]
    )
    assert row.mp_token_expires_at is not None


@pytest.mark.asyncio
async def test_callback_rejects_invalid_state(client):
    res = await client.get(
        "/mp/connect/callback",
        params={"code": "TG-code", "state": "state-inventado"},
        cookies={STATE_COOKIE: "state-inventado"},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_callback_state_is_single_use(client, db_session, monkeypatch):
    """Un state consumido no sirve ni para el mismo code ni otro."""
    tenant, _headers = await _tenant_with_api_key(db_session)
    _patch_mp_exchange(monkeypatch)
    state = await _started_state(tenant)

    url = f"/mp/connect/callback?code=TG-code&state={state}"
    cookies = {STATE_COOKIE: state}
    first = await client.get(url, cookies=cookies, follow_redirects=False)
    assert first.status_code == 302
    # Segundo uso del mismo state → rechazado
    assert (await client.get(url, cookies=cookies)).status_code == 400


@pytest.mark.asyncio
async def test_callback_handles_mp_cancellation(client):
    """El dueño canceló en la pantalla de MP: vuelve al panel con error."""
    res = await client.get(
        "/mp/connect/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert res.headers["location"].endswith("/panel/settings?mp=error")


# ---------------------------------------------------------------------------
# Estado y desconexión (Tarea 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mp_not_connected(client, db_session):
    _tenant, headers = await _tenant_with_api_key(db_session)

    res = await client.get("/tenants/me/mp", headers=headers)
    assert res.status_code == 200
    assert res.json() == {
        "connected": False,
        "mp_user_id": None,
        "mp_alias": None,
        "mp_token_expires_at": None,
    }


@pytest.mark.asyncio
async def test_get_mp_connected_shows_metadata_without_tokens(
    client, db_session, monkeypatch
):
    """El estado expone user_id/alias/expiración, nunca los tokens."""
    tenant, headers = await _tenant_with_api_key(db_session)
    tenant.mp_user_id = "1234567890"
    tenant.mp_alias = "negocio.demo"
    tenant.mp_access_token_enc = encrypt_token("APP_USR-access-del-vendedor")
    tenant.mp_refresh_token_enc = encrypt_token("TG-refresh-del-vendedor")
    db_session.add(tenant)
    await db_session.commit()

    res = await client.get("/tenants/me/mp", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is True
    assert body["mp_user_id"] == "1234567890"
    assert body["mp_alias"] == "negocio.demo"
    # Garantía: los tokens no viajan en la respuesta, ni cifrados
    body_str = str(body).lower()
    assert "access" not in body_str and "refresh" not in body_str


@pytest.mark.asyncio
async def test_delete_mp_clears_connection(client, db_session):
    tenant, headers = await _tenant_with_api_key(db_session)
    tenant.mp_user_id = "99999"
    tenant.mp_alias = "demo.bye"
    tenant.mp_access_token_enc = encrypt_token("APP_USR-token-a-borrar")
    db_session.add(tenant)
    await db_session.commit()

    res = await client.delete("/tenants/me/mp", headers=headers)
    assert res.status_code == 200
    assert res.json()["disconnected"] is True

    await db_session.refresh(tenant)
    assert tenant.mp_access_token_enc is None
    assert tenant.mp_refresh_token_enc is None
    assert tenant.mp_user_id is None
    assert tenant.mp_alias is None

    follow_up = await client.get("/tenants/me/mp", headers=headers)
    assert follow_up.json()["connected"] is False


@pytest.mark.asyncio
async def test_delete_mp_is_idempotent_when_never_connected(client, db_session):
    _tenant, headers = await _tenant_with_api_key(db_session)
    # Nunca conectó, pero el DELETE no falla
    res = await client.delete("/tenants/me/mp", headers=headers)
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_get_and_delete_require_api_key(client):
    assert (await client.get("/tenants/me/mp")).status_code == 401
    assert (await client.delete("/tenants/me/mp")).status_code == 401
