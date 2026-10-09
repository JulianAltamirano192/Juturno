from datetime import datetime, timedelta, timezone

import pytest

from app import outbox_worker
from app.models import Booking, NotificationOutbox, Service, Tenant
from app.outbox_worker import MAX_OUTBOX_ATTEMPTS, process_outbox
from tests.conftest import TestingSessionLocal


class _OkResponse:
    def raise_for_status(self) -> None:
        pass


@pytest.fixture
def sent(monkeypatch):
    """Intercepta los envíos a WhatsApp y registra a qué booking fueron."""
    calls: list[int] = []

    async def fake_send(self, phone, booking_id, nombre, fecha):
        calls.append(booking_id)
        return _OkResponse()

    monkeypatch.setattr(outbox_worker.WhatsAppService, "send_confirmation", fake_send)
    monkeypatch.setattr(outbox_worker.WhatsAppService, "send_reminder", fake_send)
    return calls


async def _event(
    db_session,
    key: str,
    *,
    status: str = "pending",
    retry_count: int = 0,
    created_ago: timedelta = timedelta(0),
    tenant_tz: str = "UTC",
) -> NotificationOutbox:
    tenant = Tenant(name=f"Tenant {key}", timezone=tenant_tz)
    db_session.add(tenant)
    await db_session.flush()
    service = Service(tenant_id=tenant.id, name="Turno", duration_minutes=30, price=1)
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
        price_at_booking=1,
        idempotency_key=f"outbox-{key}",
        status="confirmed",
    )
    db_session.add(booking)
    await db_session.flush()
    event = NotificationOutbox(
        booking_id=booking.id,
        notification_type="confirmation",
        status=status,
        retry_count=retry_count,
        created_at=datetime.now(timezone.utc) - created_ago,
    )
    db_session.add(event)
    await db_session.commit()
    return event


@pytest.mark.asyncio
async def test_failed_event_is_retried_once_backoff_elapsed(db_session, sent):
    """Tras 1 intento fallido se reintenta a partir de 1 minuto de creado."""
    event = await _event(
        db_session,
        "due",
        status="failed",
        retry_count=1,
        created_ago=timedelta(minutes=2),
    )

    await process_outbox(TestingSessionLocal)

    await db_session.refresh(event)
    assert event.status == "sent"
    assert sent == [event.booking_id]


@pytest.mark.asyncio
async def test_failed_event_waits_for_backoff(db_session, sent):
    """Tras 2 intentos fallidos el próximo es a los 3 minutos de creado."""
    event = await _event(
        db_session,
        "wait",
        status="failed",
        retry_count=2,
        created_ago=timedelta(minutes=2),
    )

    await process_outbox(TestingSessionLocal)

    await db_session.refresh(event)
    assert event.status == "failed"
    assert sent == []


@pytest.mark.asyncio
async def test_failed_event_gives_up_after_max_attempts(db_session, sent):
    event = await _event(
        db_session,
        "dead",
        status="failed",
        retry_count=MAX_OUTBOX_ATTEMPTS,
        created_ago=timedelta(days=7),
    )

    await process_outbox(TestingSessionLocal)

    await db_session.refresh(event)
    assert event.status == "failed"
    assert sent == []


@pytest.mark.asyncio
async def test_one_broken_event_does_not_block_the_others(db_session, sent):
    """Un error fuera del envío (timezone inválida) marca solo ese evento como
    fallido; los demás se envían y quedan commiteados."""
    broken = await _event(db_session, "broken", tenant_tz="Not/AZone")
    ok = await _event(db_session, "ok")

    await process_outbox(TestingSessionLocal)

    await db_session.refresh(broken)
    await db_session.refresh(ok)
    assert broken.status == "failed"
    assert broken.retry_count == 1
    assert ok.status == "sent"
    assert sent == [ok.booking_id]


@pytest.mark.asyncio
async def test_stale_failed_event_is_not_retried(db_session, sent):
    """Un fallido viejo (p. ej. de antes de existir los reintentos) no se
    reenvía: avisaría de un turno con horas o días de atraso."""
    event = await _event(
        db_session,
        "stale",
        status="failed",
        retry_count=1,
        created_ago=timedelta(hours=3),
    )

    await process_outbox(TestingSessionLocal)

    await db_session.refresh(event)
    assert event.status == "failed"
    assert sent == []


@pytest.mark.asyncio
async def test_send_error_marks_failed_and_counts_attempt(db_session, monkeypatch):
    async def failing_send(self, phone, booking_id, nombre, fecha):
        raise RuntimeError("Meta caído")

    monkeypatch.setattr(
        outbox_worker.WhatsAppService, "send_confirmation", failing_send
    )
    event = await _event(db_session, "meta-down")

    await process_outbox(TestingSessionLocal)

    await db_session.refresh(event)
    assert event.status == "failed"
    assert event.retry_count == 1
    assert event.error_message == "Meta caído"


@pytest.mark.asyncio
async def test_db_error_in_one_event_does_not_abort_the_batch(
    db_session, sent, monkeypatch
):
    """Un error de base dentro del envío deja la transacción abortada; aun así
    ese evento queda failed y el resto del lote se procesa."""
    from sqlalchemy import text

    broken = await _event(db_session, "db-broken")
    ok = await _event(db_session, "db-ok")
    real_send_event = outbox_worker._send_event

    async def send_event(session, whatsapp, event):
        if event.id == broken.id:
            await session.execute(text("SELECT 1/0"))
        await real_send_event(session, whatsapp, event)

    monkeypatch.setattr(outbox_worker, "_send_event", send_event)

    await process_outbox(TestingSessionLocal)

    await db_session.refresh(broken)
    await db_session.refresh(ok)
    assert broken.status == "failed"
    assert broken.retry_count == 1
    assert ok.status == "sent"


def test_format_booking_datetime_uses_tenant_timezone_literally():
    """WhatsApp must show the same local time as the slots and the agenda,
    which use tenant.timezone as-is (UTC included)."""
    dt = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    assert outbox_worker.format_booking_datetime(dt, "UTC") == "15/10/2026 a las 12:00"
    assert (
        outbox_worker.format_booking_datetime(dt, "America/Argentina/Buenos_Aires")
        == "15/10/2026 a las 09:00"
    )
