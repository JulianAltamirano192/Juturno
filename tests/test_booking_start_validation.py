"""
Finding #2: booking creation (public and API key) only accepts a start_time the
slot grid would offer, for an active service and staff member.

422 = the time is not bookable; 409 stays reserved for an occupied slot.
"""

from datetime import date, datetime, time, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models import BusinessHours, Service, Staff, Tenant
from tests.test_integration import _auth_headers, _create_api_key
from tests.test_public_endpoints import FAKE_MP_RESULT, MP_PATCH

ENDPOINTS = ["public", "api"]
TOMORROW = date.today() + timedelta(days=1)


async def _setup(db_session, *, tz="UTC", service_active=True, minutes=60):
    tenant = Tenant(name="Negocio Validación", timezone=tz)
    db_session.add(tenant)
    await db_session.flush()
    service = Service(
        tenant_id=tenant.id,
        name="Servicio",
        duration_minutes=minutes,
        price=1000.0,
        is_active=service_active,
    )
    db_session.add(service)
    await db_session.commit()
    raw_key = await _create_api_key(db_session, tenant.id)
    return tenant, service, raw_key


async def _post(client, endpoint, raw_key, tenant, service, start, **extra):
    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Cliente",
        "client_phone": "1134567890",
        "start_time": start.isoformat(),
        "idempotency_key": f"val-{endpoint}-{start.isoformat()}",
        **extra,
    }
    with patch(MP_PATCH, new=AsyncMock(return_value=FAKE_MP_RESULT)):
        if endpoint == "public":
            return await client.post("/public/bookings", json=payload)
        return await client.post(
            "/bookings", json=payload, headers=_auth_headers(raw_key)
        )


def _utc(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=ZoneInfo("UTC"))


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_valid_slot_is_accepted(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session)
    res = await _post(client, endpoint, raw_key, tenant, service, _utc(TOMORROW, 10))
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    "start",
    [
        pytest.param(_utc(date.today() - timedelta(days=1), 10), id="past-day"),
        pytest.param(_utc(TOMORROW, 20), id="after-hours"),
        pytest.param(_utc(TOMORROW, 7), id="before-hours"),
        pytest.param(_utc(TOMORROW, 10, 15), id="off-grid"),
        pytest.param(_utc(TOMORROW, 10, 0, 30), id="seconds"),
        pytest.param(_utc(TOMORROW, 17, 30), id="overflows-window"),
        pytest.param(datetime.fromisoformat("0001-01-01T00:00:00+05:00"), id="year-1"),
        pytest.param(
            datetime.fromisoformat("9999-12-31T23:30:00-05:00"), id="year-9999"
        ),
    ],
)
async def test_unbookable_start_is_rejected(client, db_session, endpoint, start):
    tenant, service, raw_key = await _setup(db_session)
    res = await _post(client, endpoint, raw_key, tenant, service, start)
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_start_is_checked_in_tenant_timezone(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(
        db_session, tz="America/Argentina/Buenos_Aires"
    )
    # 10:00Z is 07:00 in Buenos Aires: before the 09-18 fallback window.
    res = await _post(client, endpoint, raw_key, tenant, service, _utc(TOMORROW, 10))
    assert res.status_code == 422, res.text

    local = datetime.combine(
        TOMORROW, time(10), tzinfo=ZoneInfo("America/Argentina/Buenos_Aires")
    )
    res = await _post(client, endpoint, raw_key, tenant, service, local)
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_closed_day_is_rejected(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session)
    other_day = (TOMORROW.weekday() + 1) % 7
    db_session.add(
        BusinessHours(
            tenant_id=tenant.id,
            day_of_week=other_day,
            start_time=time(9),
            end_time=time(18),
        )
    )
    await db_session.commit()
    res = await _post(client, endpoint, raw_key, tenant, service, _utc(TOMORROW, 10))
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_inactive_service_is_rejected(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session, service_active=False)
    res = await _post(client, endpoint, raw_key, tenant, service, _utc(TOMORROW, 10))
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_inactive_staff_is_rejected(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session)
    staff = Staff(tenant_id=tenant.id, name="Profesional", is_active=False)
    db_session.add(staff)
    await db_session.commit()
    res = await _post(
        client,
        endpoint,
        raw_key,
        tenant,
        service,
        _utc(TOMORROW, 10),
        staff_id=staff.id,
    )
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_occupied_slot_still_returns_409(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session)
    start = _utc(TOMORROW, 10)
    first = await _post(client, endpoint, raw_key, tenant, service, start)
    assert first.status_code == 201, first.text
    res = await _post(
        client,
        endpoint,
        raw_key,
        tenant,
        service,
        start,
        idempotency_key=f"val-{endpoint}-second",
    )
    assert res.status_code == 409, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_staff_own_hours_are_used(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session)
    staff = Staff(tenant_id=tenant.id, name="Profesional")
    db_session.add(staff)
    await db_session.flush()
    db_session.add(
        BusinessHours(
            tenant_id=tenant.id,
            staff_id=staff.id,
            day_of_week=TOMORROW.weekday(),
            start_time=time(14),
            end_time=time(18),
        )
    )
    await db_session.commit()

    # 10:00 is inside the 09-18 fallback but outside the staff's own hours.
    res = await _post(
        client,
        endpoint,
        raw_key,
        tenant,
        service,
        _utc(TOMORROW, 10),
        staff_id=staff.id,
    )
    assert res.status_code == 422, res.text
    res = await _post(
        client,
        endpoint,
        raw_key,
        tenant,
        service,
        _utc(TOMORROW, 14),
        staff_id=staff.id,
    )
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_idempotent_retry_skips_validation(client, db_session, endpoint):
    """A retry of an existing booking returns it even if the service was
    deactivated afterwards: the idempotency lookup runs before validation."""
    tenant, service, raw_key = await _setup(db_session)
    start = _utc(TOMORROW, 10)
    first = await _post(client, endpoint, raw_key, tenant, service, start)
    assert first.status_code == 201, first.text

    service.is_active = False
    db_session.add(service)
    await db_session.commit()

    retry = await _post(client, endpoint, raw_key, tenant, service, start)
    assert retry.status_code == 200, retry.text
    assert retry.json()["booking_id"] == first.json()["booking_id"]
