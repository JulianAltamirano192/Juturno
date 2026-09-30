"""Tests de integración para el CRUD de personal del panel."""

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import Tenant, Staff
from app.session import create_session_token
from app.csrf import generate_csrf_token
from tests.conftest import TestingSessionLocal


def make_session_cookie(tenant):
    return create_session_token(tenant.id, tenant.session_version)


async def make_tenant_with_staff(session):
    tenant = Tenant(
        name="Test Biz Staff",
        slug=f"test-staff-{int(__import__('time').time() * 1000)}",
        owner_email="staff@test.com",
        password_hash="x",
        session_version=1,
    )
    session.add(tenant)
    await session.flush()
    staff = Staff(
        tenant_id=tenant.id,
        name="Juan Pérez",
        is_active=True,
    )
    session.add(staff)
    await session.commit()
    await session.refresh(tenant)
    await session.refresh(staff)
    return tenant, staff


@pytest.mark.asyncio
async def test_staff_list_requires_session():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/panel/staff", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_staff_list_with_valid_session():
    async with TestingSessionLocal() as session:
        tenant, staff = await make_tenant_with_staff(session)

    cookie = make_session_cookie(tenant)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        resp = await client.get("/panel/staff")
    assert resp.status_code == 200
    assert "Juan Pérez" in resp.text
    assert "Personal" in resp.text


@pytest.mark.asyncio
async def test_create_staff_valid():
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="Biz Create Staff",
            slug=f"biz-staff-{int(__import__('time').time() * 1000)}",
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
            "/panel/staff/new",
            data={
                "name": "María González",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/panel/staff"

    # Verificar en DB
    async with TestingSessionLocal() as session:
        from sqlalchemy import select

        stmt = select(Staff).where(Staff.name == "María González")
        new_staff = (await session.execute(stmt)).scalar_one_or_none()
        assert new_staff is not None
        assert new_staff.name == "María González"
        assert new_staff.is_active is True


@pytest.mark.asyncio
async def test_create_staff_missing_name_shows_error():
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name="Biz Error Staff",
            slug=f"biz-error-{int(__import__('time').time() * 1000)}",
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
            "/panel/staff/new",
            data={
                "name": "",
                "csrf_token": csrf,
            },
        )
    assert resp.status_code == 200
    assert "El nombre es obligatorio" in resp.text


@pytest.mark.asyncio
async def test_edit_staff():
    async with TestingSessionLocal() as session:
        tenant, staff = await make_tenant_with_staff(session)

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/staff/{staff.id}/edit",
            data={
                "name": "Juan Actualizado",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/panel/staff"

    async with TestingSessionLocal() as session:
        updated = await session.get(Staff, staff.id)
    assert updated.name == "Juan Actualizado"


@pytest.mark.asyncio
async def test_toggle_staff():
    async with TestingSessionLocal() as session:
        tenant, staff = await make_tenant_with_staff(session)
    assert staff.is_active is True

    cookie = make_session_cookie(tenant)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/staff/{staff.id}/toggle",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
    assert resp.status_code == 303

    async with TestingSessionLocal() as session:
        toggled = await session.get(Staff, staff.id)
    assert toggled.is_active is False

    # Toggle back
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/staff/{staff.id}/toggle",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
    assert resp.status_code == 303

    async with TestingSessionLocal() as session:
        toggled2 = await session.get(Staff, staff.id)
    assert toggled2.is_active is True


@pytest.mark.asyncio
async def test_edit_staff_of_other_tenant_returns_404():
    async with TestingSessionLocal() as session:
        tenant_a = Tenant(
            name="Biz A Staff",
            slug="biz-a-staff-404",
            owner_email="a404@test.com",
            password_hash="x",
            session_version=1,
        )
        tenant_b = Tenant(
            name="Biz B Staff",
            slug="biz-b-staff-404",
            owner_email="b404@test.com",
            password_hash="x",
            session_version=1,
        )
        session.add(tenant_a)
        session.add(tenant_b)
        await session.flush()
        staff_b = Staff(
            tenant_id=tenant_b.id,
            name="Staff de B",
            is_active=True,
        )
        session.add(staff_b)
        await session.commit()
        await session.refresh(tenant_a)
        await session.refresh(staff_b)

    # A intenta editar el staff de B
    cookie = make_session_cookie(tenant_a)
    csrf = generate_csrf_token()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("juturno_session", cookie)
        client.cookies.set("csrf_token", csrf)
        resp = await client.post(
            f"/panel/staff/{staff_b.id}/edit",
            data={
                "name": "Hack",
                "csrf_token": csrf,
            },
        )
    assert resp.status_code == 404
