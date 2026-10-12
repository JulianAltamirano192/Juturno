"""Los slots de un servicio o staff inactivo (o ajeno) dan 404, igual que la
reserva: antes se ofrecían horarios que después no se podían reservar."""

from datetime import date, timedelta

import pytest

from app.models import Service, Staff, Tenant
from tests.test_integration import _auth_headers, _create_api_key

TOMORROW = date.today() + timedelta(days=1)


async def _setup(db_session, service_active=True, staff_active=True):
    tenant = Tenant(name="Negocio", timezone="UTC")
    other = Tenant(name="Otro", timezone="UTC")
    db_session.add_all([tenant, other])
    await db_session.flush()
    service = Service(
        tenant_id=tenant.id,
        name="Corte",
        duration_minutes=30,
        price=100,
        is_active=service_active,
    )
    staff = Staff(tenant_id=tenant.id, name="Ana", is_active=staff_active)
    foreign_staff = Staff(tenant_id=other.id, name="Beto")
    db_session.add_all([service, staff, foreign_staff])
    await db_session.commit()
    return tenant, service, staff, foreign_staff


def _url(prefix, tenant, service, staff_id=None):
    url = f"{prefix}?tenant_id={tenant.id}&service_id={service.id}&day={TOMORROW}"
    return url + (f"&staff_id={staff_id}" if staff_id else "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["service_inactive", "staff_inactive", "staff_foreign"]
)
async def test_public_slots_404(client, db_session, case):
    tenant, service, staff, foreign = await _setup(
        db_session,
        service_active=case != "service_inactive",
        staff_active=case != "staff_inactive",
    )
    staff_id = foreign.id if case == "staff_foreign" else staff.id

    res = await client.get(_url("/public/available-slots", tenant, service, staff_id))

    assert res.status_code == 404


@pytest.mark.asyncio
async def test_public_slots_active_staff_ok(client, db_session):
    tenant, service, staff, _ = await _setup(db_session)

    res = await client.get(_url("/public/available-slots", tenant, service, staff.id))

    assert res.status_code == 200
    assert res.json()["slots"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["service_inactive", "staff_inactive", "staff_foreign"]
)
async def test_api_slots_404(client, db_session, case):
    tenant, service, staff, foreign = await _setup(
        db_session,
        service_active=case != "service_inactive",
        staff_active=case != "staff_inactive",
    )
    key = await _create_api_key(db_session, tenant.id)
    staff_id = foreign.id if case == "staff_foreign" else staff.id

    res = await client.get(
        _url("/bookings/available-slots", tenant, service, staff_id),
        headers=_auth_headers(key),
    )

    assert res.status_code == 404
