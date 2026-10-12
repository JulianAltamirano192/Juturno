"""
Errores de formularios del navegador (CSRF vencido, rate limit): página HTML
en vez de JSON crudo, que el celular descarga como archivo.
"""

import pytest

from app.main import limiter
from app.models import Tenant
from app.session import create_session_token

HTML = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


@pytest.mark.asyncio
async def test_logout_with_stale_csrf_offers_fresh_logout_form(client):
    res = await client.post(
        "/logout",
        data={"csrf_token": "a" * 64},
        cookies={"csrf_token": "b" * 64},
        headers=HTML,
        follow_redirects=False,
    )

    assert res.status_code == 403
    assert res.headers["content-type"].startswith("text/html")
    assert 'action="/logout"' in res.text
    # El form nuevo lleva el token vigente de la cookie: confirmar cierra sesión.
    assert 'value="' + "b" * 64 + '"' in res.text

    res2 = await client.post(
        "/logout",
        data={"csrf_token": "b" * 64},
        cookies={"csrf_token": "b" * 64},
        headers=HTML,
        follow_redirects=False,
    )
    assert res2.status_code == 303
    assert res2.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_logout_without_csrf_cookie_sets_one(client):
    res = await client.post(
        "/logout", data={"csrf_token": "x"}, headers=HTML, follow_redirects=False
    )

    assert res.status_code == 403
    token = res.cookies.get("csrf_token")
    assert token and f'value="{token}"' in res.text


@pytest.mark.asyncio
async def test_panel_stale_csrf_renders_html(client, db_session):
    tenant = Tenant(name="Negocio", session_version=1)
    db_session.add(tenant)
    await db_session.commit()
    client.cookies.set(
        "juturno_session", create_session_token(tenant.id, tenant.session_version)
    )

    res = await client.post(
        "/panel/agenda/999/confirm",
        data={"csrf_token": "a" * 64},
        cookies={"csrf_token": "b" * 64},
        headers=HTML,
        follow_redirects=False,
    )

    assert res.status_code == 403
    assert res.headers["content-type"].startswith("text/html")
    assert "recargá" in res.text.lower()


@pytest.mark.asyncio
async def test_csrf_error_stays_json_for_api_clients(client):
    res = await client.post(
        "/logout",
        data={"csrf_token": "a" * 64},
        cookies={"csrf_token": "b" * 64},
        follow_redirects=False,
    )

    assert res.status_code == 403
    assert res.json() == {"detail": "CSRF token invalid"}


@pytest.mark.asyncio
async def test_login_rate_limit_renders_html(client):
    limiter.enabled = True
    limiter._limiter.storage.reset()
    try:
        for _ in range(11):
            res = await client.post(
                "/login",
                data={"owner_email": "x@x.com", "password": "nope"},
                headers=HTML,
            )
    finally:
        limiter.enabled = False
        limiter._limiter.storage.reset()

    assert res.status_code == 429
    assert res.headers["content-type"].startswith("text/html")
    assert "Demasiados intentos" in res.text
    assert 'href="/login"' in res.text
