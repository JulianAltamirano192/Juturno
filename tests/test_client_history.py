"""Tests de la pestaña Clientes del panel (historial agrupado por teléfono)."""

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.models import Booking, Service, Staff, Tenant
from tests.test_agenda_panel import TENANT_TZ, make_session_cookie

PHONE_ANA = "5491111111111"
PHONE_BETO = "5492222222222"


async def _tenant(session, name="Clientes Biz"):
    tenant = Tenant(
        name=name,
        slug=f"clientes-{uuid4().hex[:12]}",
        owner_email=f"clientes-{uuid4().hex[:12]}@test.com",
        password_hash="x",
        session_version=1,
        timezone=TENANT_TZ,
    )
    session.add(tenant)
    await session.flush()
    service = Service(
        tenant_id=tenant.id,
        name="Corte",
        duration_minutes=30,
        price=Decimal("1000.00"),
        is_active=True,
    )
    session.add(service)
    await session.commit()
    return tenant, service


async def _booking(
    session, tenant, service, phone, name, day, status="completed", staff=None
):
    start = datetime.combine(day, time(10, 0), tzinfo=ZoneInfo(TENANT_TZ))
    booking = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        staff_id=staff.id if staff else None,
        client_name=name,
        client_phone=phone,
        start_time=start,
        end_time=start + timedelta(minutes=30),
        price_at_booking=Decimal("1000.00"),
        status=status,
        idempotency_key=f"clientes-{uuid4().hex}",
    )
    session.add(booking)
    await session.commit()
    return booking


async def _get(client, tenant, url, **params):
    return await client.get(
        url,
        params=params,
        cookies={"juturno_session": make_session_cookie(tenant)},
    )


def _client_rows(text):
    """Teléfonos de la lista, en el orden en que aparecen."""
    return re.findall(r'href="/panel/clientes/(\d+)"', text)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["/panel/clientes", f"/panel/clientes/{PHONE_ANA}"])
async def test_client_pages_require_session(client, url):
    resp = await client.get(url, follow_redirects=False)

    assert resp.status_code in (302, 303, 307)
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_list_groups_by_phone_newest_first(client, db_session):
    tenant, service = await _tenant(db_session)
    await _booking(db_session, tenant, service, PHONE_ANA, "Ana", date(2026, 5, 1))
    await _booking(
        db_session,
        tenant,
        service,
        PHONE_ANA,
        "Ana María",
        date(2026, 5, 8),
        status="no_show",
    )
    await _booking(db_session, tenant, service, PHONE_BETO, "Beto", date(2026, 5, 4))

    resp = await _get(client, tenant, "/panel/clientes")

    assert resp.status_code == 200
    # Una fila por teléfono, la del último turno más reciente primero.
    assert _client_rows(resp.text) == [PHONE_ANA, PHONE_BETO]
    # Se muestra el nombre del último turno, no el primero.
    assert "Ana María" in resp.text
    assert "2 turnos" in resp.text
    assert "1 ausencia" in resp.text
    assert "1 turno<" in resp.text


@pytest.mark.asyncio
async def test_list_ignores_expired_only_clients(client, db_session):
    """Un turno vencido sin pagar es una reserva abandonada, no un cliente."""
    tenant, service = await _tenant(db_session)
    await _booking(
        db_session,
        tenant,
        service,
        PHONE_ANA,
        "Ana",
        date(2026, 5, 1),
        status="expired",
    )
    await _booking(db_session, tenant, service, PHONE_BETO, "Beto", date(2026, 5, 4))

    resp = await _get(client, tenant, "/panel/clientes")

    assert _client_rows(resp.text) == [PHONE_BETO]


@pytest.mark.asyncio
async def test_list_and_detail_only_show_own_tenant(client, db_session):
    tenant, service = await _tenant(db_session)
    other, other_service = await _tenant(db_session, name="Otro")
    await _booking(db_session, tenant, service, PHONE_ANA, "Ana", date(2026, 5, 1))
    await _booking(
        db_session, other, other_service, PHONE_BETO, "Ajeno", date(2026, 5, 2)
    )
    await _booking(
        db_session, other, other_service, PHONE_ANA, "Ana en otro", date(2026, 5, 3)
    )

    listing = await _get(client, tenant, "/panel/clientes")
    detail = await _get(client, tenant, f"/panel/clientes/{PHONE_ANA}")
    foreign = await _get(client, tenant, f"/panel/clientes/{PHONE_BETO}")

    assert _client_rows(listing.text) == [PHONE_ANA]
    assert "Ajeno" not in listing.text
    assert "Ana en otro" not in detail.text
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_search_by_name_or_phone(client, db_session):
    tenant, service = await _tenant(db_session)
    await _booking(db_session, tenant, service, PHONE_ANA, "Ana", date(2026, 5, 1))
    await _booking(db_session, tenant, service, PHONE_BETO, "Beto", date(2026, 5, 4))

    by_name = await _get(client, tenant, "/panel/clientes", q="  bEt ")
    by_phone = await _get(client, tenant, "/panel/clientes", q="11 1111")
    no_match = await _get(client, tenant, "/panel/clientes", q="Carla")

    assert _client_rows(by_name.text) == [PHONE_BETO]
    assert _client_rows(by_phone.text) == [PHONE_ANA]
    assert _client_rows(no_match.text) == []
    assert "Ningún cliente coincide" in no_match.text


