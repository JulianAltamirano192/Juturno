"""
Tarea 4 (D-012): create_public_booking pasa a cobrar con el token del tenant.

  - Sandbox + cuenta conectada → token del tenant (descifrado)
  - Sandbox + sin cuenta → fallback al token de la plataforma
  - Producción + sin cuenta → 422 y NO se crea la reserva (ni se llama a MP)
  - Token cifrado con clave distinta a la configurada → 502 (no 500 enmascarado)

Para forzar un ciphertext "de otra clave" se cifra con una Fernet key que NO
es la activa en settings en ese momento.
"""

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app import main as main_module
from app.models import Service, Tenant
from app.mp_crypto import encrypt_token

TEST_FERNET_KEY = Fernet.generate_key().decode()
PLATFORM_TOKEN = "TEST-maplataforma-0000"
TENANT_TOKEN = "APP_USR-token-del-negocio-demo"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        main_module.settings, "MP_TOKEN_ENCRYPTION_KEY", TEST_FERNET_KEY
    )
    monkeypatch.setattr(main_module.settings, "MP_ACCESS_TOKEN", PLATFORM_TOKEN)
    # Sandbox por defecto; los tests de producción lo pisan
    monkeypatch.setattr(main_module.settings, "MP_SANDBOX", True)


@pytest.fixture
def fake_mp(monkeypatch):
    """Intercepta create_mp_preference y captura con qué token cobró."""
    captured = {}

    async def fake_create_mp_preference(**kwargs):
        captured.update(kwargs)
        return {
            "preference_id": "pref-test-1",
            "init_point": "https://www.mercadopago.com/init",
            "sandbox_init_point": "https://sandbox.mercadopago.com/init",
            "checkout_url": "https://sandbox.mercadopago.com/init",
        }

    monkeypatch.setattr(
        "app.routers.public.create_mp_preference", fake_create_mp_preference
    )
    return captured


async def _tenant_with_service(db_session, *, connected: bool):
    tenant = Tenant(
        name="Barbería Demo",
        slug=f"demo-{int(datetime.now().timestamp() * 1000)}",
        timezone="UTC",
    )
    if connected:
        tenant.mp_user_id = "555-tenant-demo"
        tenant.mp_access_token_enc = encrypt_token(TENANT_TOKEN)
        tenant.mp_refresh_token_enc = encrypt_token("TG-refresh-demo")
    db_session.add(tenant)
    await db_session.flush()
    service = Service(
        tenant_id=tenant.id, name="Corte", duration_minutes=30, price=1500.0
    )
    db_session.add(service)
    await db_session.commit()
    return tenant, service


def _booking_payload(tenant_id: int, service_id: int, key_suffix: str):
    start = datetime.now(timezone.utc) + timedelta(days=2)
    return {
        "tenant_id": tenant_id,
        "service_id": service_id,
        "client_name": "Cliente Prueba",
        "client_phone": "1134567890",
        "start_time": start.isoformat(),
        "idempotency_key": f"t4-{key_suffix}-{int(start.timestamp())}",
    }


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_tenant_conectado_cobra_con_su_token(client, db_session, fake_mp):
    tenant, service = await _tenant_with_service(db_session, connected=True)

    res = await client.post(
        "/public/bookings", json=_booking_payload(tenant.id, service.id, "own")
    )

    assert res.status_code == 201
    # El pago fue a la cuenta del negocio, no a la plataforma
    assert fake_mp["access_token"] == TENANT_TOKEN
    assert "sandbox.mercadopago.com" in res.json()["payment_url"]


@pytest.mark.asyncio
async def test_sandbox_sin_cuenta_hace_fallback_a_plataforma(
    client, db_session, fake_mp
):
    """En sandbox vale el fallback: la reserva funciona igual para pruebas."""
    tenant, service = await _tenant_with_service(db_session, connected=False)

    res = await client.post(
        "/public/bookings", json=_booking_payload(tenant.id, service.id, "sb")
    )

    assert res.status_code == 201
    assert fake_mp["access_token"] == PLATFORM_TOKEN


@pytest.mark.asyncio
async def test_produccion_sin_cuenta_bloquea_la_reserva(
    client, db_session, fake_mp, monkeypatch
):
    monkeypatch.setattr(main_module.settings, "MP_SANDBOX", False)
    tenant, service = await _tenant_with_service(db_session, connected=False)

    res = await client.post(
        "/public/bookings", json=_booking_payload(tenant.id, service.id, "prod")
    )

    assert res.status_code == 422
    assert "no configuró su cuenta de Mercado Pago" in res.json()["detail"]
    # MP no se llamó y la reserva no quedó en la DB
    assert fake_mp == {}
    count = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM booking WHERE tenant_id = :tid").bindparams(
                tid=tenant.id
            )
        )
    ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_token_indescifrable_devuelve_502(
    client, db_session, fake_mp, monkeypatch
):
    """Ciphertext hecho con otra clave → error claro, MP nunca se llama."""
    other_key = Fernet.generate_key().decode()
    tenant, service = await _tenant_with_service(db_session, connected=False)
    tenant.mp_access_token_enc = (
        Fernet(other_key.encode()).encrypt(b"token-de-otra-clave").decode()
    )
    db_session.add(tenant)
    await db_session.commit()

    res = await client.post(
        "/public/bookings", json=_booking_payload(tenant.id, service.id, "broken")
    )

    assert res.status_code == 502
    assert fake_mp == {}
