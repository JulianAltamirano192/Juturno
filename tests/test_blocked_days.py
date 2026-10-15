"""
Días bloqueados (Fase 5, ítem 31): feriados o vacaciones por rango de fechas,
del negocio (staff_id NULL, bloquea a todos) o de un profesional. Un día
bloqueado no tiene turnos (resolve_day_windows devuelve []), así que tampoco
se puede reservar (422). No se puede bloquear un rango con turnos pendientes o
confirmados: el negocio los cancela o mueve primero.
"""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import BlockedPeriod, Booking, Service, Staff
from tests.test_booking_start_validation import ENDPOINTS, _setup
from tests.test_booking_start_validation import _post as _post_booking
from tests.test_panel_mp_connect import _create_tenant, _get_csrf, _make_cookie

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAY = date.today() + timedelta(days=10)


async def _staff(db_session, tenant, name="Ana") -> Staff:
    staff = Staff(tenant_id=tenant.id, name=name)
    db_session.add(staff)
    await db_session.commit()
    await db_session.refresh(staff)
    return staff


async def _service(db_session, tenant) -> Service:
    service = Service(
        tenant_id=tenant.id, name="Corte", duration_minutes=60, price=Decimal(1000)
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    return service


async def _block(db_session, tenant, start, end, staff=None) -> BlockedPeriod:
    block = BlockedPeriod(
        tenant_id=tenant.id,
        staff_id=staff.id if staff else None,
        start_date=start,
        end_date=end,
    )
    db_session.add(block)
    await db_session.commit()
    await db_session.refresh(block)
    return block


async def _booking(db_session, tenant, service, day, status, staff=None) -> Booking:
    start = datetime.combine(day, time(10), tzinfo=TZ)
    booking = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        staff_id=staff.id if staff else None,
        client_name="Juan Pérez",
        client_phone="5491155555555",
        start_time=start,
        end_time=start + timedelta(hours=1),
        price_at_booking=Decimal(1000),
        idempotency_key=f"bd-{uuid.uuid4().hex}",
        status=status,
    )
    db_session.add(booking)
    await db_session.commit()
    return booking


async def _post(client, tenant, url, data):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    client.cookies.clear()
    return await client.post(
        url, data={**data, "csrf_token": csrf}, cookies=cookies, follow_redirects=False
    )


async def _get(client, tenant, url):
    client.cookies.clear()
    return await client.get(url, cookies={"juturno_session": _make_cookie(tenant)})


async def _rows(db_session, tenant):
    result = await db_session.execute(
        select(BlockedPeriod)
        .where(BlockedPeriod.tenant_id == tenant.id)
        .order_by(BlockedPeriod.start_date)
        .execution_options(populate_existing=True)
    )
    return [(r.staff_id, r.start_date, r.end_date, r.reason) for r in result.scalars()]


def _range(start: date, end: date, reason: str = "") -> dict:
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Turnos disponibles y reservas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_business_block_removes_slots_for_everyone(client, db_session):
    tenant = await _create_tenant(db_session, "bd-slots")
    ana = await _staff(db_session, tenant)
    service = await _service(db_session, tenant)
    await _block(db_session, tenant, DAY, DAY + timedelta(days=1))

    params = {"tenant_id": tenant.id, "service_id": service.id}
    for day in (DAY, DAY + timedelta(days=1)):
        res = await client.get("/public/available-slots", params={**params, "day": day})
        assert res.json()["slots"] == []
        res = await client.get(
            "/public/available-slots",
            params={**params, "day": day, "staff_id": ana.id},
        )
        assert res.json()["slots"] == []
    after = await client.get(
        "/public/available-slots", params={**params, "day": DAY + timedelta(days=2)}
    )
    assert after.json()["slots"][0] == "09:00"


