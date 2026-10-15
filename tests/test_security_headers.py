"""
Headers de seguridad para todas las respuestas (D-039, MEDIA del
security-auditor): ninguna página se puede meter en un iframe ajeno
(clickjacking sobre el panel) y el navegador no adivina tipos de archivo.
Las respuestas con CSP propia (/t/{slug}, el logo) la conservan.
"""

from datetime import datetime, timezone

import pytest

from app.models import TenantLogo
from tests.test_panel_mp_connect import _create_tenant, _make_cookie

BASE_CSP = "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"


def _assert_base_headers(res):
    assert res.headers["content-security-policy"] == BASE_CSP, res.headers
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/", "/login", "/register", "/health"])
@pytest.mark.asyncio
async def test_public_routes_send_base_headers(client, path):
    client.cookies.clear()
    res = await client.get(path)
    assert res.status_code == 200, res.text
    _assert_base_headers(res)


@pytest.mark.parametrize("path", ["/dashboard", "/panel/settings", "/panel/agenda"])
@pytest.mark.asyncio
async def test_panel_pages_cannot_be_framed(client, db_session, path):
    tenant = await _create_tenant(db_session, "headers-panel")
    client.cookies.clear()
    res = await client.get(path, cookies={"juturno_session": _make_cookie(tenant)})
    assert res.status_code == 200, res.text
    _assert_base_headers(res)


@pytest.mark.asyncio
async def test_redirects_and_errors_also_send_headers(client):
    client.cookies.clear()
    # Sin sesión, el panel redirige al login.
    res = await client.get("/panel/settings", follow_redirects=False)
    assert res.status_code in (302, 303)
    _assert_base_headers(res)
    _assert_base_headers(await client.get("/no-existe-esta-ruta"))


@pytest.mark.asyncio
async def test_public_booking_page_keeps_its_strict_csp(client, db_session):
    tenant = await _create_tenant(db_session, "headers-public")
    res = await client.get(f"/t/{tenant.slug}")
    csp = res.headers["content-security-policy"]
    assert "script-src 'nonce-" in csp
    assert "default-src 'none'" in csp
    # Un solo header CSP (setdefault no agrega un segundo).
    assert len(res.headers.get_list("content-security-policy")) == 1
    assert res.headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_static_files_keep_a_correct_type_under_nosniff(client):
    # Con nosniff, un .js con tipo equivocado no se ejecuta: el QR del panel
    # (item 29) dejaría de funcionar.
    res = await client.get("/static/vendor/qrcode-1.4.4.js")
    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]
    assert res.headers["x-content-type-options"] == "nosniff"
    css = await client.get("/static/fonts/fonts.css")
    assert css.headers["content-type"].startswith("text/css")


@pytest.mark.asyncio
async def test_unknown_slug_page_keeps_its_own_csp(client):
    res = await client.get("/t/no-existe-headers")
    assert res.status_code == 404
    assert "script-src 'none'" in res.headers["content-security-policy"]
    assert len(res.headers.get_list("content-security-policy")) == 1
    assert res.headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_logo_keeps_sandbox_csp_and_gets_frame_options(client, db_session):
    tenant = await _create_tenant(db_session, "headers-logo")
    db_session.add(
        TenantLogo(
            tenant_id=tenant.id, data=b"webp", updated_at=datetime.now(timezone.utc)
        )
    )
    await db_session.commit()
    res = await client.get(f"/t/{tenant.slug}/logo")
    assert res.status_code == 200
    assert res.headers["content-security-policy"] == (
        "default-src 'none'; sandbox; frame-ancestors 'none'"
    )
    assert res.headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_cors_preflight_also_gets_headers(client):
    # Fija el orden: este middleware va por fuera de CORSMiddleware.
    res = await client.options(
        "/public/bookings",
        headers={
            "Origin": "https://ejemplo.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    _assert_base_headers(res)
