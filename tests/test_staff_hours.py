"""
Horarios por profesional en el panel (Fase 5, ítem 30). /panel/horarios con
?staff=ID maneja las filas de BusinessHours de ese profesional: reemplazan al
horario del negocio para él (resolve_day_windows) y pueden ser partidos.
"""

from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import BusinessHours, Service, Staff
from tests.test_panel_mp_connect import _create_tenant, _get_csrf, _make_cookie


async def _staff(db_session, tenant, name="Ana") -> Staff:
    staff = Staff(tenant_id=tenant.id, name=name)
    db_session.add(staff)
    await db_session.commit()
    await db_session.refresh(staff)
    return staff


async def _hours(db_session, tenant, day, start, end, staff=None) -> BusinessHours:
    bh = BusinessHours(
        tenant_id=tenant.id,
        staff_id=staff.id if staff else None,
        day_of_week=day,
        start_time=start,
        end_time=end,
    )
    db_session.add(bh)
    await db_session.commit()
    await db_session.refresh(bh)
    return bh


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


async def _rows(db_session, tenant, staff=None):
    owner = (
        BusinessHours.staff_id.is_(None)
        if staff is None
        else BusinessHours.staff_id == staff.id
    )
    result = await db_session.execute(
        select(BusinessHours)
        .where(BusinessHours.tenant_id == tenant.id, owner)
        .order_by(BusinessHours.day_of_week, BusinessHours.start_time)
        .execution_options(populate_existing=True)
    )
    return [(r.day_of_week, r.start_time, r.end_time) for r in result.scalars()]


# ---------------------------------------------------------------------------
# Lista y selector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shows_only_the_selected_owner(client, db_session):
    tenant = await _create_tenant(db_session, "sh-list")
    ana = await _staff(db_session, tenant)
    await _hours(db_session, tenant, 0, time(9), time(18))
    await _hours(db_session, tenant, 0, time(14), time(16), staff=ana)

    page = await _get(client, tenant, f"/panel/horarios?staff={ana.id}")
    assert page.status_code == 200
    assert "14:00 – 16:00" in page.text
    assert "09:00 – 18:00" not in page.text
    assert "Horarios de Ana" in page.text
    assert f'href="/panel/horarios/new?staff={ana.id}"' in page.text

    page = await _get(client, tenant, "/panel/horarios")
    assert "09:00 – 18:00" in page.text
    assert "14:00 – 16:00" not in page.text
    # Selector: el negocio y cada profesional.
    assert 'href="/panel/horarios"' in page.text
    assert f'href="/panel/horarios?staff={ana.id}"' in page.text


@pytest.mark.asyncio
async def test_staff_without_hours_explains_business_fallback(client, db_session):
    tenant = await _create_tenant(db_session, "sh-empty")
    ana = await _staff(db_session, tenant)
    page = await _get(client, tenant, f"/panel/horarios?staff={ana.id}")
    assert page.status_code == 200
    assert "usa los horarios del negocio" in page.text


@pytest.mark.asyncio
async def test_staff_list_links_to_their_hours(client, db_session):
    tenant = await _create_tenant(db_session, "sh-staff-link")
    ana = await _staff(db_session, tenant)
    page = await _get(client, tenant, "/panel/staff")
    assert f'href="/panel/horarios?staff={ana.id}"' in page.text


# ---------------------------------------------------------------------------
# Alta, edición y baja
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_can_have_split_shifts(client, db_session):
    tenant = await _create_tenant(db_session, "sh-split")
    ana = await _staff(db_session, tenant)
    url = f"/panel/horarios/new?staff={ana.id}"
    form = await _get(client, tenant, url)
    assert form.status_code == 200
    assert f'action="/panel/horarios/new?staff={ana.id}"' in form.text

    for start, end in (("09:00", "13:00"), ("16:00", "20:00")):
        res = await _post(
            client,
            tenant,
            url,
            {"day_of_week": "1", "start_time": start, "end_time": end},
        )
        assert res.status_code == 303, res.text
        assert res.headers["location"] == f"/panel/horarios?staff={ana.id}"

    assert await _rows(db_session, tenant, ana) == [
        (1, time(9), time(13)),
        (1, time(16), time(20)),
    ]
    assert await _rows(db_session, tenant) == []


@pytest.mark.asyncio
async def test_overlap_is_checked_per_owner(client, db_session):
    tenant = await _create_tenant(db_session, "sh-overlap")
    ana = await _staff(db_session, tenant)
    await _hours(db_session, tenant, 1, time(9), time(18))
    url = f"/panel/horarios/new?staff={ana.id}"

    # El horario del negocio no choca con el de Ana.
    res = await _post(
        client,
        tenant,
        url,
        {"day_of_week": "1", "start_time": "10:00", "end_time": "12:00"},
    )
    assert res.status_code == 303, res.text
    # Pero dos franjas de Ana sí.
    res = await _post(
        client,
        tenant,
        url,
        {"day_of_week": "1", "start_time": "11:00", "end_time": "13:00"},
    )
    assert res.status_code == 200
    assert "Se solapa con un horario existente" in res.text
    assert await _rows(db_session, tenant, ana) == [(1, time(10), time(12))]


