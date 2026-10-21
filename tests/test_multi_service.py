"""
Varios servicios en un mismo turno (Fase 5, ítem 43).

T1 (PR 1): el turno conserva su servicio principal en `Booking.service_id` y
los extra en `booking_extra_service`. El dueño ve el combo como
"Lavado + Encerado" en el dashboard, la agenda, el historial, el CSV, las
estadísticas y el WhatsApp de aviso (este último, en test_outbox_worker.py).
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app import services
from app.models import BookingExtraService, Service
from tests.test_agenda_panel import (
    MONEY_DAY,
    TENANT_TZ,
    _money_booking,
    _money_tenant,
    make_session_cookie,
)
from tests.test_bookings_export import _export, _rows
from tests.test_stats_panel import _bars


async def _service(session, tenant, name):
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


async def _add_extras(session, booking, *services):
    for position, service in enumerate(services, start=1):
        session.add(
            BookingExtraService(
                booking_id=booking.id, position=position, service_id=service.id
            )
        )
    await session.flush()


async def _combo_booking(session, day=MONEY_DAY, hour=10, status="confirmed"):
    """Turno de "Servicio montos" + "Encerado" + "Aspirado"."""
    tenant, main = await _money_tenant(session)
    booking = await _money_booking(session, tenant, main, hour, status, day=day)
    # El orden de las filas manda, no el id de los servicios.
    aspirado = await _service(session, tenant, "Aspirado")
    encerado = await _service(session, tenant, "Encerado")
    await _add_extras(session, booking, encerado, aspirado)
    await session.commit()
    return tenant, main, booking


COMBO = "Servicio montos + Encerado + Aspirado"


async def _panel(client, tenant, path, **params):
    res = await client.get(
        path,
        params={k: str(v) for k, v in params.items()},
        cookies={"juturno_session": make_session_cookie(tenant)},
    )
    assert res.status_code == 200
    return res.text


@pytest.mark.asyncio
async def test_day_agenda_shows_combo_name(client, db_session):
    tenant, _main, _booking = await _combo_booking(db_session)

    text = await _panel(client, tenant, "/panel/agenda", day=MONEY_DAY)

    assert COMBO in text


@pytest.mark.asyncio
async def test_week_agenda_shows_combo_name(client, db_session):
    tenant, _main, _booking = await _combo_booking(db_session)

    text = await _panel(client, tenant, "/panel/agenda", view="week", day=MONEY_DAY)

    assert COMBO in text


@pytest.mark.asyncio
async def test_dashboard_shows_combo_name(client, db_session):
    today = datetime.now(ZoneInfo(TENANT_TZ)).date()
    tenant, _main, booking = await _combo_booking(db_session, day=today, hour=23)
    # 23:45 local: sigue siendo "próximo" casi todo el día.
    booking.end_time = datetime.combine(
        today + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo(TENANT_TZ)
    ) - timedelta(minutes=1)
    db_session.add(booking)
    await db_session.commit()

    text = await _panel(client, tenant, "/dashboard")

    assert COMBO in text


@pytest.mark.asyncio
async def test_client_history_shows_combo_name(client, db_session):
    tenant, _main, booking = await _combo_booking(db_session)

    text = await _panel(client, tenant, f"/panel/clientes/{booking.client_phone}")

    assert COMBO in text


@pytest.mark.asyncio
async def test_csv_service_column_shows_combo_name(db_session):
    tenant, _main, _booking = await _combo_booking(db_session, day=date(2027, 3, 10))

    res = await _export(tenant, {"from": "2027-03-10", "to": "2027-03-10"})

    rows = _rows(res)
    assert rows[1][rows[0].index("Servicio")] == COMBO


@pytest.mark.asyncio
async def test_stats_count_every_service_of_a_combo(client, db_session):
    tenant, main, _booking = await _combo_booking(db_session)
    await _money_booking(db_session, tenant, main, 12)
    await db_session.commit()

    text = await _panel(client, tenant, "/panel/estadisticas", day=MONEY_DAY)

    assert _bars(text, "servicios-periodo") == [
        ("Servicio montos", "", 100, 2),
        ("Aspirado", "", 50, 1),
        ("Encerado", "", 50, 1),
    ]


@pytest.mark.asyncio
async def test_extras_of_another_tenant_are_ignored(client, db_session):
    """Defensa: una fila extra que apunte a un servicio de otro negocio no se
    muestra (al crear turnos se valida que todos sean del tenant)."""
    tenant, main = await _money_tenant(db_session)
    booking = await _money_booking(db_session, tenant, main, 10)
    other, _ = await _money_tenant(db_session, name="Otro")
    foreign = await _service(db_session, other, "Ajeno")
    await _add_extras(db_session, booking, foreign)
    await db_session.commit()

    text = await _panel(client, tenant, "/panel/agenda", day=MONEY_DAY)

    assert "Ajeno" not in text


@pytest.mark.asyncio
async def test_single_service_booking_keeps_its_name(client, db_session):
    tenant, main = await _money_tenant(db_session)
    await _money_booking(db_session, tenant, main, 10)
    await db_session.commit()

    text = await _panel(client, tenant, "/panel/agenda", day=MONEY_DAY)

    assert "Servicio montos" in text
    assert "Servicio montos +" not in text


@pytest.mark.asyncio
async def test_extras_are_loaded_in_batches(db_session, monkeypatch):
    """Muchos turnos (CSV de un año) no pasan el tope de parámetros de
    asyncpg: los ids van en lotes y el resultado se junta."""

    tenant, _main, first = await _combo_booking(db_session)
    main2 = await _service(db_session, tenant, "Otro principal")
    second = await _money_booking(db_session, tenant, main2, 12)
    barba = await _service(db_session, tenant, "Barba")
    await _add_extras(db_session, second, barba)
    await db_session.commit()
    monkeypatch.setattr(services, "_EXTRAS_BATCH", 1)

    extras = await services.extra_services_by_booking(
        db_session, tenant.id, [first.id, second.id]
    )

    assert {k: [s.name for s in v] for k, v in extras.items()} == {
        first.id: ["Encerado", "Aspirado"],
        second.id: ["Barba"],
    }
