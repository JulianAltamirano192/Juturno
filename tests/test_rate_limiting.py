"""
Rate limiting integration tests.

Uses a dedicated fixture that enables the limiter from the start of the request,
exercising the real decorator → middleware → exception handler wiring.
"""

import httpx
import pytest
import pytest_asyncio

from app.main import app, limiter
from app.database import get_db
from tests.conftest import TestingSessionLocal


@pytest_asyncio.fixture
async def rate_limited_client():
    """AsyncClient with rate limiting enabled. Resets storage before and after."""

    async def override_get_db():
        async with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # Enable limiter and start with clean storage.
    limiter.enabled = True
    limiter._limiter.storage.reset()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    # Restore
    limiter.enabled = False
    limiter._limiter.storage.reset()


@pytest.mark.asyncio
async def test_login_rate_limit_returns_429(rate_limited_client):
    """POST /login must return 429 after 10 requests per minute from the same IP."""
    data = {"owner_email": "x@x.com", "password": "wrong"}
    # Fire 10 requests — all return 401 (bad creds) but consume the quota.
    for _ in range(10):
        await rate_limited_client.post("/login", data=data)
    # 11th request must be rate-limited.
    response = await rate_limited_client.post("/login", data=data)
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_register_rate_limit_returns_429(rate_limited_client):
    """POST /register must return 429 after 5 requests per minute from the same IP."""
    data = {
        "name": "Test",
        "owner_email": "t@t.com",
        "password": "pass",
        "csrf_token": "dummy",
    }
    for _ in range(5):
        await rate_limited_client.post("/register", data=data)
    response = await rate_limited_client.post("/register", data=data)
    assert response.status_code == 429
