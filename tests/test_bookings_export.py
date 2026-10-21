"""Tests de la exportación de turnos a CSV (GET /panel/agenda/export)."""

import csv
import io
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models import Booking, Service, Staff, Tenant
from app.session import create_session_token
from tests.conftest import TestingSessionLocal

TZ = "America/Argentina/Buenos_Aires"
HEADER = [
    "Fecha",
    "Hora",
    "Servicio",
    "Personal",
    "Cliente",
    "Teléfono",
    "Estado",
    "Precio",
    "Seña",
    "Notas",
    "Respuestas",
]


def _local(y, m, d, hh, mm):
    """Fecha/hora en la zona del negocio (Buenos Aires, UTC-3)."""
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(TZ))


async def _tenant_with_service(service_name="Corte"):
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name=f"Biz CSV {uuid.uuid4().hex[:6]}",
            slug=f"csv-{uuid.uuid4().hex[:8]}",
            owner_email=f"csv-{uuid.uuid4().hex[:8]}@test.com",
            password_hash="x",
            session_version=1,
            timezone=TZ,
        )
        session.add(tenant)
        await session.flush()
        service = Service(
            tenant_id=tenant.id,
            name=service_name,
            duration_minutes=30,
            price=Decimal("5000.00"),
        )
        session.add(service)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(service)
    return tenant, service


async def _add_staff(tenant, name):
    async with TestingSessionLocal() as session:
        staff = Staff(tenant_id=tenant.id, name=name)
        session.add(staff)
        await session.commit()
        await session.refresh(staff)
    return staff


async def _add_booking(tenant, service, start, **fields):
    # Se inserta directo en la DB: así se pueden sembrar nombres que la
    # validación pública rechazaría (p. ej. que empiecen con "=").
    data = {
        "client_name": "Cliente",
        "client_phone": "5491155555555",
        "price_at_booking": Decimal("5000.00"),
        "status": "confirmed",
    }
    data.update(fields)
    async with TestingSessionLocal() as session:
        booking = Booking(
            tenant_id=tenant.id,
            service_id=service.id,
            start_time=start,
            end_time=start + timedelta(minutes=30),
            idempotency_key=f"key-{uuid.uuid4().hex}",
            **data,
        )
        session.add(booking)
        await session.commit()
        await session.refresh(booking)
    return booking


async def _export(tenant, params, session=True, path="/panel/agenda/export"):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        if session:
            client.cookies.set(
                "juturno_session",
                create_session_token(tenant.id, tenant.session_version),
            )
        return await client.get(path, params=params, follow_redirects=False)


def _rows(res):
    text = res.content.decode("utf-8")
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:]), delimiter=";"))