@pytest.mark.asyncio
async def test_staff_block_only_removes_that_staff_slots(client, db_session):
    tenant = await _create_tenant(db_session, "bd-staff-slots")
    ana = await _staff(db_session, tenant)
    beto = await _staff(db_session, tenant, "Beto")
    service = await _service(db_session, tenant)
    await _block(db_session, tenant, DAY, DAY, staff=ana)

    params = {"tenant_id": tenant.id, "service_id": service.id, "day": DAY}
    ana_res = await client.get(
        "/public/available-slots", params={**params, "staff_id": ana.id}
    )
    assert ana_res.json()["slots"] == []
    beto_res = await client.get(
        "/public/available-slots", params={**params, "staff_id": beto.id}
    )
    assert beto_res.json()["slots"][0] == "09:00"
    business = await client.get("/public/available-slots", params=params)
    assert business.json()["slots"][0] == "09:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_booking_on_blocked_day_is_rejected(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session)
    await _block(db_session, tenant, DAY, DAY)
    start = datetime.combine(DAY, time(10), tzinfo=ZoneInfo("UTC"))
    res = await _post_booking(client, endpoint, raw_key, tenant, service, start)
    assert res.status_code == 422, res.text

    next_day = start + timedelta(days=1)
    res = await _post_booking(client, endpoint, raw_key, tenant, service, next_day)
    assert res.status_code == 201, res.text


# ---------------------------------------------------------------------------
# Panel: crear, listar y borrar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_business_block_and_list_it(client, db_session):
    tenant = await _create_tenant(db_session, "bd-create")
    res = await _post(
        client,
        tenant,
        "/panel/horarios/bloqueos/new",
        _range(DAY, DAY + timedelta(days=2), "Vacaciones"),
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/panel/horarios"
    assert await _rows(db_session, tenant) == [
        (None, DAY, DAY + timedelta(days=2), "Vacaciones")
    ]

    page = await _get(client, tenant, "/panel/horarios")
    assert "Días bloqueados" in page.text
    assert "Vacaciones" in page.text
    assert DAY.strftime("%d/%m/%Y") in page.text


@pytest.mark.asyncio
async def test_create_staff_block_is_listed_only_for_that_staff(client, db_session):
    tenant = await _create_tenant(db_session, "bd-staff-create")
    ana = await _staff(db_session, tenant)
    res = await _post(
        client,
        tenant,
        f"/panel/horarios/bloqueos/new?staff={ana.id}",
        _range(DAY, DAY, "Licencia de Ana"),
    )
    assert res.status_code == 303
    assert res.headers["location"] == f"/panel/horarios?staff={ana.id}"
    assert await _rows(db_session, tenant) == [(ana.id, DAY, DAY, "Licencia de Ana")]

    assert (
        "Licencia de Ana"
        in (await _get(client, tenant, f"/panel/horarios?staff={ana.id}")).text
    )
    assert "Licencia de Ana" not in (await _get(client, tenant, "/panel/horarios")).text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            _range(DAY, DAY - timedelta(days=1)),
            "La fecha de fin no puede ser anterior a la de inicio.",
        ),
        (
            _range(date.today() - timedelta(days=1), DAY),
            "No se pueden bloquear fechas pasadas.",
        ),
        (
            _range(DAY, date.today() + timedelta(days=366)),
            "Solo se pueden bloquear fechas del próximo año.",
        ),
        ({"start_date": "no-es-fecha", "end_date": ""}, "Elegí una fecha válida."),
        (_range(DAY, DAY, "x" * 81), "El motivo puede tener hasta 80 caracteres."),
    ],
)
async def test_invalid_block_is_rejected(client, db_session, data, message):
    tenant = await _create_tenant(db_session, f"bd-invalid-{uuid.uuid4().hex[:8]}")
    res = await _post(client, tenant, "/panel/horarios/bloqueos/new", data)
    assert res.status_code == 200
    assert message in res.text
    assert await _rows(db_session, tenant) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "confirmed"])
