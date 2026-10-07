"""Tests de la landing pública (/) y sus archivos estáticos."""

import pytest


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
