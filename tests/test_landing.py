"""Tests de la landing pública (/) y sus archivos estáticos."""

from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).parent.parent / "app" / "templates"


@pytest.mark.asyncio
async def test_landing_renders(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    text = resp.text
    assert "Tus clientes reservan y pagan" in text
    assert 'href="/login"' in text
    assert 'href="/register"' in text
    assert 'id="demo"' in text
    assert "/static/landing/hero-phone.webp" in text


@pytest.mark.asyncio
async def test_landing_static_image_served(client):
    resp = await client.get("/static/landing/hero-phone.webp")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/static/../main.py", "/static/%2e%2e/main.py"])
async def test_static_blocks_path_traversal(client, path):
    resp = await client.get(path)
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "template", ["base.html", "landing.html", "public_booking.html"]
)
def test_templates_use_self_hosted_fonts(template):
    source = (TEMPLATES_DIR / template).read_text()
    assert "fonts.googleapis.com" not in source
    assert "fonts.gstatic.com" not in source
    assert "/static/fonts/fonts.css" in source


@pytest.mark.asyncio
async def test_self_hosted_font_served(client):
    resp = await client.get("/static/fonts/sora-latin.woff2")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "font/woff2"
