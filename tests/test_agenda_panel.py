"""Tests de integración para la vista de agenda por día (Tarea 7)."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.models import Booking, Service, Tenant
from app.session import create_session_token

TENANT_TZ = "America/Argentina/Buenos_Aires"


def make_session_cookie(tenant):
    return create_session_token(tenant.id, tenant.session_version)


async def _tenant_with_bookings(session):
    """Tenant + servicio + dos turnos para hoy (uno confirmed, uno cancelled)
    y un turno confirmado mañana (para probar el filtro por día)."""
    tenant = Tenant(
        name="Agenda Biz",
        slug=f"agenda-{int(datetime.now().timestamp() * 1000)}",
        owner_email="agenda@test.com",
        password_hash="x",
        session_version=1,
        timezone=TENANT_TZ,
    )
    session.add(tenant)
    await session.flush()
    service = Service(
        tenant_id=tenant.id,
        name="Corte agenda",
        duration_minutes=30,
        price=Decimal("1000.00"),
        is_active=True,
    )
    session.add(service)
    await session.flush()

    tz = ZoneInfo(TENANT_TZ)
    today = datetime.now(tz).date()

    def _booking(start_h, start_m, status, suffix):
        start = datetime.combine(today, time(start_h, start_m), tzinfo=tz)
        return Booking(
            tenant_id=tenant.id,
            service_id=service.id,
            client_name=f"Cliente {suffix}",
            client_phone="5491100000000",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            price_at_booking=Decimal("1000.00"),
            status=status,
            idempotency_key=f"agenda-test-{suffix}",
        )

    b1 = _booking(10, 0, "confirmed", "morning")
    b2 = _booking(11, 0, "cancelled", "cancelled")

    tomorrow = today + timedelta(days=1)
    start_tomorrow = datetime.combine(tomorrow, time(10, 0), tzinfo=tz)
    b3 = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        client_name="Cliente Manana",
        client_phone="5491100000001",
        start_time=start_tomorrow,
        end_time=start_tomorrow + timedelta(minutes=30),
        price_at_booking=Decimal("1000.00"),
        status="confirmed",
        idempotency_key="agenda-test-tomorrow",
    )

    session.add_all([b1, b2, b3])
    await session.commit()
    await session.refresh(tenant)
    return tenant, today


@pytest.mark.asyncio
async def test_agenda_requires_session(client):
    resp = await client.get("/panel/agenda", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_agenda_shows_todays_bookings(client, db_session):
    tenant, _today = await _tenant_with_bookings(db_session)
    cookie = make_session_cookie(tenant)

    resp = await client.get("/panel/agenda", cookies={"juturno_session": cookie})

    assert resp.status_code == 200
    text = resp.text
    # Los dos turnos de hoy aparecen
    assert "Cliente morning" in text
    assert "Cliente cancelled" in text
    assert "Corte agenda" in text
    # El de mañana NO aparece (filtro por día)
    assert "Cliente Manana" not in text
    # Distinción visual de estados
    assert "badge-confirmed" in text
    assert "badge-cancelled" in text


@pytest.mark.asyncio
async def test_agenda_filters_by_day_param(client, db_session):
    tenant, today = await _tenant_with_bookings(db_session)
    cookie = make_session_cookie(tenant)
    tomorrow = (today + timedelta(days=1)).isoformat()

    resp = await client.get(
        f"/panel/agenda?day={tomorrow}",
        cookies={"juturno_session": cookie},
    )

    assert resp.status_code == 200
    text = resp.text
    assert "Cliente Manana" in text
    assert "Cliente morning" not in text


@pytest.mark.asyncio
async def test_agenda_empty_day_shows_empty_state(client, db_session):
    tenant, today = await _tenant_with_bookings(db_session)
    cookie = make_session_cookie(tenant)
    past = (today - timedelta(days=10)).isoformat()

    resp = await client.get(
        f"/panel/agenda?day={past}",
        cookies={"juturno_session": cookie},
    )

    assert resp.status_code == 200
    assert "No hay turnos" in resp.text


@pytest.mark.asyncio
async def test_agenda_shows_local_time_not_utc(client, db_session):
    """El horario mostrado es en el timezone del tenant (ART = UTC-3)."""
    tenant, _today = await _tenant_with_bookings(db_session)
    cookie = make_session_cookie(tenant)

    resp = await client.get("/panel/agenda", cookies={"juturno_session": cookie})

    assert resp.status_code == 200
    # El turno a las 10:00 ART aparece como 10:00, no como 13:00 UTC
    assert "10:00" in resp.text
    assert "13:00" not in resp.text


@pytest.mark.asyncio
async def test_agenda_isolation_between_tenants(client, db_session):
    """Los turnos de otro tenant no aparecen en mi agenda."""
    tenant, today = await _tenant_with_bookings(db_session)

    tz = ZoneInfo(TENANT_TZ)
    other = Tenant(
        name="Otro Negocio",
        slug=f"agenda-other-{int(datetime.now().timestamp() * 1000)}",
        owner_email="other@test.com",
        password_hash="x",
        session_version=1,
        timezone=TENANT_TZ,
    )
    db_session.add(other)
    await db_session.flush()
    svc_other = Service(
        tenant_id=other.id,
        name="Servicio ajeno",
        duration_minutes=30,
        price=Decimal("500.00"),
        is_active=True,
    )
    db_session.add(svc_other)
    await db_session.flush()
    start = datetime.combine(today, time(9, 0), tzinfo=tz)
    db_session.add(
        Booking(
            tenant_id=other.id,
            service_id=svc_other.id,
            client_name="Cliente Ajeno",
            client_phone="5491100000002",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            price_at_booking=Decimal("500.00"),
            status="confirmed",
            idempotency_key="agenda-other-booking",
        )
    )
    await db_session.commit()

    cookie = make_session_cookie(tenant)
    resp = await client.get("/panel/agenda", cookies={"juturno_session": cookie})

    assert resp.status_code == 200
    assert "Cliente Ajeno" not in resp.text
    assert "Servicio ajeno" not in resp.text


@pytest.mark.asyncio
async def test_agenda_invalid_day_falls_back_to_today(client, db_session):
    """?day= inválido o mal formateado → se muestra hoy, no un 422 JSON."""
    tenant, _ = await _tenant_with_bookings(db_session)
    cookie = make_session_cookie(tenant)

    resp = await client.get(
        "/panel/agenda?day=no-es-una-fecha",
        cookies={"juturno_session": cookie},
    )

    assert resp.status_code == 200
    # Cae a hoy: aparecen los turnos de hoy
    assert "Cliente morning" in resp.text


@pytest.mark.asyncio
async def test_agenda_shows_booking_crossing_midnight(client, db_session):
    """Un turno que empieza 23:30 del día anterior y termina 00:30 de hoy
    aparece en la vista de hoy (filtro por solapamiento)."""
    tenant = Tenant(
        name="Agenda Medianoche",
        slug=f"agenda-midnight-{int(datetime.now().timestamp() * 1000)}",
        owner_email="midnight@test.com",
        password_hash="x",
        session_version=1,
        timezone=TENANT_TZ,
    )
    db_session.add(tenant)
    await db_session.flush()
    service = Service(
        tenant_id=tenant.id,
        name="Turno nocturno",
        duration_minutes=60,
        price=Decimal("2000.00"),
        is_active=True,
    )
    db_session.add(service)
    await db_session.flush()

    tz = ZoneInfo(TENANT_TZ)
    today = datetime.now(tz).date()
    yesterday = today - timedelta(days=1)
    # Turno: ayer 23:30 → hoy 00:30 (hora local del tenant)
    start = datetime.combine(yesterday, time(23, 30), tzinfo=tz)
    db_session.add(
        Booking(
            tenant_id=tenant.id,
            service_id=service.id,
            client_name="Cliente Nocturno",
            client_phone="5491100000003",
            start_time=start,
            end_time=start + timedelta(minutes=60),
            price_at_booking=Decimal("2000.00"),
            status="confirmed",
            idempotency_key="agenda-midnight-booking",
        )
    )
    await db_session.commit()

    cookie = make_session_cookie(tenant)

    # Mirando HOY → aparece (aunque empezó ayer)
    resp_today = await client.get(
        "/panel/agenda",
        params={"day": today.isoformat()},
        cookies={"juturno_session": cookie},
    )
    assert resp_today.status_code == 200
    assert "Cliente Nocturno" in resp_today.text

    # Y mirando AYER también (donde empezó)
    resp_yesterday = await client.get(
        "/panel/agenda",
        params={"day": yesterday.isoformat()},
        cookies={"juturno_session": cookie},
    )
    assert resp_yesterday.status_code == 200
    assert "Cliente Nocturno" in resp_yesterday.text
