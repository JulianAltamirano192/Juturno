"""
Tiempo libre entre turnos por servicio (Fase 5, ítem 32), opt-in: por defecto
0. El turno guarda end_time = inicio + duración + tiempo libre, así el EXCLUDE
de booking y el chequeo de mp_webhooks lo respetan sin cambios. Los slots
nuevos tampoco pueden pisar con su tiempo libre al turno siguiente.
"""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Booking, BusinessHours, Service, Staff
from tests.conftest import TestingSessionLocal
from tests.test_booking_start_validation import ENDPOINTS, _setup
from tests.test_booking_start_validation import _post as _post_booking
from tests.test_panel_manual_booking import _post as _post_manual
from tests.test_panel_manual_booking import _setup as _setup_manual
from tests.test_panel_manual_booking import _valid_form
from tests.test_panel_mp_connect import _create_tenant, _get_csrf, _make_cookie

UTC = ZoneInfo("UTC")
DAY = date.today() + timedelta(days=3)


async def _service(db_session, tenant, buffer=0, minutes=60) -> Service:
    service = Service(
        tenant_id=tenant.id,
        name=f"Servicio {buffer}",
        duration_minutes=minutes,
        price=Decimal(1000),
        buffer_minutes=buffer,
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    return service


async def _booking(
    db_session, tenant, service, start: datetime, minutes: int, staff=None
):
    db_session.add(
        Booking(
            tenant_id=tenant.id,
            service_id=service.id,
            staff_id=staff.id if staff else None,
            client_name="Cliente",
            client_phone="5491155555555",
            start_time=start,
            end_time=start + timedelta(minutes=minutes),
            price_at_booking=Decimal(1000),
            idempotency_key=f"buf-{uuid.uuid4().hex}",
            status="confirmed",
        )
    )
    await db_session.commit()


async def _slots(client, tenant, service, staff=None) -> list[str]:
    params = {"tenant_id": tenant.id, "service_id": service.id, "day": DAY}
    if staff:
        params["staff_id"] = staff.id
    res = await client.get("/public/available-slots", params=params)
    return res.json()["slots"]


async def _post(client, tenant, url, data):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    client.cookies.clear()
    return await client.post(
        url, data={**data, "csrf_token": csrf}, cookies=cookies, follow_redirects=False
    )


def _service_form(**overrides) -> dict:
    return {
        "name": "Corte",
        "duration_minutes": "60",
        "price": "1000.00",
        "deposit_amount": "",
        **overrides,
    }


# ---------------------------------------------------------------------------
# Reservas: end_time incluye el tiempo libre
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_booking_end_includes_buffer(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session)
    service.buffer_minutes = 15
    db_session.add(service)
    await db_session.commit()

    start = datetime.combine(DAY, time(10), tzinfo=UTC)
    res = await _post_booking(client, endpoint, raw_key, tenant, service, start)
    assert res.status_code == 201, res.text
    booking = (
        await db_session.execute(
            select(Booking)
            .where(Booking.tenant_id == tenant.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert booking.end_time - booking.start_time == timedelta(minutes=75)

    # El tiempo libre ocupa el horario: un turno a las 11:00 se pisa (409).
    res = await _post_booking(
        client, endpoint, raw_key, tenant, service, start + timedelta(hours=1)
    )
    assert res.status_code == 409, res.text


@pytest.mark.asyncio
async def test_manual_booking_end_includes_buffer():
    tenant, active, _, _ = await _setup_manual()
    async with TestingSessionLocal() as session:
        service = await session.get(Service, active.id)
        service.buffer_minutes = 30
        await session.commit()

    res = await _post_manual(tenant, _valid_form(active))
    assert res.status_code == 303
    async with TestingSessionLocal() as session:
        booking = (
            await session.execute(select(Booking).where(Booking.tenant_id == tenant.id))
        ).scalar_one()
    assert booking.end_time - booking.start_time == timedelta(minutes=75)


# ---------------------------------------------------------------------------
# Turnos disponibles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_buffer_slots_are_unchanged(client, db_session):
    tenant = await _create_tenant(db_session, "buf-none")
    service = await _service(db_session, tenant)
    await _booking(
        db_session, tenant, service, datetime.combine(DAY, time(11), UTC), 60
    )

    slots = await _slots(client, tenant, service)
    assert slots == [
        "09:00", "09:30", "10:00", "12:00", "12:30", "13:00", "13:30",
        "14:00", "14:30", "15:00", "15:30", "16:00", "16:30", "17:00",
    ]  # fmt: skip


@pytest.mark.asyncio
async def test_buffer_only_looks_at_the_same_staff(client, db_session):
    tenant = await _create_tenant(db_session, "buf-staff")
    ana = Staff(tenant_id=tenant.id, name="Ana")
    beto = Staff(tenant_id=tenant.id, name="Beto")
    db_session.add_all([ana, beto])
    await db_session.commit()
    service = await _service(db_session, tenant, buffer=30)
    await _booking(
        db_session, tenant, service, datetime.combine(DAY, time(11), UTC), 90, ana
    )

    assert "10:00" not in await _slots(client, tenant, service, ana)
    assert "10:00" in await _slots(client, tenant, service, beto)


@pytest.mark.asyncio
async def test_buffer_with_split_schedule(client, db_session):
    tenant = await _create_tenant(db_session, "buf-split")
    for start, end in ((time(9), time(12)), (time(14), time(18))):
        db_session.add(
            BusinessHours(
                tenant_id=tenant.id,
                day_of_week=DAY.weekday(),
                start_time=start,
                end_time=end,
            )
        )
    await db_session.commit()
    service = await _service(db_session, tenant, buffer=30)
    # Turno de 14:00 a 15:30 (60 + 30 libres).
    await _booking(
        db_session, tenant, service, datetime.combine(DAY, time(14), UTC), 90
    )

    # La mañana termina en 11:00 (cierre 12:00): el tiempo libre no tiene que
    # entrar antes del cierre ni lo afecta el turno de la tarde.
    slots = await _slots(client, tenant, service)
    assert slots == [
        "09:00", "09:30", "10:00", "10:30", "11:00",
        "15:30", "16:00", "16:30", "17:00",
    ]  # fmt: skip


@pytest.mark.asyncio
async def test_buffer_of_previous_booking_pushes_next_slot(client, db_session):
    tenant = await _create_tenant(db_session, "buf-after")
    service = await _service(db_session, tenant, buffer=15)
    # Turno de 09:00 con 15 min libres: ocupa hasta 10:15.
    await _booking(db_session, tenant, service, datetime.combine(DAY, time(9), UTC), 75)

    slots = await _slots(client, tenant, service)
    assert slots[0] == "10:30"


@pytest.mark.asyncio
async def test_slot_buffer_cannot_overlap_next_booking(client, db_session):
    tenant = await _create_tenant(db_session, "buf-before")
    plain = await _service(db_session, tenant)
    buffered = await _service(db_session, tenant, buffer=30)
    await _booking(db_session, tenant, plain, datetime.combine(DAY, time(11), UTC), 60)

    plain_slots = await _slots(client, tenant, plain)
    assert "10:00" in plain_slots
    # 10:00 + 60 min + 30 libres = 11:30, se pisa con el turno de las 11:00.
    buffered_slots = await _slots(client, tenant, buffered)
    assert "10:00" not in buffered_slots
    assert "09:30" in buffered_slots


@pytest.mark.asyncio
async def test_last_slot_does_not_need_buffer_before_closing(client, db_session):
    tenant = await _create_tenant(db_session, "buf-close")
    service = await _service(db_session, tenant, buffer=30)
    # Fallback 09-18: el último turno de 60 min empieza 17:00 aunque su
    # tiempo libre termine después del cierre.
    assert (await _slots(client, tenant, service))[-1] == "17:00"


# ---------------------------------------------------------------------------
# Panel: formulario de servicio
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_service_has_no_buffer_by_default(client, db_session):
    tenant = await _create_tenant(db_session, "buf-default")
    res = await _post(client, tenant, "/panel/services/new", _service_form())
    assert res.status_code == 303
    service = (
        await db_session.execute(select(Service).where(Service.tenant_id == tenant.id))
    ).scalar_one()
    assert service.buffer_minutes == 0


@pytest.mark.asyncio
async def test_service_buffer_is_saved_and_shown_on_edit(client, db_session):
    tenant = await _create_tenant(db_session, "buf-save")
    res = await _post(
        client, tenant, "/panel/services/new", _service_form(buffer_minutes="15")
    )
    assert res.status_code == 303
    service = (
        await db_session.execute(select(Service).where(Service.tenant_id == tenant.id))
    ).scalar_one()
    assert service.buffer_minutes == 15

    res = await _post(
        client,
        tenant,
        f"/panel/services/{service.id}/edit",
        _service_form(buffer_minutes="45"),
    )
    assert res.status_code == 303
    await db_session.refresh(service)
    assert service.buffer_minutes == 45

    client.cookies.clear()
    page = await client.get(
        f"/panel/services/{service.id}/edit",
        cookies={"juturno_session": _make_cookie(tenant)},
    )
    assert '<option value="45" selected>' in page.text
    listing = await client.get(
        "/panel/services", cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "+45 min libres" in listing.text


@pytest.mark.asyncio
async def test_edit_without_buffer_field_keeps_it(client, db_session):
    tenant = await _create_tenant(db_session, "buf-keep")
    service = await _service(db_session, tenant, buffer=20)
    res = await _post(
        client, tenant, f"/panel/services/{service.id}/edit", _service_form()
    )
    assert res.status_code == 303
    await db_session.refresh(service)
    assert service.buffer_minutes == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["7", "-15", "abc", "999"])
async def test_invalid_buffer_is_rejected(client, db_session, value):
    tenant = await _create_tenant(db_session, f"buf-bad-{uuid.uuid4().hex[:6]}")
    res = await _post(
        client, tenant, "/panel/services/new", _service_form(buffer_minutes=value)
    )
    assert res.status_code == 200
    assert "Elegí un tiempo libre de la lista." in res.text
    rows = await db_session.execute(
        select(Service).where(Service.tenant_id == tenant.id)
    )
    assert rows.scalars().all() == []
