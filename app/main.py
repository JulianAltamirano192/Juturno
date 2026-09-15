# app/main.py
from datetime import date, datetime, time, timezone
from typing import Optional, List, Annotated
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
import logging

from fastapi import FastAPI, Depends, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.exc import IntegrityError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import async_session_maker, get_db
from app.models import Tenant, Service, Staff, Booking, NotificationOutbox
from app.services import calculate_available_slots
from app.scheduler import process_reminders
from app.outbox_worker import process_outbox
from app.mp_webhooks import router as mp_router
from app.webhooks import router as whatsapp_router
from app.config import settings
from app.auth import get_current_tenant


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# --- SCHEDULER + LIFESPAN ---
scheduler = AsyncIOScheduler()
# ... resto del archivo


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestiona el ciclo de vida de la aplicación FastAPI.
    Arranca el scheduler de recordatorios al iniciar y lo apaga limpiamente al cerrar.
    """
    scheduler.add_job(
        process_reminders,
        'interval',
        minutes=5,
        args=[async_session_maker],
        id='reminder_job',
        replace_existing=True
    )
    scheduler.add_job(
        process_outbox,
        'interval',
        minutes=1,
        args=[async_session_maker],
        id='outbox_job',
        replace_existing=True
    )
    scheduler.start()
    print("Scheduler distribuido de recordatorios iniciado correctamente.")

    yield

    scheduler.shutdown()
    print("Scheduler detenido de forma segura.")


# --- APP ---

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(mp_router)
app.include_router(whatsapp_router)


# --- HEALTHCHECK ---

@app.get("/health")
async def health():
    return {"status": "ok"}


# --- SCHEMAS PARA SLOTS ---

class SlotQuery(BaseModel):
    tenant_id: int = Field(gt=0, description="ID del negocio")
    service_id: int = Field(gt=0, description="ID del servicio requerido")
    day: date = Field(description="Fecha a consultar YYYY-MM-DD")
    staff_id: Optional[int] = Field(default=None, description="ID del profesional")

    @field_validator("day", mode="before")
    @classmethod
    def not_in_past(cls, v):
        d = date.fromisoformat(v) if isinstance(v, str) else v
        if d < date.today():
            raise ValueError("No se pueden consultar fechas pasadas")
        return d


class AvailableSlotsResponse(BaseModel):
    date: date
    service_duration_min: int
    timezone: str
    slots: List[str]


# --- SCHEMAS PARA BOOKINGS ---

class BookingCreate(BaseModel):
    tenant_id: int
    service_id: int
    staff_id: Optional[int] = None
    client_name: str
    client_phone: str
    start_time: datetime
    end_time: datetime
    price_at_booking: float
    idempotency_key: str


# --- ENDPOINTS ---

@app.get("/bookings/available-slots", response_model=AvailableSlotsResponse)
async def get_available_slots(
    tenant_id: Annotated[int, Query(gt=0, description="ID del negocio")],
    service_id: Annotated[int, Query(gt=0, description="ID del servicio")],
    day: Annotated[date, Query(description="Fecha YYYY-MM-DD")],
    staff_id: Annotated[Optional[int], Query(description="ID del profesional")] = None,
    current_tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """Devuelve los slots libres para un servicio/día/staff."""

    if day < date.today():
        raise HTTPException(status_code=400, detail="No se pueden consultar fechas pasadas")

    if tenant_id != current_tenant.id:
        # La API key es válida pero para otro tenant: 404 para no
        # revelar si el tenant_id existe.
        raise HTTPException(status_code=404, detail="Service not found")

    service = await session.get(Service, service_id)
    if not service or service.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Service not found")

    tenant = current_tenant

    tenant_timezone = ZoneInfo(tenant.timezone)
    window_start = datetime.combine(day, time(9, 0), tzinfo=tenant_timezone)
    window_end = datetime.combine(day, time(18, 0), tzinfo=tenant_timezone)

    stmt = select(Booking).where(
        and_(
            Booking.tenant_id == tenant_id,
            Booking.status.in_(["pending", "confirmed"]),
            Booking.start_time < window_end,
            Booking.end_time > window_start
        )
    )

    if staff_id:
        stmt = stmt.where(Booking.staff_id == staff_id)

    result = await session.execute(stmt)
    bookings_db = result.scalars().all()

    bookings_intervals = [(b.start_time, b.end_time) for b in bookings_db]

    slots = calculate_available_slots(
        window_start=window_start,
        window_end=window_end,
        bookings=bookings_intervals,
        duration_min=service.duration_minutes,
        granularity_min=30
    )

    return AvailableSlotsResponse(
        date=day,
        service_duration_min=service.duration_minutes,
        timezone=tenant.timezone,
        slots=slots
    )


@app.post("/bookings", status_code=201)
async def create_booking(
    payload: BookingCreate,
    current_tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """
    Crea una reserva con Patrón Outbox transaccional.
    Commitea booking + notificación en una sola transacción atómica.
    Devuelve 409 si el slot ya está ocupado (ExcludeConstraint).
    """
    if payload.tenant_id != current_tenant.id:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=400, detail="end_time debe ser posterior a start_time")

    tenant = current_tenant

    service = await session.get(Service, payload.service_id)
    if not service or service.tenant_id != payload.tenant_id:
        raise HTTPException(status_code=404, detail="Service not found for tenant")

    if payload.staff_id is not None:
        staff = await session.get(Staff, payload.staff_id)
        if not staff or staff.tenant_id != payload.tenant_id:
            raise HTTPException(status_code=404, detail="Staff not found for tenant")

    tenant_timezone = ZoneInfo(tenant.timezone)
    start_time = payload.start_time
    end_time = payload.end_time
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=tenant_timezone)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=tenant_timezone)

    new_booking = Booking(
        tenant_id=payload.tenant_id,
        service_id=payload.service_id,
        staff_id=payload.staff_id,
        client_name=payload.client_name,
        client_phone=payload.client_phone,
        start_time=start_time,
        end_time=end_time,
        price_at_booking=service.price,
        idempotency_key=payload.idempotency_key
    )

    session.add(new_booking)
    try:
        await session.flush()
        outbox_event = NotificationOutbox(
            booking_id=new_booking.id,
            notification_type="confirmation",
            status="pending"
        )
        session.add(outbox_event)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Slot ya reservado o superpuesto")

    return {"message": "Reserva confirmada", "booking_id": new_booking.id}