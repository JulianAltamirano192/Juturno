"""Tests del alta manual de turnos desde el panel (GET/POST /panel/agenda/new)."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.csrf import generate_csrf_token
from app.main import app
from app.models import Booking, NotificationOutbox, Service, Staff, Tenant
from app.session import create_session_token
from tests.conftest import TestingSessionLocal

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


async def _setup():
    """Tenant (zona AR) con un servicio activo, uno inactivo y un staff activo."""
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name=f"Biz Manual {uuid.uuid4().hex[:6]}",
            slug=f"manual-{uuid.uuid4().hex[:8]}",
            owner_email=f"manual-{uuid.uuid4().hex[:8]}@test.com",
            password_hash="x",
            session_version=1,
            timezone="America/Argentina/Buenos_Aires",
        )
        session.add(tenant)
        await session.flush()
        active = Service(
            tenant_id=tenant.id,
            name="Servicio Activo",
            duration_minutes=45,
            price=Decimal("8000.00"),
            deposit_amount=Decimal("2000.00"),
        )
        inactive = Service(
            tenant_id=tenant.id,
            name="Servicio Inactivo",
            duration_minutes=30,
            price=Decimal("1000.00"),
            is_active=False,
        )
        staff = Staff(tenant_id=tenant.id, name="Ana Staff")
        session.add_all([active, inactive, staff])
        await session.commit()
        for obj in (tenant, active, inactive, staff):
            await session.refresh(obj)
        return tenant, active, inactive, staff


def _future_day() -> str:
    return (datetime.now(TZ).date() + timedelta(days=2)).isoformat()


def _valid_form(service: Service, **overrides) -> dict:
    data = {
        "service_id": str(service.id),
        "staff_id": "",
        "date": _future_day(),
        "time": "10:00",
        "client_name": "María Gómez",
        "client_phone": "11 5555-5555",
    }
    data.update(overrides)
    return data


async def _post(tenant, data, csrf=True):
    token = generate_csrf_token()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(
            "juturno_session",
            create_session_token(tenant.id, tenant.session_version),
        )
        if csrf:
            client.cookies.set("csrf_token", token)
            data = {"csrf_token": token, **data}
        return await client.post("/panel/agenda/new", data=data, follow_redirects=False)


async def _bookings(tenant_id: int) -> list[Booking]:
    async with TestingSessionLocal() as session:
        result = await session.execute(
            select(Booking).where(Booking.tenant_id == tenant_id)
        )
        return list(result.scalars().all())


async def _outbox_count(booking_id: int) -> int:
    async with TestingSessionLocal() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(NotificationOutbox)
                .where(
                    NotificationOutbox.booking_id == booking_id,
                    NotificationOutbox.notification_type == "confirmation",
                )
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_get_form_lists_only_active_services_of_tenant():
    tenant, active, _, _ = await _setup()
    _, other_service, _, _ = await _setup()
    day = _future_day()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(
            "juturno_session",
            create_session_token(tenant.id, tenant.session_version),
        )
        resp = await client.get(f"/panel/agenda/new?day={day}")
    assert resp.status_code == 200
    assert 'name="csrf_token"' in resp.text
    assert "csrf_token" in resp.cookies
    assert f'value="{active.id}"' in resp.text
    assert "Servicio Inactivo" not in resp.text
    assert f'value="{other_service.id}"' not in resp.text
    assert "Ana Staff" in resp.text
    assert "Sin asignar" in resp.text
    assert f'value="{day}"' in resp.text


@pytest.mark.asyncio
async def test_get_form_requires_session():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/panel/agenda/new", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_post_requires_session():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/panel/agenda/new", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_post_without_csrf_returns_403():
    tenant, active, _, _ = await _setup()
    resp = await _post(tenant, _valid_form(active), csrf=False)
    assert resp.status_code == 403
    assert await _bookings(tenant.id) == []


@pytest.mark.asyncio
async def test_create_manual_booking_happy_path():
    tenant, active, _, _ = await _setup()
    day = _future_day()
    resp = await _post(tenant, _valid_form(active, date=day))
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/panel/agenda?day={day}"

    [booking] = await _bookings(tenant.id)
    assert booking.status == "confirmed"
    assert booking.status_changed_by == "owner"
    assert booking.deposit_at_booking == Decimal(0)
    assert booking.price_at_booking == Decimal("8000.00")
    assert booking.staff_id is None
    assert booking.client_name == "María Gómez"
    assert booking.client_phone == "5491155555555"
    assert booking.idempotency_key.startswith("panel-")
    expected_start = datetime.fromisoformat(f"{day}T10:00").replace(tzinfo=TZ)
    assert booking.start_time == expected_start.astimezone(timezone.utc)
    assert booking.end_time - booking.start_time == timedelta(minutes=45)
    assert await _outbox_count(booking.id) == 1


@pytest.mark.asyncio
async def test_create_manual_booking_with_staff():
    tenant, active, _, staff = await _setup()
    resp = await _post(tenant, _valid_form(active, staff_id=str(staff.id)))
    assert resp.status_code == 303
    [booking] = await _bookings(tenant.id)
    assert booking.staff_id == staff.id


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"date": "2020-01-01"}, id="past"),
        pytest.param({"date": "no-es-fecha"}, id="invalid-date"),
        pytest.param({"time": "25:99"}, id="invalid-time"),
        pytest.param({"time": ""}, id="empty-time"),
        pytest.param({"client_name": "   "}, id="empty-name"),
        pytest.param({"client_phone": "123"}, id="invalid-phone"),
        pytest.param({"service_id": "abc"}, id="bad-service-id"),
        # 23:00 en UTC-3 pasa del año 9999 en UTC: error de form, no un 500.
        pytest.param({"date": "9999-12-31", "time": "23:00"}, id="date-overflow"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_input_rerenders_form_and_saves_nothing(overrides):
    tenant, active, _, _ = await _setup()
    form = _valid_form(active, **overrides)
    resp = await _post(tenant, form)
    assert resp.status_code == 400
    assert 'class="field-error"' in resp.text
    # Conserva lo que escribió el usuario
    assert 'name="client_name"' in resp.text
    assert await _bookings(tenant.id) == []


@pytest.mark.asyncio
async def test_today_past_time_is_rejected():
    tenant, active, _, _ = await _setup()
    past = datetime.now(TZ) - timedelta(minutes=5)
    resp = await _post(
        tenant,
        _valid_form(active, date=past.date().isoformat(), time=past.strftime("%H:%M")),
    )
    assert resp.status_code == 400
    assert await _bookings(tenant.id) == []


@pytest.mark.asyncio
async def test_inactive_service_is_rejected():
    tenant, _, inactive, _ = await _setup()
    resp = await _post(tenant, _valid_form(inactive))
    assert resp.status_code == 400
    assert await _bookings(tenant.id) == []
    # El select no debe caer en otro servicio que el dueño no eligió.
    assert '<option value="" disabled selected>Elegí un servicio</option>' in resp.text


@pytest.mark.asyncio
async def test_service_of_other_tenant_is_rejected():
    tenant, _, _, _ = await _setup()
    _, other_service, _, _ = await _setup()
    resp = await _post(tenant, _valid_form(other_service))
    assert resp.status_code == 400
    assert await _bookings(tenant.id) == []
    assert await _bookings(other_service.tenant_id) == []


@pytest.mark.asyncio
async def test_staff_of_other_tenant_is_rejected():
    tenant, active, _, _ = await _setup()
    _, _, _, other_staff = await _setup()
    resp = await _post(tenant, _valid_form(active, staff_id=str(other_staff.id)))
    assert resp.status_code == 400
    assert await _bookings(tenant.id) == []


@pytest.mark.asyncio
async def test_inactive_staff_is_rejected():
    tenant, active, _, staff = await _setup()
    async with TestingSessionLocal() as session:
        db_staff = await session.get(Staff, staff.id)
        db_staff.is_active = False
        await session.commit()
    resp = await _post(tenant, _valid_form(active, staff_id=str(staff.id)))
    assert resp.status_code == 400
    assert await _bookings(tenant.id) == []


@pytest.mark.asyncio
async def test_overlap_with_confirmed_booking_returns_409():
    tenant, active, _, _ = await _setup()
    day = _future_day()
    first = await _post(tenant, _valid_form(active, date=day, time="10:00"))
    assert first.status_code == 303

    resp = await _post(
        tenant,
        _valid_form(active, date=day, time="10:30", client_name="Otro Cliente"),
    )
    assert resp.status_code == 409
    assert "se superpone" in resp.text
    assert 'value="Otro Cliente"' in resp.text
    assert f'value="{active.id}"' in resp.text  # form still lists services
    bookings = await _bookings(tenant.id)
    assert len(bookings) == 1
    assert bookings[0].client_name == "María Gómez"
    # El turno rechazado no encoló nada: solo la confirmación del primero.
    async with TestingSessionLocal() as session:
        outbox = (
            await session.execute(
                select(func.count())
                .select_from(NotificationOutbox)
                .join(Booking, Booking.id == NotificationOutbox.booking_id)
                .where(Booking.tenant_id == tenant.id)
            )
        ).scalar_one()
    assert outbox == 1


@pytest.mark.asyncio
async def test_overlap_with_pending_booking_returns_409():
    tenant, active, _, _ = await _setup()
    start = datetime.fromisoformat(f"{_future_day()}T10:00").replace(tzinfo=TZ)
    async with TestingSessionLocal() as session:
        session.add(
            Booking(
                tenant_id=tenant.id,
                service_id=active.id,
                client_name="Pendiente Seña",
                client_phone="5491166666666",
                start_time=start,
                end_time=start + timedelta(minutes=45),
                price_at_booking=active.price,
                deposit_at_booking=active.deposit_amount,
                idempotency_key=f"test-{uuid.uuid4().hex}",
                status="pending",
            )
        )
        await session.commit()

    resp = await _post(
        tenant, _valid_form(active, date=start.date().isoformat(), time="10:15")
    )
    assert resp.status_code == 409
    assert [b.status for b in await _bookings(tenant.id)] == ["pending"]


@pytest.mark.asyncio
async def test_seconds_in_time_are_dropped():
    # Algunos navegadores mandan HH:MM:SS; el turno igual empieza en el minuto.
    tenant, active, _, _ = await _setup()
    resp = await _post(tenant, _valid_form(active, time="10:00:30.5"))
    assert resp.status_code == 303
    [booking] = await _bookings(tenant.id)
    assert booking.start_time.second == 0
    assert booking.start_time.microsecond == 0


@pytest.mark.asyncio
async def test_agenda_shows_new_booking_link():
    tenant, _, _, _ = await _setup()
    day = _future_day()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(
            "juturno_session",
            create_session_token(tenant.id, tenant.session_version),
        )
        resp = await client.get(f"/panel/agenda?day={day}")
    assert resp.status_code == 200
    assert f"/panel/agenda/new?day={day}" in resp.text


async def _add_manual_bookings(tenant, service, count, created_at, actor="owner"):
    """Turnos manuales (idempotency `panel-*`) en horarios lejanos y distintos."""
    base = datetime.now(timezone.utc) + timedelta(days=30)
    async with TestingSessionLocal() as session:
        for i in range(count):
            start = base + timedelta(hours=i)
            session.add(
                Booking(
                    tenant_id=tenant.id,
                    service_id=service.id,
                    client_name="Cliente Manual",
                    client_phone="5491166666666",
                    start_time=start,
                    end_time=start + timedelta(minutes=45),
                    price_at_booking=service.price,
                    deposit_at_booking=Decimal(0),
                    idempotency_key=f"panel-{uuid.uuid4().hex}",
                    status="confirmed",
                    status_changed_by=actor,
                    created_at=created_at,
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_daily_cap_on_manual_bookings():
    # Cada turno manual manda un WhatsApp desde el número de la plataforma:
    # tope por tenant para que un script no mande spam a cualquier número.
    tenant, active, _, _ = await _setup()
    other_tenant, other_service, _, _ = await _setup()
    now = datetime.now(timezone.utc)
    await _add_manual_bookings(tenant, active, 50, now - timedelta(hours=1))
    await _add_manual_bookings(other_tenant, other_service, 5, now)

    resp = await _post(tenant, _valid_form(active))
    assert resp.status_code == 400
    assert "límite de 50 turnos manuales" in resp.text
    assert len(await _bookings(tenant.id)) == 50

    # Los otros tenants no se ven afectados.
    resp = await _post(other_tenant, _valid_form(other_service))
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_daily_cap_counts_only_last_24_hours():
    tenant, active, _, _ = await _setup()
    await _add_manual_bookings(
        tenant, active, 50, datetime.now(timezone.utc) - timedelta(hours=25)
    )
    resp = await _post(tenant, _valid_form(active))
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_daily_cap_ignores_public_bookings_with_panel_like_key():
    # La reserva pública elige su idempotency_key: una clave `panel-*` de un
    # cliente no debe gastar el cupo diario del negocio.
    tenant, active, _, _ = await _setup()
    await _add_manual_bookings(
        tenant, active, 50, datetime.now(timezone.utc), actor="client"
    )
    resp = await _post(tenant, _valid_form(active))
    assert resp.status_code == 303
