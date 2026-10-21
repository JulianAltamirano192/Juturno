"""Tests de la pestaña Estadísticas del panel (resumen de plata por semana o mes)."""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.models import Service
from app.routers.panel import _pct
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


# --- Ítem 18: gráficos de turnos, servicios y días -------------------------


def _section(text, block):
    assert f'id="{block}"' in text
    return text.split(f'id="{block}"', 1)[1].split("</section>", 1)[0]


def _legend(text):
    """Cantidades de la barra apilada de estados: {"Confirmados": 2, ...}."""
    html = _section(text, "turnos-periodo")
    pairs = re.findall(
        r'<li><i class="[^"]*"></i>(.*?) <strong>(\d+)</strong></li>', html
    )
    return {label: int(n) for label, n in pairs}


def _bars(text, block):
    """Filas de un gráfico de barras: [(nombre, etiqueta, ancho %, cantidad)]."""
    html = _section(text, block)
    rows = re.findall(
        r'<span class="bar-l">(.*?)</span>(?:<em class="tag[^"]*">(.*?)</em>)?'
        r'<span class="bar"><span style="width:(\d+)%"></span></span>'
        r"<strong>(\d+)</strong>",
        html,
    )
    return [(name, tag, int(width), int(n)) for name, tag, width, n in rows]


async def _extra_service(session, tenant, name):
    service = Service(
        tenant_id=tenant.id,
        name=name,
        duration_minutes=30,
        price=Decimal("1000.00"),
        is_active=True,
    )
    session.add(service)
    await session.flush()
    return service


@pytest.mark.asyncio
async def test_stats_counts_bookings_by_status(client, db_session):
    tenant, service = await _money_tenant(db_session)
    june = date(2026, 6, 10)
    for hour, status in [
        (9, "confirmed"),
        (10, "confirmed"),
        (11, "completed"),
        (12, "no_show"),
        (13, "cancelled"),
        (14, "expired"),
    ]:
        await _money_booking(db_session, tenant, service, hour, status, day=june)
    # Empieza el 31/5 y termina el 1/6: cuenta en mayo, no en junio.
    await _money_booking(
        db_session, tenant, service, 23, minute=45, day=date(2026, 5, 31)
    )
    await db_session.commit()

    text = await _get_stats(client, tenant, period="month", day=JUNE_DAY)

    assert _legend(text) == {
        "Confirmados": 2,
        "Completados": 1,
        "No se presentaron": 1,
        "Cancelados": 1,
    }


@pytest.mark.asyncio
async def test_stats_top_services_counts_only_sold_and_caps_at_five(client, db_session):
    tenant, _service = await _money_tenant(db_session)
    sold = {"Alfa": 3, "Beta": 2, "Delta": 1, "Eco": 1, "Foxtrot": 1, "Golf": 1}
    day = date(2026, 6, 1)
    for name, count in sold.items():
        service = await _extra_service(db_session, tenant, name)
        for i in range(count):
            await _money_booking(
                db_session, tenant, service, 9 + i, "completed", day=day
            )
        day += timedelta(days=1)
    # Cancelados y no presentados no son "más pedidos".
    charlie = await _extra_service(db_session, tenant, "Charlie")
    for hour in range(9, 14):
        await _money_booking(db_session, tenant, charlie, hour, "cancelled", day=day)
    await _money_booking(db_session, tenant, charlie, 15, "no_show", day=day)
    await db_session.commit()

    text = await _get_stats(client, tenant, period="month", day=JUNE_DAY)

    assert _bars(text, "servicios-periodo") == [
        ("Alfa", "", 100, 3),
        ("Beta", "", 67, 2),
        ("Delta", "", 33, 1),
        ("Eco", "", 33, 1),
        ("Foxtrot", "", 33, 1),
    ]


@pytest.mark.asyncio
async def test_stats_weekdays_mark_busiest_and_quietest(client, db_session):
    tenant, service = await _money_tenant(db_session)
    for hour in (9, 10, 11):
        await _money_booking(db_session, tenant, service, hour, day=WEEK_MONDAY)
    await _money_booking(db_session, tenant, service, 10, "completed", day=MONEY_DAY)
    friday = WEEK_MONDAY + timedelta(days=4)
    for hour in (9, 10):
        await _money_booking(db_session, tenant, service, hour, day=friday)
    await _money_booking(db_session, tenant, service, 10, "cancelled", day=WEEK_SUNDAY)
    await db_session.commit()

    text = await _get_stats(client, tenant, day=MONEY_DAY)

    # "Más tranquilo" es el de menos turnos entre los días con alguno: un día
    # cerrado (0) no dice nada.
    assert _bars(text, "dias-periodo") == [
        ("Lunes", "Más movido", 100, 3),
        ("Martes", "", 0, 0),
        ("Miércoles", "Más tranquilo", 33, 1),
        ("Jueves", "", 0, 0),
        ("Viernes", "", 67, 2),
        ("Sábado", "", 0, 0),
        ("Domingo", "", 0, 0),
    ]
    # Por día del mes solo en la vista mensual.
    assert 'id="por-dia"' not in text


@pytest.mark.asyncio
async def test_stats_weekdays_without_difference_have_no_tags(client, db_session):
    tenant, service = await _money_tenant(db_session)
    await _money_booking(db_session, tenant, service, 10, day=WEEK_MONDAY)
    await db_session.commit()

    text = await _get_stats(client, tenant, day=MONEY_DAY)

    assert all(tag == "" for _name, tag, _w, _n in _bars(text, "dias-periodo"))


