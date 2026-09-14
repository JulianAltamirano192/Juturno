import pytest
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from app.models import ProcessedWebhookEvent, Booking, Payment, Tenant, Service
from sqlalchemy import text
from app import mp_webhooks
from httpx import Response

# Helper para firmar webhooks
def _sign_webhook(data_id: str, request_id: str, timestamp: int, secret: str) -> str:
    manifest = f"id:{data_id};request-id:{request_id};ts:{timestamp};"
    expected_hmac = hmac.new(
        secret.encode(),
        manifest.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"ts={timestamp},v1={expected_hmac}"

@pytest.mark.asyncio
async def test_webhook_idempotency_retry(client, db_session, monkeypatch):
    """Test A1: Idempotencia con reintento (falla primera vez, pasa segunda)."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    
    # Creamos un mock que lanza error en el primer llamado y pasa en el segundo
    call_count = 0
    async def mock_get_payment_status(data_id: str):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("Simulated network failure")
        return "approved"
    
    monkeypatch.setattr(mp_webhooks, "get_payment_status", mock_get_payment_status)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_999"
    request_id = "req_1"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {"id": "evt_retry_test", "action": "payment.updated", "data": {"id": data_id}}
    headers = {"x-signature": signature, "x-request-id": request_id}

    # Primer intento (Falla)
    try:
        await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    except Exception as e:
        assert "Simulated network failure" in str(e)
    
    # Verificamos que quedó en estado 'failed' para trazabilidad
    result = await db_session.execute(text("SELECT status FROM payment_events WHERE event_id='evt_retry_test'"))
    assert result.scalar_one() == "failed"

    # Preparar base de datos para que el status approved del segundo llamado funcione
    # Necesitamos un tenant, service, booking y payment.
    tenant = Tenant(name="Test Tenant", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(tenant_id=tenant.id, name="Servicio", duration_minutes=30, price=100.0)
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
        idempotency_key="book_idx_1",
        status="pending"
    )
    db_session.add(booking)
    await db_session.flush()

    payment = Payment(
        booking_id=booking.id,
        amount=100.0,
        mp_payment_id=data_id,
        method="mp",
        status="pending"
    )
    db_session.add(payment)
    await db_session.commit()

    # Segundo intento (Reintento de Mercado Pago)
    res2 = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.text == "EVENT_PROCESSED"
    
    # Verificar que el booking fue confirmado
    await db_session.refresh(booking)
    assert booking.status == "confirmed"

@pytest.mark.asyncio
async def test_webhook_idempotency_processed(client, db_session, monkeypatch):
    """Test A2: Idempotencia después de que ya se procesó con éxito."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    
    async def mock_get_payment_status(data_id: str):
        return "approved"
    
    monkeypatch.setattr(mp_webhooks, "get_payment_status", mock_get_payment_status)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_888"
    request_id = "req_2"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {"id": "evt_processed_test", "action": "payment.updated", "data": {"id": data_id}}
    headers = {"x-signature": signature, "x-request-id": request_id}

    res1 = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res1.status_code == 200

    # Segundo intento
    res2 = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.text == "DUPLICATE_EVENT_IGNORED"

@pytest.mark.asyncio
async def test_webhook_replay_attack(client, db_session, monkeypatch):
    """Test C1: Replay attack (timestamp muy viejo)."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    
    # Timestamp de hace 10 minutos (fuera de ventana de 5 min)
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
    """Test C2: Timestamp válido actual."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    
    async def mock_get_payment_status(data_id: str):
        return "pending"
    
    monkeypatch.setattr(mp_webhooks, "get_payment_status", mock_get_payment_status)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_222"
    request_id = "req_4"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {"id": "evt_valid_ts_test", "action": "payment.updated", "data": {"id": data_id}}
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"
