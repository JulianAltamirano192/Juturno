"""Tests para /panel/settings y el flujo OAuth de MP desde el panel."""

import re
from datetime import datetime, timedelta, timezone

import pytest
import redis.asyncio as redis
from cryptography.fernet import Fernet

from app import mp_connect
from app.models import Booking, Payment, Service, Tenant
from app.mp_crypto import encrypt_token
from app.session import create_session_token

TEST_FERNET_KEY = Fernet.generate_key().decode()
STATE_COOKIE = "mp_oauth_state"


@pytest.fixture(autouse=True)
def _mp_oauth_settings(monkeypatch):
    monkeypatch.setattr(
        mp_connect.settings, "MP_MARKETPLACE_CLIENT_ID", "test-client-id"
    )
    monkeypatch.setattr(
        mp_connect.settings, "MP_MARKETPLACE_CLIENT_SECRET", "test-secret"
    )
    monkeypatch.setattr(
        mp_connect.settings,
        "MP_MARKETPLACE_REDIRECT_URL",
        "https://api.juturno.com/mp/connect/callback",
    )
    monkeypatch.setattr(mp_connect.settings, "MP_TOKEN_ENCRYPTION_KEY", TEST_FERNET_KEY)


def _make_cookie(tenant: Tenant) -> str:
    return create_session_token(tenant.id, tenant.session_version)


async def _create_tenant(db_session, suffix: str) -> Tenant:
    tenant = Tenant(
        name=f"Test Negocio {suffix}",
        slug=f"test-panel-mp-{suffix}",
        owner_email=f"panel-mp-{suffix}@test.com",
        password_hash="x",
        session_version=1,
        timezone="UTC",
    )
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


async def _get_csrf(client, cookie: str) -> tuple[str, dict]:
    """GET /panel/settings → (csrf_token del form, cookies para el POST)."""
    resp = await client.get("/panel/settings", cookies={"juturno_session": cookie})
    m = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert m, "No se encontró CSRF token en la página"
    return m.group(1), {**dict(resp.cookies), "juturno_session": cookie}


async def _connect_mp(db_session, tenant: Tenant) -> None:
    tenant.mp_access_token_enc = encrypt_token("fake-token")
    tenant.mp_user_id = "123456"
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)


async def _add_booking(
    db_session,
    tenant: Tenant,
    status: str,
    created_ago: timedelta,
    with_preference: bool = True,
    days_ahead: int = 1,
) -> None:
    service = Service(
        tenant_id=tenant.id, name="Servicio", duration_minutes=30, price=1000.0
    )
    db_session.add(service)
    await db_session.flush()
    start = datetime.now(timezone.utc) + timedelta(days=days_ahead)
    booking = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        client_name="Cliente",
        client_phone="5493584166288",
        start_time=start,
        end_time=start + timedelta(minutes=30),
        price_at_booking=1000.0,
        idempotency_key=f"panel-mp-{tenant.id}-{status}-{days_ahead}",
        status=status,
        created_at=datetime.now(timezone.utc) - created_ago,
    )
    db_session.add(booking)
    await db_session.flush()
    db_session.add(
        Payment(
            booking_id=booking.id,
            amount=500,
            method="mercado_pago",
            status="pending",
            mp_preference_id="pref-123" if with_preference else None,
        )
    )
    await db_session.commit()


