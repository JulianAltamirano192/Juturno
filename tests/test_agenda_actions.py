"""Tests de integración para las acciones sobre turno (Tarea 8)."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.csrf import generate_csrf_token
from app.main import app
from app.models import Booking, Service, Tenant
from app.session import create_session_token
from tests.conftest import TestingSessionLocal


def _make_session_cookie(tenant):
    return create_session_token(tenant.id, tenant.session_version)


async def _setup(session, status="pending", start_offset_hours=-1):
    tenant = Tenant(
        name=f"Biz Agenda {uuid.uuid4().hex[:6]}",
        slug=f"agenda-{uuid.uuid4().hex[:8]}",
        owner_email=f"agenda-{uuid.uuid4().hex[:8]}@test.com",
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
        client_name="Cliente Agenda",
        client_phone="+5491155555555",
        start_time=start,
        end_time=end,
        price_at_booking=Decimal("5000.00"),
        idempotency_key=f"key-{uuid.uuid4().hex}",
        status=status,
    )
    session.add(booking)
    await session.commit()
    await session.refresh(tenant)
    await session.refresh(booking)
    return tenant, booking


@pytest.mark.asyncio
async def test_confirm_requires_session():
    async with TestingSessionLocal() as session:
        _, booking = await _setup(session, status="pending")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/panel/agenda/{booking.id}/confirm",
            data={},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_confirm_pending_booking():
    async with TestingSessionLocal() as session:
        tenant, booking = await _setup(session, status="pending")

    cookie = _make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/agenda/{booking.id}/confirm",
            data={"csrf_token": csrf, "day": "2026-10-02"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "/panel/agenda" in resp.headers["location"]

    async with TestingSessionLocal() as session2:
        updated = await session2.get(Booking, booking.id)
    assert updated.status == "confirmed"
    assert updated.status_changed_by == "owner"
    assert updated.status_changed_at is not None


@pytest.mark.asyncio
async def test_cancel_pending_booking_with_reason():
    async with TestingSessionLocal() as session:
        tenant, booking = await _setup(session, status="pending")

    cookie = _make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/agenda/{booking.id}/cancel",
            data={
                "csrf_token": csrf,
                "reason": "Cliente avisó que no puede",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303

    async with TestingSessionLocal() as session2:
        updated = await session2.get(Booking, booking.id)
    assert updated.status == "cancelled"
    assert updated.cancellation_reason == "Cliente avisó que no puede"


@pytest.mark.asyncio
async def test_complete_confirmed_booking_past():
    async with TestingSessionLocal() as session:
        tenant, booking = await _setup(
            session, status="confirmed", start_offset_hours=-1
        )

    cookie = _make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/agenda/{booking.id}/complete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
    assert resp.status_code == 303

    async with TestingSessionLocal() as session2:
        updated = await session2.get(Booking, booking.id)
    assert updated.status == "completed"
    assert updated.completed_at is not None


@pytest.mark.asyncio
async def test_complete_future_booking_returns_409():
    async with TestingSessionLocal() as session:
        tenant, booking = await _setup(
            session, status="confirmed", start_offset_hours=+2
        )

    cookie = _make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/agenda/{booking.id}/complete",
            data={"csrf_token": csrf},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_no_show_confirmed_booking_past():
    async with TestingSessionLocal() as session:
        tenant, booking = await _setup(
            session, status="confirmed", start_offset_hours=-1
        )

    cookie = _make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/agenda/{booking.id}/no-show",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
    assert resp.status_code == 303

    async with TestingSessionLocal() as session2:
        updated = await session2.get(Booking, booking.id)
    assert updated.status == "no_show"
    assert updated.no_show_at is not None


@pytest.mark.asyncio
async def test_confirm_already_cancelled_returns_409():
    async with TestingSessionLocal() as session:
        tenant, booking = await _setup(session, status="cancelled")

    cookie = _make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/agenda/{booking.id}/confirm",
            data={"csrf_token": csrf},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cross_tenant_booking_returns_404():
    async with TestingSessionLocal() as session:
        _, booking_a = await _setup(session, status="pending")
        tenant_b, _ = await _setup(session, status="pending")

    # Tenant B intenta operar sobre booking de A
    cookie = _make_session_cookie(tenant_b)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/agenda/{booking_a.id}/confirm",
            data={"csrf_token": csrf},
        )
    assert resp.status_code == 404
