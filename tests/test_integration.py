import pytest
import httpx
import hmac
import hashlib
from datetime import datetime, timedelta, date
from sqlalchemy.ext.create_async_engine import create_async_engine # type: ignore
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

# Importamos la app de FastAPI y los modelos SQLModel
from app.main import app
from app.models import SQLModel, Tenant, Service, Booking, ProcessedWebhookEvent, NotificationOutbox

# URL de la base de datos de test (apuntando al contenedor de Docker 'db')
TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@db:5432/saas_db"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestingSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

@pytest.fixture(autouse=True)
async def setup_db():
    """Fixture: Crea las tablas, asegura la extensión btree_gist y limpia al terminar"""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist;"))
        await conn.run_sync(SQLModel.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)

@pytest.fixture
async def db_session():
    """Fixture: Provee sesión de BD y hace rollback aislando cada test"""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        async with TestingSessionLocal(bind=conn) as session:
            yield session
            await transaction.rollback()

@pytest.fixture
async def client():
    """Fixture: Cliente HTTP asíncrono para testear la API de FastAPI"""
    async with httpx.AsyncClient(app=app, base_url="http://test") as ac:
        yield ac

# --- SUITE DE PRUEBAS DE INTEGRACIÓN ---

@pytest.mark.asyncio
async def test_booking_flow_and_available_slots(client, db_session):
    """Test: Crear tenant, servicio, reservar un turno y verificar que desaparece de available-slots"""
    # 1. Crear Tenant y Service de prueba
    tenant = Tenant(name="Salon Test", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()
    
    service = Service(tenant_id=tenant.id, name="Corte de Pelo", duration_minutes=60, price=1500.0)
    db_session.add(service)
    await db_session.flush()

    day = date.today() + timedelta(days=1)
    
    # 2. Hacer POST a /bookings
    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "staff_id": None,
        "client_name": "Carlos Gomez",
        "client_phone": "3584123456",
        "start_time": f"{day}T10:00:00",
        "end_time": f"{day}T11:00:00",
        "price_at_booking": 1500.0,
        "idempotency_key": "unique-booking-key-01"
    }
    res = await client.post("/bookings", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert "booking_id" in data

    # 3. Consultar /bookings/available-slots y verificar que las 10:00 ya no están disponibles
    res_slots = await client.get(f"/bookings/available-slots?tenant_id={tenant.id}&service_id={service.id}&day={day}")
    assert res_slots.status_code == 200
    slots = res_slots.json()["slots"]
    assert "10:00" not in slots
    assert "09:00" in slots  # El slot anterior debería mantenerse libre

@pytest.mark.asyncio
async def test_double_booking_conflict(client, db_session):
    """Test: Intentar reservar el mismo slot exacto debe retornar 409 Conflict"""
    tenant = Tenant(name="Salon Test 2")
    db_session.add(tenant)
    await db_session.flush()
    
    service = Service(tenant_id=tenant.id, name="Manicura", duration_minutes=60, price=2000.0)
    db_session.add(service)
    await db_session.flush()

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Ana Perez",
        "client_phone": "3584998877",
        "start_time": "2026-10-15T14:00:00",
        "end_time": "2026-10-15T15:00:00",
        "price_at_booking": 2000.0,
        "idempotency_key": "key-conflict-1"
    }
    
    # Primer intento: Exitoso (201)
    res1 = await client.post("/bookings", json=payload)
    assert res1.status_code == 201

    # Segundo intento con otra clave de idempotencia pero mismo horario exacto: Bloqueado por ExcludeConstraint (409)
    payload["idempotency_key"] = "key-conflict-2"
    res2 = await client.post("/bookings", json=payload)
    assert res2.status_code == 409

@pytest.mark.asyncio
async def test_webhook_mp_idempotency(client, db_session):
    """Test: Enviar el mismo webhook de Mercado Pago 2 veces solo procesa 1 efecto"""
    secret = "tu_secreto_de_webhook"  # Debe coincidir con el secret configurado en tu mp_webhooks.py
    
    # Generar firma HMAC válida para el test
    manifest = "id:pay_999;request-id:req_888;ts:12345;"
    hash_hmac = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    signature = f"ts=12345,v1={hash_hmac}"

    payload = {"id": "evt_duplicate_test", "action": "payment.updated", "data": {"id": "pay_999"}}
    headers = {"x-signature": signature, "x-request-id": "req_888"}

    # Primer envío (Debe procesarse con éxito)
    res1 = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res1.status_code == 200
    assert res1.text == "EVENT_PROCESSED"

    # Segundo envío idéntico (Debe ser interceptado por la tabla payment_events y retornar 200 sin duplicar lógica)
    res2 = await client.post("/webhooks/mercadopago", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.text == "DUPLICATE_EVENT_IGNORED"

@pytest.mark.asyncio
async def test_outbox_created_on_booking(client, db_session):
    """Test: Verificar que al crear una reserva se genera automáticamente el registro 'pending' en la Outbox"""
    tenant = Tenant(name="Tenant Outbox")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(tenant_id=tenant.id, name="Spa", duration_minutes=30, price=5000.0)
    db_session.add(service)
    await db_session.commit()  # Commit para consolidar IDs previos al request HTTP

    payload = {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": "Lucía",
        "client_phone": "3584112233",
        "start_time": "2026-11-01T16:00:00",
        "end_time": "2026-11-01T16:30:00",
        "price_at_booking": 5000.0,
        "idempotency_key": "outbox-test-key-99"
    }
    
    res = await client.post("/bookings", json=payload)
    assert res.status_code == 201

    # Consultar directamente la tabla notification_outbox en la BD
    result = await db_session.execute(text("SELECT status, notification_type FROM notification_outbox"))
    rows = result.fetchall()
    
    assert len(rows) == 1
    assert rows[0][0] == "pending"
    assert rows[0][1] == "confirmation"