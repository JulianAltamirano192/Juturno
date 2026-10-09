import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app import mp_webhooks
from app.models import Booking, Payment, Service, Tenant


# Helper para firmar webhooks
def _sign_webhook(data_id: str, request_id: str, timestamp: int, secret: str) -> str:
    # Template de MP: id:[data.id_url];request-id:[x-request-id];ts:[ts];
    # data.id va en minúsculas y, si no viene, se omite del template.
    id_part = f"id:{data_id.lower()};" if data_id else ""
    manifest = f"{id_part}request-id:{request_id};ts:{timestamp};"
    expected_hmac = hmac.new(
        secret.encode(), manifest.encode(), hashlib.sha256
    ).hexdigest()
    return f"ts={timestamp},v1={expected_hmac}"


def _webhook_url(payload: dict) -> str:
    """URL como la arma MP: el data.id va en el query (es lo que se firma)."""
    data_id = payload.get("data", {}).get("id")
    if not data_id:
        return "/webhooks/mercadopago"
    return f"/webhooks/mercadopago?data.id={data_id}&type=payment"


def _make_payment_details(
    status: str,
    external_reference: str,
    amount: float = 100.0,
    method: str = "visa",
    currency_id: str = "ARS",
    collector_id: str | int | None = None,
) -> dict:
    """Construye un dict que simula la respuesta de MP /v1/payments/{id}."""
    d = {
        "status": status,
        "external_reference": external_reference,
        "transaction_amount": amount,
        "payment_method_id": method,
        "currency_id": currency_id,
        "date_approved": (
            datetime.now(timezone.utc).isoformat() if status == "approved" else None
        ),
    }
    if collector_id is not None:
        d["collector_id"] = collector_id
    return d


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

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Simulated network failure")
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
        await client.post(_webhook_url(payload), json=payload, headers=headers)

    # Verificar que quedó en 'failed' para trazabilidad
    result = await db_session.execute(
        text("SELECT status FROM payment_events WHERE event_id='pay_999:req_1'")
    )
    assert result.scalar_one() == "failed"

    # Segundo intento (reintento de MP): debe procesar exitosamente
    res2 = await client.post(_webhook_url(payload), json=payload, headers=headers)
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

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
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

    res1 = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res1.status_code == 200
    assert res1.text == "EVENT_PROCESSED"

    # Segundo intento: mismo event_id → debe ser ignorado
    res2 = await client.post(_webhook_url(payload), json=payload, headers=headers)
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

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
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

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
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

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
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

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
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
        _webhook_url(payload1),
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
        _webhook_url(payload2),
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

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 401
    assert res.json()["detail"] == "Firma de Mercado Pago inválida"

    # El evento no debe quedar registrado: el rechazo es previo al gate de idempotencia
    result = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM payment_events WHERE event_id='pay_bad_sig:req_bad_sig'"
        )
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

    res = await client.post(_webhook_url(payload), json=payload)
    assert res.status_code == 401
    assert res.json()["detail"] == "Firma de Mercado Pago inválida"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, body",
    [
        ("id=123456&topic=payment", None),
        ("id=555&topic=payment", {"data": {"id": "555"}}),
        (
            "id=987&topic=merchant_order",
            {"resource": "https://api.mercadolibre.com/merchant_orders/987"},
        ),
    ],
)
async def test_webhook_ipn_acknowledged_without_processing(
    client, db_session, monkeypatch, query, body
):
    """IPN (?id=X&topic=...) llega sin firma validable; el mismo evento llega
    como Webhook firmado. Se responde 200 para cortar los reintentos de MP,
    sin consultar el pago ni registrar el evento."""
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", "test-webhook-secret")

    async def fail_get_payment(*args, **kwargs):
        raise AssertionError("IPN must not be processed")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", fail_get_payment)

    res = await client.post(f"/webhooks/mercadopago?{query}", json=body)
    assert res.status_code == 200
    assert res.text == "IPN_IGNORED"

    result = await db_session.execute(text("SELECT COUNT(*) FROM payment_events"))
    assert result.scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not-json",
        b"[1, 2]",
        b'"text"',
        b"\xff\xfe\xfa",
        b'{"data": 1}',
        b'{"data": null}',
        b"[" * 200000,
    ],
)
async def test_webhook_malformed_body_returns_400(client, db_session, body):
    """Un body vacío, inválido o que no es un objeto JSON es un request mal
    formado (400), no un error del servidor (500)."""
    res = await client.post(
        "/webhooks/mercadopago?data.id=pay_x&type=payment",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_webhook_with_data_id_and_topic_still_requires_signature(
    client, db_session, monkeypatch
):
    """Agregar ?topic= a una notificación con data.id no saltea la firma."""
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", "test-webhook-secret")

    res = await client.post(
        "/webhooks/mercadopago?data.id=pay_x&type=payment&topic=payment",
        json={"action": "payment.updated", "data": {"id": "pay_x"}},
    )
    assert res.status_code == 401


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

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_IGNORED_NO_DATA_ID"

    # No debe quedar ningún evento registrado (la tabla parte vacía en cada test)
    result = await db_session.execute(text("SELECT COUNT(*) FROM payment_events"))
    assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_webhook_signature_uses_lowercased_query_data_id(
    client, db_session, monkeypatch
):
    """MP firma el data.id del query string en minúsculas si es alfanumérico."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    called_with = []

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        called_with.append(data_id)

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    signature = _sign_webhook("ABC123", "req_upper", ts, secret)
    res = await client.post(
        "/webhooks/mercadopago?data.id=ABC123&type=payment",
        json={"id": "evt_upper", "type": "payment", "data": {"id": "ABC123"}},
        headers={"x-signature": signature, "x-request-id": "req_upper"},
    )
    assert res.status_code == 200
    assert res.text == "PAYMENT_NOT_FOUND_ON_MP"
    assert called_with == ["ABC123"]


@pytest.mark.asyncio
async def test_webhook_processes_signed_query_id_not_body_id(
    client, db_session, monkeypatch
):
    """La firma cubre el data.id del query; el del body no está firmado y no
    puede elegir qué pago se procesa."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    called_with = []

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        called_with.append(data_id)

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    signature = _sign_webhook("pay_signed", "req_swap", ts, secret)
    res = await client.post(
        "/webhooks/mercadopago?data.id=pay_signed&type=payment",
        json={"id": "evt_swap", "type": "payment", "data": {"id": "pay_other"}},
        headers={"x-signature": signature, "x-request-id": "req_swap"},
    )
    assert res.status_code == 200
    assert called_with == ["pay_signed"]


@pytest.mark.asyncio
async def test_webhook_body_data_id_without_query_is_ignored(
    client, db_session, monkeypatch
):
    """Sin data.id en el query la firma no cubre ningún id: no se procesa el
    del body."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        raise AssertionError("no debe consultar MP")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    signature = _sign_webhook("", "req_body_only", ts, secret)
    res = await client.post(
        "/webhooks/mercadopago",
        json={"id": "evt_body_only", "data": {"id": "pay_body"}},
        headers={"x-signature": signature, "x-request-id": "req_body_only"},
    )
    assert res.status_code == 200
    assert res.text == "EVENT_IGNORED_NO_DATA_ID"


@pytest.mark.asyncio
async def test_webhook_replay_with_new_body_id_is_duplicate(
    client, db_session, monkeypatch
):
    """La clave de idempotencia sale solo de valores firmados: reenviar un
    request firmado cambiando el "id" del body no saltea el dedupe."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    calls = 0

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        nonlocal calls
        calls += 1
        return _make_payment_details("pending", "not-a-booking")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    headers = {
        "x-signature": _sign_webhook("pay_replay", "req_replay", ts, secret),
        "x-request-id": "req_replay",
    }
    url = "/webhooks/mercadopago?data.id=pay_replay&type=payment"
    first = {"id": "evt_original", "data": {"id": "pay_replay"}}
    replay = {"id": "evt_forged", "data": {"id": "pay_replay"}}

    res1 = await client.post(url, json=first, headers=headers)
    res2 = await client.post(url, json=replay, headers=headers)

    assert res1.text == "NO_BOOKING_LINKED"
    assert res2.text == "DUPLICATE_EVENT_IGNORED"
    assert calls == 1


@pytest.mark.asyncio
async def test_webhook_replay_with_other_id_case_is_duplicate(
    client, db_session, monkeypatch
):
    """La firma usa data.id en minúsculas: "ABC" y "abc" firman igual, así
    que la clave de idempotencia también se normaliza."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        return _make_payment_details("pending", "not-a-booking")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    headers = {
        "x-signature": _sign_webhook("abc123", "req_case", ts, secret),
        "x-request-id": "req_case",
    }
    body = {"data": {"id": "abc123"}}
    res1 = await client.post(
        "/webhooks/mercadopago?data.id=ABC123&type=payment", json=body, headers=headers
    )
    res2 = await client.post(
        "/webhooks/mercadopago?data.id=abc123&type=payment", json=body, headers=headers
    )

    assert res1.text == "NO_BOOKING_LINKED"
    assert res2.text == "DUPLICATE_EVENT_IGNORED"


@pytest.mark.asyncio
async def test_webhook_booking_integrity_error_is_not_reported_as_processed(
    client, db_session, monkeypatch
):
    """Solo el INSERT duplicado del Payment es idempotente. Un IntegrityError
    de la reserva (p. ej. el horario se ocupó) deja el evento 'failed' para
    que MP reintente, en vez de darlo por procesado."""
    from sqlalchemy.exc import IntegrityError

    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    booking = await _create_booking(db_session, "book_integrity")

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        return _make_payment_details("approved", f"booking-{booking.id}")

    async def failing_transition(session, booking, new_status, actor="system"):
        raise IntegrityError("UPDATE booking", {}, Exception("excl_overlapping"))

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)
    monkeypatch.setattr(mp_webhooks, "transition_booking_status", failing_transition)

    ts = int(datetime.now(timezone.utc).timestamp())
    headers = {
        "x-signature": _sign_webhook("pay_integrity", "req_integrity", ts, secret),
        "x-request-id": "req_integrity",
    }
    with pytest.raises(IntegrityError):
        await client.post(
            "/webhooks/mercadopago?data.id=pay_integrity&type=payment",
            json={"data": {"id": "pay_integrity"}},
            headers=headers,
        )

    result = await db_session.execute(
        text(
            "SELECT status FROM payment_events "
            "WHERE event_id='pay_integrity:req_integrity'"
        )
    )
    assert result.scalar_one() == "failed"


@pytest.mark.asyncio
async def test_webhook_payment_not_found_on_mp(client, db_session, monkeypatch):
    """
    Test B4: MP responde 404 para el pago (ID del simulador o evento viejo)
    -> PAYMENT_NOT_FOUND_ON_MP (200, MP no reintenta), evento 'failed': así
    una entrega posterior con la misma clave todavía se puede procesar.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
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

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "PAYMENT_NOT_FOUND_ON_MP"

    result = await db_session.execute(
        text(
            "SELECT status FROM payment_events WHERE event_id='pay_ghost_404:req_ghost'"
        )
    )
    assert result.scalar_one() == "failed"


@pytest.mark.asyncio
async def test_webhook_fake_user_id_replay_does_not_burn_the_event(
    client, db_session, monkeypatch
):
    """El user_id del body no está firmado: un reenvío con un user_id falso
    consulta con el token equivocado (404) y no debe impedir que la entrega
    legítima, con la misma clave firmada, confirme la reserva."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    booking = await _create_booking(db_session, "book_fake_user_id")

    async def token_from_body(session, payload):
        return payload.get("user_id")

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        if access_token != "tenant-token":
            return None
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "_resolve_token_for_payment", token_from_body)
    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    headers = {
        "x-signature": _sign_webhook("pay_uid", "req_uid", ts, secret),
        "x-request-id": "req_uid",
    }
    url = "/webhooks/mercadopago?data.id=pay_uid&type=payment"

    forged = await client.post(
        url, json={"user_id": "evil", "data": {"id": "pay_uid"}}, headers=headers
    )
    legit = await client.post(
        url,
        json={"user_id": "tenant-token", "data": {"id": "pay_uid"}},
        headers=headers,
    )

    assert forged.text == "PAYMENT_NOT_FOUND_ON_MP"
    assert legit.text == "EVENT_PROCESSED"
    await db_session.refresh(booking)
    assert booking.status == "confirmed"


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

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
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

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "NO_BOOKING_LINKED"

    result = await db_session.execute(
        text(
            "SELECT status FROM payment_events WHERE event_id='pay_unlinked_1:req_unlinked'"
        )
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

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
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

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
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

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
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

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 504

    # El evento queda 'failed' para que el reintento de MP lo reprocese
    result = await db_session.execute(
        text(
            "SELECT status FROM payment_events WHERE event_id='pay_timeout:req_timeout'"
        )
    )
    assert result.scalar_one() == "failed"


# ---------------------------------------------------------------------------
# create_mp_preference: selección de URL de checkout según MP_SANDBOX
# ---------------------------------------------------------------------------

FAKE_PREFERENCE_RESPONSE = {
    "id": "pref-123",
    "init_point": "https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=pref-123",
    "sandbox_init_point": "https://sandbox.mercadopago.com.ar/checkout/v1/redirect?pref_id=pref-123",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mp_sandbox, expected_url_key",
    [(True, "sandbox_init_point"), (False, "init_point")],
)
async def test_create_mp_preference_selects_checkout_url_by_mode(
    monkeypatch, mp_sandbox, expected_url_key
):
    """
    Con credenciales de prueba (MP_SANDBOX=true) el checkout debe usar
    sandbox_init_point; con producción (false), init_point. Mandar al
    cliente a la URL del ambiente equivocado hace el pago imposible.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setattr(mp_webhooks.settings, "MP_SANDBOX", mp_sandbox)

    fake_response = MagicMock()
    fake_response.is_success = True
    fake_response.json.return_value = FAKE_PREFERENCE_RESPONSE

    client_mock = AsyncMock()
    client_mock.post.return_value = fake_response
    client_mock.__aenter__.return_value = client_mock

    with patch("app.mp_webhooks.httpx.AsyncClient", return_value=client_mock):
        result = await mp_webhooks.create_mp_preference(
            booking_id=1,
            amount=30.0,
            client_name="Test",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )

    assert result["checkout_url"] == FAKE_PREFERENCE_RESPONSE[expected_url_key]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured_url",
    ["https://sandbox.example.test/webhooks/mercadopago", ""],
)
async def test_create_mp_preference_uses_configured_notification_url(
    monkeypatch, configured_url
):
    """
    notification_url sale de MP_NOTIFICATION_URL (por entorno) y no está
    hardcodeada a producción. Vacía = no se manda: MP no notifica a nadie
    en vez de mandarle a prod los pagos de sandbox/local.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setattr(mp_webhooks.settings, "MP_NOTIFICATION_URL", configured_url)

    fake_response = MagicMock()
    fake_response.is_success = True
    fake_response.json.return_value = FAKE_PREFERENCE_RESPONSE

    client_mock = AsyncMock()
    client_mock.post.return_value = fake_response
    client_mock.__aenter__.return_value = client_mock

    with patch("app.mp_webhooks.httpx.AsyncClient", return_value=client_mock):
        await mp_webhooks.create_mp_preference(
            booking_id=1,
            amount=30.0,
            client_name="Test",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )

    body = client_mock.post.call_args.kwargs["json"]
    if configured_url:
        assert body["notification_url"] == configured_url
    else:
        assert "notification_url" not in body


@pytest.mark.asyncio
async def test_search_approved_payment_ids_filters_by_reference_and_status():
    """La búsqueda usa el external_reference y el token del tenant, y solo
    devuelve pagos aprobados."""
    from unittest.mock import AsyncMock, MagicMock, patch

    fake_response = MagicMock()
    fake_response.json.return_value = {
        "results": [
            {"id": 11, "status": "approved"},
            {"id": 12, "status": "rejected"},
        ]
    }
    client_mock = AsyncMock()
    client_mock.get.return_value = fake_response
    client_mock.__aenter__.return_value = client_mock

    with patch("app.mp_webhooks.httpx.AsyncClient", return_value=client_mock):
        ids = await mp_webhooks.search_approved_payment_ids("booking-7", "tok")

    assert ids == ["11"]
    kwargs = client_mock.get.call_args.kwargs
    assert kwargs["params"] == {"external_reference": "booking-7"}
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    fake_response.raise_for_status.assert_called_once()


# ---------------------------------------------------------------------------
# Pago aprobado tardío sobre reserva expirada
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_approved_late_payment_reconfirms_expired(
    client, db_session, monkeypatch
):
    """
    Test B8: pago aprobado que llega DESPUÉS de que la reserva venció por
    falta de seña → si el horario sigue libre, se re-confirma y se encola
    el WhatsApp de confirmación.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "late-payment-ok")
    booking.status = "expired"
    db_session.add(booking)
    await db_session.commit()

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = f"pay-late-{booking.id}"
    request_id = "req_late_ok"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {
        "id": f"evt-late-ok-{booking.id}",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    await db_session.refresh(booking)
    assert booking.status == "confirmed"

    # El WhatsApp de confirmación quedó encolado
    result = await db_session.execute(
        text(
            "SELECT status FROM notification_outbox "
            "WHERE booking_id = :bid AND notification_type = 'confirmation'"
        ).bindparams(bid=booking.id)
    )
    assert result.scalar_one() == "pending"


# ---------------------------------------------------------------------------
# Guards de seguridad: tenant mismatch y monto insuficiente (Issue #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_tenant_mismatch(client, db_session, monkeypatch):
    """collector_id del response de MP difiere del mp_user_id del tenant → TENANT_MISMATCH."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_tenant_mismatch")
    tenant = await db_session.get(Tenant, booking.tenant_id)
    tenant.mp_user_id = "my-mp-account"
    db_session.add(tenant)
    await db_session.commit()

    async def mock_get_payment_details(data_id: str, access_token=None):
        return _make_payment_details(
            "approved", f"booking-{booking.id}", collector_id=99999
        )  # 99999 ≠ "my-mp-account"

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_mismatch_1"
    request_id = "req_mismatch_1"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_mismatch_1",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "TENANT_MISMATCH"

    await db_session.refresh(booking)
    assert booking.status == "pending"


@pytest.mark.asyncio
async def test_webhook_deposit_amount_zero(client, db_session, monkeypatch):
    """deposit_amount=0 significa seña gratis; cualquier pago positivo confirma."""
    from decimal import Decimal

    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_deposit_zero")
    service = await db_session.get(Service, booking.service_id)
    service.deposit_amount = Decimal(0)
    db_session.add(service)
    await db_session.commit()

    async def mock_get_payment_details(data_id: str, access_token=None):
        return _make_payment_details("approved", f"booking-{booking.id}", amount=0.01)

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_zero_dep"
    request_id = "req_zero_dep"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_zero_dep",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    await db_session.refresh(booking)
    assert booking.status == "confirmed"


@pytest.mark.asyncio
async def test_webhook_deposit_null_rounding(client, db_session, monkeypatch):
    """Precio 10.75, deposit=None → seña efectiva = 3.23 (30% con ROUND_HALF_UP). Pago exacto confirma."""
    from decimal import Decimal

    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_rounding")
    service = await db_session.get(Service, booking.service_id)
    service.price = Decimal("10.75")
    service.deposit_amount = None
    db_session.add(service)
    await db_session.commit()

    async def mock_get_payment_details(data_id: str, access_token=None):
        return _make_payment_details("approved", f"booking-{booking.id}", amount=3.23)

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_rounding"
    request_id = "req_rounding"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_rounding",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    await db_session.refresh(booking)
    assert booking.status == "confirmed"


@pytest.mark.asyncio
async def test_webhook_amount_too_low(client, db_session, monkeypatch):
    """Pago por debajo de la seña explícita → AMOUNT_INSUFFICIENT, sin confirmar."""
    from decimal import Decimal

    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_amount_low")
    service = await db_session.get(Service, booking.service_id)
    service.deposit_amount = Decimal("50.00")
    db_session.add(service)
    await db_session.commit()

    async def mock_get_payment_details(data_id: str, access_token=None):
        return _make_payment_details("approved", f"booking-{booking.id}", amount=10.0)

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_low_1"
    request_id = "req_low_1"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {"id": "evt_low_1", "action": "payment.updated", "data": {"id": data_id}}
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "AMOUNT_INSUFFICIENT"

    await db_session.refresh(booking)
    assert booking.status == "pending"


@pytest.mark.asyncio
async def test_webhook_amount_exact(client, db_session, monkeypatch):
    """Pago exactamente igual a la seña explícita → confirma normalmente."""
    from decimal import Decimal

    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_amount_exact")
    service = await db_session.get(Service, booking.service_id)
    service.deposit_amount = Decimal("50.00")
    db_session.add(service)
    await db_session.commit()

    async def mock_get_payment_details(data_id: str, access_token=None):
        return _make_payment_details("approved", f"booking-{booking.id}", amount=50.0)

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_exact_1"
    request_id = "req_exact_1"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_exact_1",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    await db_session.refresh(booking)
    assert booking.status == "confirmed"


@pytest.mark.asyncio
async def test_webhook_amount_overpaid(client, db_session, monkeypatch):
    """Pago mayor a la seña explícita → confirma normalmente."""
    from decimal import Decimal

    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_amount_over")
    service = await db_session.get(Service, booking.service_id)
    service.deposit_amount = Decimal("50.00")
    db_session.add(service)
    await db_session.commit()

    async def mock_get_payment_details(data_id: str, access_token=None):
        return _make_payment_details("approved", f"booking-{booking.id}", amount=200.0)

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_over_1"
    request_id = "req_over_1"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {"id": "evt_over_1", "action": "payment.updated", "data": {"id": data_id}}
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    await db_session.refresh(booking)
    assert booking.status == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount,expected_text,expected_booking_status",
    [
        (25.0, "AMOUNT_INSUFFICIENT", "pending"),  # < 30% de 100
        (35.0, "EVENT_PROCESSED", "confirmed"),  # > 30% de 100
    ],
)
async def test_webhook_deposit_null_service(
    client, db_session, monkeypatch, amount, expected_text, expected_booking_status
):
    """Con deposit_amount=None, la seña efectiva es price * 30%."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    key = f"book_null_dep_{int(amount)}"
    booking = await _create_booking(db_session, key)
    # deposit_amount=None por defecto; price=100 → seña efectiva = 30

    async def mock_get_payment_details(data_id: str, access_token=None):
        return _make_payment_details("approved", f"booking-{booking.id}", amount=amount)

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = f"pay_null_{int(amount)}"
    request_id = f"req_null_{int(amount)}"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": f"evt_null_{int(amount)}",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == expected_text

    await db_session.refresh(booking)
    assert booking.status == expected_booking_status


@pytest.mark.asyncio
async def test_webhook_approved_late_payment_slot_taken_keeps_expired(
    client, db_session, monkeypatch
):
    """
    Test B9: pago aprobado tardío pero el horario fue tomado por otra
    reserva confirmada → la reserva vencida queda 'expired' y no se
    encola ningún WhatsApp.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "late-payment-taken")
    booking.status = "expired"

    # Otra reserva confirmada ocupa exactamente el mismo horario
    overlapping = Booking(
        tenant_id=booking.tenant_id,
        service_id=booking.service_id,
        client_name="Otro Cliente",
        client_phone="5491100000000",
        start_time=booking.start_time,
        end_time=booking.end_time,
        price_at_booking=100.0,
        idempotency_key="overlapping-confirmed",
        status="confirmed",
    )
    db_session.add_all([booking, overlapping])
    await db_session.commit()

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = f"pay-late-taken-{booking.id}"
    request_id = "req_late_taken"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {
        "id": f"evt-late-taken-{booking.id}",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    await db_session.refresh(booking)
    assert booking.status == "expired"

    # Nada encolado para esta reserva
    result = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM notification_outbox "
            "WHERE booking_id = :bid AND notification_type = 'confirmation'"
        ).bindparams(bid=booking.id)
    )
    assert result.scalar_one() == 0


# ---------------------------------------------------------------------------
# Media 3: guards run BEFORE Payment is created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_no_payment_on_tenant_mismatch(client, db_session, monkeypatch):
    """After TENANT_MISMATCH no Payment row must exist in the DB (Media 3)."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_no_pay_mismatch")
    tenant = await db_session.get(Tenant, booking.tenant_id)
    tenant.mp_user_id = "tenant-account-123"
    db_session.add(tenant)
    await db_session.commit()

    data_id = "pay_no_pay_mismatch"

    async def mock_get_payment_details(did: str, access_token=None):
        return _make_payment_details(
            "approved", f"booking-{booking.id}", collector_id=99999
        )

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    request_id = "req_no_pay_mismatch"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_no_pay_mismatch",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "TENANT_MISMATCH"

    result = await db_session.execute(
        text("SELECT COUNT(*) FROM payment WHERE mp_payment_id = :pid").bindparams(
            pid=data_id
        )
    )
    assert result.scalar_one() == 0, "Payment must NOT be committed on guard rejection"


