from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import Booking, Payment, Service, Tenant

# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

FAKE_MP_RESULT = {
    "preference_id": "fake-pref-id-123",
    "init_point": "https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=fake-pref-id-123",
    "sandbox_init_point": "https://sandbox.mercadopago.com.ar/checkout/v1/redirect?pref_id=fake-pref-id-123",
    # Contrato real de create_mp_preference con MP_SANDBOX=true (por defecto)
    "checkout_url": "https://sandbox.mercadopago.com.ar/checkout/v1/redirect?pref_id=fake-pref-id-123",
}

MP_PATCH = "app.routers.public.create_mp_preference"

# Bookings must be in the future: never hardcode a date that will expire.
FUTURE_DAY = date.today() + timedelta(days=7)


# ---------------------------------------------------------------------------
# Tenant detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_public_tenant_detail_by_slug(client, db_session):
    """Obtener detalle público del tenant y sus servicios por slug (sin API Key)."""
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
    """Obtener detalle público del tenant por ID numérico (sin API Key)."""
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


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_public_available_slots(client, db_session):
    """Obtener slots disponibles por día en endpoint público (sin API Key)."""
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


# ---------------------------------------------------------------------------
# Public bookings — MP integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_public_booking_returns_payment_url(client, db_session):
    """
    Al crear un booking público exitoso debe nacer en 'pending',
    crear un Payment con mp_preference_id, y devolver payment_url.
    """
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
        "start_time": f"{FUTURE_DAY}T15:00:00Z",
        "idempotency_key": "pub-booking-mp-01",
    }

    with patch(MP_PATCH, new=AsyncMock(return_value=FAKE_MP_RESULT)):
        res = await client.post("/public/bookings", json=payload)

    assert res.status_code == 201
    data = res.json()
    assert "booking_id" in data
    assert data["payment_url"] == FAKE_MP_RESULT["checkout_url"]

    booking_id = data["booking_id"]
    booking = await db_session.get(Booking, booking_id)
    assert booking is not None
    assert booking.status == "pending"
    assert booking.client_name == "Valeria"
    assert booking.end_time.isoformat().startswith(f"{FUTURE_DAY}T15:45:00")

    # Verificar Payment creado
    stmt = select(Payment).where(Payment.booking_id == booking_id)
    payment = (await db_session.execute(stmt)).scalar_one_or_none()
    assert payment is not None
    assert payment.method == "mercado_pago"
    assert payment.status == "pending"
    assert payment.mp_preference_id == FAKE_MP_RESULT["preference_id"]
    assert payment.mp_checkout_url == FAKE_MP_RESULT["checkout_url"]
    assert float(payment.amount) == round(6000.0 * 0.30, 2)


@pytest.mark.asyncio
async def test_create_public_booking_deposit_amount_explicit(client, db_session):
    """Si deposit_amount está definido en el servicio, se usa ese valor (no el 30%)."""
    tenant = Tenant(name="Spa Relax 2", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id,
        name="Masaje Corporal",
        duration_minutes=60,
        price=8000.0,
        deposit_amount=1500.0,
    )
    db_session.add(service)
    await db_session.commit()

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Marcos",
        "client_phone": "5491122334455",
        "start_time": f"{FUTURE_DAY}T10:00:00Z",
        "idempotency_key": "pub-booking-deposit-01",
    }

    with patch(MP_PATCH, new=AsyncMock(return_value=FAKE_MP_RESULT)) as mock_mp:
        res = await client.post("/public/bookings", json=payload)

    assert res.status_code == 201
    # Verificar que se llamó con el monto explícito de seña
    mock_mp.assert_awaited_once()
    call_kwargs = mock_mp.call_args
    assert call_kwargs.kwargs["amount"] == 1500.0


@pytest.mark.asyncio
async def test_create_public_booking_idempotent_returns_same_url(client, db_session):
    """
    Dos requests con el mismo idempotency_key deben devolver el mismo booking_id
    y la misma payment_url, sin llamar a MP una segunda vez.
    """
    tenant = Tenant(name="Estudio Pilates", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Clase Individual", duration_minutes=60, price=5000.0
    )
    db_session.add(service)
    await db_session.commit()

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Lucía",
        "client_phone": "5491133445566",
        "start_time": f"{FUTURE_DAY}T09:00:00Z",
        "idempotency_key": "pub-idem-key-01",
    }

    with patch(MP_PATCH, new=AsyncMock(return_value=FAKE_MP_RESULT)):
        res1 = await client.post("/public/bookings", json=payload)

    assert res1.status_code == 201
    data1 = res1.json()

    # Segunda llamada — MP NO debe ser invocado de nuevo
    with patch(MP_PATCH, new=AsyncMock(return_value=FAKE_MP_RESULT)) as mock_mp2:
        res2 = await client.post("/public/bookings", json=payload)

    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["booking_id"] == data1["booking_id"]
    assert data2["payment_url"] == FAKE_MP_RESULT["checkout_url"]
    mock_mp2.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_public_booking_rolls_back_if_mp_fails(client, db_session):
    """
    Si MP rechaza la creación de la preferencia, el booking NO debe quedar
    en la base de datos (rollback atómico).
    """
    from fastapi import HTTPException as FastAPIHTTPException

    tenant = Tenant(name="Centro Estética", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Lifting Facial", duration_minutes=30, price=7000.0
    )
    db_session.add(service)
    await db_session.commit()

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Carmen",
        "client_phone": "5491144556677",
        "start_time": f"{FUTURE_DAY}T11:00:00Z",
        "idempotency_key": "pub-booking-mp-fail-01",
    }

    mp_error = FastAPIHTTPException(
        status_code=502, detail="Timeout creando preferencia en Mercado Pago"
    )
    with patch(MP_PATCH, new=AsyncMock(side_effect=mp_error)):
        res = await client.post("/public/bookings", json=payload)

    assert res.status_code == 502

    # El booking no debe existir en DB
    stmt = select(Booking).where(Booking.idempotency_key == "pub-booking-mp-fail-01")
    booking = (await db_session.execute(stmt)).scalar_one_or_none()
    assert booking is None