@pytest.mark.asyncio
async def test_edit_staff_hours_keeps_owner_and_checks_its_overlap(client, db_session):
    tenant = await _create_tenant(db_session, "sh-edit")
    ana = await _staff(db_session, tenant)
    bh = await _hours(db_session, tenant, 2, time(9), time(12), staff=ana)
    await _hours(db_session, tenant, 2, time(14), time(18), staff=ana)
    await _hours(db_session, tenant, 2, time(9), time(18))  # negocio

    form = await _get(client, tenant, f"/panel/horarios/{bh.id}/edit")
    assert form.status_code == 200
    assert f'href="/panel/horarios?staff={ana.id}"' in form.text

    url = f"/panel/horarios/{bh.id}/edit"
    res = await _post(
        client,
        tenant,
        url,
        {"day_of_week": "2", "start_time": "11:00", "end_time": "15:00"},
    )
    assert res.status_code == 200  # pisa 14-18 de Ana
    assert "se solapa con uno existente (14:00–18:00)" in res.text

    res = await _post(
        client,
        tenant,
        url,
        {"day_of_week": "2", "start_time": "08:00", "end_time": "13:00"},
    )
    assert res.status_code == 303, res.text
    assert res.headers["location"] == f"/panel/horarios?staff={ana.id}"
    assert await _rows(db_session, tenant, ana) == [
        (2, time(8), time(13)),
        (2, time(14), time(18)),
    ]


@pytest.mark.asyncio
async def test_delete_staff_hours_returns_to_their_list(client, db_session):
    tenant = await _create_tenant(db_session, "sh-delete")
    ana = await _staff(db_session, tenant)
    bh = await _hours(db_session, tenant, 3, time(9), time(12), staff=ana)
    res = await _post(client, tenant, f"/panel/horarios/{bh.id}/delete", {})
    assert res.status_code == 303, res.text
    assert res.headers["location"] == f"/panel/horarios?staff={ana.id}"
    assert await _rows(db_session, tenant, ana) == []


# ---------------------------------------------------------------------------
# Aislamiento entre negocios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_of_another_tenant_is_not_found(client, db_session):
    tenant = await _create_tenant(db_session, "sh-iso")
    other = await _create_tenant(db_session, "sh-iso-other")
    theirs = await _staff(db_session, other, "Ajeno")
    their_bh = await _hours(db_session, other, 0, time(9), time(12), staff=theirs)

    assert (
        await _get(client, tenant, f"/panel/horarios?staff={theirs.id}")
    ).status_code == 404
    assert (
        await _get(client, tenant, f"/panel/horarios/new?staff={theirs.id}")
    ).status_code == 404
    res = await _post(
        client,
        tenant,
        f"/panel/horarios/new?staff={theirs.id}",
        {"day_of_week": "0", "start_time": "13:00", "end_time": "14:00"},
    )
    assert res.status_code == 404
    assert (
        await _get(client, tenant, f"/panel/horarios/{their_bh.id}/edit")
    ).status_code == 404
    res = await _post(
        client,
        tenant,
        f"/panel/horarios/{their_bh.id}/edit",
        {"day_of_week": "0", "start_time": "07:00", "end_time": "08:00"},
    )
    assert res.status_code == 404
    res = await _post(client, tenant, f"/panel/horarios/{their_bh.id}/delete", {})
    assert res.status_code == 404
    assert await _rows(db_session, other, theirs) == [(0, time(9), time(12))]


# ---------------------------------------------------------------------------
# Turnos disponibles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_slots_follow_staff_hours(client, db_session):
    tenant = await _create_tenant(db_session, "sh-slots")
    ana = await _staff(db_session, tenant)
    service = Service(
        tenant_id=tenant.id,
        name="Corte",
        duration_minutes=60,
        price=Decimal(1000),
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)

    monday = date.today() + timedelta(days=7 - date.today().weekday())
    await _hours(db_session, tenant, 0, time(9), time(12))
    await _hours(db_session, tenant, 0, time(15), time(17), staff=ana)

    params = {"tenant_id": tenant.id, "service_id": service.id, "day": monday}
    business = await client.get("/public/available-slots", params=params)
    assert business.json()["slots"] == ["09:00", "09:30", "10:00", "10:30", "11:00"]
    staff = await client.get(
        "/public/available-slots", params={**params, "staff_id": ana.id}
    )
    assert staff.json()["slots"] == ["15:00", "15:30", "16:00"]
    # Ana tiene horario propio solo el lunes: el martes está cerrada para ella.
    tuesday = await client.get(
        "/public/available-slots",
        params={**params, "day": monday + timedelta(days=1), "staff_id": ana.id},
    )
    assert tuesday.json()["slots"] == []
