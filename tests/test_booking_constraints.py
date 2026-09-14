import pytest
from datetime import datetime, timezone, timedelta
from app.models import Tenant, Service, Booking
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text

@pytest.mark.asyncio
async def test_booking_two_tenants_same_time_no_staff(db_session):
    """Test B1: Dos tenants pueden agendar sin staff a la misma hora sin colisionar."""
    tenant_a = Tenant(name="Tenant A", timezone="UTC")
    tenant_b = Tenant(name="Tenant B", timezone="UTC")
    db_session.add_all([tenant_a, tenant_b])
    await db_session.flush()

    service_a = Service(tenant_id=tenant_a.id, name="Servicio A", duration_minutes=60, price=100.0)
    service_b = Service(tenant_id=tenant_b.id, name="Servicio B", duration_minutes=60, price=100.0)
    db_session.add_all([service_a, service_b])
    await db_session.flush()

    start_time = datetime.now(timezone.utc)
    end_time = start_time + timedelta(hours=1)

    booking_a = Booking(
        tenant_id=tenant_a.id,
        service_id=service_a.id,
        staff_id=None,
        client_name="Cliente A",
        client_phone="111",
        start_time=start_time,
        end_time=end_time,
        price_at_booking=100.0,
        idempotency_key="key_a",
        status="pending"
    )
    db_session.add(booking_a)
    await db_session.commit()

    booking_b = Booking(
        tenant_id=tenant_b.id,
        service_id=service_b.id,
        staff_id=None,
        client_name="Cliente B",
        client_phone="222",
        start_time=start_time,
        end_time=end_time,
        price_at_booking=100.0,
        idempotency_key="key_b",
        status="pending"
    )
    db_session.add(booking_b)
    # Debe pasar sin IntegrityError (ExclusionViolation)
    await db_session.commit()
    
    assert booking_a.id is not None
    assert booking_b.id is not None

@pytest.mark.asyncio
async def test_booking_same_tenant_same_time_no_staff_fails(db_session):
    """Test B2: Mismo tenant no puede agendar dos turnos sin staff a la misma hora."""
    tenant = Tenant(name="Tenant C", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(tenant_id=tenant.id, name="Servicio C", duration_minutes=60, price=100.0)
    db_session.add(service)
    await db_session.flush()

    start_time = datetime.now(timezone.utc)
    end_time = start_time + timedelta(hours=1)

    booking_1 = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        staff_id=None,
        client_name="Cliente 1",
        client_phone="111",
        start_time=start_time,
        end_time=end_time,
        price_at_booking=100.0,
        idempotency_key="key_1",
        status="pending"
    )
    db_session.add(booking_1)
    await db_session.commit()

    booking_2 = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        staff_id=None,
        client_name="Cliente 2",
        client_phone="222",
        start_time=start_time,
        end_time=end_time,
        price_at_booking=100.0,
        idempotency_key="key_2", # Clave de idempotencia distinta para forzar que sea por el constraint de tiempo
        status="pending"
    )
    db_session.add(booking_2)
    
    with pytest.raises(IntegrityError) as exc_info:
        await db_session.commit()
    
    assert "excl_overlapping_bookings" in str(exc_info.value)