# ---------------------------------------------------------------------------
# Seña en el endpoint público de tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_tenant_detail_includes_deposit_amount_fallback(
    client, db_session
):
    """PublicServiceRead calcula deposit_amount con el fallback del 30% del precio."""
    tenant = Tenant(name="Kinesiología Sur", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Sesión", duration_minutes=45, price=10000.0
    )
    db_session.add(service)
    await db_session.commit()

    res = await client.get(f"/public/tenants/{tenant.id}")
    assert res.status_code == 200
    assert res.json()["services"][0]["deposit_amount"] == 3000.0


@pytest.mark.asyncio
async def test_public_tenant_detail_includes_deposit_amount_explicit(
    client, db_session
):
    """Si el servicio tiene deposit_amount definido, el endpoint devuelve ese valor."""
    tenant = Tenant(name="Estética Norte", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id,
        name="Limpieza Facial",
        duration_minutes=60,
        price=9000.0,
        deposit_amount=1500.0,
    )
    db_session.add(service)
    await db_session.commit()

    res = await client.get(f"/public/tenants/{tenant.id}")
    assert res.status_code == 200
    assert res.json()["services"][0]["deposit_amount"] == 1500.0


# ---------------------------------------------------------------------------
# Página pública /t/{slug}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_booking_page_renders_html(client, db_session):
    """GET /t/{slug} renderiza la página pública con tenant, servicios y seña."""
    tenant = Tenant(
        name="Barbería Central",
        slug="barberia-central",
        timezone="America/Argentina/Buenos_Aires",
    )
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id,
        name="Corte Clásico",
        duration_minutes=30,
        price=4000.0,
        is_active=True,
    )
    db_session.add(service)
    await db_session.commit()

    res = await client.get("/t/barberia-central")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "Barbería Central" in res.text
    # tojson escapa los no-ASCII: "Clásico" viaja como \u00e1 y el cliente lo parsea
    assert "Corte Cl\\u00e1sico" in res.text
    # La seña con fallback del 30% ($1.200) viaja embebida al cliente
    assert "1200.0" in res.text


@pytest.mark.asyncio
async def test_public_booking_page_unknown_slug_returns_404_html(client, db_session):
    """GET /t/{slug} inexistente devuelve 404 con HTML (es una URL para humanos)."""
    res = await client.get("/t/negocio-inexistente")
    assert res.status_code == 404
    assert res.headers["content-type"].startswith("text/html")
    assert "No encontramos ese negocio" in res.text


# ---------------------------------------------------------------------------
# Normalización del teléfono del cliente
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_public_booking_normalizes_local_phone(client, db_session):
    """
    El cliente escribe su teléfono en formato local (03584 166288), como
    hace la mayoría, y el booking debe quedar guardado normalizado al
    formato que exige WhatsApp (5493584166288).
    """
    tenant = Tenant(name="Barbería Local", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Corte", duration_minutes=30, price=5000.0
    )
    db_session.add(service)
    await db_session.commit()

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Ana",
        "client_phone": "03584 166288",
        "start_time": f"{FUTURE_DAY}T13:00:00Z",
        "idempotency_key": "pub-booking-phone-norm-01",
    }

    with patch(MP_PATCH, new=AsyncMock(return_value=FAKE_MP_RESULT)):
        res = await client.post("/public/bookings", json=payload)

    assert res.status_code == 201
    booking = await db_session.get(Booking, res.json()["booking_id"])
    assert booking.client_phone == "5493584166288"


@pytest.mark.asyncio
async def test_create_public_booking_rejects_bad_phone(client, db_session):
    """
    Un teléfono irreconocible debe rechazarse con 422 antes de crear
    nada y sin llamar a MP: mejor que el cliente lo corrija en pantalla
    que confirmar una reserva cuyo WhatsApp nunca va a llegar.
    """
    tenant = Tenant(name="Barbería Phone", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="Corte", duration_minutes=30, price=5000.0
    )
    db_session.add(service)
    await db_session.commit()

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Ana",
        "client_phone": "1234",
        "start_time": f"{FUTURE_DAY}T13:00:00Z",
        "idempotency_key": "pub-booking-phone-bad-01",
    }

    with patch(MP_PATCH, new=AsyncMock(return_value=FAKE_MP_RESULT)) as mock_mp:
        res = await client.post("/public/bookings", json=payload)

    assert res.status_code == 422
    assert "WhatsApp" in res.json()["detail"]
    mock_mp.assert_not_awaited()

    stmt = select(Booking).where(Booking.idempotency_key == "pub-booking-phone-bad-01")
    booking = (await db_session.execute(stmt)).scalar_one_or_none()
    assert booking is None
