import pytest
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException
from app.models import Booking, Payment, Tenant, Service
from sqlalchemy import text
from app import mp_webhooks


# Helper para firmar webhooks
def _sign_webhook(data_id: str, request_id: str, timestamp: int, secret: str) -> str:
    manifest = f"id:{data_id};request-id:{request_id};ts:{timestamp};"
    expected_hmac = hmac.new(
        secret.encode(), manifest.encode(), hashlib.sha256
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
            datetime.now(timezone.utc).isoformat() if status == "approved" else None
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

    payload = {
        "id": "evt_retry_test",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
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

    payload = {
        "id": "evt_processed_test",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
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

    payload = {
        "id": "evt_replay_test",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 403
    assert (
        res.json()["detail"] == "Timestamp del webhook fuera de ventana de tolerancia"
    )


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

    payload = {
        "id": "evt_valid_ts_test",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    # El booking NO debe estar confirmado (el pago está pending)
    await db_session.refresh(booking)
    assert booking.status == "pending"


@pytest.mark.asyncio
async def test_webhook_different_event_ids_same_payment_no_duplication(
    client, db_session, monkeypatch
):
    """
    Test A3: Múltiples webhooks con DISTINTO event_id para el mismo pago de MP.
    Verifica que no se duplique Payment, ni Booking status, ni mensaje en NotificationOutbox.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_idx_dup_test")

    async def mock_get_payment_details(data_id: str):
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_dup_777"

    # Evento 1
    sig1 = _sign_webhook(data_id, "req_dup_1", ts, secret)
    payload1 = {
        "id": "evt_dup_1",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    res1 = await client.post(
        "/webhooks/mercadopago",
        json=payload1,
        headers={"x-signature": sig1, "x-request-id": "req_dup_1"},
    )
    assert res1.status_code == 200

    # Evento 2 (distinto event_id pero mismo payment)
    sig2 = _sign_webhook(data_id, "req_dup_2", ts, secret)
    payload2 = {
        "id": "evt_dup_2",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    res2 = await client.post(
        "/webhooks/mercadopago",
        json=payload2,
        headers={"x-signature": sig2, "x-request-id": "req_dup_2"},
    )
    assert res2.status_code == 200

    # Verificar que solo hay 1 registro de Payment para este mp_payment_id
    pay_count = await db_session.execute(
        text("SELECT COUNT(*) FROM payment WHERE mp_payment_id='pay_dup_777'")
    )
    assert pay_count.scalar_one() == 1

    # Verificar que solo hay 1 registro en NotificationOutbox para este booking
    outbox_count = await db_session.execute(
        text(
            f"SELECT COUNT(*) FROM notification_outbox WHERE booking_id={booking.id} AND notification_type='confirmation'"
        )
    )
    assert outbox_count.scalar_one() == 1

    # Verificar estado del booking
    await db_session.refresh(booking)
    assert booking.status == "confirmed"


# ---------------------------------------------------------------------------
# Cobertura de caminos del webhook no cubiertos por los tests anteriores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_invalid_signature_rejected(client, db_session, monkeypatch):
    """
    Test B1: Firma HMAC con secret incorrecto debe rechazarse con 401
    y NO registrar el evento (falla antes del gate de idempotencia).
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_bad_sig"
    request_id = "req_bad_sig"

    # Firmamos con OTRO secret: el HMAC no va a coincidir con settings.MP_SECRET_KEY
    signature = _sign_webhook(data_id, request_id, ts, "otro-secret-incorrecto")

    payload = {
        "id": "evt_invalid_sig",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 401
    assert res.json()["detail"] == "Firma de Mercado Pago inválida"

    # El evento no debe quedar registrado: el rechazo es previo al gate de idempotencia
    result = await db_session.execute(
        text("SELECT COUNT(*) FROM payment_events WHERE event_id='evt_invalid_sig'")
    )
    assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_webhook_missing_signature_headers_rejected(
    client, db_session, monkeypatch
):
    """
    Test B2: Request sin headers x-signature / x-request-id debe rechazarse con 401.
    Sin firma no hay autenticidad verificable: fail closed.
    """
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", "test-webhook-secret")

    payload = {
        "id": "evt_no_headers",
        "action": "payment.updated",
        "data": {"id": "pay_x"},
    }

    res = await client.post("/webhooks/mercadopago", json=payload)
    assert res.status_code == 401
    assert res.json()["detail"] == "Firma de Mercado Pago inválida"


@pytest.mark.asyncio
async def test_webhook_without_data_id_ignored(client, db_session, monkeypatch):
    """
    Test B3: Payload sin data_id extraíble (sin data.id ni id ni query params)
    debe salir limpio con EVENT_IGNORED_NO_DATA_ID, sin registrar el evento.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    ts = int(datetime.now(timezone.utc).timestamp())
    # data_id vacío: el manifest firma id vacío
    signature = _sign_webhook("", "req_no_data", ts, secret)

    # Sin "id" ni "data": no hay nada extraíble
    payload = {"action": "payment.updated"}
    headers = {"x-signature": signature, "x-request-id": "req_no_data"}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_IGNORED_NO_DATA_ID"

    # No debe quedar ningún evento registrado (la tabla parte vacía en cada test)
    result = await db_session.execute(text("SELECT COUNT(*) FROM payment_events"))
    assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_webhook_payment_not_found_on_mp(client, db_session, monkeypatch):
    """
    Test B4: MP responde 404 para el pago (ID del simulador o evento viejo)
    -> PAYMENT_NOT_FOUND_ON_MP, evento marcado 'processed'.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    async def mock_get_payment_details(data_id: str):
        return None  # get_payment_details devuelve None si MP responde 404

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_ghost_404"
    request_id = "req_ghost"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {
        "id": "evt_not_found",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "PAYMENT_NOT_FOUND_ON_MP"

    # El evento igual queda 'processed': MP no debe reintentarlo eternamente
    result = await db_session.execute(
        text("SELECT status FROM payment_events WHERE event_id='evt_not_found'")
    )
    assert result.scalar_one() == "processed"


@pytest.mark.asyncio
async def test_webhook_payment_without_booking_link_ignored(
    client, db_session, monkeypatch
):
    """
    Test B5: external_reference sin prefijo 'booking-' (pago no vinculado a Juturno)
    -> NO_BOOKING_LINKED, evento 'processed', sin confirmar nada.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    async def mock_get_payment_details(data_id: str):
        return _make_payment_details("approved", "orden-externa-777")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_unlinked_1"
    request_id = "req_unlinked"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {
        "id": "evt_unlinked",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "NO_BOOKING_LINKED"

    result = await db_session.execute(
        text("SELECT status FROM payment_events WHERE event_id='evt_unlinked'")
    )
    assert result.scalar_one() == "processed"


@pytest.mark.asyncio
async def test_webhook_updates_existing_pending_payment(
    client, db_session, monkeypatch
):
    """
    Test B6: Payment ya registrado en estado 'pending' que el webhook trae 'approved'
    -> actualiza status y paid_at, confirma el booking y genera el outbox.
    Cubre la rama de actualización (no la de auto-creación de Payment).
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_update_pending_pay")

    # Payment preexistente en pending, como queda tras la reserva pública
    payment = Payment(
        booking_id=booking.id,
        amount=30.0,
        mp_payment_id="pay_update_1",
        method="mercado_pago",
        status="pending",
    )
    db_session.add(payment)
    await db_session.commit()

    async def mock_get_payment_details(data_id: str):
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_update_1"
    request_id = "req_update"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {
        "id": "evt_update_pending",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    # El Payment se actualizó a approved con paid_at seteado
    result = await db_session.execute(
        text("SELECT status, paid_at FROM payment WHERE mp_payment_id='pay_update_1'")
    )
    row = result.one()
    assert row[0] == "approved"
    assert row[1] is not None

    # El booking pasó a confirmed y generó exactamente un outbox de confirmación
    await db_session.refresh(booking)
    assert booking.status == "confirmed"

    outbox_count = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM notification_outbox "
            f"WHERE booking_id={booking.id} AND notification_type='confirmation'"
        )
    )
    assert outbox_count.scalar_one() == 1


@pytest.mark.asyncio
async def test_webhook_mp_timeout_marks_event_failed(client, db_session, monkeypatch):
    """
    Test B7: get_payment_details lanza HTTPException (timeout 504)
    -> el evento queda 'failed' y el 504 se propaga para que MP reintente.
    Cubre la rama except HTTPException (distinta de la Exception genérica).
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    async def mock_get_payment_details(data_id: str):
        raise HTTPException(status_code=504, detail="Timeout consultando Mercado Pago")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_timeout"
    request_id = "req_timeout"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {
        "id": "evt_timeout",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res.status_code == 504

    # El evento queda 'failed' para que el reintento de MP lo reprocese
    result = await db_session.execute(
        text("SELECT status FROM payment_events WHERE event_id='evt_timeout'")
    )
    assert result.scalar_one() == "failed"