async def _raw_state(state: str) -> str | None:
    client = redis.from_url(mp_connect.settings.REDIS_URL, decode_responses=True)
    try:
        return await client.get(mp_connect._state_key(state))
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# /panel/settings — página de configuración
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_requires_session(client):
    resp = await client.get("/panel/settings", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_settings_page_mp_not_connected(client, db_session):
    tenant = await _create_tenant(db_session, "nomp")
    cookie = _make_cookie(tenant)
    resp = await client.get("/panel/settings", cookies={"juturno_session": cookie})
    assert resp.status_code == 200
    assert "Sin cuenta conectada" in resp.text
    assert "Conectar Mercado Pago" in resp.text


@pytest.mark.asyncio
async def test_settings_page_mp_connected(client, db_session):
    tenant = await _create_tenant(db_session, "withmp")
    tenant.mp_access_token_enc = encrypt_token("fake-access-token")
    tenant.mp_alias = "mi.negocio"
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    cookie = _make_cookie(tenant)
    resp = await client.get("/panel/settings", cookies={"juturno_session": cookie})
    assert resp.status_code == 200
    assert "mi.negocio" in resp.text
    assert "Desconectar" in resp.text


@pytest.mark.asyncio
async def test_settings_flash_connected(client, db_session):
    tenant = await _create_tenant(db_session, "flash-conn")
    cookie = _make_cookie(tenant)
    resp = await client.get(
        "/panel/settings?mp=connected", cookies={"juturno_session": cookie}
    )
    assert resp.status_code == 200
    assert "conectada" in resp.text.lower()


@pytest.mark.asyncio
async def test_settings_flash_disconnected(client, db_session):
    tenant = await _create_tenant(db_session, "flash-disc")
    cookie = _make_cookie(tenant)
    resp = await client.get(
        "/panel/settings?mp=disconnected", cookies={"juturno_session": cookie}
    )
    assert resp.status_code == 200
    assert "desconectada" in resp.text.lower()


# ---------------------------------------------------------------------------
# POST /panel/mp/connect/start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_mp_connect_start_requires_session(client):
    resp = await client.post("/panel/mp/connect/start", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_panel_mp_connect_start_get_not_allowed(client, db_session):
    tenant = await _create_tenant(db_session, "mp-start-get")
    resp = await client.get(
        "/panel/mp/connect/start",
        cookies={"juturno_session": _make_cookie(tenant)},
        follow_redirects=False,
    )
    assert resp.status_code == 405


@pytest.mark.asyncio
async def test_panel_mp_connect_start_requires_csrf(client, db_session):
    tenant = await _create_tenant(db_session, "mp-start-nocsrf")
    resp = await client.post(
        "/panel/mp/connect/start",
        cookies={"juturno_session": _make_cookie(tenant)},
        data={},
        follow_redirects=False,
    )
    assert resp.status_code == 403


async def _post_connect_start(client, tenant: Tenant):
    csrf, cookies = await _get_csrf(client, _make_cookie(tenant))
    return await client.post(
        "/panel/mp/connect/start",
        cookies=cookies,
        data={"csrf_token": csrf},
        follow_redirects=False,
    )


def _state_set_cookie(resp) -> str:
    """Header Set-Cookie (en minúsculas) de la cookie que ata el state."""
    cookies = [
        c for c in resp.headers.get_list("set-cookie") if c.startswith(STATE_COOKIE)
    ]
    assert len(cookies) == 1, resp.headers.get_list("set-cookie")
    return cookies[0].lower()


@pytest.mark.asyncio
async def test_panel_mp_connect_start_redirects_to_mp(client, db_session, monkeypatch):
    # Panel en juturno.com, callback en api.juturno.com (como en producción)
    monkeypatch.setattr(mp_connect.settings, "PUBLIC_BASE_URL", "https://juturno.com")
    tenant = await _create_tenant(db_session, "mp-start")
    resp = await _post_connect_start(client, tenant)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "mercadopago.com" in location
    m = re.search(r"state=([^&]+)", location)
    assert m
    state = m.group(1)
    # En Redis solo vive el tenant_id, nunca una URL.
    assert await _raw_state(state) == str(tenant.id)

    # El state queda atado a este navegador, y la cookie llega al callback
    # aunque viva en otro subdominio.
    cookie = _state_set_cookie(resp)
    assert cookie.startswith(f"{STATE_COOKIE}={state.lower()};")
    assert "httponly" in cookie
    assert "path=/mp/connect/callback" in cookie
    assert "samesite=lax" in cookie
    assert "max-age=600" in cookie
    assert "domain=juturno.com" in cookie


@pytest.mark.asyncio
async def test_panel_mp_connect_start_same_host_cookie_is_host_only(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(mp_connect.settings, "PUBLIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setattr(
        mp_connect.settings,
        "MP_MARKETPLACE_REDIRECT_URL",
        "http://localhost:8000/mp/connect/callback",
    )
    tenant = await _create_tenant(db_session, "mp-start-local")
    resp = await _post_connect_start(client, tenant)
    assert resp.status_code == 302
    assert "domain=" not in _state_set_cookie(resp)


@pytest.mark.asyncio
async def test_panel_mp_connect_start_without_mp_config_returns_503(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(mp_connect.settings, "MP_MARKETPLACE_CLIENT_ID", "")
    tenant = await _create_tenant(db_session, "mp-start-noconf")
    resp = await _post_connect_start(client, tenant)
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_settings_connect_button_is_csrf_form(client, db_session):
    tenant = await _create_tenant(db_session, "mp-form")
    resp = await client.get(
        "/panel/settings", cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert 'action="/panel/mp/connect/start"' in resp.text
    assert 'href="/panel/mp/connect/start"' not in resp.text


# ---------------------------------------------------------------------------
# POST /panel/mp/disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_mp_disconnect_requires_session(client):
    resp = await client.post("/panel/mp/disconnect", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_panel_mp_disconnect_requires_csrf(client, db_session):
    tenant = await _create_tenant(db_session, "disc-nocsrf")
    cookie = _make_cookie(tenant)
    resp = await client.post(
        "/panel/mp/disconnect",
        cookies={"juturno_session": cookie},
        data={},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_panel_mp_disconnect_clears_connection(client, db_session):
    tenant = await _create_tenant(db_session, "disc-ok")
    tenant.mp_access_token_enc = encrypt_token("fake-token")
    tenant.mp_user_id = "123456"
    tenant.mp_alias = "negocio.test"
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    cookie = _make_cookie(tenant)
    # GET settings para obtener CSRF
    get_resp = await client.get("/panel/settings", cookies={"juturno_session": cookie})
    m = re.search(r'name="csrf_token" value="([^"]+)"', get_resp.text)
    assert m, "No se encontró CSRF token en la página"
    csrf = m.group(1)

    resp = await client.post(
        "/panel/mp/disconnect",
        cookies={**dict(get_resp.cookies), "juturno_session": cookie},
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/panel/settings" in resp.headers["location"]

    await db_session.refresh(tenant)
    assert tenant.mp_access_token_enc is None
    assert tenant.mp_user_id is None
    assert tenant.mp_alias is None


@pytest.mark.asyncio
async def test_panel_mp_disconnect_blocked_by_pending_deposit(client, db_session):
    tenant = await _create_tenant(db_session, "disc-pending")
    tenant.deposit_expiration_minutes = 30
    await _connect_mp(db_session, tenant)
    await _add_booking(db_session, tenant, "pending", timedelta(minutes=5))

    csrf, cookies = await _get_csrf(client, _make_cookie(tenant))
    resp = await client.post(
        "/panel/mp/disconnect",
        cookies=cookies,
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/panel/settings?mp=pending"

    await db_session.refresh(tenant)
    assert tenant.mp_access_token_enc is not None

    page = await client.get(
        "/panel/settings?mp=pending", cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "señas pendientes" in page.text


@pytest.mark.asyncio
async def test_panel_mp_disconnect_blocked_without_expiration_limit(client, db_session):
    """Sin límite de expiración configurado, una seña pendiente puede pagarse."""
    tenant = await _create_tenant(db_session, "disc-noexp")
    tenant.deposit_expiration_minutes = None
    await _connect_mp(db_session, tenant)
    await _add_booking(db_session, tenant, "pending", timedelta(days=3))

    csrf, cookies = await _get_csrf(client, _make_cookie(tenant))
    resp = await client.post(
        "/panel/mp/disconnect",
        cookies=cookies,
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/panel/settings?mp=pending"


@pytest.mark.asyncio
async def test_panel_mp_disconnect_ok_with_expired_or_confirmed(client, db_session):
    tenant = await _create_tenant(db_session, "disc-expired")
    tenant.deposit_expiration_minutes = 30
    await _connect_mp(db_session, tenant)
    # Pendiente cuyo plazo de seña ya venció (el job aún no la marcó)
    await _add_booking(db_session, tenant, "pending", timedelta(hours=2))
    await _add_booking(
        db_session, tenant, "confirmed", timedelta(minutes=5), days_ahead=2
    )
    # Pendiente sin preferencia de MP: no hay seña que pueda acreditarse
    await _add_booking(
        db_session,
        tenant,
        "pending",
        timedelta(minutes=1),
        with_preference=False,
        days_ahead=3,
    )

    csrf, cookies = await _get_csrf(client, _make_cookie(tenant))
    resp = await client.post(
        "/panel/mp/disconnect",
        cookies=cookies,
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/panel/settings?mp=disconnected"
    await db_session.refresh(tenant)
    assert tenant.mp_access_token_enc is None


@pytest.mark.asyncio
async def test_panel_mp_disconnect_ignores_other_tenants_pending(client, db_session):
    tenant = await _create_tenant(db_session, "disc-iso")
    other = await _create_tenant(db_session, "disc-iso-other")
    await _connect_mp(db_session, tenant)
    await _add_booking(db_session, other, "pending", timedelta(minutes=1))

    csrf, cookies = await _get_csrf(client, _make_cookie(tenant))
    resp = await client.post(
        "/panel/mp/disconnect",
        cookies=cookies,
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/panel/settings?mp=disconnected"


# ---------------------------------------------------------------------------
# Callback: el state solo vale en el navegador que inició la conexión
# ---------------------------------------------------------------------------


def _patch_exchange(monkeypatch) -> None:
    async def mock_exchange(code: str):
        return {
            "access_token": "APP_USR-panel-access",
            "refresh_token": "TG-panel-refresh",
            "expires_in": 15552000,
            "user_id": 9876543210,
        }

    async def mock_profile(access_token: str):
        return "9876543210", "negocio.panel"

    monkeypatch.setattr(mp_connect, "_exchange_code_for_tokens", mock_exchange)
    monkeypatch.setattr(mp_connect, "_fetch_mp_profile", mock_profile)


@pytest.mark.asyncio
async def test_callback_with_state_cookie_redirects_to_panel(
    client, db_session, monkeypatch
):
    """State + cookie del mismo navegador → 302 al panel, URL armada en server."""
    monkeypatch.setattr(mp_connect.settings, "PUBLIC_BASE_URL", "https://juturno.test/")
    tenant = await _create_tenant(db_session, "cb-redir")
    state = "panel-state-test-abc"
    await mp_connect._store_state(state, tenant.id)
    _patch_exchange(monkeypatch)

    resp = await client.get(
        f"/mp/connect/callback?code=panel-code&state={state}",
        cookies={STATE_COOKIE: state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert (
        resp.headers["location"] == "https://juturno.test/panel/settings?mp=connected"
    )
    assert "max-age=0" in _state_set_cookie(resp)

    await db_session.refresh(tenant)
    assert tenant.mp_access_token_enc is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie_value", [None, "state-de-otro-navegador"])
async def test_callback_rejects_state_from_another_browser(
    client, db_session, monkeypatch, cookie_value
):
    """Un link de autorización compartido no vincula la cuenta MP de otra
    sesión: sin la cookie del navegador que inició el flujo, se rechaza."""
    monkeypatch.setattr(mp_connect.settings, "PUBLIC_BASE_URL", "https://juturno.test")
    tenant = await _create_tenant(db_session, f"cb-other-{cookie_value is None}")
    state = f"shared-state-{cookie_value is None}"
    await mp_connect._store_state(state, tenant.id)
    _patch_exchange(monkeypatch)

    cookies = {STATE_COOKIE: cookie_value} if cookie_value else {}
    resp = await client.get(
        f"/mp/connect/callback?code=stolen-code&state={state}",
        cookies=cookies,
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert (
        resp.headers["location"]
        == "https://juturno.test/panel/settings?mp=other_browser"
    )
    # El state no se consume: quien inició el flujo todavía puede terminarlo.
    assert await _raw_state(state) == str(tenant.id)

    await db_session.refresh(tenant)
    assert tenant.mp_access_token_enc is None
    assert tenant.mp_user_id is None

    page = await client.get(
        "/panel/settings?mp=other_browser",
        cookies={"juturno_session": _make_cookie(tenant)},
    )
    assert "mismo navegador" in page.text


@pytest.mark.asyncio
async def test_callback_error_redirects_to_settings(client, db_session, monkeypatch):
    monkeypatch.setattr(mp_connect.settings, "PUBLIC_BASE_URL", "https://juturno.test")
    tenant = await _create_tenant(db_session, "cb-err-panel")
    state = "panel-state-err"
    await mp_connect._store_state(state, tenant.id)

    resp = await client.get(
        f"/mp/connect/callback?error=access_denied&state={state}",
        cookies={STATE_COOKIE: state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://juturno.test/panel/settings?mp=error"
    assert await _raw_state(state) is None
    assert "max-age=0" in _state_set_cookie(resp)

    page = await client.get(
        "/panel/settings?mp=error", cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "No se pudo conectar Mercado Pago" in page.text


@pytest.mark.asyncio
async def test_callback_error_without_cookie_keeps_state(client, db_session):
    """Sin la cookie, un ?error= no puede quemar el flujo de otro navegador."""
    tenant = await _create_tenant(db_session, "cb-err-nocookie")
    state = "panel-state-err-nocookie"
    await mp_connect._store_state(state, tenant.id)

    resp = await client.get(
        f"/mp/connect/callback?error=access_denied&state={state}",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/panel/settings?mp=error")
    assert await _raw_state(state) == str(tenant.id)
