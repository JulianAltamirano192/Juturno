"""Tests de integración para el CRUD de horarios de atención del panel."""

from datetime import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.csrf import generate_csrf_token
from app.main import app
from app.models import BusinessHours, Staff, Tenant
from app.session import create_session_token
from tests.conftest import TestingSessionLocal


def make_session_cookie(tenant):
    return create_session_token(tenant.id, tenant.session_version)


async def _tenant_with_business_hours(
    session, day_of_week=0, start="09:00", end="18:00"
):
    tenant = Tenant(
        name=f"Biz BH {int(__import__('time').time() * 1000)}",
        slug=f"biz-bh-{int(__import__('time').time() * 1000)}",
        owner_email="bh@test.com",
        password_hash="x",
        session_version=1,
    )
    session.add(tenant)
    await session.flush()
    bh = BusinessHours(
        tenant_id=tenant.id,
        staff_id=None,
        day_of_week=day_of_week,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end),
    )
    session.add(bh)
    await session.commit()
    await session.refresh(tenant)
    await session.refresh(bh)
    return tenant, bh


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bh_list_requires_session():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/panel/horarios", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_bh_list_with_valid_session():
    async with TestingSessionLocal() as session:
        tenant, _bh = await _tenant_with_business_hours(session)

    cookie = make_session_cookie(tenant)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        resp = await client.get("/panel/horarios")
    assert resp.status_code == 200
    assert "Horarios de atención" in resp.text
    assert "Lunes" in resp.text
    assert "09:00" in resp.text
    assert "18:00" in resp.text


@pytest.mark.asyncio
async def test_create_bh_valid():
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="Biz BH Create",
            slug=f"bh-create-{int(__import__('time').time() * 1000)}",
            owner_email="create@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            "/panel/horarios/new",
            data={
                "day_of_week": "0",
                "start_time": "09:00",
                "end_time": "18:00",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/panel/horarios"

    # Verificar en DB
    async with TestingSessionLocal() as session:
        from sqlalchemy import select

        stmt = select(BusinessHours).where(BusinessHours.tenant_id == tenant.id)
        bh = (await session.execute(stmt)).scalar_one_or_none()
        assert bh is not None
        assert bh.day_of_week == 0
        assert str(bh.start_time) == "09:00:00"
        assert str(bh.end_time) == "18:00:00"
        assert bh.staff_id is None


@pytest.mark.asyncio
async def test_create_bh_invalid_times_shows_error():
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="Biz BH Error",
            slug=f"bh-error-{int(__import__('time').time() * 1000)}",
            owner_email="error@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        # start_time >= end_time
        resp = await client.post(
            "/panel/horarios/new",
            data={
                "day_of_week": "1",
                "start_time": "18:00",
                "end_time": "09:00",
                "csrf_token": csrf,
            },
        )
    assert resp.status_code == 200
    assert "apertura debe ser anterior" in resp.text


@pytest.mark.asyncio
async def test_create_bh_missing_fields_shows_error():
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="Biz BH Missing",
            slug=f"bh-missing-{int(__import__('time').time() * 1000)}",
            owner_email="missing@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            "/panel/horarios/new",
            data={
                "day_of_week": "",
                "start_time": "",
                "end_time": "",
                "csrf_token": csrf,
            },
        )
    assert resp.status_code == 200
    assert "obligatoria" in resp.text


@pytest.mark.asyncio
async def test_create_bh_overlap_same_day_rejected():
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="Biz BH Overlap",
            slug=f"bh-overlap-{int(__import__('time').time() * 1000)}",
            owner_email="overlap@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant)
        await session.flush()
        # Horario existente: 09:00-12:00 lunes
        existing = BusinessHours(
            tenant_id=tenant.id,
            staff_id=None,
            day_of_week=0,
            start_time=time(9, 0),
            end_time=time(12, 0),
        )
        session.add(existing)
        await session.commit()
        await session.refresh(tenant)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        # Intento crear 11:00-14:00 lunes (se solapa con 09:00-12:00)
        resp = await client.post(
            "/panel/horarios/new",
            data={
                "day_of_week": "0",
                "start_time": "11:00",
                "end_time": "14:00",
                "csrf_token": csrf,
            },
        )
    assert resp.status_code == 200
    assert "se solapa" in resp.text.lower()


