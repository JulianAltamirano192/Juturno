import pytest
import hmac
import hashlib
from datetime import datetime, timedelta, date, timezone
from sqlalchemy import text

from app.models import Tenant, Service, ApiKey, Booking
from app import mp_webhooks
from app.auth import hash_api_key


# --- HELPERS DE AUTENTICACIÓN ---


async def _create_api_key(db_session, tenant_id: int) -> str:
    """Crea una ApiKey para el tenant y devuelve la key en texto plano."""
    raw_key = f"test-key-tenant-{tenant_id}"
    db_session.add(
        ApiKey(
            tenant_id=tenant_id,
            key_hash=hash_api_key(raw_key),
        )
    )
    await db_session.commit()
    return raw_key


def _auth_headers(raw_key: str) -> dict:
    return {"X-Tenant-API-Key": raw_key}


# --- SUITE DE PRUEBAS DE INTEGRACIÓN ---


@pytest.mark.asyncio
async def test_booking_flow_and_available_slots(client, db_session):
    """Test: Crear tenant, servicio, reservar un turno y verificar que desaparece de available-slots"""
    tenant = Tenant(name="Salon Test", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Corte de Pelo", duration_minutes=60, price=1500.0
    )
    db_session.add(service)
    await db_session.commit()

    raw_key = await _create_api_key(db_session, tenant.id)

    day = date.today() + timedelta(days=1)

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "staff_id": None,
        "client_name": "Carlos Gomez",
        "client_phone": "3584123456",
        "start_time": f"{day}T10:00:00",
        "end_time": f"{day}T11:00:00",
        "price_at_booking": 1500.0,
        "idempotency_key": "unique-booking-key-01",
    }
    res = await client.post("/bookings", json=payload, headers=_auth_headers(raw_key))
    assert res.status_code == 201
    data = res.json()
    assert "booking_id" in data

    res_slots = await client.get(
        f"/bookings/available-slots?tenant_id={tenant.id}&service_id={service.id}&day={day}",
        headers=_auth_headers(raw_key),
    )
    assert res_slots.status_code == 200
    slots = res_slots.json()["slots"]
    assert "10:00" not in slots
    assert "09:00" in slots


@pytest.mark.asyncio
async def test_double_booking_conflict(client, db_session):
    """Test: Intentar reservar el mismo slot exacto debe retornar 409 Conflict"""
    tenant = Tenant(name="Salon Test 2")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Manicura", duration_minutes=60, price=2000.0
    )
    db_session.add(service)
    await db_session.commit()

    raw_key = await _create_api_key(db_session, tenant.id)

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Ana Perez",
        "client_phone": "3584998877",
        "start_time": "2026-10-15T14:00:00",
        "end_time": "2026-10-15T15:00:00",
        "price_at_booking": 2000.0,
        "idempotency_key": "key-conflict-1",
    }

    res1 = await client.post("/bookings", json=payload, headers=_auth_headers(raw_key))
    assert res1.status_code == 201

    payload["idempotency_key"] = "key-conflict-2"
    res2 = await client.post("/bookings", json=payload, headers=_auth_headers(raw_key))
    assert res2.status_code == 409


@pytest.mark.asyncio
async def test_webhook_mp_idempotency(client, db_session, monkeypatch):
    """Test: Enviar el mismo webhook de Mercado Pago 2 veces solo procesa 1 efecto"""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    # Crear un booking real para que el webhook tenga a quién confirmar
    tenant = Tenant(name="Tenant MP Idempotency", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id,
        name="Servicio MP",
        duration_minutes=30,
        price=100.0,
    )
    db_session.add(service)
    await db_session.flush()

    booking = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        client_name="Cliente MP",
        client_phone="3584000000",
        start_time=datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 12, 1, 15, 30, tzinfo=timezone.utc),
        price_at_booking=100.0,
        idempotency_key="mp-idempotency-test-1",
        status="pending",
    )
    db_session.add(booking)
    await db_session.commit()
    await db_session.refresh(booking)

    async def approved_payment_details(data_id: str):
        return {
            "status": "approved",
            "external_reference": f"booking-{booking.id}",
            "transaction_amount": 100.0,
            "payment_method_id": "visa",
            "date_approved": datetime.now(timezone.utc).isoformat(),
        }

    monkeypatch.setattr(mp_webhooks, "get_payment_details", approved_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    manifest = f"id:pay_999;request-id:req_888;ts:{ts};"
    hash_hmac = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    signature = f"ts={ts},v1={hash_hmac}"

    payload = {
        "id": "evt_duplicate_test",
        "action": "payment.updated",
        "data": {"id": "pay_999"},
    }
    headers = {"x-signature": signature, "x-request-id": "req_888"}

    res1 = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res1.status_code == 200
    assert res1.text == "EVENT_PROCESSED"

    res2 = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.text == "DUPLICATE_EVENT_IGNORED"


@pytest.mark.asyncio
async def test_outbox_created_on_booking(client, db_session):
    """Test: Verificar que al crear una reserva se genera automáticamente el registro 'pending' en la Outbox"""
    tenant = Tenant(name="Tenant Outbox")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Spa", duration_minutes=30, price=5000.0
    )
    db_session.add(service)
    await db_session.commit()

    raw_key = await _create_api_key(db_session, tenant.id)

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Lucía",
        "client_phone": "3584112233",
        "start_time": "2026-11-01T16:00:00",
        "end_time": "2026-11-01T16:30:00",
        "price_at_booking": 5000.0,
        "idempotency_key": "outbox-test-key-99",
    }

    res = await client.post("/bookings", json=payload, headers=_auth_headers(raw_key))
    assert res.status_code == 201

    result = await db_session.execute(
        text("SELECT status, notification_type FROM notification_outbox")
    )
    rows = result.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "pending"
    assert rows[0][1] == "confirmation"
