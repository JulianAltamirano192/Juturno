from datetime import datetime, timedelta, timezone

import pytest
import redis.asyncio as redis_async

from app import scheduler
from app.auth import hash_api_key
from app.config import settings
from app.models import ApiKey, Booking, Service, Tenant
from app.scheduler import process_deposit_expiration
from tests.conftest import TestingSessionLocal


# scheduler._get_redis_client() devuelve un cliente atado al event loop
# actual. Como pytest-asyncio crea un loop por test, parcheamos la
# función para que devuelva un cliente fresco por test.
@pytest.fixture(autouse=True)
def _fresh_redis_client(monkeypatch):
    fake_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)
    monkeypatch.setattr(scheduler, "_get_redis_client", lambda: fake_client)


async def _tenant_with_pending_booking(db_session, *, expiration_minutes, created_ago):
    """Crea tenant + servicio + booking pending con created_at desplazado."""
    tenant = Tenant(name=f"Tenant exp {expiration_minutes}", timezone="UTC")
    # Asignación explícita: None = NULL real (desactiva la expiración)
    tenant.deposit_expiration_minutes = expiration_minutes
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Corte", duration_minutes=30, price=1000.0
    )
    db_session.add(service)
    await db_session.flush()

    start = datetime.now(timezone.utc) + timedelta(days=1)
    booking = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        client_name="Cliente",
        client_phone="5493584166288",
        start_time=start,
        end_time=start + timedelta(minutes=30),
        price_at_booking=1000.0,
        idempotency_key=f"exp-{expiration_minutes}-{created_ago}",
        status="pending",
        created_at=datetime.now(timezone.utc) - created_ago,
    )
    db_session.add(booking)
    await db_session.commit()
    return tenant, booking


# ---------------------------------------------------------------------------
# Job de expiración
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_expires_stale_pending_and_keeps_fresh(db_session):
    """El pending que superó el límite del tenant vence; el que está
    dentro de la ventana no se toca."""
    _, stale = await _tenant_with_pending_booking(
        db_session, expiration_minutes=15, created_ago=timedelta(minutes=20)
    )
    _, fresh = await _tenant_with_pending_booking(
        db_session, expiration_minutes=15, created_ago=timedelta(minutes=5)
    )

    await process_deposit_expiration(TestingSessionLocal)

    await db_session.refresh(stale)
    await db_session.refresh(fresh)
    assert stale.status == "expired"
    assert fresh.status == "pending"


@pytest.mark.asyncio
async def test_job_respects_tenants_without_expiration(db_session):
    """NULL desactiva la expiración: un pending de días atrás queda vivo."""
    _, booking = await _tenant_with_pending_booking(
        db_session, expiration_minutes=None, created_ago=timedelta(days=3)
    )

    await process_deposit_expiration(TestingSessionLocal)

    await db_session.refresh(booking)
    assert booking.status == "pending"


@pytest.mark.asyncio
async def test_job_ignores_non_pending_bookings(db_session):
    """Reservas confirmed/cancelled nunca se tocan, por viejas que sean."""
    tenant = Tenant(
        name="Tenant confirmed viejo", timezone="UTC", deposit_expiration_minutes=15
    )
    db_session.add(tenant)
    await db_session.flush()
    service = Service(
        tenant_id=tenant.id, name="Corte", duration_minutes=30, price=1000.0
    )
    db_session.add(service)
    await db_session.flush()

    start = datetime.now(timezone.utc) + timedelta(days=2)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    confirmed = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        client_name="Confirmado",
        client_phone="5491100000000",
        start_time=start,
        end_time=start + timedelta(minutes=30),
        price_at_booking=1000.0,
        idempotency_key="exp-confirmed",
        status="confirmed",
        created_at=old,
    )
    cancelled = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        client_name="Cancelado",
        client_phone="5491100000001",
        start_time=start + timedelta(minutes=60),
        end_time=start + timedelta(minutes=90),
        price_at_booking=1000.0,
        idempotency_key="exp-cancelled",
        status="cancelled",
        created_at=old,
    )
    db_session.add_all([confirmed, cancelled])
    await db_session.commit()

    await process_deposit_expiration(TestingSessionLocal)

    await db_session.refresh(confirmed)
    await db_session.refresh(cancelled)
    assert confirmed.status == "confirmed"
    assert cancelled.status == "cancelled"


# ---------------------------------------------------------------------------
# Configuración por el tenant autenticado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_tenants_me_updates_expiration_limit(client, db_session):
    """PATCH /tenants/me setea el límite, null lo desactiva, 0 es inválido
    y sin API key no pasa del 401."""
    tenant = Tenant(name="Patch Test", timezone="UTC", deposit_expiration_minutes=15)
    db_session.add(tenant)
    await db_session.flush()
    raw_key = f"test-key-tenant-{tenant.id}"
    db_session.add(ApiKey(tenant_id=tenant.id, key_hash=hash_api_key(raw_key)))
    await db_session.commit()
    headers = {"X-Tenant-API-Key": raw_key}

    # Sin API key → 401
    res0 = await client.patch("/tenants/me", json={"deposit_expiration_minutes": 45})
    assert res0.status_code == 401

    # Valor válido → actualiza y lo devuelve
    res1 = await client.patch(
        "/tenants/me", json={"deposit_expiration_minutes": 45}, headers=headers
    )
    assert res1.status_code == 200
    assert res1.json()["deposit_expiration_minutes"] == 45
    await db_session.refresh(tenant)
    assert tenant.deposit_expiration_minutes == 45

    # null desactiva la expiración
    res2 = await client.patch(
        "/tenants/me", json={"deposit_expiration_minutes": None}, headers=headers
    )
    assert res2.status_code == 200
    assert res2.json()["deposit_expiration_minutes"] is None

    # 0 es inválido (mínimo 1 minuto)
    res3 = await client.patch(
        "/tenants/me", json={"deposit_expiration_minutes": 0}, headers=headers
    )
    assert res3.status_code == 422