@pytest.mark.asyncio
async def test_search_treats_wildcards_literally(client, db_session):
    tenant, service = await _tenant(db_session)
    await _booking(db_session, tenant, service, PHONE_ANA, "Ana", date(2026, 5, 1))

    resp = await _get(client, tenant, "/panel/clientes", q="%")

    assert _client_rows(resp.text) == []


@pytest.mark.asyncio
async def test_detail_lists_every_booking_newest_first(client, db_session):
    tenant, service = await _tenant(db_session)
    staff = Staff(tenant_id=tenant.id, name="Lucía", is_active=True)
    db_session.add(staff)
    await db_session.commit()
    await _booking(db_session, tenant, service, PHONE_ANA, "Ana", date(2026, 5, 1))
    await _booking(
        db_session,
        tenant,
        service,
        PHONE_ANA,
        "Ana",
        date(2026, 5, 8),
        status="no_show",
        staff=staff,
    )
    await _booking(
        db_session,
        tenant,
        service,
        PHONE_ANA,
        "Ana",
        date(2026, 4, 20),
        status="expired",
    )
    await _booking(db_session, tenant, service, PHONE_BETO, "Beto", date(2026, 5, 4))

    resp = await _get(client, tenant, f"/panel/clientes/{PHONE_ANA}")

    assert resp.status_code == 200
    assert "Beto" not in resp.text
    days = re.findall(r'href="/panel/agenda\?day=([\d-]+)"', resp.text)
    # El vencido también se lista: el dueño ve que reservó y no pagó.
    assert days == ["2026-05-08", "2026-05-01", "2026-04-20"]
    for label in ("No se presentó", "Completado", "Expirado"):
        assert label in resp.text
    assert "Lucía" in resp.text
    assert "Corte" in resp.text
    # El vencido se lista pero no entra en los contadores.
    stats = dict(re.findall(r"<span>([^<]+)</span><strong>(\d+)</strong>", resp.text))
    assert stats == {"Completados": "1", "No se presentó": "1", "Cancelados": "0"}
    assert "https://wa.me/" + PHONE_ANA in resp.text


@pytest.mark.asyncio
async def test_detail_unknown_phone_is_404(client, db_session):
    tenant, _ = await _tenant(db_session)

    resp = await _get(client, tenant, "/panel/clientes/5499999999999")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_agenda_links_client_to_history(client, db_session):
    tenant, service = await _tenant(db_session)
    today = datetime.now(ZoneInfo(TENANT_TZ)).date()
    await _booking(
        db_session, tenant, service, PHONE_ANA, "Ana", today, status="confirmed"
    )

    resp = await _get(client, tenant, "/panel/agenda", day=today.isoformat())

    assert f'href="/panel/clientes/{PHONE_ANA}"' in resp.text


@pytest.mark.asyncio
async def test_panel_nav_has_clients_tab(client, db_session):
    tenant, _ = await _tenant(db_session)

    resp = await _get(client, tenant, "/panel/clientes")
    tabbar = re.search(r'<nav class="tabbar".*?</nav>', resp.text, re.DOTALL).group(0)

    assert 'class="nav-a on" href="/panel/clientes" aria-current="page"' in resp.text
    # En celular no hay 8.ª pestaña: Clientes vive dentro de Agenda.
    assert "/panel/clientes" not in tabbar
    assert 'class="tab on" href="/panel/agenda" aria-current="page"' in tabbar


@pytest.mark.asyncio
@pytest.mark.parametrize("view", [None, "week"])
async def test_agenda_toggle_links_to_clients(client, db_session, view):
    tenant, _ = await _tenant(db_session)
    params = {"view": view} if view else {}

    resp = await _get(client, tenant, "/panel/agenda", **params)

    assert '<a class="m-only" href="/panel/clientes">Clientes</a>' in resp.text


@pytest.mark.asyncio
async def test_nul_byte_in_search_or_phone_is_not_500(client, db_session):
    """Postgres rechaza el byte NUL en un texto: no puede llegar a la query."""
    tenant, service = await _tenant(db_session)
    await _booking(db_session, tenant, service, PHONE_ANA, "Ana", date(2026, 5, 1))

    search = await _get(client, tenant, "/panel/clientes", q="An\x00a")
    detail = await _get(client, tenant, "/panel/clientes/549%00")

    assert search.status_code == 200
    # Se descarta el NUL: queda "Ana".
    assert _client_rows(search.text) == [PHONE_ANA]
    assert detail.status_code == 404


@pytest.mark.asyncio
async def test_list_is_capped_with_note(client, db_session, monkeypatch):
    monkeypatch.setattr("app.routers.panel._CLIENTS_LIST_MAX", 2)
    tenant, service = await _tenant(db_session)
    for i, phone in enumerate(["5491000000001", "5491000000002", "5491000000003"]):
        await _booking(
            db_session, tenant, service, phone, f"C{i}", date(2026, 5, 1 + i)
        )

    resp = await _get(client, tenant, "/panel/clientes")

    assert _client_rows(resp.text) == ["5491000000003", "5491000000002"]
    assert "Usá el buscador" in resp.text
