"""
WhatsApp del negocio (aviso de turnos nuevos, T1): el número se normaliza a
'549XXXXXXXXXX' tanto en el registro como en Ajustes; vacío = sin avisos.
"""

import pytest
from sqlalchemy import select

from app.csrf import CSRF_COOKIE_NAME, generate_csrf_token
from app.models import Tenant
from tests.test_panel_mp_connect import _create_tenant, _get_csrf, _make_cookie

INVALID_MSG = "Ese número no parece un celular válido"

# ---------------------------------------------------------------------------
# Registro: POST /register
# ---------------------------------------------------------------------------


async def _register(client, email: str, whatsapp: str):
    csrf = generate_csrf_token()
    return await client.post(
        "/register",
        data={
            "name": "Negocio WhatsApp",
            "owner_email": email,
            "password": "supersecretpassword123",
            "whatsapp_number": whatsapp,
            "csrf_token": csrf,
        },
        cookies={CSRF_COOKIE_NAME: csrf},
        follow_redirects=False,
    )


async def _tenant_by_email(db_session, email: str) -> Tenant | None:
    result = await db_session.execute(select(Tenant).where(Tenant.owner_email == email))
    return result.scalar_one_or_none()


@pytest.mark.asyncio
async def test_register_normalizes_whatsapp(client, db_session):
    res = await _register(client, "wa-ok@test.com", "11 5555 5555")
    assert res.status_code == 303, res.text
    tenant = await _tenant_by_email(db_session, "wa-ok@test.com")
    assert tenant is not None
    assert tenant.whatsapp_number == "5491155555555"


@pytest.mark.asyncio
async def test_register_rejects_invalid_whatsapp(client, db_session):
    res = await _register(client, "wa-bad@test.com", "123")
    assert res.status_code == 400
    assert INVALID_MSG in res.text
    # El formulario conserva lo escrito para que lo corrija.
    assert 'value="123"' in res.text
    assert await _tenant_by_email(db_session, "wa-bad@test.com") is None


@pytest.mark.asyncio
async def test_register_blank_whatsapp_is_null(client, db_session):
    res = await _register(client, "wa-blank@test.com", "   ")
    assert res.status_code == 303, res.text
    tenant = await _tenant_by_email(db_session, "wa-blank@test.com")
    assert tenant is not None
    assert tenant.whatsapp_number is None


# ---------------------------------------------------------------------------
# Panel: POST /panel/settings/whatsapp
# ---------------------------------------------------------------------------


async def _post_whatsapp(client, tenant, value, *, with_csrf=True):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    data = {"whatsapp_number": value}
    if with_csrf:
        data["csrf_token"] = csrf
    else:
        cookies.pop("csrf_token", None)
    client.cookies.clear()
    return await client.post(
        "/panel/settings/whatsapp",
        data=data,
        cookies=cookies,
        follow_redirects=False,
    )


async def _stored(db_session, tenant) -> str | None:
    await db_session.refresh(tenant)
    return tenant.whatsapp_number


async def _tenant_with_number(db_session, suffix: str, number: str | None):
    tenant = await _create_tenant(db_session, suffix)
    tenant.whatsapp_number = number
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


@pytest.mark.asyncio
async def test_panel_saves_normalized_whatsapp(client, db_session):
    tenant = await _tenant_with_number(db_session, "wa-save", None)
    other = await _tenant_with_number(db_session, "wa-other", "5493584166288")

    res = await _post_whatsapp(client, tenant, "+54 9 11 5555-5555")
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?whatsapp=saved"
    assert await _stored(db_session, tenant) == "5491155555555"
    # El número de otro negocio no se toca.
    assert await _stored(db_session, other) == "5493584166288"

    page = await client.get(
        res.headers["location"], cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "banner-ok" in page.text
    assert "Guardamos el WhatsApp" in page.text
    assert 'value="5491155555555"' in page.text


@pytest.mark.asyncio
async def test_panel_blank_whatsapp_removes_number(client, db_session):
    tenant = await _tenant_with_number(db_session, "wa-rm", "5491155555555")
    res = await _post_whatsapp(client, tenant, "  ")
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?whatsapp=removed"
    assert await _stored(db_session, tenant) is None


@pytest.mark.asyncio
async def test_panel_invalid_whatsapp_keeps_number(client, db_session):
    tenant = await _tenant_with_number(db_session, "wa-inv", "5491155555555")
    res = await _post_whatsapp(client, tenant, "123")
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?whatsapp=invalid"
    assert await _stored(db_session, tenant) == "5491155555555"

    page = await client.get(
        res.headers["location"], cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "banner-danger" in page.text
    assert INVALID_MSG in page.text


@pytest.mark.asyncio
async def test_panel_whatsapp_requires_csrf(client, db_session):
    tenant = await _tenant_with_number(db_session, "wa-csrf", "5491155555555")
    res = await _post_whatsapp(client, tenant, "", with_csrf=False)
    assert res.status_code == 403
    assert await _stored(db_session, tenant) == "5491155555555"


@pytest.mark.asyncio
async def test_panel_whatsapp_requires_session(client):
    res = await client.post(
        "/panel/settings/whatsapp",
        data={"whatsapp_number": "1155555555"},
        follow_redirects=False,
    )
    assert res.status_code in (302, 303, 307)
    assert "/login" in res.headers["location"]


@pytest.mark.asyncio
async def test_settings_shows_whatsapp_form(client, db_session):
    tenant = await _tenant_with_number(db_session, "wa-show", "5493584166288")
    res = await client.get(
        "/panel/settings", cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert res.status_code == 200
    assert 'action="/panel/settings/whatsapp"' in res.text
    assert 'name="whatsapp_number"' in res.text
    assert 'value="5493584166288"' in res.text
