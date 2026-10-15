"""
El cliente elige el profesional (Fase 5, ítem 16). Con 2 o más profesionales
activos, la página pública suma el paso "¿Con quién?" y "Sin preferencia"
ofrece cualquier profesional libre: los horarios son la unión de los de cada
uno y al reservar se asigna el de menos turnos ese día. Un turno sin
profesional ocupa a todos (el EXCLUDE no lo compara con los asignados).
"""

import json
import re
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import BlockedPeriod, Booking, BusinessHours, Service, Staff
from tests.test_booking_start_validation import _post as _post_booking
from tests.test_integration import _create_api_key
from tests.test_panel_mp_connect import _create_tenant

UTC = ZoneInfo("UTC")
DAY = date.today() + timedelta(days=3)


async def _setup(db_session, suffix, staff_names=("Ana", "Beto")):
    tenant = await _create_tenant(db_session, f"cps-{suffix}")
    service = Service(
        tenant_id=tenant.id,
        name="Corte",
        duration_minutes=60,
        price=Decimal(1000),
        deposit_amount=Decimal(0),
    )
    staff = [Staff(tenant_id=tenant.id, name=n) for n in staff_names]
    db_session.add_all([service, *staff])
    await db_session.commit()
    for obj in (service, *staff):
        await db_session.refresh(obj)
    return tenant, service, staff


async def _booking(db_session, tenant, service, hour, staff=None, minutes=60):
    start = datetime.combine(DAY, time(hour), UTC)
    db_session.add(
        Booking(
            tenant_id=tenant.id,
            service_id=service.id,
            staff_id=staff.id if staff else None,
            client_name="Otro cliente",
            client_phone="5491155555555",
            start_time=start,
            end_time=start + timedelta(minutes=minutes),
            price_at_booking=Decimal(1000),
            idempotency_key=f"cps-{uuid.uuid4().hex}",
            status="confirmed",
        )
    )
    await db_session.commit()


async def _slots(client, tenant, service, staff=None) -> list[str]:
    params = {"tenant_id": tenant.id, "service_id": service.id, "day": DAY}
    if staff:
        params["staff_id"] = staff.id
    res = await client.get("/public/available-slots", params=params)
    assert res.status_code == 200, res.text
    return res.json()["slots"]


async def _book(client, tenant, service, hour, **extra):
    start = datetime.combine(DAY, time(hour), UTC)
    return await _post_booking(client, "public", None, tenant, service, start, **extra)


async def _staff_of(db_session, booking_id):
    booking = await db_session.get(Booking, booking_id, populate_existing=True)
    return booking.staff_id


def _page_staff(html: str) -> list[dict]:
    match = re.search(r"staff: (\[.*?\]),", html)
    assert match, "la página no publica la lista de profesionales"
    return json.loads(match.group(1))


# ---------------------------------------------------------------------------
# Página pública
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_lists_active_staff_when_two_or_more(client, db_session):
    tenant, _, (ana, beto, carla) = await _setup(
        db_session, "page", ("Ana", "Beto", "Carla")
    )
    carla.is_active = False
    db_session.add(carla)
    await db_session.commit()

    page = await client.get(f"/t/{tenant.slug}")
    assert _page_staff(page.text) == [
        {"id": ana.id, "name": "Ana"},
        {"id": beto.id, "name": "Beto"},
    ]


@pytest.mark.asyncio
async def test_page_has_no_staff_step_with_a_single_staff(client, db_session):
    tenant, _, _ = await _setup(db_session, "page-one", ("Ana",))
    page = await client.get(f"/t/{tenant.slug}")
    assert _page_staff(page.text) == []


# ---------------------------------------------------------------------------
# Turnos disponibles con "Sin preferencia"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_any_staff_slot_stays_while_someone_is_free(client, db_session):
    tenant, service, (ana, beto) = await _setup(db_session, "any")
    await _booking(db_session, tenant, service, 10, ana)
    assert "10:00" in await _slots(client, tenant, service)

    await _booking(db_session, tenant, service, 10, beto)
    assert "10:00" not in await _slots(client, tenant, service)


@pytest.mark.asyncio
async def test_any_staff_slots_are_the_union_of_staff_hours(client, db_session):
    tenant, service, (ana, beto) = await _setup(db_session, "union")
    for staff, start, end in ((ana, time(9), time(11)), (beto, time(15), time(17))):
        db_session.add(
            BusinessHours(
                tenant_id=tenant.id,
                staff_id=staff.id,
                day_of_week=DAY.weekday(),
                start_time=start,
                end_time=end,
            )
        )
    await db_session.commit()

    assert await _slots(client, tenant, service) == [
        "09:00",
        "09:30",
        "10:00",
        "15:00",
        "15:30",
        "16:00",
    ]