@pytest.mark.asyncio
async def test_export_headers_bom_and_header_row():
    tenant, _ = await _tenant_with_service()
    res = await _export(tenant, {"from": "2027-03-01", "to": "2027-03-31"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "charset=utf-8" in res.headers["content-type"]
    assert (
        res.headers["content-disposition"]
        == 'attachment; filename="turnos_2027-03-01_2027-03-31.csv"'
    )
    # Cloudflare cachea .csv por defecto y la URL del mes es igual para
    # todos los tenants: la respuesta no se puede guardar en ningún caché.
    assert res.headers["cache-control"] == "no-store, private"
    assert _rows(res) == [HEADER]


@pytest.mark.asyncio
async def test_export_old_csv_extension_path_does_not_serve_csv():
    tenant, _ = await _tenant_with_service()
    res = await _export(
        tenant,
        {"from": "2027-03-01", "to": "2027-03-31"},
        path="/panel/agenda/export.csv",
    )
    assert res.status_code != 200
    assert not res.headers.get("content-type", "").startswith("text/csv")


@pytest.mark.asyncio
async def test_export_row_format_staff_notes_and_amounts():
    tenant, service = await _tenant_with_service("Lavado completo")
    staff = await _add_staff(tenant, "Ana")
    await _add_booking(
        tenant,
        service,
        _local(2027, 3, 10, 9, 5),
        client_name="Juan Pérez",
        staff_id=staff.id,
        price_at_booking=Decimal(5000),
        deposit_at_booking=Decimal("1500.5"),
        internal_notes="Trae su material",
    )
    await _add_booking(
        tenant,
        service,
        _local(2027, 3, 10, 11, 0),
        client_name="Sin personal",
        deposit_at_booking=None,
    )

    rows = _rows(await _export(tenant, {"from": "2027-03-01", "to": "2027-03-31"}))
    assert rows[1] == [
        "10/03/2027",
        "09:05",
        "Lavado completo",
        "Ana",
        "Juan Pérez",
        "5491155555555",
        "Confirmado",
        "5000,00",
        "1500,50",
        "Trae su material",
        "",  # Respuestas (ítem 42): este turno no tiene
    ]
    # Sin personal → celda vacía; sin seña → 0,00; sin nota → vacío
    assert rows[2][3] == ""
    assert rows[2][8] == "0,00"
    assert rows[2][9] == ""


@pytest.mark.asyncio
async def test_export_includes_all_statuses_ordered_by_start():
    tenant, service = await _tenant_with_service()
    statuses = ["pending", "confirmed", "expired", "cancelled", "no_show", "completed"]
    for i, st in enumerate(reversed(statuses)):
        await _add_booking(tenant, service, _local(2027, 3, 20 - i, 10, 0), status=st)
    rows = _rows(await _export(tenant, {"from": "2027-03-01", "to": "2027-03-31"}))
    assert [r[6] for r in rows[1:]] == [
        "Pendiente de pago",
        "Confirmado",
        "Expirado",
        "Cancelado",
        "No se presentó",
        "Completado",
    ]


@pytest.mark.asyncio
async def test_export_range_uses_tenant_timezone():
    tenant, service = await _tenant_with_service()
    # 23:00 local del día anterior a "from" → afuera
    await _add_booking(tenant, service, _local(2027, 2, 28, 23, 0), client_name="Antes")
    # 00:00 local de "from" (03:00 UTC) → adentro
    await _add_booking(tenant, service, _local(2027, 3, 1, 0, 0), client_name="Inicio")
    # 23:30 local de "to" = 02:30 UTC del día siguiente → adentro
    await _add_booking(tenant, service, _local(2027, 3, 31, 23, 30), client_name="Fin")
    # 00:30 local del día siguiente a "to" → afuera
    await _add_booking(
        tenant, service, _local(2027, 4, 1, 0, 30), client_name="Después"
    )

    rows = _rows(await _export(tenant, {"from": "2027-03-01", "to": "2027-03-31"}))
    assert [r[4] for r in rows[1:]] == ["Inicio", "Fin"]
    assert rows[2][:2] == ["31/03/2027", "23:30"]


@pytest.mark.asyncio
async def test_export_only_current_tenant():
    tenant, service = await _tenant_with_service()
    other, other_service = await _tenant_with_service()
    await _add_booking(tenant, service, _local(2027, 3, 5, 10, 0), client_name="Mío")
    await _add_booking(
        other, other_service, _local(2027, 3, 5, 10, 0), client_name="Ajeno"
    )
    rows = _rows(await _export(tenant, {"from": "2027-03-01", "to": "2027-03-31"}))
    assert [r[4] for r in rows[1:]] == ["Mío"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
async def test_export_escapes_formula_injection(prefix):
    tenant, service = await _tenant_with_service(f"{prefix}SUMA(1)")
    staff = await _add_staff(tenant, f"{prefix}Staff")
    await _add_booking(
        tenant,
        service,
        _local(2027, 3, 5, 10, 0),
        staff_id=staff.id,
        client_name=f'{prefix}HYPERLINK("x")',
        internal_notes=f"{prefix}1+1",
    )
    row = _rows(await _export(tenant, {"from": "2027-03-01", "to": "2027-03-31"}))[1]
    assert row[2] == f"'{prefix}SUMA(1)"
    assert row[3] == f"'{prefix}Staff"
    assert row[4] == f'\'{prefix}HYPERLINK("x")'
    assert row[9] == f"'{prefix}1+1"


@pytest.mark.asyncio
async def test_export_quotes_fields_against_semicolon_injection():
    # Excel es-AR usa ";" como separador de listas: un ";" sin comillas
    # partiría la celda y "=cmd..." quedaría como fórmula en otra columna.
    tenant, service = await _tenant_with_service()
    name = "Ana;=cmd|'/C calc'!A0;"
    await _add_booking(tenant, service, _local(2027, 3, 5, 10, 0), client_name=name)
    res = await _export(tenant, {"from": "2027-03-01", "to": "2027-03-31"})
    line = res.content.decode("utf-8")[1:].splitlines()[1]
    assert f'"{name}"' in line
    assert line.startswith('"') and line.endswith('"')
    row = _rows(res)[1]
    assert len(row) == len(HEADER)
    assert row[4] == name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"from": "2027-13-01", "to": "2027-03-31"},
        {"from": "hola", "to": "2027-03-31"},
        {"from": "2027-03-01"},
        {"from": "2027-03-31", "to": "2027-03-01"},
        # 367 días inclusive
        {"from": "2027-01-01", "to": "2028-01-02"},
        # El día siguiente a "to" no existe (OverflowError)
        {"from": "9999-12-30", "to": "9999-12-31"},
    ],
)
async def test_export_invalid_range_redirects(params):
    tenant, _ = await _tenant_with_service()
    res = await _export(tenant, params)
    assert res.status_code == 303
    assert res.headers["location"] == "/panel/agenda?export=invalid"


@pytest.mark.asyncio
async def test_export_max_range_366_days_ok():
    tenant, _ = await _tenant_with_service()
    # 2028 es bisiesto: 2028-01-01..2028-12-31 son 366 días inclusive
    res = await _export(tenant, {"from": "2028-01-01", "to": "2028-12-31"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_export_requires_session():
    tenant, _ = await _tenant_with_service()
    res = await _export(
        tenant, {"from": "2027-03-01", "to": "2027-03-31"}, session=False
    )
    assert res.status_code == 303
    assert "/login" in res.headers["location"]


@pytest.mark.asyncio
async def test_agenda_shows_export_form_and_invalid_flash():
    tenant, _ = await _tenant_with_service()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(
            "juturno_session", create_session_token(tenant.id, tenant.session_version)
        )
        res = await client.get("/panel/agenda?day=2027-02-15")
        assert 'action="/panel/agenda/export"' in res.text
        assert 'value="2027-02-01"' in res.text
        assert 'value="2027-02-28"' in res.text
        assert "Revisá las fechas" not in res.text
        # Al pie de la agenda, plegado
        assert res.text.index("No hay turnos para este día") < res.text.index(
            'action="/panel/agenda/export"'
        )
        assert '<details class="ag-export">' in res.text
        assert "<summary>Descargar turnos (CSV)</summary>" in res.text

        res = await client.get("/panel/agenda?export=invalid")
        assert "Revisá las fechas: el rango puede ser de hasta un año." in res.text
        assert '<details class="ag-export" open>' in res.text
