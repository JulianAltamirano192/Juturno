"""
Logo en el header del panel: Tenant.logo_updated_at marca si el negocio tiene
logo y su versión, así base.html lo muestra sin una consulta extra por página.
"""

import re

import pytest

from app.models import Tenant
from tests.test_business_profile import _image_bytes, _post_logo, _remove_logo
from tests.test_panel_mp_connect import _create_tenant, _make_cookie

_AVATAR_IMG = re.compile(r'<img class="avatar" src="/t/([^/]+)/logo\?v=(\d+)"')


async def _dashboard(client, tenant):
    client.cookies.clear()
    return await client.get(
        "/dashboard", cookies={"juturno_session": _make_cookie(tenant)}
    )


@pytest.mark.asyncio
async def test_panel_header_shows_logo_after_upload(client, db_session):
    tenant = await _create_tenant(db_session, "panel-logo")
    other = await _create_tenant(db_session, "panel-logo-other")
    page = await _dashboard(client, tenant)
    assert not _AVATAR_IMG.search(page.text)
    assert '<span class="avatar">T</span>' in page.text

    await _post_logo(client, tenant, _image_bytes("PNG"))
    await db_session.refresh(tenant)
    assert tenant.logo_updated_at is not None

    page = await _dashboard(client, tenant)
    found = _AVATAR_IMG.findall(page.text)
    # Barra lateral y header del celular.
    assert len(found) == 2
    assert {slug for slug, _ in found} == {tenant.slug}
    # El negocio de al lado sigue con su inicial.
    page = await _dashboard(client, other)
    assert not _AVATAR_IMG.search(page.text)


@pytest.mark.asyncio
async def test_replacing_logo_changes_the_version(client, db_session):
    tenant = await _create_tenant(db_session, "panel-logo-v")
    await _post_logo(client, tenant, _image_bytes("PNG"))
    first = _AVATAR_IMG.search((await _dashboard(client, tenant)).text)[2]
    await _post_logo(client, tenant, _image_bytes("JPEG"))
    second = _AVATAR_IMG.search((await _dashboard(client, tenant)).text)[2]
    assert first != second


@pytest.mark.asyncio
async def test_removing_logo_restores_initial_everywhere(client, db_session):
    tenant = await _create_tenant(db_session, "panel-logo-rm")
    await _post_logo(client, tenant, _image_bytes("PNG"))
    await _remove_logo(client, tenant)
    await db_session.refresh(tenant)
    assert tenant.logo_updated_at is None

    page = await _dashboard(client, tenant)
    assert not _AVATAR_IMG.search(page.text)
    public = await client.get(f"/t/{tenant.slug}")
    assert "/logo?v=" not in public.text


@pytest.mark.asyncio
async def test_public_and_settings_use_the_tenant_version(client, db_session):
    tenant = await _create_tenant(db_session, "panel-logo-pub")
    await _post_logo(client, tenant, _image_bytes("PNG"))
    version = _AVATAR_IMG.search((await _dashboard(client, tenant)).text)[2]

    public = await client.get(f"/t/{tenant.slug}")
    assert f"/t/{tenant.slug}/logo?v={version}" in public.text
    settings = await client.get(
        "/panel/settings", cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert f"/t/{tenant.slug}/logo?v={version}" in settings.text


@pytest.mark.asyncio
async def test_tenant_without_slug_keeps_initial(client, db_session):
    tenant = Tenant(
        name="Sin Slug",
        owner_email="panel-logo-noslug@test.com",
        password_hash="x",
        session_version=1,
    )
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)
    await _post_logo(client, tenant, _image_bytes("PNG"))
    page = await _dashboard(client, tenant)
    assert page.status_code == 200
    assert not _AVATAR_IMG.search(page.text)
    assert '<span class="avatar">S</span>' in page.text