@pytest.mark.asyncio
async def test_create_bh_no_overlap_allowed():
    """Horarios que solo tocan en el borde (fin=inicio) NO se solapan."""
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="Biz BH Touch",
            slug=f"bh-touch-{int(__import__('time').time() * 1000)}",
            owner_email="touch@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant)
        await session.flush()
        existing = BusinessHours(
            tenant_id=tenant.id,
            staff_id=None,
            day_of_week=0,
            start_time=time(9, 0),
            end_time=time(12, 0),
        )
        session.add(existing)
        await session.commit()
        await session.refresh(tenant)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        # 12:00-14:00 toca exactamente el final del existente -> permitido
        resp = await client.post(
            "/panel/horarios/new",
            data={
                "day_of_week": "0",
                "start_time": "12:00",
                "end_time": "14:00",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/panel/horarios"


@pytest.mark.asyncio
async def test_edit_bh():
    async with TestingSessionLocal() as session:
        tenant, bh = await _tenant_with_business_hours(session)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/horarios/{bh.id}/edit",
            data={
                "day_of_week": "2",
                "start_time": "10:00",
                "end_time": "16:00",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/panel/horarios"

    async with TestingSessionLocal() as session:
        updated = await session.get(BusinessHours, bh.id)
    assert updated.day_of_week == 2
    assert str(updated.start_time) == "10:00:00"
    assert str(updated.end_time) == "16:00:00"


@pytest.mark.asyncio
async def test_delete_bh():
    async with TestingSessionLocal() as session:
        tenant, bh = await _tenant_with_business_hours(session)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/horarios/{bh.id}/delete",
            data={"csrf_token": csrf},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/panel/horarios"

    async with TestingSessionLocal() as session:
        deleted = await session.get(BusinessHours, bh.id)
    assert deleted is None


@pytest.mark.asyncio
async def test_edit_bh_of_other_tenant_returns_404():
    async with TestingSessionLocal() as session:
        tenant_a = Tenant(
            name="Biz A BH",
            slug="biz-a-bh-404",
            owner_email="a404@test.com",
            password_hash="x",
            session_version=1,
        )
        tenant_b = Tenant(
            name="Biz B BH",
            slug="biz-b-bh-404",
            owner_email="b404@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant_a)
        session.add(tenant_b)
        await session.flush()
        bh_b = BusinessHours(
            tenant_id=tenant_b.id,
            staff_id=None,
            day_of_week=0,
            start_time=time(9, 0),
            end_time=time(18, 0),
        )
        session.add(bh_b)
        await session.commit()
        await session.refresh(tenant_a)
        await session.refresh(bh_b)

    cookie = make_session_cookie(tenant_a)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/horarios/{bh_b.id}/edit",
            data={
                "day_of_week": "1",
                "start_time": "10:00",
                "end_time": "17:00",
                "csrf_token": csrf,
            },
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Multi-day create (Phase 3, item 12)
# ---------------------------------------------------------------------------


async def _bare_tenant(tag, existing=()):
    """Tenant without hours; `existing` = (day, start, end) rows to pre-create."""
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name=f"Biz BH {tag}",
            slug=f"bh-{tag}-{int(__import__('time').time() * 1000)}",
            owner_email=f"{tag}@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant)
        await session.flush()
        for day, start, end in existing:
            session.add(
                BusinessHours(
                    tenant_id=tenant.id,
                    staff_id=None,
                    day_of_week=day,
                    start_time=time.fromisoformat(start),
                    end_time=time.fromisoformat(end),
                )
            )
        await session.commit()
        await session.refresh(tenant)
    return tenant


async def _post_new_hours(tenant, days, start="09:00", end="18:00"):
    csrf = generate_csrf_token()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", make_session_cookie(tenant))
        client.cookies.set("csrf_token", csrf)
        return await client.post(
            "/panel/horarios/new",
            data={
                "day_of_week": days,
                "start_time": start,
                "end_time": end,
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )


async def _hours_of(tenant):
    from sqlalchemy import select

    async with TestingSessionLocal() as session:
        stmt = (
            select(BusinessHours)
            .where(BusinessHours.tenant_id == tenant.id)
            .order_by(BusinessHours.day_of_week)
        )
        return (await session.execute(stmt)).scalars().all()


async def _get_page(tenant, url):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", make_session_cookie(tenant))
        return await client.get(url)


@pytest.mark.asyncio
async def test_create_bh_multiple_days_creates_one_row_per_day():
    tenant = await _bare_tenant("multi")
    resp = await _post_new_hours(tenant, ["0", "1", "2"])
    assert resp.status_code == 303
    assert resp.headers["location"] == "/panel/horarios"

    rows = await _hours_of(tenant)
    assert [r.day_of_week for r in rows] == [0, 1, 2]
    for r in rows:
        assert str(r.start_time) == "09:00:00"
        assert str(r.end_time) == "18:00:00"
        assert r.staff_id is None


@pytest.mark.asyncio
async def test_create_bh_multiple_days_overlap_saves_nothing():
    tenant = await _bare_tenant("multi-overlap", existing=[(1, "10:00", "12:00")])
    resp = await _post_new_hours(tenant, ["0", "1", "2"])
    assert resp.status_code == 200
    assert "Se solapa con un horario existente: Martes (10:00–12:00)." in resp.text

    rows = await _hours_of(tenant)
    assert [(r.day_of_week, str(r.start_time)) for r in rows] == [(1, "10:00:00")]


@pytest.mark.asyncio
async def test_create_bh_overlap_names_every_conflicting_day():
    tenant = await _bare_tenant(
        "multi-overlap2", existing=[(1, "10:00", "12:00"), (4, "08:00", "09:30")]
    )
    resp = await _post_new_hours(tenant, ["1", "2", "4"])
    assert resp.status_code == 200
    assert "Martes (10:00–12:00)" in resp.text
    assert "Viernes (08:00–09:30)" in resp.text
    assert len(await _hours_of(tenant)) == 2


@pytest.mark.asyncio
async def test_create_bh_without_days_shows_error():
    tenant = await _bare_tenant("multi-none")
    resp = await _post_new_hours(tenant, [])
    assert resp.status_code == 200
    assert "Seleccioná al menos un día." in resp.text
    assert await _hours_of(tenant) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["7", "-1", "x"])
async def test_create_bh_invalid_day_shows_error(bad):
    tenant = await _bare_tenant(f"multi-bad{bad}")
    resp = await _post_new_hours(tenant, [bad, "0"])
    assert resp.status_code == 200
    assert "Seleccioná un día de la semana válido." in resp.text
    assert await _hours_of(tenant) == []


@pytest.mark.asyncio
async def test_create_bh_duplicate_days_create_one_row():
    tenant = await _bare_tenant("multi-dup")
    resp = await _post_new_hours(tenant, ["0", "0"])
    assert resp.status_code == 303
    assert [r.day_of_week for r in await _hours_of(tenant)] == [0]


@pytest.mark.asyncio
async def test_create_bh_rerender_keeps_checked_days():
    tenant = await _bare_tenant("multi-rerender")
    # start >= end forces a re-render
    resp = await _post_new_hours(tenant, ["2", "4"], start="18:00", end="09:00")
    assert resp.status_code == 200
    assert 'type="checkbox" id="day-2" name="day_of_week" value="2" checked' in (
        resp.text
    )
    assert 'type="checkbox" id="day-4" name="day_of_week" value="4" checked' in (
        resp.text
    )
    assert 'id="day-0" name="day_of_week" value="0" checked' not in resp.text


@pytest.mark.asyncio
async def test_new_bh_form_renders_checkboxes_monday_default():
    tenant = await _bare_tenant("multi-form")
    resp = await _get_page(tenant, "/panel/horarios/new")
    assert resp.status_code == 200
    assert 'type="checkbox" id="day-0" name="day_of_week" value="0" checked' in (
        resp.text
    )
    assert 'type="checkbox" id="day-1" name="day_of_week" value="1">' in resp.text
    assert 'type="radio"' not in resp.text


@pytest.mark.asyncio
async def test_edit_bh_form_still_renders_radios():
    async with TestingSessionLocal() as session:
        tenant, bh = await _tenant_with_business_hours(session, day_of_week=3)
    resp = await _get_page(tenant, f"/panel/horarios/{bh.id}/edit")
    assert resp.status_code == 200
    assert 'type="radio" id="day-3" name="day_of_week" value="3" checked' in (resp.text)
    assert 'type="checkbox"' not in resp.text


@pytest.mark.asyncio
async def test_create_bh_multi_ignores_other_tenant_and_staff_hours():
    """Only this business's own (staff_id IS NULL) hours can conflict."""
    other = await _bare_tenant("multi-other", existing=[(1, "10:00", "12:00")])
    tenant = await _bare_tenant("multi-scope")
    async with TestingSessionLocal() as session:
        staff = Staff(tenant_id=tenant.id, name="Profesional")
        session.add(staff)
        await session.flush()
        session.add(
            BusinessHours(
                tenant_id=tenant.id,
                staff_id=staff.id,
                day_of_week=2,
                start_time=time(10),
                end_time=time(12),
            )
        )
        await session.commit()

    resp = await _post_new_hours(tenant, ["1", "2"])

    assert resp.status_code == 303
    rows = [r for r in await _hours_of(tenant) if r.staff_id is None]
    assert [r.day_of_week for r in rows] == [1, 2]
    assert len(await _hours_of(other)) == 1


@pytest.mark.asyncio
async def test_create_bh_multi_adjacent_ranges_are_allowed():
    tenant = await _bare_tenant(
        "multi-adjacent", existing=[(0, "07:00", "09:00"), (1, "18:00", "20:00")]
    )
    resp = await _post_new_hours(tenant, ["0", "1"])
    assert resp.status_code == 303
    assert len(await _hours_of(tenant)) == 4


@pytest.mark.asyncio
async def test_create_bh_multi_without_csrf_is_rejected():
    tenant = await _bare_tenant("multi-csrf")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", make_session_cookie(tenant))
        resp = await client.post(
            "/panel/horarios/new",
            data={
                "day_of_week": ["0", "1"],
                "start_time": "09:00",
                "end_time": "18:00",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 403
    assert await _hours_of(tenant) == []