async def test_block_with_active_bookings_is_rejected(client, db_session, status):
    tenant = await _create_tenant(db_session, f"bd-busy-{status}")
    service = await _service(db_session, tenant)
    await _booking(db_session, tenant, service, DAY + timedelta(days=1), status)

    res = await _post(
        client,
        tenant,
        "/panel/horarios/bloqueos/new",
        _range(DAY, DAY + timedelta(days=2)),
    )
    assert res.status_code == 200
    assert "Hay turnos en esas fechas" in res.text
    assert "Juan Pérez" in res.text
    assert await _rows(db_session, tenant) == []


@pytest.mark.asyncio
async def test_block_ignores_inactive_and_out_of_range_bookings(client, db_session):
    tenant = await _create_tenant(db_session, "bd-free")
    service = await _service(db_session, tenant)
    await _booking(db_session, tenant, service, DAY, "cancelled")
    await _booking(db_session, tenant, service, DAY + timedelta(days=1), "confirmed")

    res = await _post(client, tenant, "/panel/horarios/bloqueos/new", _range(DAY, DAY))
    assert res.status_code == 303
    assert await _rows(db_session, tenant) == [(None, DAY, DAY, None)]


@pytest.mark.asyncio
async def test_staff_block_only_checks_that_staff_bookings(client, db_session):
    tenant = await _create_tenant(db_session, "bd-staff-busy")
    ana = await _staff(db_session, tenant)
    beto = await _staff(db_session, tenant, "Beto")
    service = await _service(db_session, tenant)
    await _booking(db_session, tenant, service, DAY, "confirmed", staff=beto)

    ok = await _post(
        client, tenant, f"/panel/horarios/bloqueos/new?staff={ana.id}", _range(DAY, DAY)
    )
    assert ok.status_code == 303
    busy = await _post(
        client,
        tenant,
        f"/panel/horarios/bloqueos/new?staff={beto.id}",
        _range(DAY, DAY),
    )
    assert busy.status_code == 200
    assert "Hay turnos en esas fechas" in busy.text
    assert await _rows(db_session, tenant) == [(ana.id, DAY, DAY, None)]


@pytest.mark.asyncio
async def test_delete_block_returns_to_its_owner_list(client, db_session):
    tenant = await _create_tenant(db_session, "bd-delete")
    ana = await _staff(db_session, tenant)
    block = await _block(db_session, tenant, DAY, DAY, staff=ana)

    res = await _post(client, tenant, f"/panel/horarios/bloqueos/{block.id}/delete", {})
    assert res.status_code == 303
    assert res.headers["location"] == f"/panel/horarios?staff={ana.id}"
    assert await _rows(db_session, tenant) == []


@pytest.mark.asyncio
async def test_past_blocks_are_not_listed(client, db_session):
    tenant = await _create_tenant(db_session, "bd-past")
    yesterday = date.today() - timedelta(days=2)
    await _block(db_session, tenant, yesterday, yesterday)
    await _block(db_session, tenant, DAY, DAY)

    page = await _get(client, tenant, "/panel/horarios")
    assert DAY.strftime("%d/%m/%Y") in page.text
    assert yesterday.strftime("%d/%m/%Y") not in page.text


