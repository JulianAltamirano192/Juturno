"""Tests del dashboard (/dashboard): resumen del día, próximos turnos y checklist."""

from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.models import Booking, Service, Tenant
from app.session import create_session_token

TENANT_TZ = "America/Argentina/Buenos_Aires"


def make_session_cookie(tenant):
    return create_session_token(tenant.id, tenant.session_version)


def _tenant(name, slug_prefix):
    return Tenant(
        name=name,
        slug=f"{slug_prefix}-{int(datetime.now().timestamp() * 1000)}",
        owner_email=f"{slug_prefix}@test.com",
        password_hash="x",
        session_version=1,
        timezone=TENANT_TZ,
    )


async def _setup(session):
    """Tenant con un servicio y turnos de hoy en varios estados, más un tenant
    ajeno con un turno hoy (no debe filtrarse al dashboard del primero)."""
    tenant = _tenant("Dashboard Biz", "dash")
    other = _tenant("Otro Biz", "dash-otro")
    session.add_all([tenant, other])
    await session.flush()

    service = Service(
        tenant_id=tenant.id,
        name="Servicio dash",
        duration_minutes=30,
        price=Decimal("1000.00"),
        is_active=True,
    )
    other_service = Service(
        tenant_id=other.id,
        name="Servicio ajeno",
        duration_minutes=30,
        price=Decimal("1000.00"),
        is_active=True,
    )
    session.add_all([service, other_service])
    await session.flush()

    tz = ZoneInfo(TENANT_TZ)
    today = datetime.now(tz).date()

    def _booking(t, svc, start, end, status, client, key):
        return Booking(
            tenant_id=t.id,
            service_id=svc.id,
            client_name=client,
            client_phone="5491100000000",
            start_time=datetime.combine(today, start, tzinfo=tz),
            end_time=datetime.combine(today, end, tzinfo=tz),
            price_at_booking=Decimal("1000.00"),
            status=status,
            idempotency_key=key,
        )

    session.add_all(
        [
            # Termina a las 23:58 → sigue "por venir" durante casi todo el día.
            _booking(
                tenant,
                service,
                time(0, 40),
                time(23, 58),
                "confirmed",
                "Cliente Largo",
                "dash-1",
            ),
            _booking(
                tenant,
                service,
                time(0, 5),
                time(0, 35),
                "pending",
                "Cliente Pendiente",
                "dash-2",
            ),
            _booking(
                tenant,
                service,
                time(1, 0),
                time(1, 30),
                "cancelled",
                "Cliente Cancelado",
                "dash-3",
            ),
            _booking(
                tenant,
                service,
                time(2, 0),
                time(2, 30),
                "completed",
                "Cliente Completo",
                "dash-4",
            ),
            _booking(
                other,
                other_service,
                time(10, 0),
                time(10, 30),
                "confirmed",
                "Cliente Ajeno",
                "dash-5",
            ),
        ]
    )
    await session.commit()
    await session.refresh(tenant)
    return tenant


@pytest.mark.asyncio
async def test_dashboard_today_summary(client, db_session):
    tenant = await _setup(db_session)
    resp = await client.get(
        "/dashboard", cookies={"juturno_session": make_session_cookie(tenant)}
    )

    assert resp.status_code == 200
    text = resp.text
    # Cancelados y expirados no cuentan como turnos del día.
    assert 'data-stat="total">3<' in text
    assert 'data-stat="confirmed">1<' in text
    assert 'data-stat="pending">1<' in text
    assert 'data-stat="completed">1<' in text


@pytest.mark.asyncio
async def test_dashboard_upcoming_is_tenant_scoped(client, db_session):
    tenant = await _setup(db_session)
    resp = await client.get(
        "/dashboard", cookies={"juturno_session": make_session_cookie(tenant)}
    )

    text = resp.text
    assert "Cliente Largo" in text
    assert "Cliente Ajeno" not in text
    assert "Servicio ajeno" not in text
    # Cancelados y completados no son "próximos".
    assert "Cliente Cancelado" not in text
    assert "Cliente Completo" not in text


@pytest.mark.asyncio
async def test_dashboard_setup_checklist_and_link(client, db_session):
    tenant = await _setup(db_session)
    resp = await client.get(
        "/dashboard", cookies={"juturno_session": make_session_cookie(tenant)}
    )

    text = resp.text
    assert 'data-setup="services" data-done="true"' in text
    assert 'data-setup="hours" data-done="false"' in text
    assert 'data-setup="staff" data-done="false"' in text
    assert 'data-setup="mp" data-done="false"' in text
    assert f"/t/{tenant.slug}" in text
