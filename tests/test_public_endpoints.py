import pytest
from datetime import date, timedelta
from app.models import Tenant, Service, Booking


@pytest.mark.asyncio
async def test_get_public_tenant_detail_by_slug(client, db_session):
    """Test: Obtener detalle público del tenant y sus servicios por slug (sin API Key)."""
    tenant = Tenant(
        name="Barbería Club",
        slug="barberia-club",
        timezone="America/Argentina/Buenos_Aires",
    )
    db_session.add(tenant)
    await db_session.flush()

    service1 = Service(
        tenant_id=tenant.id,
        name="Corte de Pelo",
        duration_minutes=30,
        price=4000.0,
        is_active=True,
    )
    service2 = Service(
        tenant_id=tenant.id,
        name="Corte Inactivo",
        duration_minutes=30,
        price=2000.0,
        is_active=False,
    )
    db_session.add_all([service1, service2])
    await db_session.commit()

    res = await client.get("/public/tenants/barberia-club")
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "Barbería Club"
    assert data["slug"] == "barberia-club"
    assert len(data["services"]) == 1
    assert data["services"][0]["name"] == "Corte de Pelo"
    assert data["services"][0]["price"] == 4000.0


@pytest.mark.asyncio
async def test_get_public_tenant_detail_by_id(client, db_session):
    """Test: Obtener detalle público del tenant por ID (sin API Key)."""
    tenant = Tenant(name="Peluquería Express", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id,
        name="Peinado",
        duration_minutes=60,
        price=3500.0,
        is_active=True,
    )
    db_session.add(service)
    await db_session.commit()

    res = await client.get(f"/public/tenants/{tenant.id}")
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "Peluquería Express"
    assert len(data["services"]) == 1


@pytest.mark.asyncio
async def test_get_public_available_slots(client, db_session):
    """Test: Obtener slots disponibles por día en endpoint público (sin API Key)."""
    tenant = Tenant(name="Consultorio Dental", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Limpieza", duration_minutes=30, price=10000.0
    )
    db_session.add(service)
    await db_session.commit()

    tomorrow = date.today() + timedelta(days=1)

    res = await client.get(
        f"/public/available-slots?tenant_id={tenant.id}&service_id={service.id}&day={tomorrow}"
    )
    assert res.status_code == 200
    data = res.json()
    assert "slots" in data
    assert "09:00" in data["slots"]


@pytest.mark.asyncio
async def test_create_public_booking_pending(client, db_session):
    """Test: Crear booking público (sin API Key) nace en estado 'pending'."""
    tenant = Tenant(name="Spa Relax", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Masaje Facial", duration_minutes=45, price=6000.0
    )
    db_session.add(service)
    await db_session.commit()

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Valeria",
        "client_phone": "5491188776655",
        "start_time": "2026-11-15T15:00:00Z",
        "idempotency_key": "pub-booking-key-01",
    }

    res = await client.post("/public/bookings", json=payload)
    assert res.status_code == 201
    booking_id = res.json()["booking_id"]

    booking = await db_session.get(Booking, booking_id)
    assert booking is not None
    assert booking.status == "pending"
    assert booking.client_name == "Valeria"
    assert booking.end_time.isoformat().startswith("2026-11-15T15:45:00")
