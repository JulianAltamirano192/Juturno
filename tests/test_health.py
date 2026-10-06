from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_health_ok(client):
    res = await client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["checks"]["api"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"


@pytest.mark.asyncio
async def test_health_redis_down(client, monkeypatch):
    mock_redis = AsyncMock()
    mock_redis.ping.side_effect = ConnectionError("redis unreachable")
    mock_redis.aclose = AsyncMock()

    with patch("app.routers.public.aioredis.from_url", return_value=mock_redis):
        res = await client.get("/health")

    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "ok"
    assert "error" in body["checks"]["redis"]