@pytest.mark.asyncio
async def test_unassigned_booking_takes_every_staff(client, db_session):
    tenant, service, (ana, _) = await _setup(db_session, "unassigned")
    await _booking(db_session, tenant, service, 10)

    assert "10:00" not in await _slots(client, tenant, service)
    assert "10:00" not in await _slots(client, tenant, service, ana)


# ---------------------------------------------------------------------------
# Reserva con "Sin preferencia": asignación automática
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_any_staff_booking_goes_to_the_least_busy(client, db_session):
    tenant, service, (ana, beto) = await _setup(db_session, "least")
    await _booking(db_session, tenant, service, 15, ana)

    res = await _book(client, tenant, service, 10)
    assert res.status_code == 201, res.text
    assert res.json()["staff_name"] == "Beto"
    assert await _staff_of(db_session, res.json()["booking_id"]) == beto.id


@pytest.mark.asyncio
async def test_any_staff_booking_skips_a_busy_staff(client, db_session):
    tenant, service, (ana, beto) = await _setup(db_session, "skip")
    # Ana tiene menos turnos ese día pero está ocupada a las 10.
    await _booking(db_session, tenant, service, 10, ana)
    await _booking(db_session, tenant, service, 15, beto)
    await _booking(db_session, tenant, service, 16, beto)

    res = await _book(client, tenant, service, 10)
    assert res.status_code == 201, res.text
    assert await _staff_of(db_session, res.json()["booking_id"]) == beto.id
    # Ya no queda nadie libre a las 10.
    res = await _book(client, tenant, service, 10, idempotency_key="cps-skip-2")
    assert res.status_code == 409, res.text


@pytest.mark.asyncio
async def test_any_staff_tie_goes_to_the_oldest_staff(client, db_session):
    tenant, service, (ana, _) = await _setup(db_session, "tie")
    res = await _book(client, tenant, service, 10)
    assert res.status_code == 201, res.text
    assert await _staff_of(db_session, res.json()["booking_id"]) == ana.id


@pytest.mark.asyncio
async def test_any_staff_load_counts_the_tenant_local_day(client, db_session):
    tenant, service, (ana, beto) = await _setup(db_session, "tz")
    tenant.timezone = "America/Argentina/Buenos_Aires"
    db_session.add(tenant)
    await db_session.commit()
    local = ZoneInfo("America/Argentina/Buenos_Aires")
    # 23:30 del día anterior en Buenos Aires = 02:30 UTC de DAY: no cuenta.
    for staff, start in (
        (beto, datetime.combine(DAY - timedelta(days=1), time(23, 30), local)),
        (ana, datetime.combine(DAY, time(15), local)),
    ):
        db_session.add(
            Booking(
                tenant_id=tenant.id,
                service_id=service.id,
                staff_id=staff.id,
                client_name="Otro cliente",
                client_phone="5491155555555",
                start_time=start,
                end_time=start + timedelta(minutes=20),
                price_at_booking=Decimal(1000),
                idempotency_key=f"cps-{uuid.uuid4().hex}",
                status="confirmed",
            )
        )
    await db_session.commit()

    start = datetime.combine(DAY, time(10), local)
    res = await _post_booking(client, "public", None, tenant, service, start)
    assert res.status_code == 201, res.text
    assert await _staff_of(db_session, res.json()["booking_id"]) == beto.id


@pytest.mark.asyncio
async def test_any_staff_skips_a_staff_with_a_blocked_day(client, db_session):
    tenant, service, (ana, beto) = await _setup(db_session, "blocked")
    db_session.add(
        BlockedPeriod(
            tenant_id=tenant.id, staff_id=ana.id, start_date=DAY, end_date=DAY
        )
    )
    await db_session.commit()
    await _booking(db_session, tenant, service, 15, beto)

    assert "10:00" in await _slots(client, tenant, service)
    res = await _book(client, tenant, service, 10)
    assert res.status_code == 201, res.text
    assert await _staff_of(db_session, res.json()["booking_id"]) == beto.id


