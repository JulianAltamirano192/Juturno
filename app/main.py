# app/main.py
from datetime import date, datetime, time, timedelta
from typing import Optional, List, Annotated
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
import logging
from pathlib import Path

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, text
from sqlalchemy.exc import IntegrityError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import redis.asyncio as aioredis

from app.database import async_session_maker, get_db
from app.models import Tenant, Service, Staff, Booking, Payment
from app.services import calculate_available_slots
from app.scheduler import process_reminders
from app.outbox_worker import process_outbox
from app.mp_webhooks import router as mp_router, create_mp_preference
from app.webhooks import router as whatsapp_router
from app.config import settings
from app.auth import get_current_tenant


# --- SENTRY (inicializar antes de crear la app) ---
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        integrations=[
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
            HttpxIntegration(),
        ],
        traces_sample_rate=0.1,
        profiles_sample_rate=0.1,
        send_default_pii=False,
    )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# --- SCHEDULER + LIFESPAN ---
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestiona el ciclo de vida de la aplicación FastAPI.
    Arranca el scheduler de recordatorios al iniciar y lo apaga limpiamente al cerrar.
    """
    scheduler.add_job(
        process_reminders,
        "interval",
        minutes=5,
        args=[async_session_maker],
        id="reminder_job",
        replace_existing=True,
    )
    scheduler.add_job(
        process_outbox,
        "interval",
        minutes=1,
        args=[async_session_maker],
        id="outbox_job",
        replace_existing=True,
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

# Plantillas para la página pública de reserva (/t/{slug})
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# --- HEALTHCHECK ---


@app.get("/health")
async def health(session: AsyncSession = Depends(get_db)):
    """
    Health check profundo: verifica API, DB y Redis.
    Retorna 200 si todo OK, 503 si algo falla.
    """
    checks = {
        "api": "ok",
        "database": "unknown",
        "redis": "unknown",
    }
    is_healthy = True

    # DB
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        is_healthy = False

    # Redis
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await r.ping()
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
        is_healthy = False

    status_code = 200 if is_healthy else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ok" if is_healthy else "degraded", "checks": checks},
    )


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


class PublicServiceRead(BaseModel):
    id: int
    name: str
    duration_minutes: int
    price: float
    deposit_amount: float = Field(
        description="Seña efectiva: deposit_amount o 30% del precio"
    )


class PublicTenantDetailResponse(BaseModel):
    id: int
    name: str
    slug: Optional[str] = None
    timezone: str
    services: List[PublicServiceRead]


class PublicBookingResponse(BaseModel):
    message: str
    booking_id: int
    payment_url: str


# --- SCHEMAS PARA BOOKINGS ---


class BookingCreate(BaseModel):
    tenant_id: int
    service_id: int
    staff_id: Optional[int] = None
    client_name: str
    client_phone: str
    start_time: datetime
    end_time: Optional[datetime] = None
    price_at_booking: Optional[float] = None
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

    tenant = current_tenant

    tenant_timezone = ZoneInfo(
        tenant.timezone if tenant.timezone else "America/Argentina/Buenos_Aires"
    )
    now_local = datetime.now(tenant_timezone)
    today_local = now_local.date()

    if day < today_local:
        raise HTTPException(
            status_code=400, detail="No se pueden consultar fechas pasadas"
        )

    if tenant_id != current_tenant.id:
        # La API key es válida pero para otro tenant: 404 para no
        # revelar si el tenant_id existe.
        raise HTTPException(status_code=404, detail="Service not found")

    service = await session.get(Service, service_id)
    if not service or service.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Service not found")

    window_start = datetime.combine(day, time(9, 0), tzinfo=tenant_timezone)
    window_end = datetime.combine(day, time(18, 0), tzinfo=tenant_timezone)

    stmt = select(Booking).where(
        and_(
            Booking.tenant_id == tenant_id,
            Booking.status.in_(["pending", "confirmed"]),
            Booking.start_time < window_end,
            Booking.end_time > window_start,
        )
    )

    if staff_id:
        stmt = stmt.where(Booking.staff_id == staff_id)

    result = await session.execute(stmt)
    bookings_db = result.scalars().all()

    bookings_intervals = [
        (
            b.start_time.astimezone(tenant_timezone),
            b.end_time.astimezone(tenant_timezone),
        )
        for b in bookings_db
    ]

    slots = calculate_available_slots(
        window_start=window_start,
        window_end=window_end,
        bookings=bookings_intervals,
        duration_min=service.duration_minutes,
        granularity_min=30,
    )

    # Si la fecha es HOY en el timezone del tenant, filtrar slots pasados
    if day == today_local:
        filtered_slots = []
        for s in slots:
            slot_h, slot_m = map(int, s.split(":"))
            slot_dt = datetime.combine(
                day, time(slot_h, slot_m), tzinfo=tenant_timezone
            )
            if slot_dt >= now_local:
                filtered_slots.append(s)
        slots = filtered_slots

    return AvailableSlotsResponse(
        date=day,
        service_duration_min=service.duration_minutes,
        timezone=tenant.timezone,
        slots=slots,
    )


@app.post("/bookings", status_code=201)
async def create_booking(
    payload: BookingCreate,
    current_tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """
    Crea una reserva derivando end_time automáticamente a partir de service.duration_minutes.
    Es totalmente idempotente por idempotency_key (devuelve 200 + booking existente si se reintenta).
    Devuelve 409 si el slot ya está ocupado (ExcludeConstraint) por otra reserva distinta.
    """
    if payload.tenant_id != current_tenant.id:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # 1. Verificar si ya existe una reserva con el mismo idempotency_key para este tenant
    existing_stmt = select(Booking).where(
        and_(
            Booking.tenant_id == payload.tenant_id,
            Booking.idempotency_key == payload.idempotency_key,
        )
    )
    existing_booking = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing_booking is not None:
        return JSONResponse(
            status_code=200,
            content={
                "message": "Reserva recuperada (idempotente)",
                "booking_id": existing_booking.id,
            },
        )

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
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=tenant_timezone)

    end_time = start_time + timedelta(minutes=service.duration_minutes)

    new_booking = Booking(
        tenant_id=payload.tenant_id,
        service_id=payload.service_id,
        staff_id=payload.staff_id,
        client_name=payload.client_name,
        client_phone=payload.client_phone,
        start_time=start_time,
        end_time=end_time,
        price_at_booking=service.price,
        idempotency_key=payload.idempotency_key,
        status="pending",
    )

    session.add(new_booking)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # Manejar race condition por idempotency_key
        existing_booking = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing_booking is not None:
            return JSONResponse(
                status_code=200,
                content={
                    "message": "Reserva recuperada (idempotente)",
                    "booking_id": existing_booking.id,
                },
            )
        raise HTTPException(status_code=409, detail="Slot ya reservado o superpuesto")

    return {"message": "Reserva creada", "booking_id": new_booking.id}


# --- PUBLIC ENDPOINTS (SIN AUTENTICACIÓN) ---


@app.get("/public/tenants/{identifier}", response_model=PublicTenantDetailResponse)
async def get_public_tenant_detail(
    identifier: str,
    session: AsyncSession = Depends(get_db),
):
    """Devuelve la información pública del negocio y sus servicios activos (por ID o por slug)."""
    stmt = select(Tenant)
    if identifier.isdigit():
        stmt = stmt.where(Tenant.id == int(identifier))
    else:
        stmt = stmt.where(Tenant.slug == identifier)

    tenant = (await session.execute(stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    services_stmt = select(Service).where(
        and_(Service.tenant_id == tenant.id, Service.is_active.is_(True))
    )
    services = (await session.execute(services_stmt)).scalars().all()

    return PublicTenantDetailResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        timezone=tenant.timezone,
        services=[
            PublicServiceRead(
                id=s.id,
                name=s.name,
                duration_minutes=s.duration_minutes,
                price=float(s.price),
                deposit_amount=(
                    float(s.deposit_amount)
                    if s.deposit_amount is not None
                    else round(float(s.price) * 0.30, 2)
                ),
            )
            for s in services
        ],
    )


@app.get("/public/available-slots", response_model=AvailableSlotsResponse)
async def get_public_available_slots(
    tenant_id: Annotated[int, Query(gt=0, description="ID del negocio")],
    service_id: Annotated[int, Query(gt=0, description="ID del servicio")],
    day: Annotated[date, Query(description="Fecha YYYY-MM-DD")],
    staff_id: Annotated[Optional[int], Query(description="ID del profesional")] = None,
    session: AsyncSession = Depends(get_db),
):
    """Devuelve los slots libres para un servicio/día (público sin API Key)."""
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant_timezone = ZoneInfo(
        tenant.timezone if tenant.timezone else "America/Argentina/Buenos_Aires"
    )
    now_local = datetime.now(tenant_timezone)
    today_local = now_local.date()

    if day < today_local:
        raise HTTPException(
            status_code=400, detail="No se pueden consultar fechas pasadas"
        )

    service = await session.get(Service, service_id)
    if not service or service.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Service not found")

    window_start = datetime.combine(day, time(9, 0), tzinfo=tenant_timezone)
    window_end = datetime.combine(day, time(18, 0), tzinfo=tenant_timezone)

    stmt = select(Booking).where(
        and_(
            Booking.tenant_id == tenant_id,
            Booking.status.in_(["pending", "confirmed"]),
            Booking.start_time < window_end,
            Booking.end_time > window_start,
        )
    )

    if staff_id:
        stmt = stmt.where(Booking.staff_id == staff_id)

    result = await session.execute(stmt)
    bookings_db = result.scalars().all()

    bookings_intervals = [
        (
            b.start_time.astimezone(tenant_timezone),
            b.end_time.astimezone(tenant_timezone),
        )
        for b in bookings_db
    ]

    slots = calculate_available_slots(
        window_start=window_start,
        window_end=window_end,
        bookings=bookings_intervals,
        duration_min=service.duration_minutes,
        granularity_min=30,
    )

    if day == today_local:
        filtered_slots = []
        for s in slots:
            slot_h, slot_m = map(int, s.split(":"))
            slot_dt = datetime.combine(
                day, time(slot_h, slot_m), tzinfo=tenant_timezone
            )
            if slot_dt >= now_local:
                filtered_slots.append(s)
        slots = filtered_slots

    return AvailableSlotsResponse(
        date=day,
        service_duration_min=service.duration_minutes,
        timezone=tenant.timezone,
        slots=slots,
    )


@app.post("/public/bookings", status_code=201)
async def create_public_booking(
    payload: BookingCreate,
    session: AsyncSession = Depends(get_db),
):
    """
    Crea una reserva en estado 'pending' desde el flujo público (sin API Key),
    genera una preferencia de pago en Mercado Pago y devuelve el init_point.

    Si la creación de la preferencia de MP falla, el booking se revierte.
    Es totalmente idempotente por idempotency_key.
    """
    tenant = await session.get(Tenant, payload.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    existing_stmt = select(Booking).where(
        and_(
            Booking.tenant_id == payload.tenant_id,
            Booking.idempotency_key == payload.idempotency_key,
        )
    )
    existing_booking = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing_booking is not None:
        # Buscar el Payment con la URL de checkout ya generada
        payment_stmt = select(Payment).where(
            and_(
                Payment.booking_id == existing_booking.id,
                Payment.method == "mercado_pago",
                Payment.mp_checkout_url.is_not(None),
            )
        )
        existing_payment = (await session.execute(payment_stmt)).scalar_one_or_none()
        checkout_url = existing_payment.mp_checkout_url if existing_payment else ""
        return JSONResponse(
            status_code=200,
            content={
                "message": "Reserva recuperada (idempotente)",
                "booking_id": existing_booking.id,
                "payment_url": checkout_url,
            },
        )

    service = await session.get(Service, payload.service_id)
    if not service or service.tenant_id != payload.tenant_id:
        raise HTTPException(status_code=404, detail="Service not found for tenant")

    if payload.staff_id is not None:
        staff = await session.get(Staff, payload.staff_id)
        if not staff or staff.tenant_id != payload.tenant_id:
            raise HTTPException(status_code=404, detail="Staff not found for tenant")

    tenant_timezone = ZoneInfo(
        tenant.timezone if tenant.timezone else "America/Argentina/Buenos_Aires"
    )
    start_time = payload.start_time
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=tenant_timezone)

    end_time = start_time + timedelta(minutes=service.duration_minutes)

    # Calcular monto de seña: deposit_amount explícito o 30% del precio total
    if service.deposit_amount is not None:
        deposit = float(service.deposit_amount)
    else:
        deposit = round(float(service.price) * 0.30, 2)

    new_booking = Booking(
        tenant_id=payload.tenant_id,
        service_id=payload.service_id,
        staff_id=payload.staff_id,
        client_name=payload.client_name,
        client_phone=payload.client_phone,
        start_time=start_time,
        end_time=end_time,
        price_at_booking=service.price,
        idempotency_key=payload.idempotency_key,
        status="pending",
    )

    session.add(new_booking)
    try:
        # flush para obtener el id sin commitear aún
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing_booking = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing_booking is not None:
            payment_stmt = select(Payment).where(
                and_(
                    Payment.booking_id == existing_booking.id,
                    Payment.method == "mercado_pago",
                    Payment.mp_checkout_url.is_not(None),
                )
            )
            existing_payment = (
                await session.execute(payment_stmt)
            ).scalar_one_or_none()
            checkout_url = existing_payment.mp_checkout_url if existing_payment else ""
            return JSONResponse(
                status_code=200,
                content={
                    "message": "Reserva recuperada (idempotente)",
                    "booking_id": existing_booking.id,
                    "payment_url": checkout_url,
                },
            )
        raise HTTPException(status_code=409, detail="Slot ya reservado o superpuesto")

    # Crear preferencia de MP — si falla hacemos rollback y el booking no queda en DB
    try:
        mp_result = await create_mp_preference(
            booking_id=new_booking.id,
            amount=deposit,
            client_name=payload.client_name,
            back_url=(
                f"{settings.PUBLIC_BASE_URL}/t/{tenant.slug}"
                f"?booking={new_booking.id}"
            ),
        )
    except HTTPException:
        await session.rollback()
        raise

    # Registrar el Payment pendiente con el preference_id y checkout_url
    new_payment = Payment(
        booking_id=new_booking.id,
        amount=deposit,
        method="mercado_pago",
        status="pending",
        mp_preference_id=mp_result["preference_id"],
        mp_checkout_url=mp_result["init_point"],
    )
    session.add(new_payment)

    await session.commit()

    return PublicBookingResponse(
        message="Reserva creada",
        booking_id=new_booking.id,
        payment_url=mp_result["init_point"],
    )


# --- PÁGINA PÚBLICA DE RESERVA ---


@app.get("/t/{slug}", response_class=HTMLResponse)
async def public_booking_page(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """
    Página pública de reserva (mobile-first) de un negocio.

    Renderiza el tenant y sus servicios activos; los horarios y la creación
    de la reserva se consumen desde el cliente vía los endpoints /public/*.
    """
    tenant = (
        await session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if not tenant:
        return HTMLResponse(
            "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>No encontramos ese negocio</title></head>"
            '<body style="font-family:system-ui,sans-serif;text-align:center;'
            'padding:48px 24px;color:#111827">'
            "<h1 style='font-size:1.25rem;margin-bottom:12px'>No encontramos ese negocio</h1>"
            "<p style='color:#6b7280'>Revisá el enlace o contactá al negocio directamente.</p>"
            "</body></html>",
            status_code=404,
        )

    services = (
        (
            await session.execute(
                select(Service).where(
                    and_(Service.tenant_id == tenant.id, Service.is_active.is_(True))
                )
            )
        )
        .scalars()
        .all()
    )

    services_data = [
        {
            "id": s.id,
            "name": s.name,
            "duration_minutes": s.duration_minutes,
            "price": float(s.price),
            "deposit_amount": (
                float(s.deposit_amount)
                if s.deposit_amount is not None
                else round(float(s.price) * 0.30, 2)
            ),
        }
        for s in services
    ]

    return templates.TemplateResponse(
        request,
        "public_booking.html",
        {
            "tenant_name": tenant.name,
            "tenant_id": tenant.id,
            "timezone": tenant.timezone or "America/Argentina/Buenos_Aires",
            "services": services_data,
        },
    )
