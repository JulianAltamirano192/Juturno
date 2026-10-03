"""Tests unitarios del service layer de transiciones de estado (Tarea 8)."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.booking_actions import (
    VALID_TRANSITIONS,
    BookingNotStartedError,
    InvalidTransitionError,
    transition_booking_status,
)
from app.models import Booking, NotificationOutbox, Service, Tenant
from tests.conftest import TestingSessionLocal


async def _make_booking(session, status="pending", start_offset_hours=-1):
    """Crea tenant + service + booking. start_offset_hours=-1 = ya empezó."""
    tenant = Tenant(
        name="Test Biz Actions",
        slug=f"test-actions-{uuid.uuid4().hex[:8]}",
        owner_email=f"actions-{uuid.uuid4().hex[:8]}@test.com",
        password_hash="x",
        session_version=1,
    )
    session.add(tenant)
    await session.flush()

    service = Service(
        tenant_id=tenant.id,
        name="Corte",
        duration_minutes=30,
        price=Decimal("5000.00"),
    )
    session.add(service)
    await session.flush()

    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=start_offset_hours)
    end = start + timedelta(minutes=30)

    booking = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        client_name="Cliente Test",
        client_phone="+5491155555555",
        start_time=start,
        end_time=end,
        price_at_booking=Decimal("5000.00"),
        idempotency_key=f"key-{uuid.uuid4().hex}",
        status=status,
    )
    session.add(booking)
    await session.commit()
    await session.refresh(booking)
    return tenant, service, booking


@pytest.mark.asyncio
async def test_pending_to_confirmed():
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(session, status="pending")

        result = await transition_booking_status(
            session, booking, "confirmed", actor="owner"
        )
        await session.commit()

        assert result.status == "confirmed"
        assert result.status_changed_at is not None
        assert result.status_changed_by == "owner"


@pytest.mark.asyncio
async def test_pending_to_cancelled_sets_reason():
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(session, status="pending")

        result = await transition_booking_status(
            session,
            booking,
            "cancelled",
            actor="owner",
            reason="Cliente pidió cancelar",
        )
        await session.commit()

        assert result.status == "cancelled"
        assert result.cancellation_reason == "Cliente pidió cancelar"
        assert result.status_changed_by == "owner"


@pytest.mark.asyncio
async def test_confirmed_to_completed_past():
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(
            session, status="confirmed", start_offset_hours=-1
        )

        result = await transition_booking_status(
            session, booking, "completed", actor="owner"
        )
        await session.commit()

        assert result.status == "completed"
        assert result.completed_at is not None


@pytest.mark.asyncio
async def test_confirmed_to_no_show_past():
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(
            session, status="confirmed", start_offset_hours=-1
        )

        result = await transition_booking_status(
            session, booking, "no_show", actor="owner"
        )
        await session.commit()

        assert result.status == "no_show"
        assert result.no_show_at is not None


@pytest.mark.asyncio
async def test_completed_requires_started():
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(
            session, status="confirmed", start_offset_hours=+1
        )

        with pytest.raises(BookingNotStartedError):
            await transition_booking_status(
                session, booking, "completed", actor="owner"
            )


@pytest.mark.asyncio
async def test_no_show_requires_started():
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(
            session, status="confirmed", start_offset_hours=+1
        )

        with pytest.raises(BookingNotStartedError):
            await transition_booking_status(session, booking, "no_show", actor="owner")


@pytest.mark.asyncio
async def test_invalid_transition_from_terminal():
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(session, status="cancelled")

        with pytest.raises(InvalidTransitionError):
            await transition_booking_status(
                session, booking, "confirmed", actor="owner"
            )


@pytest.mark.asyncio
async def test_invalid_transition_completed_to_cancelled():
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(session, status="completed")

        with pytest.raises(InvalidTransitionError):
            await transition_booking_status(
                session, booking, "cancelled", actor="owner"
            )


@pytest.mark.asyncio
async def test_expired_to_confirmed_allowed():
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(session, status="expired")

        result = await transition_booking_status(
            session, booking, "confirmed", actor="webhook_mp"
        )
        await session.commit()

        assert result.status == "confirmed"
        assert result.status_changed_by == "webhook_mp"


@pytest.mark.asyncio
async def test_cancel_cancels_pending_outbox():
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(session, status="confirmed")

        # Crear un outbox pendiente
        outbox = NotificationOutbox(
            booking_id=booking.id,
            notification_type="confirmation",
            status="pending",
        )
        session.add(outbox)
        await session.commit()

        await transition_booking_status(
            session, booking, "cancelled", actor="owner", reason="test"
        )
        await session.commit()

        async with TestingSessionLocal() as session2:
            from sqlalchemy import select

            evt = (
                await session2.execute(
                    select(NotificationOutbox).where(
                        NotificationOutbox.booking_id == booking.id
                    )
                )
            ).scalar_one()
            assert evt.status == "cancelled"
            assert evt.error_message == "booking_cancelled"


@pytest.mark.asyncio
async def test_transition_does_not_commit():
    """Si no commiteamos, el cambio no se persiste."""
    async with TestingSessionLocal() as session:
        _, _, booking = await _make_booking(session, status="pending")
        booking_id = booking.id

        await transition_booking_status(session, booking, "confirmed", actor="owner")
        # Sin commit — rollback implícito al cerrar la sesión
        await session.rollback()

    async with TestingSessionLocal() as session2:
        fresh = await session2.get(Booking, booking_id)
        assert fresh.status == "pending"


def test_valid_transitions_matrix():
    """Sanity check de la matriz de transiciones."""
    assert VALID_TRANSITIONS["pending"] == {"confirmed", "cancelled", "expired"}
    assert VALID_TRANSITIONS["confirmed"] == {"cancelled", "no_show", "completed"}
    assert VALID_TRANSITIONS["expired"] == {"confirmed"}
    assert VALID_TRANSITIONS["cancelled"] == set()
    assert VALID_TRANSITIONS["no_show"] == set()
    assert VALID_TRANSITIONS["completed"] == set()
