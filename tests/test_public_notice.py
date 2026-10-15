"""
Aviso destacado del negocio (Fase 3, ítem 39): texto corto que el dueño edita
en Ajustes y que la página pública /t/{slug} muestra escapado; vacío = sin aviso.
"""

import pytest

from app.models import PUBLIC_NOTICE_MAX
from tests.test_panel_mp_connect import _create_tenant, _get_csrf, _make_cookie

# ---------------------------------------------------------------------------
# Panel: POST /panel/settings/notice
# ---------------------------------------------------------------------------


async def _post_notice(client, tenant, value, *, with_csrf=True):
    cookie = _make_cookie(tenant)
    csrf, cookies = await _get_csrf(client, cookie)
    data = {"public_notice": value}
    if with_csrf:
        data["csrf_token"] = csrf
    else:
        cookies.pop("csrf_token", None)
    client.cookies.clear()
    return await client.post(
        "/panel/settings/notice",
        data=data,
        cookies=cookies,
        follow_redirects=False,
    )


async def _tenant_with_notice(db_session, suffix: str, notice: str | None):
    tenant = await _create_tenant(db_session, suffix)
    tenant.public_notice = notice
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


async def _stored(db_session, tenant) -> str | None:
    await db_session.refresh(tenant)
    return tenant.public_notice


@pytest.mark.asyncio
async def test_panel_saves_notice(client, db_session):
    tenant = await _tenant_with_notice(db_session, "notice-save", None)
    other = await _tenant_with_notice(db_session, "notice-other", "Aviso ajeno")

    # El textarea manda CRLF: se guarda con \n y sin espacios en los bordes.
    res = await _post_notice(
        client, tenant, "  Cerramos el lunes.\r\nVolvemos el martes.  "
    )
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?notice=saved"
    assert (
        await _stored(db_session, tenant) == "Cerramos el lunes.\nVolvemos el martes."
    )
    # El aviso de otro negocio no se toca.
    assert await _stored(db_session, other) == "Aviso ajeno"

    page = await client.get(
        res.headers["location"], cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "banner-ok" in page.text
    assert "Guardamos el aviso" in page.text
    assert "Cerramos el lunes.\nVolvemos el martes.</textarea>" in page.text
    assert f'maxlength="{PUBLIC_NOTICE_MAX}"' in page.text


@pytest.mark.asyncio
async def test_panel_blank_notice_removes_it(client, db_session):
    tenant = await _tenant_with_notice(db_session, "notice-rm", "Aviso viejo")
    res = await _post_notice(client, tenant, "  \r\n ")
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?notice=removed"
    assert await _stored(db_session, tenant) is None


@pytest.mark.asyncio
async def test_panel_notice_max_length_is_accepted(client, db_session):
    tenant = await _tenant_with_notice(db_session, "notice-max", None)
    res = await _post_notice(client, tenant, "a" * PUBLIC_NOTICE_MAX)
    assert res.headers["location"] == "/panel/settings?notice=saved"
    assert await _stored(db_session, tenant) == "a" * PUBLIC_NOTICE_MAX


@pytest.mark.asyncio
async def test_panel_too_long_notice_keeps_previous(client, db_session):
    tenant = await _tenant_with_notice(db_session, "notice-long", "Aviso viejo")
    res = await _post_notice(client, tenant, "a" * (PUBLIC_NOTICE_MAX + 1))
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/panel/settings?notice=too_long"
    assert await _stored(db_session, tenant) == "Aviso viejo"

    page = await client.get(
        res.headers["location"], cookies={"juturno_session": _make_cookie(tenant)}
    )
    assert "banner-danger" in page.text
    assert f"hasta {PUBLIC_NOTICE_MAX} caracteres" in page.text


@pytest.mark.asyncio
async def test_panel_notice_with_control_chars_is_rejected(client, db_session):
    tenant = await _tenant_with_notice(db_session, "notice-nul", "Aviso viejo")
    # NUL haría fallar el INSERT en Postgres (500).
    res = await _post_notice(client, tenant, "Hola\x00chau")
    assert res.headers["location"] == "/panel/settings?notice=invalid"
    assert await _stored(db_session, tenant) == "Aviso viejo"


@pytest.mark.asyncio
async def test_panel_notice_requires_csrf(client, db_session):
    tenant = await _tenant_with_notice(db_session, "notice-csrf", None)
    res = await _post_notice(client, tenant, "Aviso", with_csrf=False)
    assert res.status_code == 403
    assert await _stored(db_session, tenant) is None


@pytest.mark.asyncio
async def test_panel_notice_requires_session(client, db_session):
    tenant = await _tenant_with_notice(db_session, "notice-nosess", None)
    res = await client.post(
        "/panel/settings/notice",
        data={"public_notice": "Aviso"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/login")
    assert await _stored(db_session, tenant) is None


# ---------------------------------------------------------------------------
# Página pública: GET /t/{slug}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_page_shows_escaped_notice(client, db_session):
    tenant = await _tenant_with_notice(
        db_session, "notice-pub", "Feriado <script>alert(1)</script>\nAbrimos el martes"
    )
    res = await client.get(f"/t/{tenant.slug}")
    assert res.status_code == 200
    assert 'class="notice"' in res.text
    # El prefijo deja claro que el aviso es del negocio, no de Juturno.
    assert "<strong>Aviso de Test Negocio notice-pub:</strong> Feriado" in res.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in res.text
    assert "<script>alert(1)</script>" not in res.text
    assert "Abrimos el martes" in res.text


@pytest.mark.asyncio
async def test_public_page_without_notice_has_no_banner(client, db_session):
    tenant = await _tenant_with_notice(db_session, "notice-none", None)
    res = await client.get(f"/t/{tenant.slug}")
    assert res.status_code == 200
    assert 'class="notice"' not in res.text
