"""Tests de integración para la vista de agenda por día (Tarea 7)."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

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


# ---------------------------------------------------------------------------
# Vista semanal (?view=week)
# ---------------------------------------------------------------------------

# Semana fija de lunes a domingo (pasada, para no depender de "hoy").
WEEK_MONDAY = date(2026, 6, 1)
WEEK_SUNDAY = WEEK_MONDAY + timedelta(days=6)


async def _tenant_with_week(session, prefix="Cliente"):
    """Tenant con turnos en los bordes de la semana (hora local del tenant)."""
    assert WEEK_MONDAY.weekday() == 0
    tenant = Tenant(
        name=f"Semana {prefix}",
        slug=f"semana-{prefix.lower()}-{int(datetime.now().timestamp() * 1000)}",
        owner_email=f"semana-{prefix.lower()}@test.com",
        password_hash="x",
        session_version=1,
        timezone=TENANT_TZ,
    )
    session.add(tenant)
    await session.flush()
    service = Service(
        tenant_id=tenant.id,
        name="Servicio semanal",
        duration_minutes=30,
        price=Decimal("1000.00"),
        is_active=True,
    )
    session.add(service)
    await session.flush()

    tz = ZoneInfo(TENANT_TZ)

    def _booking(day, hh, mm, suffix, status="confirmed"):
        client = f"{prefix} {suffix}"
        start = datetime.combine(day, time(hh, mm), tzinfo=tz)
        return Booking(
            tenant_id=tenant.id,
            service_id=service.id,
            client_name=client,
            client_phone="5491100000000",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            price_at_booking=Decimal("1000.00"),
            status=status,
            idempotency_key=f"semana-{client}",
        )

    session.add_all(
        [
            _booking(WEEK_MONDAY, 0, 15, "Lunes"),
            # 22:30 ART del domingo = 01:30 UTC del lunes siguiente.
            _booking(WEEK_SUNDAY, 22, 30, "Domingo", "pending"),
            # Domingo anterior 23:15 ART = lunes 02:15 UTC: no es de la semana.
            _booking(WEEK_MONDAY - timedelta(days=1), 23, 15, "Previo"),
            _booking(WEEK_SUNDAY + timedelta(days=1), 0, 15, "Siguiente"),
        ]
    )
    await session.commit()
    await session.refresh(tenant)
    return tenant


async def _get_week(client, tenant, day=WEEK_MONDAY + timedelta(days=2)):
    return await client.get(
        "/panel/agenda",
        params={"view": "week", "day": str(day)},
        cookies={"juturno_session": make_session_cookie(tenant)},
    )


@pytest.mark.asyncio
async def test_week_requires_session(client):
    resp = await client.get(
        "/panel/agenda", params={"view": "week"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_week_shows_monday_to_sunday_in_tenant_timezone(client, db_session):
    tenant = await _tenant_with_week(db_session)

    resp = await _get_week(client, tenant)

    assert resp.status_code == 200
    text = resp.text
    assert "Cliente Lunes" in text
    assert "Cliente Domingo" in text
    assert "Cliente Previo" not in text
    assert "Cliente Siguiente" not in text
    # Hora local, ordenado por inicio y con etiqueta de estado.
    assert "00:15" in text and "22:30" in text
    assert text.index("Cliente Lunes") < text.index("Cliente Domingo")
    assert "Pendiente de pago" in text and "Servicio semanal" in text
    # Días vacíos de la semana y enlace de cada día a la vista diaria.
    assert "Sin turnos" in text
    assert f'href="/panel/agenda?day={WEEK_MONDAY + timedelta(days=1)}"' in text
    # Navegación a semana anterior / siguiente y "Esta semana" (no es la actual).
    assert f"view=week&day={WEEK_MONDAY - timedelta(days=7)}" in text
    assert f"view=week&day={WEEK_MONDAY + timedelta(days=7)}" in text
    assert "Esta semana" in text


@pytest.mark.asyncio
async def test_week_is_read_only(client, db_session):
    tenant = await _tenant_with_week(db_session)

    text = (await _get_week(client, tenant)).text

    assert "/confirm" not in text
    assert "/cancel" not in text
    assert "/notes" not in text


@pytest.mark.asyncio
async def test_week_isolation_between_tenants(client, db_session):
    tenant = await _tenant_with_week(db_session)
    await _tenant_with_week(db_session, "Ajeno")

    text = (await _get_week(client, tenant)).text

    assert "Cliente Lunes" in text
    assert "Ajeno" not in text


@pytest.mark.asyncio
async def test_week_escapes_client_name(client, db_session):
    tenant = await _tenant_with_week(db_session)
    booking = (
        await db_session.execute(
            select(Booking).where(
                Booking.tenant_id == tenant.id,
                Booking.client_name == "Cliente Lunes",
            )
        )
    ).scalar_one()
    booking.client_name = "<script>alert(1)</script>"
    await db_session.commit()

    text = (await _get_week(client, tenant)).text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


@pytest.mark.asyncio
async def test_week_invalid_day_falls_back_to_current_week(client, db_session):
    tenant, today = await _tenant_with_bookings(db_session)

    resp = await client.get(
        "/panel/agenda",
        params={"view": "week", "day": "no-es-una-fecha"},
        cookies={"juturno_session": make_session_cookie(tenant)},
    )

    assert resp.status_code == 200
    assert "Cliente morning" in resp.text
    monday = today - timedelta(days=today.weekday())
    assert f'href="/panel/agenda?day={monday}"' in resp.text
    assert "Esta semana" not in resp.text


@pytest.mark.asyncio
async def test_unknown_view_falls_back_to_daily(client, db_session):
    tenant = await _tenant_with_week(db_session)

    resp = await client.get(
        "/panel/agenda",
        params={"view": "month", "day": str(WEEK_MONDAY)},
        cookies={"juturno_session": make_session_cookie(tenant)},
    )

    assert resp.status_code == 200
    assert "Cliente Lunes" in resp.text
    assert "Cliente Domingo" not in resp.text
    assert "Sin turnos" not in resp.text


@pytest.mark.asyncio
async def test_daily_view_links_to_its_week(client, db_session):
    tenant = await _tenant_with_week(db_session)

    resp = await client.get(
        "/panel/agenda",
        params={"day": str(WEEK_SUNDAY)},
        cookies={"juturno_session": make_session_cookie(tenant)},
    )

    assert resp.status_code == 200
    assert f"/panel/agenda?view=week&day={WEEK_SUNDAY}" in resp.text