@pytest.mark.asyncio
async def test_any_staff_respects_the_service_free_time(client, db_session):
    tenant, service, (ana, beto) = await _setup(db_session, "buffer")
    service.buffer_minutes = 30
    db_session.add(service)
    await db_session.commit()
    await _booking(db_session, tenant, service, 11, ana)
    await _booking(db_session, tenant, service, 15, beto)
    await _booking(db_session, tenant, service, 16, beto)

    # 10:00 + 60 min + 30 libres = 11:30: pisa a Ana, va a Beto.
    res = await _book(client, tenant, service, 10)
    assert res.status_code == 201, res.text
    assert await _staff_of(db_session, res.json()["booking_id"]) == beto.id


@pytest.mark.asyncio
async def test_any_staff_booking_on_a_closed_time_is_422(client, db_session):
    tenant, service, _ = await _setup(db_session, "closed")
    res = await _book(client, tenant, service, 20)
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_any_staff_booking_falls_back_when_exclude_rejects(client, db_session):
    tenant, service, (ana, beto) = await _setup(db_session, "race")
    await _booking(db_session, tenant, service, 10, ana)

    # Simula que Ana parecía libre al elegir candidatos y alguien la tomó
    # antes del INSERT: el EXCLUDE rechaza y se prueba con Beto.
    async def stale_candidates(**kwargs):
        return [ana, beto]

    with patch("app.routers.public.free_staff_for", stale_candidates):
        res = await _book(client, tenant, service, 10)
    assert res.status_code == 201, res.text
    assert await _staff_of(db_session, res.json()["booking_id"]) == beto.id


@pytest.mark.asyncio
async def test_any_staff_retry_is_idempotent(client, db_session):
    tenant, service, _ = await _setup(db_session, "idem")
    key = f"cps-idem-{uuid.uuid4().hex}"
    first = await _book(client, tenant, service, 10, idempotency_key=key)
    second = await _book(client, tenant, service, 10, idempotency_key=key)
    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["booking_id"] == first.json()["booking_id"]
    assert second.json()["staff_name"] == first.json()["staff_name"] == "Ana"
    rows = await db_session.execute(
        select(Booking).where(Booking.tenant_id == tenant.id)
    )
    assert len(rows.scalars().all()) == 1


# ---------------------------------------------------------------------------
# Reserva eligiendo profesional / negocio con un solo profesional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chosen_staff_keeps_that_staff(client, db_session):
    tenant, service, (ana, _) = await _setup(db_session, "chosen")
    res = await _book(client, tenant, service, 10, staff_id=ana.id)
    assert res.status_code == 201, res.text
    assert res.json()["staff_name"] == "Ana"
    assert await _staff_of(db_session, res.json()["booking_id"]) == ana.id


@pytest.mark.asyncio
async def test_chosen_staff_over_an_unassigned_booking_is_409(client, db_session):
    tenant, service, (ana, _) = await _setup(db_session, "chosen-busy")
    await _booking(db_session, tenant, service, 10)
    res = await _book(client, tenant, service, 10, staff_id=ana.id)
    assert res.status_code == 409, res.text


@pytest.mark.asyncio
async def test_single_staff_business_keeps_unassigned_bookings(client, db_session):
    tenant, service, _ = await _setup(db_session, "single", ("Ana",))
    res = await _book(client, tenant, service, 10)
    assert res.status_code == 201, res.text
    assert res.json()["staff_name"] is None
    assert await _staff_of(db_session, res.json()["booking_id"]) is None


@pytest.mark.asyncio
async def test_unassigned_booking_cannot_overlap_an_assigned_one(client, db_session):
    tenant, service, (ana,) = await _setup(db_session, "single-busy", ("Ana",))
    await _booking(db_session, tenant, service, 10, ana)
    res = await _book(client, tenant, service, 10)
    assert res.status_code == 409, res.text


@pytest.mark.asyncio
async def test_api_booking_with_staff_over_an_unassigned_one_is_409(client, db_session):
    tenant, service, (ana, _) = await _setup(db_session, "api")
    raw_key = await _create_api_key(db_session, tenant.id)
    await _booking(db_session, tenant, service, 10)

    start = datetime.combine(DAY, time(10), UTC)
    res = await _post_booking(
        client, "api", raw_key, tenant, service, start, staff_id=ana.id
    )
    assert res.status_code == 409, res.text
    res = await _post_booking(client, "api", raw_key, tenant, service, start)
    assert res.status_code == 409, res.text


@pytest.mark.asyncio
async def test_staff_of_another_tenant_is_404(client, db_session):
    tenant, service, _ = await _setup(db_session, "iso")
    _, _, (theirs, _) = await _setup(db_session, "iso-other")
    res = await _book(client, tenant, service, 10, staff_id=theirs.id)
    assert res.status_code == 404, res.text