# ---------------------------------------------------------------------------
# Media 1: fail-open when mp_user_id is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_no_mp_user_id_production_rejected(
    client, db_session, monkeypatch
):
    """Tenant without mp_user_id in production → TENANT_MISMATCH (fail closed)."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    monkeypatch.setattr(mp_webhooks.settings, "ENVIRONMENT", "production")

    booking = await _create_booking(db_session, "book_prod_no_mp")
    # tenant.mp_user_id is None (default from _create_booking)

    async def mock_get_payment_details(data_id: str, access_token=None):
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_prod_no_mp"
    request_id = "req_prod_no_mp"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_prod_no_mp",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "TENANT_MISMATCH"

    await db_session.refresh(booking)
    assert booking.status == "pending"


@pytest.mark.asyncio
async def test_webhook_no_mp_user_id_sandbox_allowed(client, db_session, monkeypatch):
    """Tenant without mp_user_id in sandbox → booking is confirmed (guard skipped)."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    # ENVIRONMENT is "development" by default in tests → is_production = False

    booking = await _create_booking(db_session, "book_sandbox_no_mp")
    # tenant.mp_user_id is None (default)

    async def mock_get_payment_details(data_id: str, access_token=None):
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_sandbox_no_mp"
    request_id = "req_sandbox_no_mp"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_sandbox_no_mp",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    await db_session.refresh(booking)
    assert booking.status == "confirmed"