@pytest.mark.asyncio
async def test_blocks_of_another_tenant_are_not_found(client, db_session):
    tenant = await _create_tenant(db_session, "bd-iso")
    other = await _create_tenant(db_session, "bd-iso-other")
    theirs = await _staff(db_session, other, "Ajeno")
    their_block = await _block(db_session, other, DAY, DAY)

    assert (
        await _get(client, tenant, f"/panel/horarios/bloqueos/new?staff={theirs.id}")
    ).status_code == 404
    res = await _post(
        client,
        tenant,
        f"/panel/horarios/bloqueos/new?staff={theirs.id}",
        _range(DAY, DAY),
    )
    assert res.status_code == 404
    res = await _post(
        client, tenant, f"/panel/horarios/bloqueos/{their_block.id}/delete", {}
    )
    assert res.status_code == 404
    assert await _rows(db_session, other) == [(None, DAY, DAY, None)]
    assert await _rows(db_session, tenant) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_booking_with_blocked_staff_is_rejected(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session)
    ana = await _staff(db_session, tenant)
    beto = await _staff(db_session, tenant, "Beto")
    await _block(db_session, tenant, DAY, DAY, staff=ana)
    start = datetime.combine(DAY, time(10), tzinfo=ZoneInfo("UTC"))

    res = await _post_booking(
        client, endpoint, raw_key, tenant, service, start, staff_id=ana.id
    )
    assert res.status_code == 422, res.text
    res = await _post_booking(
        client, endpoint, raw_key, tenant, service, start, staff_id=beto.id
    )
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
async def test_bookings_are_checked_by_the_tenant_local_date(client, db_session):
    tenant = await _create_tenant(db_session, "bd-tz")
    tenant.timezone = "America/Argentina/Buenos_Aires"
    db_session.add(tenant)
    await db_session.commit()
    service = await _service(db_session, tenant)
    # 23:30 en Buenos Aires = 02:30 UTC del día siguiente.
    start = datetime.combine(DAY, time(23, 30), tzinfo=TZ)
    db_session.add(
        Booking(
            tenant_id=tenant.id,
            service_id=service.id,
            client_name="Juan Pérez",
            client_phone="5491155555555",
            start_time=start,
            end_time=start + timedelta(minutes=20),
            price_at_booking=Decimal(1000),
            idempotency_key=f"bd-{uuid.uuid4().hex}",
            status="confirmed",
        )
    )
    await db_session.commit()

    busy = await _post(client, tenant, "/panel/horarios/bloqueos/new", _range(DAY, DAY))
    assert "Hay turnos en esas fechas" in busy.text
    next_day = DAY + timedelta(days=1)
    ok = await _post(
        client, tenant, "/panel/horarios/bloqueos/new", _range(next_day, next_day)
    )
    assert ok.status_code == 303
    assert await _rows(db_session, tenant) == [(None, next_day, next_day, None)]


@pytest.mark.asyncio
async def test_bookings_already_over_do_not_prevent_blocking_today(client, db_session):
    tenant = await _create_tenant(db_session, "bd-today")
    service = await _service(db_session, tenant)
    now = datetime.now(ZoneInfo("UTC"))
    db_session.add(
        Booking(
            tenant_id=tenant.id,
            service_id=service.id,
            client_name="Juan Pérez",
            client_phone="5491155555555",
            start_time=now - timedelta(hours=2),
            end_time=now - timedelta(hours=1),
            price_at_booking=Decimal(1000),
            idempotency_key=f"bd-{uuid.uuid4().hex}",
            status="confirmed",
        )
    )
    await db_session.commit()

    today = now.date()
    res = await _post(
        client, tenant, "/panel/horarios/bloqueos/new", _range(today, today)
    )
    assert res.status_code == 303
    assert await _rows(db_session, tenant) == [(None, today, today, None)]


@pytest.mark.asyncio
async def test_create_and_delete_require_csrf(client, db_session):
    tenant = await _create_tenant(db_session, "bd-csrf")
    block = await _block(db_session, tenant, DAY, DAY)
    cookies = {"juturno_session": _make_cookie(tenant)}
    client.cookies.clear()

    res = await client.post(
        "/panel/horarios/bloqueos/new",
        data=_range(DAY + timedelta(days=1), DAY + timedelta(days=1)),
        cookies=cookies,
    )
    assert res.status_code == 403
    res = await client.post(
        f"/panel/horarios/bloqueos/{block.id}/delete", data={}, cookies=cookies
    )
    assert res.status_code == 403
    assert await _rows(db_session, tenant) == [(None, DAY, DAY, None)]
