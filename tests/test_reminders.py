"""
Finding #6: process_reminders used a fixed [now+24h, now+24h+5m] window, so a
skipped run or a booking confirmed less than 24 h ahead never got a reminder.
Now every confirmed, not-yet-reminded booking starting within 24 h gets one.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import redis.asyncio as redis_async
from sqlalchemy import select

from app import scheduler
from app.config import settings
from app.models import Booking, NotificationOutbox, Service, Tenant
from app.scheduler import process_reminders
from tests.conftest import TestingSessionLocal


@pytest.fixture(autouse=True)
def _fresh_redis_client(monkeypatch):
    # Same reason as in test_deposit_expiration: one event loop per test.
    client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)
    monkeypatch.setattr(scheduler, "_get_redis_client", lambda: client)


async def _booking(
    session, tenant, service, *, starts_in, status="confirmed", sent=False
):
    start = datetime.now(timezone.utc) + starts_in
    booking = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        client_name="Cliente",
        client_phone="5493584166288",
        start_time=start,
        end_time=start + timedelta(minutes=30),
        price_at_booking=1000,
        idempotency_key=f"rem-{uuid.uuid4().hex}",
        status=status,
        reminder_sent=sent,
    )
    session.add(booking)
    return booking


@pytest.mark.asyncio
async def test_reminders_cover_every_due_booking_once(db_session):
    tenant = Tenant(name="Negocio Recordatorios", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()
    service = Service(
        tenant_id=tenant.id, name="Turno", duration_minutes=30, price=1000
    )
    db_session.add(service)
    await db_session.flush()

    due = {
        # Confirmed less than 24 h ahead, or the 5-min run was skipped.
        "in_2h": await _booking(
            db_session, tenant, service, starts_in=timedelta(hours=2)
        ),
        "in_23h": await _booking(
            db_session, tenant, service, starts_in=timedelta(hours=23)
        ),
    }
    not_due = {
        "in_25h": await _booking(
            db_session, tenant, service, starts_in=timedelta(hours=25)
        ),
        "started": await _booking(
            db_session, tenant, service, starts_in=-timedelta(minutes=10)
        ),
        "pending": await _booking(
            db_session, tenant, service, starts_in=timedelta(hours=3), status="pending"
        ),
        "cancelled": await _booking(
            db_session,
            tenant,
            service,
            starts_in=timedelta(hours=4),
            status="cancelled",
        ),
        "already_sent": await _booking(
            db_session, tenant, service, starts_in=timedelta(hours=5), sent=True
        ),
    }
    await db_session.commit()

    await process_reminders(TestingSessionLocal)
    await process_reminders(TestingSessionLocal)  # a second run adds nothing

    async with TestingSessionLocal() as session:
        reminders = (
            (
                await session.execute(
                    select(NotificationOutbox.booking_id).where(
                        NotificationOutbox.status == "pending",
                        NotificationOutbox.notification_type == "reminder",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sorted(reminders) == sorted(b.id for b in due.values())
        for b in due.values():
            assert (await session.get(Booking, b.id)).reminder_sent is True
        for name, b in not_due.items():
            if name != "already_sent":
                assert (await session.get(Booking, b.id)).reminder_sent is False, name


@pytest.mark.asyncio
async def test_reminders_skip_bookings_locked_by_another_run(db_session):
    """If the 30 s Redis lock expires mid-run, an overlapping run must skip the
    rows the first one holds instead of waiting and reminding them twice."""
    tenant = Tenant(name="Negocio Lock", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()
    service = Service(
        tenant_id=tenant.id, name="Turno", duration_minutes=30, price=1000
    )
    db_session.add(service)
    await db_session.flush()
    booking = await _booking(db_session, tenant, service, starts_in=timedelta(hours=2))
    await db_session.commit()

    async with TestingSessionLocal() as holder:
        await holder.execute(
            select(Booking).where(Booking.id == booking.id).with_for_update()
        )
        await asyncio.wait_for(process_reminders(TestingSessionLocal), timeout=5)

    async with TestingSessionLocal() as session:
        assert (await session.get(Booking, booking.id)).reminder_sent is False
        count = (
            await session.execute(
                select(NotificationOutbox.id).where(
                    NotificationOutbox.booking_id == booking.id
                )
            )
        ).all()
        assert count == []
