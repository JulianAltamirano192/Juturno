import pytest
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from app.models import ProcessedWebhookEvent, Booking, Payment, Tenant, Service
from sqlalchemy import text
from app import mp_webhooks

# Helper para firmar webhooks
def _sign_webhook(data_id: str, request_id: str, timestamp: int, secret: str) -> str:
    manifest = f"id:{data_id};request-id:{request_id};ts:{timestamp};"
    expected_hmac = hmac.new(
        secret.encode(),
        manifest.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"ts={timestamp},v1={expected_hmac}"


def _make_payment_details(
    status: str,
    external_reference: str,
    amount: float = 100.0,
    method: str = "visa",
) -> dict:
    """Construye un dict que simula la respuesta de MP /v1/payments/{id}."""
    return {
        "status": status,
        "external_reference": external_reference,
        "transaction_amount": amount,
        "payment_method_id": method,
        "date_approved": (
            datetime.now(timezone.utc).isoformat()
            if status == "approved"
            else None
        ),
    }


# Helper para crear un booking listo para pruebas
async def _create_booking(db_session, idempotency_key: str) -> Booking:
    tenant = Tenant(name=f"Tenant-{idempotency_key}", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id,
        name="Servicio Test",
        duration_minutes=30,
        price=100.0,
    )
    db_session.add(service)
    await db_session.flush()

    booking = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        client_name="Cliente",
        client_phone="123",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc) + timedelta(minutes=30),
        price_at_booking=100.0,
        idempotency_key=idempotency_key,
        status="pending",
    )
    db_session.add(booking)
    await db_session.commit()
    await db_session.refresh(booking)
    return booking


@pytest.mark.asyncio
async def test_webhook_idempotency_retry(client, db_session, monkeypatch):
    """
    Test A1: Idempotencia con reintento.
    - Primer intento: falla durante la consulta a MP.
    - Segundo intento: procesa exitosamente y confirma el booking.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_idx_1")

    call_count = 0

    async def mock_get_payment_details(data_id: str):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("Simulated network failure")
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_999"
    request_id = "req_1"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {"id": "evt_retry_test", "action": "payment.updated", "data": {"id": data_id}}
    headers = {"x-signature": signature, "x-request-id": request_id}

    # Primer intento: debe fallar con la excepción simulada
    with pytest.raises(Exception, match="Simulated network failure"):
        await client.post("/webhooks/mercadopago", json=payload, headers=headers)

    # Verificar que quedó en 'failed' para trazabilidad
    result = await db_session.execute(
        text("SELECT status FROM payment_events WHERE event_id='evt_retry_test'")
    )
    assert result.scalar_one() == "failed"

    # Segundo intento (reintento de MP): debe procesar exitosamente
    res2 = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.text == "EVENT_PROCESSED"

    # Verificar que el booking fue confirmado
    await db_session.refresh(booking)
    assert booking.status == "confirmed"

    # Verificar que se creó el Payment automáticamente
    pay_result = await db_session.execute(
        text("SELECT status FROM payment WHERE mp_payment_id='pay_999'")
    )
    assert pay_result.scalar_one() == "approved"


@pytest.mark.asyncio
async def test_webhook_idempotency_processed(client, db_session, monkeypatch):
    """
    Test A2: Idempotencia después de procesar.
    - Primer intento: procesa exitosamente.
    - Segundo intento (mismo event_id): devuelve DUPLICATE sin reprocesar.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_idx_2")

    async def mock_get_payment_details(data_id: str):
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_888"
    request_id = "req_2"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {"id": "evt_processed_test", "action": "payment.updated", "data": {"id": data_id}}
    headers = {"x-signature": signature, "x-request-id": request_id}

    res1 = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res1.status_code == 200
    assert res1.text == "EVENT_PROCESSED"

    # Segundo intento: mismo event_id → debe ser ignorado
    res2 = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.text == "DUPLICATE_EVENT_IGNORED"


@pytest.mark.asyncio
async def test_webhook_replay_attack(client, db_session, monkeypatch):
    """Test C1: Replay attack (timestamp muy viejo) debe rechazarse con 403."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    ts = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp())
    data_id = "pay_111"
    request_id = "req_3"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {"id": "evt_replay_test", "action": "payment.updated", "data": {"id": data_id}}
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 403
    assert res.json()["detail"] == "Timestamp del webhook fuera de ventana de tolerancia"


@pytest.mark.asyncio
async def test_webhook_valid_timestamp(client, db_session, monkeypatch):
    """
    Test C2: Timestamp válido actual.
    El pago está 'pending' (no approved), así que el booking NO se confirma,
    pero el evento sí se procesa.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_idx_3")

    async def mock_get_payment_details(data_id: str):
        return _make_payment_details("pending", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_222"
    request_id = "req_4"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {"id": "evt_valid_ts_test", "action": "payment.updated", "data": {"id": data_id}}
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    # El booking NO debe estar confirmado (el pago está pending)
    await db_session.refresh(booking)
    assert booking.status == "pending"