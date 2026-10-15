"""
Panel instalable (Fase 3, ítem 40): manifest + íconos, sin service worker.
"""

import json
import struct

import pytest

MANIFEST_URL = "/static/manifest.webmanifest"


def _png_size(data: bytes) -> tuple[int, int]:
    # Ancho y alto del chunk IHDR, que siempre va primero en un PNG.
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


@pytest.mark.asyncio
async def test_manifest_is_served_with_installable_fields(client):
    res = await client.get(MANIFEST_URL)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/manifest+json")

    manifest = json.loads(res.text)
    assert manifest["name"] == "Juturno"
    assert manifest["short_name"] == "Juturno"
    assert manifest["start_url"] == "/dashboard"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"] == "#08080b"
    assert manifest["background_color"] == "#08080b"

    sizes = {(icon["sizes"], icon.get("purpose", "any")) for icon in manifest["icons"]}
    assert ("192x192", "any") in sizes
    assert ("512x512", "any") in sizes
    assert ("512x512", "maskable") in sizes


@pytest.mark.asyncio
async def test_manifest_icons_exist_with_declared_size(client):
    manifest = json.loads((await client.get(MANIFEST_URL)).text)
    for icon in manifest["icons"]:
        res = await client.get(icon["src"])
        assert res.status_code == 200, icon["src"]
        assert res.headers["content-type"] == icon["type"] == "image/png"
        width, height = _png_size(res.content)
        assert f"{width}x{height}" == icon["sizes"], icon["src"]


@pytest.mark.asyncio
async def test_panel_pages_link_the_manifest(client):
    # base.html lo usan el login, el registro y todo el panel.
    res = await client.get("/login")
    assert res.status_code == 200
    assert f'<link rel="manifest" href="{MANIFEST_URL}">' in res.text