# ---------------------------------------------------------------------------
# Baja 1: currency_id must be ARS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_currency_not_ars(client, db_session, monkeypatch):
    """Payment in a non-ARS currency → CURRENCY_NOT_SUPPORTED, booking stays pending."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_currency_usd")

    async def mock_get_payment_details(data_id: str, access_token=None):
        return _make_payment_details(
            "approved", f"booking-{booking.id}", currency_id="USD"
        )

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = "pay_currency_usd"
    request_id = "req_currency_usd"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_currency_usd",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "CURRENCY_NOT_SUPPORTED"

    await db_session.refresh(booking)
    assert booking.status == "pending"


# ---------------------------------------------------------------------------
# Media A: tenant guard for non-approved statuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_pending_payment_tenant_mismatch_no_payment(
    client, db_session, monkeypatch
):
    """Pending payment with wrong collector_id → TENANT_MISMATCH, no Payment row (Media A)."""
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_pending_mismatch")
    tenant = await db_session.get(Tenant, booking.tenant_id)
    tenant.mp_user_id = "real-account-456"
    db_session.add(tenant)
    await db_session.commit()

    data_id = "pay_pending_mismatch"

    async def mock_get_payment_details(did: str, access_token=None):
        d = _make_payment_details(
            "pending", f"booking-{booking.id}", collector_id="attacker-999"
        )
        d["status"] = "pending"
        return d

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    request_id = "req_pending_mismatch"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_pending_mismatch",
        "action": "payment.created",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "TENANT_MISMATCH"

    result = await db_session.execute(
        text("SELECT COUNT(*) FROM payment WHERE mp_payment_id = :pid").bindparams(
            pid=data_id
        )
    )
    assert (
        result.scalar_one() == 0
    ), "Payment must NOT be created for mismatched pending webhook"


@pytest.mark.asyncio
async def test_webhook_production_collector_matches_confirms(
    client, db_session, monkeypatch
):
    """In production, matching collector_id and mp_user_id → booking confirmed (regression guard)."""
    from decimal import Decimal

    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)
    monkeypatch.setattr(mp_webhooks.settings, "ENVIRONMENT", "production")

    booking = await _create_booking(db_session, "book_prod_collector_match")
    tenant = await db_session.get(Tenant, booking.tenant_id)
    tenant.mp_user_id = "correct-account-789"
    db_session.add(tenant)
    service = await db_session.get(Service, booking.service_id)
    service.deposit_amount = Decimal("30.00")
    db_session.add(service)
    await db_session.commit()

    data_id = "pay_prod_match"

    async def mock_get_payment_details(did: str, access_token=None):
        return _make_payment_details(
            "approved",
            f"booking-{booking.id}",
            amount=30.0,
            collector_id="correct-account-789",
        )

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    request_id = "req_prod_match"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_prod_match",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    await db_session.refresh(booking)
    assert booking.status == "confirmed"


# ---------------------------------------------------------------------------
# Baja E: NaN / non-finite transaction_amount must not crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_nan_transaction_amount(client, db_session, monkeypatch):
    """transaction_amount='NaN' must return AMOUNT_INSUFFICIENT, not crash with 500 (Baja E)."""
    from decimal import Decimal

    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "book_nan_amount")
    service = await db_session.get(Service, booking.service_id)
    service.deposit_amount = Decimal("30.00")
    db_session.add(service)
    await db_session.commit()

    data_id = "pay_nan_amount"

    async def mock_get_payment_details(did: str, access_token=None):
        d = _make_payment_details("approved", f"booking-{booking.id}")
        d["transaction_amount"] = "NaN"
        return d

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    request_id = "req_nan_amount"
    signature = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": "evt_nan_amount",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "AMOUNT_INSUFFICIENT"

    await db_session.refresh(booking)
    assert booking.status == "pending"


# ---------------------------------------------------------------------------
# Media B — UNIQUE constraint on mp_payment_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payment_mp_payment_id_unique_constraint(db_session):
    """UNIQUE on Payment.mp_payment_id: two rows with the same non-null id must raise IntegrityError."""
    from decimal import Decimal

    from sqlalchemy.exc import IntegrityError

    booking = await _create_booking(db_session, "book_unique_pay")

    p1 = Payment(
        booking_id=booking.id,
        amount=Decimal("100.00"),
        mp_payment_id="pay_unique_constraint_test",
        method="visa",
        status="pending",
    )
    db_session.add(p1)
    await db_session.flush()

    p2 = Payment(
        booking_id=booking.id,
        amount=Decimal("100.00"),
        mp_payment_id="pay_unique_constraint_test",
        method="visa",
        status="approved",
    )
    db_session.add(p2)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Ítem 4 — deposit_at_booking en Payment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_guard_uses_booking_deposit_at_booking(
    client, db_session, monkeypatch
):
    """
    Guard 3 uses booking.deposit_at_booking when set, not the current service price.
    Scenario: tenant raised service price after booking was created; the client pays
    the original deposit amount → must be accepted, not rejected.
    """
    from decimal import Decimal

    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    tenant = Tenant(name="Tenant-bk-dep-guard", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id,
        name="Servicio bk dep",
        duration_minutes=30,
        price=Decimal("500.00"),
        deposit_amount=Decimal("100.00"),
    )
    db_session.add(service)
    await db_session.flush()

    # Booking with deposit_at_booking snapshotted at creation time (100)
    booking = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        client_name="Cliente",
        client_phone="123",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc) + timedelta(minutes=30),
        price_at_booking=Decimal("500.00"),
        deposit_at_booking=Decimal("100.00"),
        idempotency_key="bk-dep-snap-guard",
        status="pending",
    )
    db_session.add(booking)
    await db_session.commit()
    await db_session.refresh(booking)

    # Raise service price — current effective_deposit would now be 300
    service.price = Decimal("1000.00")
    service.deposit_amount = Decimal("300.00")
    db_session.add(service)
    await db_session.commit()

    data_id = f"pay_bk_dep_{booking.id}"

    async def mock_details(did: str, access_token: str | None = None):
        return _make_payment_details("approved", f"booking-{booking.id}", amount=100.0)

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    request_id = "req_bk_dep"
    sig = _sign_webhook(data_id, request_id, ts, secret)
    payload = {
        "id": f"evt_bk_dep_{booking.id}",
        "action": "payment.updated",
        "data": {"id": data_id},
    }

    res = await client.post(
        _webhook_url(payload),
        json=payload,
        headers={"x-signature": sig, "x-request-id": request_id},
    )
    assert res.status_code == 200
    assert (
        res.text == "EVENT_PROCESSED"
    ), f"Expected EVENT_PROCESSED (guard should use booking.deposit_at_booking=100), got: {res.text}"


# ---------------------------------------------------------------------------
# Locking: SELECT FOR UPDATE en booking (MEDIA #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_already_confirmed_booking_no_duplicate_outbox(
    client, db_session, monkeypatch
):
    """
    Test B-LOCK1: booking ya confirmado cuando llega un webhook aprobado.
    Simula la carrera en que otro webhook (o proceso) confirmó el booking
    justo antes. El SELECT FOR UPDATE garantiza que leemos el estado
    comprometido ('confirmed'), por lo que la transición se saltea y NO se
    crea un outbox duplicado.
    """
    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "already-confirmed-lock")
    booking.status = "confirmed"
    db_session.add(booking)
    await db_session.commit()

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = f"pay-confirmed-{booking.id}"
    request_id = "req_confirmed_lock"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {
        "id": f"evt-confirmed-lock-{booking.id}",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    await db_session.refresh(booking)
    assert booking.status == "confirmed"

    # El booking ya estaba confirmado: no hubo doble transición.
    # Sí se crea exactamente un outbox (no había ninguno; la deduplicación
    # la hace el guard existing_outbox is None, no el estado del booking).
    outbox_count = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM notification_outbox "
            f"WHERE booking_id = {booking.id} AND notification_type = 'confirmation'"
        )
    )
    assert outbox_count.scalar_one() == 1


@pytest.mark.asyncio
async def test_webhook_confirmed_booking_with_existing_outbox_no_duplicate(
    client, db_session, monkeypatch
):
    """
    Test B-LOCK2: booking confirmado con outbox de confirmación ya creado.
    El webhook no debe crear un segundo outbox (guard existing_outbox is None).
    """
    from app.models import NotificationOutbox

    secret = "test-webhook-secret"
    monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", secret)

    booking = await _create_booking(db_session, "confirmed-with-outbox")
    booking.status = "confirmed"
    db_session.add(booking)
    await db_session.flush()

    existing_outbox = NotificationOutbox(
        booking_id=booking.id,
        notification_type="confirmation",
        status="sent",
    )
    db_session.add(existing_outbox)
    await db_session.commit()

    async def mock_get_payment_details(data_id: str, access_token: str | None = None):
        return _make_payment_details("approved", f"booking-{booking.id}")

    monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

    ts = int(datetime.now(timezone.utc).timestamp())
    data_id = f"pay-conf-outbox-{booking.id}"
    request_id = "req_conf_outbox"
    signature = _sign_webhook(data_id, request_id, ts, secret)

    payload = {
        "id": f"evt-conf-outbox-{booking.id}",
        "action": "payment.updated",
        "data": {"id": data_id},
    }
    headers = {"x-signature": signature, "x-request-id": request_id}

    res = await client.post(_webhook_url(payload), json=payload, headers=headers)
    assert res.status_code == 200
    assert res.text == "EVENT_PROCESSED"

    outbox_count = await db_session.execute(
        text(
            "SELECT COUNT(*) FROM notification_outbox "
            f"WHERE booking_id = {booking.id} AND notification_type = 'confirmation'"
        )
    )
    assert outbox_count.scalar_one() == 1


@pytest.mark.asyncio
async def test_create_mp_preference_expires_with_the_booking():
    """The payment link expires at MP together with the booking, and cash /
    ATM methods (payable days later) are excluded, so no payment can arrive
    after the tenant is allowed to disconnect MP."""
    from unittest.mock import AsyncMock, MagicMock, patch

    fake_response = MagicMock()
    fake_response.is_success = True
    fake_response.json.return_value = FAKE_PREFERENCE_RESPONSE

    client_mock = AsyncMock()
    client_mock.post.return_value = fake_response
    client_mock.__aenter__.return_value = client_mock

    expires_at = datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
    with patch("app.mp_webhooks.httpx.AsyncClient", return_value=client_mock):
        await mp_webhooks.create_mp_preference(
            booking_id=1, amount=30.0, client_name="Test", expires_at=expires_at
        )

    body = client_mock.post.call_args.kwargs["json"]
    assert body["expires"] is True
    assert body["expiration_date_to"] == "2026-10-15T12:30:00.000+00:00"
    assert {"id": "ticket"} in body["payment_methods"]["excluded_payment_types"]
    assert {"id": "atm"} in body["payment_methods"]["excluded_payment_types"]
