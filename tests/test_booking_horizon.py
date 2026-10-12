"""
Booking horizon (roadmap 2b.5): each business picks how far ahead clients can
book (15/30/60/90 days, default 90). Day `today + horizon` is still bookable,
`today + horizon + 1` is not: no slots are offered and bookings get 422.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import Tenant
from tests.test_booking_start_validation import ENDPOINTS, _post, _setup, _utc
from tests.test_integration import _auth_headers
from tests.test_panel_mp_connect import _create_tenant, _get_csrf, _make_cookie

HORIZON = 15


def _today_utc() -> date:
    # _setup creates the tenant in UTC.
    return datetime.now(timezone.utc).date()


async def _setup_horizon(db_session, horizon=HORIZON):
    tenant, service, raw_key = await _setup(db_session)
    tenant.booking_horizon_days = horizon
    db_session.add(tenant)
    await db_session.commit()
    return tenant, service, raw_key


async def _slots(client, endpoint, raw_key, tenant, service, day: date):
    params = {"tenant_id": tenant.id, "service_id": service.id, "day": str(day)}
    if endpoint == "public":
        return await client.get("/public/available-slots", params=params)
    return await client.get(
        "/bookings/available-slots", params=params, headers=_auth_headers(raw_key)
    )


@pytest.mark.asyncio
async def test_new_tenant_defaults_to_90_days(db_session):
    tenant = Tenant(name="Negocio Default", timezone="UTC")
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)
    assert tenant.booking_horizon_days == 90


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_slots_offered_up_to_horizon(client, db_session, endpoint):
    tenant, service, raw_key = await _setup_horizon(db_session)
    last_day = _today_utc() + timedelta(days=HORIZON)

    res = await _slots(client, endpoint, raw_key, tenant, service, last_day)
    assert res.status_code == 200, res.text
    assert "10:00" in res.json()["slots"]

    res = await _slots(
        client, endpoint, raw_key, tenant, service, last_day + timedelta(days=1)
    )
    assert res.status_code == 200, res.text
    assert res.json()["slots"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_booking_within_horizon_is_accepted(client, db_session, endpoint):
    tenant, service, raw_key = await _setup_horizon(db_session)
    start = _utc(_today_utc() + timedelta(days=HORIZON), 10)
    res = await _post(client, endpoint, raw_key, tenant, service, start)
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_booking_beyond_horizon_is_rejected(client, db_session, endpoint):
    tenant, service, raw_key = await _setup_horizon(db_session)
    start = _utc(_today_utc() + timedelta(days=HORIZON + 1), 10)
    res = await _post(client, endpoint, raw_key, tenant, service, start)
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_default_horizon_rejects_day_91(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session)
    start = _utc(_today_utc() + timedelta(days=91), 10)
    res = await _post(client, endpoint, raw_key, tenant, service, start)
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_horizon_counts_days_in_tenant_timezone(client, db_session, endpoint):
    """UTC+14: most of the day the tenant's date is already UTC's tomorrow."""
    tz = ZoneInfo("Pacific/Kiritimati")
    tenant, service, raw_key = await _setup(db_session, tz=tz.key)
    tenant.booking_horizon_days = HORIZON
    db_session.add(tenant)
    await db_session.commit()
    last_day = datetime.now(tz).date() + timedelta(days=HORIZON)

    start = datetime.combine(last_day, time(10), tzinfo=tz)
    res = await _post(client, endpoint, raw_key, tenant, service, start)
    assert res.status_code == 201, res.text

    res = await _post(
        client, endpoint, raw_key, tenant, service, start + timedelta(days=1)
    )
    assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# Panel: POST /panel/settings/booking-horizon
# ---------------------------------------------------------------------------


async def _post_horizon(client, db_session, tenant, value, *, with_csrf=True):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    data = {"booking_horizon_days": value}
    if with_csrf:
        data["csrf_token"] = csrf
    else:
        cookies.pop("csrf_token", None)
    client.cookies.clear()
    return await client.post(
        "/panel/settings/booking-horizon",
        data=data,
        cookies=cookies,
        follow_redirects=False,
    )


async def _stored_horizon(db_session, tenant) -> int:
    await db_session.refresh(tenant)
    return tenant.booking_horizon_days


@pytest.mark.asyncio
async def test_settings_shows_horizon_select(client, db_session):
    tenant = await _create_tenant(db_session, "hz-show")
    resp = await client.get(
        "/panel/settings", cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert resp.status_code == 200
    assert 'name="booking_horizon_days"' in resp.text
    assert '<option value="90" selected' in resp.text


@pytest.mark.asyncio
async def test_panel_saves_valid_horizon(client, db_session):
    tenant = await _create_tenant(db_session, "hz-ok")
    res = await _post_horizon(client, db_session, tenant, "30")
    assert res.status_code == 303, res.text
    assert res.headers["location"].startswith("/panel/settings?")
    assert await _stored_horizon(db_session, tenant) == 30

    page = await client.get(
        res.headers["location"], cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert '<option value="30" selected' in page.text
    assert "banner-ok" in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["45", "abc", "", "-15", "90.0"])
async def test_panel_rejects_invalid_horizon(client, db_session, value):
    tenant = await _create_tenant(db_session, f"hz-bad-{value.encode().hex()}")
    res = await _post_horizon(client, db_session, tenant, value)
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?horizon=invalid"
    assert await _stored_horizon(db_session, tenant) == 90

    page = await client.get(
        res.headers["location"], cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "banner-danger" in page.text


@pytest.mark.asyncio
async def test_panel_horizon_requires_csrf(client, db_session):
    tenant = await _create_tenant(db_session, "hz-csrf")
    res = await _post_horizon(client, db_session, tenant, "30", with_csrf=False)
    assert res.status_code == 403
    assert await _stored_horizon(db_session, tenant) == 90


@pytest.mark.asyncio
async def test_panel_horizon_requires_session(client, db_session):
    res = await client.post(
        "/panel/settings/booking-horizon",
        data={"booking_horizon_days": "30"},
        follow_redirects=False,
    )
    assert res.status_code in (302, 303, 307)
    assert "/login" in res.headers["location"]
