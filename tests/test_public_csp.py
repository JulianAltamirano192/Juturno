"""
Content-Security-Policy de la página pública /t/{slug} (pendiente del
security-auditor del ítem 41): si algo llegara a inyectarse en la página, no
puede cargar recursos de otros sitios, mandar datos afuera ni ejecutar scripts
que no sean el nuestro.
"""

import re

import pytest

from tests.test_panel_mp_connect import _create_tenant


def _directives(header: str) -> dict[str, str]:
    return {
        name: value.strip()
        for name, _, value in (d.strip().partition(" ") for d in header.split(";"))
        if name
    }


@pytest.mark.asyncio
async def test_public_page_sends_strict_csp(client, db_session):
    tenant = await _create_tenant(db_session, "csp-page")
    res = await client.get(f"/t/{tenant.slug}")
    assert res.status_code == 200
    csp = _directives(res.headers["content-security-policy"])

    assert csp["default-src"] == "'none'"
    # Solo nuestro script, marcado con el nonce de esta respuesta.
    nonce = re.fullmatch(r"'nonce-([A-Za-z0-9_-]{22,})'", csp["script-src"])
    assert nonce, csp["script-src"]
    assert f'<script nonce="{nonce[1]}">' in res.text
    # Nada sale ni entra de otros sitios: ni imágenes, ni fuentes, ni fetch.
    assert csp["img-src"] == "'self'"
    assert csp["font-src"] == "'self'"
    assert csp["connect-src"] == "'self'"
    assert csp["style-src"] == "'self' 'unsafe-inline'"
    assert csp["base-uri"] == "'none'"
    assert csp["form-action"] == "'none'"
    assert csp["frame-ancestors"] == "'none'"
    assert csp["object-src"] == "'none'"


@pytest.mark.asyncio
async def test_csp_nonce_changes_on_every_response(client, db_session):
    tenant = await _create_tenant(db_session, "csp-nonce")
    first = await client.get(f"/t/{tenant.slug}")
    second = await client.get(f"/t/{tenant.slug}")
    assert (
        first.headers["content-security-policy"]
        != second.headers["content-security-policy"]
    )


@pytest.mark.asyncio
async def test_public_page_has_no_inline_event_handlers(client, db_session):
    # Un nonce no habilita onclick="...": tienen que ir por addEventListener.
    tenant = await _create_tenant(db_session, "csp-handlers")
    res = await client.get(f"/t/{tenant.slug}")
    assert not re.search(r"\son[a-z]+\s*=", res.text), re.search(
        r".{40}\son[a-z]+\s*=.{40}", res.text
    )
    assert "<script>" not in res.text  # todo <script> lleva el nonce


@pytest.mark.asyncio
async def test_unknown_slug_page_also_sends_csp(client):
    res = await client.get("/t/no-existe-csp")
    assert res.status_code == 404
    csp = _directives(res.headers["content-security-policy"])
    assert csp["default-src"] == "'none'"
    assert csp["script-src"] == "'none'"
    assert csp["frame-ancestors"] == "'none'"
    assert res.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_public_page_is_never_cached(client, db_session):
    # Una caché compartida (p. ej. "Cache Everything" en Cloudflare) serviría
    # el mismo nonce a todos y haría pública la defensa.
    tenant = await _create_tenant(db_session, "csp-cache")
    res = await client.get(f"/t/{tenant.slug}")
    assert res.headers["cache-control"] == "no-store"
