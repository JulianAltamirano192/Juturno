"""Tests de integración para el CRUD de servicios del panel."""

import pytest
from httpx import AsyncClient, ASGITransport
from decimal import Decimal

from app.main import app
from app.models import Tenant, Service
from app.session import create_session_token
from app.csrf import generate_csrf_token
from tests.conftest import TestingSessionLocal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_session_cookie(tenant: Tenant) -> str:
    return create_session_token(tenant.id, tenant.session_version)


async def make_tenant_with_service(session) -> tuple[Tenant, Service]:
    tenant = Tenant(
        name="Test Biz",
        slug="test-biz-svc",
        owner_email="svc@test.com",
        password_hash="x",
        session_version=1,
    )
    session.add(tenant)
    await session.flush()

    service = Service(
        tenant_id=tenant.id,
        name="Corte",
        duration_minutes=30,
        price=Decimal("1000.00"),
        deposit_amount=None,
        is_active=True,
    )
    session.add(service)
    await session.commit()
    await session.refresh(tenant)
    await session.refresh(service)
    return tenant, service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_services_list_requires_session():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/panel/services", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_services_list_with_valid_session():
    async with TestingSessionLocal() as session:
        tenant, service = await make_tenant_with_service(session)

    cookie = make_session_cookie(tenant)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        resp = await client.get("/panel/services")
    assert resp.status_code == 200
    assert "Corte" in resp.text
    assert "Servicios" in resp.text


@pytest.mark.asyncio
async def test_create_service_valid():
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="Biz Create",
            slug="biz-create-svc",
            owner_email="create@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            "/panel/services/new",
            data={
                "name": "Tatuaje chico",
                "duration_minutes": "60",
                "price": "8000.00",
                "deposit_amount": "",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/panel/services"


@pytest.mark.asyncio
async def test_create_service_missing_price_shows_error():
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="Biz Error",
            slug="biz-error-svc",
            owner_email="error@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            "/panel/services/new",
            data={
                "name": "Algo",
                "duration_minutes": "30",
                "price": "",
                "deposit_amount": "",
                "csrf_token": csrf,
            },
        )
    assert resp.status_code == 200
    assert "El precio debe ser" in resp.text


@pytest.mark.asyncio
async def test_create_service_no_deposit_stores_none():
    """deposit_amount=None en DB → effective_deposit devuelve 30%."""
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="Biz None Dep",
            slug="biz-none-dep",
            owner_email="nonedep@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        await client.post(
            "/panel/services/new",
            data={
                "name": "Depilación",
                "duration_minutes": "45",
                "price": "2000.00",
                "deposit_amount": "",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )

    # Verificar en DB
    async with TestingSessionLocal() as session:
        from sqlalchemy import select

        stmt = select(Service).where(Service.name == "Depilación")
        svc = (await session.execute(stmt)).scalar_one_or_none()
    assert svc is not None
    assert svc.deposit_amount is None

    from app.services import effective_deposit

    assert effective_deposit(svc.price, svc.deposit_amount) == Decimal("600.00")


@pytest.mark.asyncio
async def test_edit_service():
    async with TestingSessionLocal() as session:
        tenant, service = await make_tenant_with_service(session)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/services/{service.id}/edit",
            data={
                "name": "Corte actualizado",
                "duration_minutes": "45",
                "price": "1200.00",
                "deposit_amount": "400.00",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303

    async with TestingSessionLocal() as session:
        updated = await session.get(Service, service.id)
    assert updated.name == "Corte actualizado"
    assert updated.price == Decimal("1200.00")
    assert updated.deposit_amount == Decimal("400.00")


@pytest.mark.asyncio
async def test_toggle_service():
    async with TestingSessionLocal() as session:
        tenant, service = await make_tenant_with_service(session)
    assert service.is_active is True

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/services/{service.id}/toggle",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
    assert resp.status_code == 303

    async with TestingSessionLocal() as session:
        toggled = await session.get(Service, service.id)
    assert toggled.is_active is False


@pytest.mark.asyncio
async def test_edit_service_of_other_tenant_returns_404():
    async with TestingSessionLocal() as session:
        tenant_a = Tenant(
            name="Biz A",
            slug="biz-a-404",
            owner_email="a404@test.com",
            password_hash="x",
            session_version=1,
        )
        tenant_b = Tenant(
            name="Biz B",
            slug="biz-b-404",
            owner_email="b404@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant_a)
        session.add(tenant_b)
        await session.flush()
        service_b = Service(
            tenant_id=tenant_b.id,
            name="Servicio de B",
            duration_minutes=30,
            price=Decimal("500.00"),
            is_active=True,
        )
        session.add(service_b)
        await session.commit()
        await session.refresh(tenant_a)
        await session.refresh(service_b)

    # A intenta editar el servicio de B
    cookie = make_session_cookie(tenant_a)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/services/{service_b.id}/edit",
            data={
                "name": "Hack",
                "duration_minutes": "10",
                "price": "1.00",
                "deposit_amount": "",
                "csrf_token": csrf,
            },
        )
    assert resp.status_code == 404
