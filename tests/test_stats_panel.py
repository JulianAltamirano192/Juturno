"""Tests de la pestaña Estadísticas del panel (resumen de plata por semana o mes)."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.test_agenda_panel import (
    MONEY_DAY,
    TENANT_TZ,
    _expected,
    _money_booking,
    _money_tenant,
    _summary,
    make_session_cookie,
)

# MONEY_DAY es miércoles 6/5/2026: su semana va del lunes 4/5 al domingo 10/5.
WEEK_MONDAY = date(2026, 5, 4)
WEEK_SUNDAY = date(2026, 5, 10)
# Junio tiene 30 días.
JUNE_DAY = date(2026, 6, 15)


async def _get_stats(client, tenant, **params):
    resp = await client.get(
        "/panel/estadisticas",
        params={k: str(v) for k, v in params.items()},
        cookies={"juturno_session": make_session_cookie(tenant)},
    )
    assert resp.status_code == 200
    return resp.text


def _active_period(text):
    match = re.search(r'aria-current="true">(Semana|Mes)</a>', text)
    return match.group(1) if match else None


@pytest.mark.asyncio
async def test_stats_requires_session(client):
    resp = await client.get("/panel/estadisticas", follow_redirects=False)

    assert resp.status_code in (302, 303, 307)
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
@pytest.mark.parametrize("period", [None, "week", "otra-cosa"])
async def test_stats_week_covers_monday_to_sunday(client, db_session, period):
    tenant, service = await _money_tenant(db_session)
    await _money_booking(
        db_session,
        tenant,
        service,
        10,
        day=WEEK_MONDAY,
        payments=[("100.00", "approved")],
    )
    await _money_booking(
        db_session,
        tenant,
        service,
        10,
        day=WEEK_SUNDAY,
        status="completed",
        payments=[("200.00", "approved")],
    )
    await _money_booking(
        db_session, tenant, service, 11, day=WEEK_SUNDAY, status="cancelled"
    )
    # Fuera de la semana: domingo anterior, lunes siguiente y un turno que
    # empieza el domingo anterior y termina el lunes (cuenta en su semana).
    await _money_booking(
        db_session,
        tenant,
        service,
        10,
        day=WEEK_MONDAY - timedelta(days=1),
        payments=[("888.00", "approved")],
    )
    await _money_booking(
        db_session,
        tenant,
        service,
        10,
        day=WEEK_SUNDAY + timedelta(days=1),
        payments=[("999.00", "approved")],
    )
    await _money_booking(
        db_session,
        tenant,
        service,
        23,
        minute=45,
        day=WEEK_MONDAY - timedelta(days=1),
        payments=[("777.00", "approved")],
    )
    await db_session.commit()

    params = {"day": MONEY_DAY}
    if period is not None:
        params["period"] = period
    text = await _get_stats(client, tenant, **params)

    assert _summary(text, "periodo") == _expected(300, "1 persona", 2000, 1700)
    assert "Semana del 4/5 al 10/5" in text
    assert _active_period(text) == "Semana"
    for amount in ("$ 888", "$ 999", "$ 777"):
        assert amount not in text


@pytest.mark.asyncio
async def test_stats_month_covers_first_to_last_day(client, db_session):
    tenant, service = await _money_tenant(db_session)
    await _money_booking(
        db_session,
        tenant,
        service,
        10,
        day=date(2026, 6, 1),
        payments=[("100.00", "approved")],
    )
    await _money_booking(
        db_session,
        tenant,
        service,
        10,
        day=date(2026, 6, 30),
        status="completed",
        price="500.00",
        payments=[("200.00", "approved")],
    )
    await _money_booking(
        db_session, tenant, service, 10, day=date(2026, 6, 20), status="no_show"
    )
    await _money_booking(
        db_session,
        tenant,
        service,
        10,
        day=date(2026, 5, 31),
        payments=[("777.00", "approved")],
    )
    await _money_booking(
        db_session,
        tenant,
        service,
        10,
        day=date(2026, 7, 1),
        payments=[("666.00", "approved")],
    )
    await db_session.commit()

    text = await _get_stats(client, tenant, period="month", day=JUNE_DAY)

    assert _summary(text, "periodo") == _expected(300, "1 persona", 1500, 1200)
    assert "Junio 2026" in text
    assert _active_period(text) == "Mes"
    assert "$ 777" not in text
    assert "$ 666" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "period, day, prev_day, next_day",
    [
        ("week", MONEY_DAY, "2026-04-27", "2026-05-11"),
        ("month", date(2026, 3, 31), "2026-02-01", "2026-04-01"),
        ("month", date(2026, 1, 15), "2025-12-01", "2026-02-01"),
    ],
)
async def test_stats_prev_and_next_period_links(
    client, db_session, period, day, prev_day, next_day
):
    tenant, _service = await _money_tenant(db_session)
    await db_session.commit()

    text = await _get_stats(client, tenant, period=period, day=day)

    assert f"/panel/estadisticas?period={period}&day={prev_day}" in text
    assert f"/panel/estadisticas?period={period}&day={next_day}" in text


@pytest.mark.asyncio
async def test_stats_ignore_other_tenants(client, db_session):
    tenant, service = await _money_tenant(db_session)
    await _money_booking(
        db_session, tenant, service, 10, payments=[("300.00", "approved")]
    )
    other, other_service = await _money_tenant(db_session, name="Otro montos")
    await _money_booking(
        db_session,
        other,
        other_service,
        10,
        price="5000.00",
        payments=[("999.00", "approved")],
    )
    await _money_booking(db_session, other, other_service, 11, status="cancelled")
    await db_session.commit()

    text = await _get_stats(client, tenant, day=MONEY_DAY)

    assert _summary(text, "periodo") == _expected(300, "0 personas", 1000, 700)
    assert "$ 999" not in text


@pytest.mark.asyncio
async def test_stats_empty_period_shows_zeros(client, db_session):
    tenant, _service = await _money_tenant(db_session)
    await db_session.commit()

    text = await _get_stats(client, tenant, period="month", day=JUNE_DAY)

    assert _summary(text, "periodo") == _expected(0, "0 personas", 0, 0)


@pytest.mark.asyncio
async def test_stats_invalid_day_falls_back_to_this_week(client, db_session):
    tenant, _service = await _money_tenant(db_session)
    await db_session.commit()

    text = await _get_stats(client, tenant, day="no-es-fecha")

    today = datetime.now(ZoneInfo(TENANT_TZ)).date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    assert f"Semana del {monday.day}/{monday.month} al {sunday.day}/{sunday.month}" in (
        text
    )


@pytest.mark.asyncio
async def test_stats_nav_item_is_active_only_on_its_page(client, db_session):
    tenant, _service = await _money_tenant(db_session)
    await db_session.commit()
    active = 'href="/panel/estadisticas" aria-current="page">'

    stats_text = await _get_stats(client, tenant)
    agenda = await client.get(
        "/panel/agenda", cookies={"juturno_session": make_session_cookie(tenant)}
    )

    assert active in stats_text
    # Barra lateral con el nombre completo; barra inferior del celular, corto.
    assert "Estadísticas</a>" in stats_text
    assert "Números</span></a>" in stats_text
    assert 'href="/panel/estadisticas"' in agenda.text
    assert active not in agenda.text


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["week", "month"])
@pytest.mark.parametrize("day", ["0001-01-01", "9999-12-31"])
async def test_stats_extreme_day_falls_back_to_today(client, db_session, period, day):
    """Una fecha en los bordes de `date` no rompe con 500 al calcular el
    período anterior o siguiente: cae a hoy."""
    tenant, _service = await _money_tenant(db_session)
    await db_session.commit()

    text = await _get_stats(client, tenant, period=period, day=day)

    assert str(datetime.now(ZoneInfo(TENANT_TZ)).year) in text
