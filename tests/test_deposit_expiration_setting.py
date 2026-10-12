"""
Deposit deadline in panel settings (roadmap 13b): the business picks how long
clients have to pay the deposit before the booking expires and frees the slot.
Only fixed choices up to 24 h; "no expiration" is not offered (the payment link
dies after 48 h anyway, so the slot would stay blocked with nobody able to pay).
"""

import pytest

from tests.test_panel_mp_connect import _create_tenant, _get_csrf, _make_cookie


async def _post_deadline(client, tenant, value, *, with_csrf=True):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    data = {"deposit_expiration_minutes": value}
    if with_csrf:
        data["csrf_token"] = csrf
    else:
        cookies.pop("csrf_token", None)
    client.cookies.clear()
    return await client.post(
        "/panel/settings/deposit-expiration",
        data=data,
        cookies=cookies,
        follow_redirects=False,
    )


async def _stored_deadline(db_session, tenant) -> int | None:
    await db_session.refresh(tenant)
    return tenant.deposit_expiration_minutes


async def _settings_page(client, tenant) -> str:
    resp = await client.get(
        "/panel/settings", cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert resp.status_code == 200
    return resp.text


@pytest.mark.asyncio
async def test_settings_shows_deadline_select(client, db_session):
    tenant = await _create_tenant(db_session, "dl-show")
    page = await _settings_page(client, tenant)
    assert 'name="deposit_expiration_minutes"' in page
    assert '<option value="15" selected>15 minutos</option>' in page
    assert '<option value="1440">24 horas</option>' in page


@pytest.mark.asyncio
async def test_panel_saves_valid_deadline(client, db_session):
    tenant = await _create_tenant(db_session, "dl-ok")
    other = await _create_tenant(db_session, "dl-other")
    res = await _post_deadline(client, tenant, "60")
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?deposit=saved"
    assert await _stored_deadline(db_session, tenant) == 60
    assert await _stored_deadline(db_session, other) == 15

    page = await client.get(
        res.headers["location"], cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert '<option value="60" selected>1 hora</option>' in page.text
    assert "banner-ok" in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["45", "abc", "", "-15", "60.0", "0"])
async def test_panel_rejects_invalid_deadline(client, db_session, value):
    tenant = await _create_tenant(db_session, f"dl-bad-{value.encode().hex()}")
    res = await _post_deadline(client, tenant, value)
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?deposit=invalid"
    assert await _stored_deadline(db_session, tenant) == 15

    page = await client.get(
        res.headers["location"], cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "banner-danger" in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_value", "label"), [(None, "Sin vencimiento"), (45, "45 minutos")]
)
async def test_deadline_outside_choices_is_shown_not_replaced(
    client, db_session, api_value, label
):
    # Set through PATCH /tenants/me (API key): the select must not pretend it
    # is 15 minutes, and saving without choosing must not change it.
    tenant = await _create_tenant(db_session, f"dl-api-{api_value}")
    tenant.deposit_expiration_minutes = api_value
    db_session.add(tenant)
    await db_session.commit()

    page = await _settings_page(client, tenant)
    assert f'<option value="" selected disabled>{label}</option>' in page

    res = await _post_deadline(client, tenant, "")
    assert res.headers["location"] == "/panel/settings?deposit=invalid"
    assert await _stored_deadline(db_session, tenant) == api_value


@pytest.mark.asyncio
async def test_panel_deadline_requires_csrf(client, db_session):
    tenant = await _create_tenant(db_session, "dl-csrf")
    res = await _post_deadline(client, tenant, "60", with_csrf=False)
    assert res.status_code == 403
    assert await _stored_deadline(db_session, tenant) == 15


@pytest.mark.asyncio
async def test_panel_deadline_requires_session(client):
    res = await client.post(
        "/panel/settings/deposit-expiration",
        data={"deposit_expiration_minutes": "60"},
        follow_redirects=False,
    )
    assert res.status_code in (302, 303, 307)
    assert "/login" in res.headers["location"]