@pytest.mark.asyncio
@pytest.mark.parametrize("month, days", [(6, 30), (2, 28)])
async def test_stats_month_has_one_column_per_day(client, db_session, month, days):
    tenant, service = await _money_tenant(db_session)
    for hour in (9, 10):
        await _money_booking(
            db_session, tenant, service, hour, day=date(2026, month, 15)
        )
    await _money_booking(db_session, tenant, service, 9, day=date(2026, month, 3))
    await _money_booking(
        db_session, tenant, service, 11, "cancelled", day=date(2026, month, 3)
    )
    # 23:30 local del último día: en UTC ya es el mes siguiente, pero cuenta acá.
    await _money_booking(
        db_session, tenant, service, 23, minute=30, day=date(2026, month, days)
    )
    await db_session.commit()

    text = await _get_stats(client, tenant, period="month", day=date(2026, month, 1))

    html = _section(text, "por-dia")
    columns = re.findall(
        rf'<rect [^>]*height="(\d+)"[^>]*><title>(\d+)/{month}: (\d+) turnos?</title>',
        html,
    )
    assert len(columns) == days
    heights = {int(d): (int(h), int(n)) for h, d, n in columns}
    assert heights[15] == (100, 2)
    assert heights[3] == (50, 1)
    assert heights[1] == (0, 0)
    assert heights[days] == (50, 1)
    assert "El más movido fue el 15/" in html


@pytest.mark.asyncio
async def test_stats_empty_period_shows_empty_state(client, db_session):
    tenant, _service = await _money_tenant(db_session)
    await db_session.commit()

    text = await _get_stats(client, tenant, period="month", day=JUNE_DAY)

    assert "Sin turnos en este período" in text
    assert 'id="turnos-periodo"' not in text


@pytest.mark.asyncio
async def test_stats_charts_ignore_other_tenants(client, db_session):
    tenant, service = await _money_tenant(db_session)
    await _money_booking(db_session, tenant, service, 10)
    other, other_service = await _money_tenant(db_session, name="Otro montos")
    await _money_booking(db_session, other, other_service, 10)
    await _money_booking(db_session, other, other_service, 11, "cancelled")
    await db_session.commit()

    text = await _get_stats(client, tenant, day=MONEY_DAY)

    assert _legend(text)["Confirmados"] == 1
    assert _legend(text)["Cancelados"] == 0
    assert _bars(text, "servicios-periodo") == [("Servicio montos", "", 100, 1)]


@pytest.mark.asyncio
async def test_stats_service_name_is_escaped(client, db_session):
    tenant, _service = await _money_tenant(db_session)
    evil = await _extra_service(db_session, tenant, '<script>x</script>"><b>')
    await _money_booking(db_session, tenant, evil, 10)
    await db_session.commit()

    text = await _get_stats(client, tenant, day=MONEY_DAY)

    html = _section(text, "servicios-periodo")
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html


@pytest.mark.asyncio
async def test_stats_only_lost_bookings_show_statuses_without_bars(client, db_session):
    tenant, service = await _money_tenant(db_session)
    await _money_booking(db_session, tenant, service, 10, "cancelled")
    await _money_booking(db_session, tenant, service, 11, "no_show")
    await db_session.commit()

    text = await _get_stats(client, tenant, day=MONEY_DAY)

    assert _legend(text)["Cancelados"] == 1
    assert _legend(text)["No se presentaron"] == 1
    for block in ("servicios-periodo", "dias-periodo"):
        assert "Sin turnos confirmados o completados." in _section(text, block)


def test_stats_bar_never_hides_a_nonzero_count():
    assert _pct(1, 250) == 1
    assert _pct(0, 250) == 0
    assert _pct(3, 0) == 0
    assert _pct(2, 3) == 67


@pytest.mark.asyncio
async def test_stats_weekday_ties_mark_every_tied_day(client, db_session):
    tenant, service = await _money_tenant(db_session)
    friday = WEEK_MONDAY + timedelta(days=4)
    for day in (WEEK_MONDAY, friday):
        for hour in (9, 10):
            await _money_booking(db_session, tenant, service, hour, day=day)
    tuesday = WEEK_MONDAY + timedelta(days=1)
    for day in (tuesday, MONEY_DAY):
        await _money_booking(db_session, tenant, service, 10, day=day)
    await db_session.commit()

    text = await _get_stats(client, tenant, day=MONEY_DAY)

    tags = {name: tag for name, tag, _w, _n in _bars(text, "dias-periodo")}
    assert tags == {
        "Lunes": "Más movido",
        "Martes": "Más tranquilo",
        "Miércoles": "Más tranquilo",
        "Jueves": "",
        "Viernes": "Más movido",
        "Sábado": "",
        "Domingo": "",
    }


@pytest.mark.asyncio
async def test_stats_services_with_same_name_are_separate_rows(client, db_session):
    tenant, service = await _money_tenant(db_session)
    twin = await _extra_service(db_session, tenant, service.name)
    await _money_booking(db_session, tenant, service, 9)
    await _money_booking(db_session, tenant, twin, 10)
    await _money_booking(db_session, tenant, twin, 11)
    await db_session.commit()

    text = await _get_stats(client, tenant, day=MONEY_DAY)

    assert _bars(text, "servicios-periodo") == [
        ("Servicio montos", "", 100, 2),
        ("Servicio montos", "", 50, 1),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("period, shown", [("month", True), ("week", False)])
async def test_stats_weekday_chart_explains_month_sum(
    client, db_session, period, shown
):
    tenant, service = await _money_tenant(db_session)
    await _money_booking(db_session, tenant, service, 10)
    await db_session.commit()

    text = await _get_stats(client, tenant, period=period, day=MONEY_DAY)

    html = _section(text, "dias-periodo")
    assert ("Suma de todo el mes" in html) is shown
