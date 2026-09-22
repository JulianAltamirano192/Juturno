import pytest
import hmac
import hashlib
from datetime import datetime, timedelta, date, time, timezone
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
async def test_outbox_created_only_on_approved_payment(client, db_session, monkeypatch):
    """Test: Al crear reserva (pending) NO se genera outbox; solo se genera al aprobarse el pago por MP."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    tenant = Tenant(name="Tenant Outbox Test")
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
    booking_id = res.json()["booking_id"]

    # 1. Al crear la reserva (estado pending), NO debe haber nada en notification_outbox
    result_before = await db_session.execute(
        text("SELECT COUNT(*) FROM notification_outbox")
    )
    assert result_before.scalar_one() == 0

    # 2. Simular pago aprobado por webhook MP
    async def approved_payment_details(data_id: str):
        return {
            "status": "approved",
            "external_reference": f"booking-{booking_id}",
            "transaction_amount": 5000.0,
            "payment_method_id": "pix",
            "date_approved": datetime.now(timezone.utc).isoformat(),
        }

    monkeypatch.setattr(mp_webhooks, "get_payment_details", approved_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_outbox_100"
    request_id = "req_outbox_100"
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    hash_hmac = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    signature = f"ts={ts},v1={hash_hmac}"

    wh_payload = {
        "id": "evt_outbox_test",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    wh_headers = {"x-signature": signature, "x-request-id": request_id}

    res_wh = await client.post(
        "/webhooks/mercadopago", json=wh_payload, headers=wh_headers
    )
    assert res_wh.status_code == 200

    # 3. Ahora sí debe existir la notificación de confirmación en notification_outbox
    result_after = await db_session.execute(
        text("SELECT status, notification_type FROM notification_outbox")
    )
    rows = result_after.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "pending"
    assert rows[0][1] == "confirmation"


@pytest.mark.asyncio
async def test_booking_end_time_derived_from_duration(client, db_session):
    """Test: Crear reserva sin enviar end_time debe derivarlo de duration_minutes del servicio."""
    tenant = Tenant(name="Tenant Derived EndTime", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    # Servicio de 45 minutos
    service = Service(
        tenant_id=tenant.id, name="Corte + Barba", duration_minutes=45, price=2500.0
    )
    db_session.add(service)
    await db_session.commit()

    raw_key = await _create_api_key(db_session, tenant.id)

    # El payload NO incluye end_time
    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Marcos",
        "client_phone": "11223344",
        "start_time": "2026-11-10T10:00:00Z",
        "idempotency_key": "key-derived-end-time-1",
    }

    res = await client.post("/bookings", json=payload, headers=_auth_headers(raw_key))
    assert res.status_code == 201
    booking_id = res.json()["booking_id"]

    booking = await db_session.get(Booking, booking_id)
    assert booking is not None
    assert booking.start_time.isoformat().startswith("2026-11-10T10:00:00")
    # end_time debe ser exactamente start_time + 45 min
    assert booking.end_time.isoformat().startswith("2026-11-10T10:45:00")


@pytest.mark.asyncio
async def test_get_available_slots_today_filters_past_hours(client, db_session):
    """Test: Al consultar slots para HOY, no se deben devolver horarios pasados."""
    tenant = Tenant(
        name="Tenant Today Slots", timezone="America/Argentina/Buenos_Aires"
    )
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Masaje", duration_minutes=30, price=3000.0
    )
    db_session.add(service)
    await db_session.commit()

    raw_key = await _create_api_key(db_session, tenant.id)

    from zoneinfo import ZoneInfo

    now_local = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))
    today = now_local.date()

    res = await client.get(
        f"/bookings/available-slots?tenant_id={tenant.id}&service_id={service.id}&day={today}",
        headers=_auth_headers(raw_key),
    )
    assert res.status_code == 200
    slots = res.json()["slots"]

    # Ningún slot devuelto debe ser anterior a la hora actual en hora local del tenant
    for slot_str in slots:
        slot_h, slot_m = map(int, slot_str.split(":"))
        slot_dt = datetime.combine(
            today,
            time(slot_h, slot_m),
            tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"),
        )
        assert slot_dt >= now_local


@pytest.mark.asyncio
async def test_booking_creation_idempotency_retry_returns_200(client, db_session):
    """Test: Reintentar la creación con el mismo idempotency_key devuelve 200 y el mismo booking_id."""
    tenant = Tenant(name="Tenant Idempotency Booking", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Depilación", duration_minutes=30, price=1800.0
    )
    db_session.add(service)
    await db_session.commit()

    raw_key = await _create_api_key(db_session, tenant.id)

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Laura",
        "client_phone": "15443322",
        "start_time": "2026-12-20T11:00:00Z",
        "idempotency_key": "unique-retry-key-777",
    }

    # Primer intento -> 201 Created
    res1 = await client.post("/bookings", json=payload, headers=_auth_headers(raw_key))
    assert res1.status_code == 201
    booking_id_1 = res1.json()["booking_id"]

    # Segundo intento (reintento exacto con la misma clave) -> 200 OK con el mismo booking_id
    res2 = await client.post("/bookings", json=payload, headers=_auth_headers(raw_key))
    assert res2.status_code == 200
    booking_id_2 = res2.json()["booking_id"]

    assert booking_id_1 == booking_id_2
