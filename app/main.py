# app/main.py
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
import sentry_sdk
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_serializer, field_validator
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import and_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    RedirectToLoginException,
    get_current_tenant,
    get_current_tenant_from_session,
)
from app.booking_actions import (
    BookingNotStartedError,
    InvalidTransitionError,
    transition_booking_status,
)
from app.config import settings
from app.csrf import (
    CSRF_COOKIE_NAME,
    generate_csrf_token,
    set_csrf_cookie,
    validate_csrf,
    validate_csrf_double_submit,
)
from app.database import async_session_maker, get_db
from app.models import Booking, BusinessHours, Payment, Service, Staff, Tenant
from app.mp_connect import (
    ERR_PAGO_NO_CONFIGURADO,
    resolve_mp_access_token,
)
from app.mp_connect import (
    router as mp_connect_router,
)
from app.mp_crypto import MPTokenCryptoError
from app.mp_webhooks import create_mp_preference
from app.mp_webhooks import router as mp_router
from app.outbox_worker import process_outbox
from app.password import hash_password, verify_password
from app.phone import InvalidPhoneError, normalize_whatsapp_phone
from app.scheduler import (
    process_deposit_expiration,
    process_mp_token_refresh,
    process_reminders,
)
from app.services import (
    compute_available_slots,
    effective_deposit,
)
from app.session import (
    SESSION_COOKIE_NAME,
    delete_session_cookie,
    parse_session_token,
    sanitize_next_url,
    set_session_cookie,
)
from app.slug import generate_unique_slug
from app.webhooks import router as whatsapp_router

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
logger = logging.getLogger(__name__)


# --- SCHEDULER + LIFESPAN ---
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestiona el ciclo de vida de la aplicación FastAPI.
    Arranca el scheduler de recordatorios al iniciar y lo apaga limpiamente al cerrar.
    En entorno de test (TEST_DATABASE_URL seteada) NO arranca el scheduler
    para evitar colisiones de conexión con los tests.
    """
    # En tests no arrancamos el scheduler: los tests corren su propia DB aislada
    # y el scheduler en background causaría colisiones de conexión (asyncpg InterfaceError).
    # TEST_DATABASE_URL solo existe y es no-vacía en entorno de test (docker compose exec -e TEST_DATABASE_URL=...).
    test_db_url = getattr(settings, "TEST_DATABASE_URL", None)
    if not test_db_url:
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
        scheduler.add_job(
            process_deposit_expiration,
            "interval",
            minutes=1,
            args=[async_session_maker],
            id="deposit_expiration_job",
            replace_existing=True,
        )
        scheduler.add_job(
            process_mp_token_refresh,
            "interval",
            minutes=1440,
            args=[async_session_maker],
            id="mp_token_refresh_job",
            replace_existing=True,
        )
        scheduler.start()
        print("Scheduler distribuido iniciado correctamente.")
    else:
        print("Entorno de test detectado: scheduler NO iniciado.")

    yield

    if not getattr(settings, "TEST_DATABASE_URL", None):
        scheduler.shutdown()
        print("Scheduler detenido de forma segura.")
    else:
        print("Entorno de test: nada que apagar.")


# --- RATE LIMITER ---
# Disabled in test environment (TEST_DATABASE_URL is set by docker-compose exec in tests).
limiter = Limiter(
    key_func=get_remote_address,
    enabled=not bool(settings.TEST_DATABASE_URL),
)

# --- APP ---

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(mp_router)
app.include_router(mp_connect_router)
app.include_router(whatsapp_router)


@app.exception_handler(RedirectToLoginException)
async def redirect_to_login_handler(request: Request, exc: RedirectToLoginException):
    """Redirige automáticamente a /login si no hay sesión activa en el panel."""
    safe_next = sanitize_next_url(exc.next_url)
    response = RedirectResponse(
        url=f"/login?next={safe_next}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    delete_session_cookie(response)
    return response


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
    staff_id: int | None = Field(default=None, description="ID del profesional")

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
    slots: list[str]


class PublicServiceRead(BaseModel):
    id: int
    name: str
    duration_minutes: int
    price: Decimal
    deposit_amount: Decimal = Field(
        description="Seña efectiva: deposit_amount o 30% del precio"
    )

    @field_serializer("price", "deposit_amount")
    def serialize_money(self, v: Decimal) -> float:
        return float(v)


class PublicTenantDetailResponse(BaseModel):
    id: int
    name: str
    slug: str | None = None
    timezone: str
    services: list[PublicServiceRead]


class PublicBookingResponse(BaseModel):
    message: str
    booking_id: int
    payment_url: str


# --- SCHEMAS PARA BOOKINGS ---


class BookingCreate(BaseModel):
    tenant_id: int
    service_id: int
    staff_id: int | None = None
    client_name: str
    client_phone: str
    start_time: datetime
    end_time: datetime | None = None
    idempotency_key: str


# --- ENDPOINTS ---


@app.get("/bookings/available-slots", response_model=AvailableSlotsResponse)
async def get_available_slots(
    tenant_id: Annotated[int, Query(gt=0, description="ID del negocio")],
    service_id: Annotated[int, Query(gt=0, description="ID del servicio")],
    day: Annotated[date, Query(description="Fecha YYYY-MM-DD")],
    staff_id: Annotated[int | None, Query(description="ID del profesional")] = None,
    current_tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """Devuelve los slots libres para un servicio/día/staff."""
    if tenant_id != current_tenant.id:
        raise HTTPException(status_code=404, detail="Service not found")

    tenant_timezone = ZoneInfo(
        current_tenant.timezone or "America/Argentina/Buenos_Aires"
    )
    if day < datetime.now(tenant_timezone).date():
        raise HTTPException(
            status_code=400, detail="No se pueden consultar fechas pasadas"
        )

    service = await session.get(Service, service_id)
    if not service or service.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Service not found")

    slots = await compute_available_slots(
        session=session,
        tenant_id=tenant_id,
        service=service,
        day=day,
        staff_id=staff_id,
        tenant_timezone=tenant_timezone,
    )
    return AvailableSlotsResponse(
        date=day,
        service_duration_min=service.duration_minutes,
        timezone=current_tenant.timezone,
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
    try:
        client_phone = normalize_whatsapp_phone(payload.client_phone)
    except InvalidPhoneError:
        raise HTTPException(
            status_code=422,
            detail=(
                "El teléfono del cliente no es válido: se espera un celular "
                "argentino con característica, ej: +54 9 11 5555 5555."
            ),
        )

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
        client_phone=client_phone,
        start_time=start_time,
        end_time=end_time,
        price_at_booking=service.price,
        deposit_at_booking=effective_deposit(service.price, service.deposit_amount),
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


class TenantSettingsUpdate(BaseModel):
    """Campos de configuración que el tenant puede actualizar de sí mismo."""

    deposit_expiration_minutes: int | None = Field(default=None, ge=1)


@app.patch("/tenants/me")
async def update_tenant_settings(
    payload: TenantSettingsUpdate,
    current_tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """
    Actualiza la configuración del tenant autenticado (API key).

    deposit_expiration_minutes: minutos que tiene el cliente para pagar la
    seña antes de que la reserva expire y libere el horario.
    null desactiva la expiración para este tenant.
    """
    updates = payload.model_dump(exclude_unset=True)
    if "deposit_expiration_minutes" in updates:
        current_tenant.deposit_expiration_minutes = updates[
            "deposit_expiration_minutes"
        ]
        session.add(current_tenant)
        await session.commit()
        await session.refresh(current_tenant)

    return {
        "tenant_id": current_tenant.id,
        "deposit_expiration_minutes": current_tenant.deposit_expiration_minutes,
    }


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
                price=s.price,
                deposit_amount=effective_deposit(s.price, s.deposit_amount),
            )
            for s in services
        ],
    )


@app.get("/public/available-slots", response_model=AvailableSlotsResponse)
async def get_public_available_slots(
    tenant_id: Annotated[int, Query(gt=0, description="ID del negocio")],
    service_id: Annotated[int, Query(gt=0, description="ID del servicio")],
    day: Annotated[date, Query(description="Fecha YYYY-MM-DD")],
    staff_id: Annotated[int | None, Query(description="ID del profesional")] = None,
    session: AsyncSession = Depends(get_db),
):
    """Devuelve los slots libres para un servicio/día (público sin API Key)."""
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant_timezone = ZoneInfo(tenant.timezone or "America/Argentina/Buenos_Aires")
    if day < datetime.now(tenant_timezone).date():
        raise HTTPException(
            status_code=400, detail="No se pueden consultar fechas pasadas"
        )

    service = await session.get(Service, service_id)
    if not service or service.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Service not found")

    slots = await compute_available_slots(
        session=session,
        tenant_id=tenant_id,
        service=service,
        day=day,
        staff_id=staff_id,
        tenant_timezone=tenant_timezone,
    )
    return AvailableSlotsResponse(
        date=day,
        service_duration_min=service.duration_minutes,
        timezone=tenant.timezone,
        slots=slots,
    )


@app.post("/public/bookings", status_code=201)
@limiter.limit("20/minute")
async def create_public_booking(
    request: Request,
    payload: BookingCreate,
    session: AsyncSession = Depends(get_db),
):
    """
    Crea una reserva en estado 'pending' desde el flujo público (sin API Key),
    genera una preferencia de pago en Mercado Pago y devuelve la URL de checkout.

    Si la creación de la preferencia de MP falla, el booking se revierte.
    Es totalmente idempotente por idempotency_key.
    """
    try:
        client_phone = normalize_whatsapp_phone(payload.client_phone)
    except InvalidPhoneError:
        raise HTTPException(
            status_code=422,
            detail=(
                "El WhatsApp no parece completo. Ingresá tu número con "
                "característica, por ejemplo: +54 9 11 5555 5555."
            ),
        )

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

    deposit = float(effective_deposit(service.price, service.deposit_amount))

    new_booking = Booking(
        tenant_id=payload.tenant_id,
        service_id=payload.service_id,
        staff_id=payload.staff_id,
        client_name=payload.client_name,
        client_phone=client_phone,
        start_time=start_time,
        end_time=end_time,
        price_at_booking=service.price,
        deposit_at_booking=effective_deposit(service.price, service.deposit_amount),
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

    # Regla de cobro (D-012): el dinero va a la cuenta del tenant.
    # En producción, un negocio sin MP conectado NO puede recibir seña —
    # se rechaza ANTES de crear booking/preference para no dejar
    # reservas pending huérfanas de un cobro imposible.
    try:
        mp_access_token = resolve_mp_access_token(tenant)
    except MPTokenCryptoError as exc:
        logger.error(f"No se pudo descifrar el token MP del tenant {tenant.id}: {exc}")
        raise HTTPException(
            status_code=502,
            detail=(
                "Error al procesar el cobro del negocio; contactá "
                "al administrador de la plataforma."
            ),
        ) from exc
    if mp_access_token is None:
        raise HTTPException(status_code=422, detail=ERR_PAGO_NO_CONFIGURADO)

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
            access_token=mp_access_token,
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
        mp_checkout_url=mp_result["checkout_url"],
    )
    session.add(new_payment)

    await session.commit()

    return PublicBookingResponse(
        message="Reserva creada",
        booking_id=new_booking.id,
        payment_url=mp_result["checkout_url"],
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
            "deposit_amount": float(effective_deposit(s.price, s.deposit_amount)),
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


# --- REGISTRO Y LOGIN (PANEL DEL NEGOCIO) ---
@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Muestra el formulario de registro de negocio."""
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "register.html",
        {
            "csrf_token": csrf_token,
            "form_data": {},
            "error_message": None,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.post("/register", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def register_submit(
    request: Request,
    name: Annotated[str, Form()],
    owner_email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    whatsapp_number: Annotated[str | None, Form()] = None,
    slug: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str | None, Form()] = None,
    session: AsyncSession = Depends(get_db),
):
    """
    Registra un nuevo negocio y dueño de forma autoservicio.
    Valida CSRF (double-submit), contraseña mínima, unicidad de email normalizado
    y resuelve colisiones de slug automáticamente.
    """
    form_data = {
        "name": name,
        "owner_email": owner_email,
        "whatsapp_number": whatsapp_number or "",
        "slug": slug or "",
    }

    # 1. Validación CSRF Double-Submit
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
    if not validate_csrf_double_submit(csrf_token, cookie_csrf):
        new_csrf = generate_csrf_token()
        response = templates.TemplateResponse(
            request,
            "register.html",
            {
                "csrf_token": new_csrf,
                "form_data": form_data,
                "error_message": "El formulario expiró o es inválido. Por favor, intentá nuevamente.",
            },
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf)
        return response

    # 2. Validación de contraseña
    if len(password) < 8:
        new_csrf = generate_csrf_token()
        response = templates.TemplateResponse(
            request,
            "register.html",
            {
                "csrf_token": new_csrf,
                "form_data": form_data,
                "error_message": "La contraseña debe tener al menos 8 caracteres.",
            },
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf)
        return response

    # 3. Normalización y verificación de email único (siempre en minúsculas)
    normalized_email = owner_email.strip().lower()
    existing_owner = (
        await session.execute(
            select(Tenant).where(Tenant.owner_email == normalized_email)
        )
    ).scalar_one_or_none()

    if existing_owner is not None:
        new_csrf = generate_csrf_token()
        response = templates.TemplateResponse(
            request,
            "register.html",
            {
                "csrf_token": new_csrf,
                "form_data": form_data,
                "error_message": "Ya existe un negocio registrado con este correo electrónico.",
            },
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf)
        return response

    # 4. Generación de slug único con manejo de colisiones
    base_slug_text = slug.strip() if slug and slug.strip() else name.strip()
    final_slug = await generate_unique_slug(session, base_slug_text)

    # 5. Hash seguro de contraseña (PBKDF2-HMAC-SHA256 con 600k iteraciones)
    pwd_hash = hash_password(password)

    # 6. Creación del tenant
    clean_whatsapp = (
        whatsapp_number.strip() if whatsapp_number and whatsapp_number.strip() else None
    )
    new_tenant = Tenant(
        name=name.strip(),
        slug=final_slug,
        owner_email=normalized_email,
        password_hash=pwd_hash,
        whatsapp_number=clean_whatsapp,
    )
    session.add(new_tenant)
    await session.commit()
    await session.refresh(new_tenant)

    return RedirectResponse(
        url="/login?registered=1", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    registered: str | None = Query(None),
    next: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
):
    """Muestra el formulario de inicio de sesión."""
    # Si ya tiene una sesión válida activa, redirigir directo al dashboard
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if session_cookie:
        parsed = parse_session_token(session_cookie)
        if parsed:
            tenant_id, session_version = parsed
            tenant = await session.get(Tenant, tenant_id)
            if tenant is not None and tenant.session_version == session_version:
                safe_next = sanitize_next_url(next)
                return RedirectResponse(
                    url=safe_next, status_code=status.HTTP_303_SEE_OTHER
                )

    csrf_token = generate_csrf_token()
    info_message = (
        "Tu cuenta fue creada con éxito. Iniciá sesión para continuar."
        if registered == "1"
        else None
    )
    safe_next = sanitize_next_url(next)

    response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "csrf_token": csrf_token,
            "info_message": info_message,
            "error_message": None,
            "owner_email": "",
            "next_url": safe_next,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.post("/login", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def login_submit(
    request: Request,
    owner_email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str | None, Form()] = None,
    session: AsyncSession = Depends(get_db),
):
    """Valida credenciales e inicia sesión estableciendo cookie firmada."""
    safe_next = sanitize_next_url(next)

    # 1. Validación CSRF Double-Submit
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
    if not validate_csrf_double_submit(csrf_token, cookie_csrf):
        new_csrf = generate_csrf_token()
        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": new_csrf,
                "info_message": None,
                "error_message": "El formulario expiró o es inválido. Por favor, intentá nuevamente.",
                "owner_email": owner_email,
                "next_url": safe_next,
            },
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf)
        return response

    # 2. Búsqueda de tenant por email normalizado
    normalized_email = owner_email.strip().lower()
    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.owner_email == normalized_email)
        )
    ).scalar_one_or_none()

    # 3. Verificación de contraseña (OWASP PBKDF2 a tiempo constante)
    if (
        tenant is None
        or not tenant.password_hash
        or not verify_password(password, tenant.password_hash)
    ):
        new_csrf = generate_csrf_token()
        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": new_csrf,
                "info_message": None,
                "error_message": "Correo electrónico o contraseña incorrectos.",
                "owner_email": owner_email,
                "next_url": safe_next,
            },
            status_code=400,
        )
        set_csrf_cookie(response, new_csrf)
        return response

    # 4. Login exitoso -> emitir cookie de sesión firmada
    response = RedirectResponse(url=safe_next, status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, tenant.id, tenant.session_version)
    return response


@app.post("/logout")
async def logout(request: Request):
    """Cierra la sesión eliminando la cookie."""
    await validate_csrf(request)
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    delete_session_cookie(response)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
):
    """Vista principal del panel del negocio."""
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


# ---------------------------------------------------------------------------
# Panel: CRUD de servicios
# ---------------------------------------------------------------------------


def _parse_service_form(form: dict) -> tuple[dict, dict]:
    """Parsea y valida los campos del formulario de servicio.

    Returns (data, errors). Si errors está vacío, data es usable para DB.
    """
    errors: dict = {}
    data: dict = {}

    name = form.get("name", "").strip()
    if not name:
        errors["name"] = "El nombre es obligatorio."
    else:
        data["name"] = name

    try:
        duration_minutes = int(form.get("duration_minutes", ""))
        if duration_minutes < 1:
            raise ValueError
        data["duration_minutes"] = duration_minutes
    except (ValueError, TypeError):
        errors["duration_minutes"] = "La duración debe ser un número entero mayor a 0."

    try:
        price = Decimal(form.get("price", "").replace(",", "."))
        if price < Decimal("0.01"):
            raise ValueError
        data["price"] = price
    except Exception:
        errors["price"] = "El precio debe ser un número mayor a 0 (ej: 5000.00)."

    deposit_raw = form.get("deposit_amount", "").strip()
    if deposit_raw == "":
        data["deposit_amount"] = None
    else:
        try:
            deposit = Decimal(deposit_raw.replace(",", "."))
            if deposit < Decimal(0):
                raise ValueError
            data["deposit_amount"] = deposit
        except Exception:
            errors["deposit_amount"] = (
                "La seña debe ser un número mayor o igual a 0 (ej: 1500.00)."
            )

    return data, errors


@app.get("/panel/services", response_class=HTMLResponse)
async def panel_services_list(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    csrf_token = generate_csrf_token()
    stmt = select(Service).where(Service.tenant_id == tenant.id).order_by(Service.id)
    services = (await session.execute(stmt)).scalars().all()
    services_with_deposit = [
        (s, effective_deposit(s.price, s.deposit_amount)) for s in services
    ]
    response = templates.TemplateResponse(
        request,
        "services_list.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "services_with_deposit": services_with_deposit,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.get("/panel/services/new", response_class=HTMLResponse)
async def panel_services_new_form(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
):
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "service_form.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "service": None,
            "form": {},
            "errors": {},
            "deposit_preview": None,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.post("/panel/services/new", response_class=HTMLResponse)
async def panel_services_new_submit(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    form = dict(await request.form())
    data, errors = _parse_service_form(form)

    csrf_token = generate_csrf_token()

    if errors:
        deposit_preview = None
        if "price" in data:
            deposit_preview = effective_deposit(data["price"], None)
        response = templates.TemplateResponse(
            request,
            "service_form.html",
            {
                "tenant": tenant,
                "csrf_token": csrf_token,
                "service": None,
                "form": form,
                "errors": errors,
                "deposit_preview": deposit_preview,
            },
        )
        set_csrf_cookie(response, csrf_token)
        return response

    new_service = Service(
        tenant_id=tenant.id,
        name=data["name"],
        duration_minutes=data["duration_minutes"],
        price=data["price"],
        deposit_amount=data.get("deposit_amount"),
        is_active=True,
    )
    session.add(new_service)
    await session.commit()
    return RedirectResponse(
        url="/panel/services", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/panel/services/{service_id}/edit", response_class=HTMLResponse)
async def panel_services_edit_form(
    request: Request,
    service_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    service = await session.get(Service, service_id)
    if not service or service.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")

    csrf_token = generate_csrf_token()
    deposit_preview = effective_deposit(service.price, None)
    form = {
        "name": service.name,
        "duration_minutes": str(service.duration_minutes),
        "price": str(service.price),
        "deposit_amount": (
            str(service.deposit_amount) if service.deposit_amount is not None else ""
        ),
    }
    response = templates.TemplateResponse(
        request,
        "service_form.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "service": service,
            "form": form,
            "errors": {},
            "deposit_preview": deposit_preview,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.post("/panel/services/{service_id}/edit", response_class=HTMLResponse)
async def panel_services_edit_submit(
    request: Request,
    service_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    service = await session.get(Service, service_id)
    if not service or service.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")

    await validate_csrf(request)
    form = dict(await request.form())
    data, errors = _parse_service_form(form)

    csrf_token = generate_csrf_token()

    if errors:
        deposit_preview = effective_deposit(data.get("price", service.price), None)
        response = templates.TemplateResponse(
            request,
            "service_form.html",
            {
                "tenant": tenant,
                "csrf_token": csrf_token,
                "service": service,
                "form": form,
                "errors": errors,
                "deposit_preview": deposit_preview,
            },
        )
        set_csrf_cookie(response, csrf_token)
        return response

    service.name = data["name"]
    service.duration_minutes = data["duration_minutes"]
    service.price = data["price"]
    service.deposit_amount = data.get("deposit_amount")
    session.add(service)
    await session.commit()
    return RedirectResponse(
        url="/panel/services", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/panel/services/{service_id}/toggle", response_class=HTMLResponse)
async def panel_services_toggle(
    request: Request,
    service_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    service = await session.get(Service, service_id)
    if not service or service.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")

    await validate_csrf(request)
    service.is_active = not service.is_active
    session.add(service)
    await session.commit()
    return RedirectResponse(
        url="/panel/services", status_code=status.HTTP_303_SEE_OTHER
    )


# --- PANEL STAFF ---


@app.get("/panel/staff", response_class=HTMLResponse)
async def panel_staff_list(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    csrf_token = generate_csrf_token()
    stmt = select(Staff).where(Staff.tenant_id == tenant.id).order_by(Staff.id)
    staff_members = (await session.execute(stmt)).scalars().all()
    response = templates.TemplateResponse(
        request,
        "staff_list.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "staff_members": staff_members,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.get("/panel/staff/new", response_class=HTMLResponse)
async def panel_staff_new_form(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
):
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "staff_form.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "staff": None,
            "form": {},
            "errors": {},
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.post("/panel/staff/new", response_class=HTMLResponse)
async def panel_staff_new_submit(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    form = dict(await request.form())
    errors = {}
    data = {}

    name = form.get("name", "").strip()
    if not name:
        errors["name"] = "El nombre es obligatorio."
    else:
        data["name"] = name

    csrf_token = generate_csrf_token()

    if errors:
        response = templates.TemplateResponse(
            request,
            "staff_form.html",
            {
                "tenant": tenant,
                "csrf_token": csrf_token,
                "staff": None,
                "form": form,
                "errors": errors,
            },
        )
        set_csrf_cookie(response, csrf_token)
        return response

    new_staff = Staff(
        tenant_id=tenant.id,
        name=data["name"],
        is_active=True,
    )
    session.add(new_staff)
    await session.commit()
    return RedirectResponse(url="/panel/staff", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/panel/staff/{staff_id}/edit", response_class=HTMLResponse)
async def panel_staff_edit_form(
    request: Request,
    staff_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    staff = await session.get(Staff, staff_id)
    if not staff or staff.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Miembro no encontrado")

    csrf_token = generate_csrf_token()
    form = {"name": staff.name}
    response = templates.TemplateResponse(
        request,
        "staff_form.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "staff": staff,
            "form": form,
            "errors": {},
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.post("/panel/staff/{staff_id}/edit", response_class=HTMLResponse)
async def panel_staff_edit_submit(
    request: Request,
    staff_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    staff = await session.get(Staff, staff_id)
    if not staff or staff.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Miembro no encontrado")

    await validate_csrf(request)
    form = dict(await request.form())
    errors = {}
    data = {}

    name = form.get("name", "").strip()
    if not name:
        errors["name"] = "El nombre es obligatorio."
    else:
        data["name"] = name

    csrf_token = generate_csrf_token()

    if errors:
        response = templates.TemplateResponse(
            request,
            "staff_form.html",
            {
                "tenant": tenant,
                "csrf_token": csrf_token,
                "staff": staff,
                "form": form,
                "errors": errors,
            },
        )
        set_csrf_cookie(response, csrf_token)
        return response

    staff.name = data["name"]
    session.add(staff)
    await session.commit()
    return RedirectResponse(url="/panel/staff", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/panel/staff/{staff_id}/toggle", response_class=HTMLResponse)
async def panel_staff_toggle(
    request: Request,
    staff_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    staff = await session.get(Staff, staff_id)
    if not staff or staff.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Miembro no encontrado")

    await validate_csrf(request)
    staff.is_active = not staff.is_active
    session.add(staff)
    await session.commit()
    return RedirectResponse(url="/panel/staff", status_code=status.HTTP_303_SEE_OTHER)


# --- PANEL BUSINESS HOURS (horarios del negocio) ---


def _parse_business_hours_form(form: dict) -> tuple[dict, dict]:
    """Parsea y valida el formulario de horarios de atención.

    Returns (data, errors). Si errors está vacío, data es usable para DB.
    """
    errors: dict = {}
    data: dict = {}

    # day_of_week
    try:
        dow = int(form.get("day_of_week", ""))
        if not 0 <= dow <= 6:
            raise ValueError
        data["day_of_week"] = dow
    except (ValueError, TypeError):
        errors["day_of_week"] = "Seleccioná un día de la semana válido."

    # start_time
    start_raw = form.get("start_time", "").strip()
    if not start_raw:
        errors["start_time"] = "La hora de apertura es obligatoria."
    else:
        try:
            data["start_time"] = time.fromisoformat(start_raw)
        except ValueError:
            errors["start_time"] = "Formato de hora inválido (use HH:MM)."

    # end_time
    end_raw = form.get("end_time", "").strip()
    if not end_raw:
        errors["end_time"] = "La hora de cierre es obligatoria."
    else:
        try:
            data["end_time"] = time.fromisoformat(end_raw)
        except ValueError:
            errors["end_time"] = "Formato de hora inválido (use HH:MM)."

    # Validación cruzada: start < end
    if "start_time" in data and "end_time" in data:
        if data["start_time"] >= data["end_time"]:
            errors["order"] = "La hora de apertura debe ser anterior a la de cierre."

    return data, errors


@app.get("/panel/horarios", response_class=HTMLResponse)
async def panel_business_hours_list(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    csrf_token = generate_csrf_token()
    stmt = (
        select(BusinessHours)
        .where(
            and_(
                BusinessHours.tenant_id == tenant.id,
                BusinessHours.staff_id.is_(None),
            )
        )
        .order_by(BusinessHours.day_of_week, BusinessHours.start_time)
    )
    business_hours = (await session.execute(stmt)).scalars().all()

    day_names = [
        "Lunes",
        "Martes",
        "Miércoles",
        "Jueves",
        "Viernes",
        "Sábado",
        "Domingo",
    ]

    response = templates.TemplateResponse(
        request,
        "business_hours_list.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "business_hours": business_hours,
            "day_names": day_names,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.get("/panel/horarios/new", response_class=HTMLResponse)
async def panel_business_hours_new_form(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
):
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "business_hours_form.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "bh": None,
            "form": {},
            "errors": {},
            "form_errors": [],
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.post("/panel/horarios/new", response_class=HTMLResponse)
async def panel_business_hours_new_submit(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    form = dict(await request.form())
    data, errors = _parse_business_hours_form(form)

    # Verificar solapamiento con horarios existentes del mismo día
    if "day_of_week" in data:
        stmt = select(BusinessHours).where(
            and_(
                BusinessHours.tenant_id == tenant.id,
                BusinessHours.staff_id.is_(None),
                BusinessHours.day_of_week == data["day_of_week"],
            )
        )
        existing = (await session.execute(stmt)).scalars().all()
        if data.get("start_time") and data.get("end_time"):
            for existing_bh in existing:
                # Verificar solapamiento: [start1, end1) ∩ [start2, end2) ≠ ∅
                if not (
                    data["end_time"] <= existing_bh.start_time
                    or data["start_time"] >= existing_bh.end_time
                ):
                    errors["overlap"] = (
                        f"Este horario se solapa con uno existente "
                        f"({existing_bh.start_time.strftime('%H:%M')}–{existing_bh.end_time.strftime('%H:%M')})."
                    )
                    break

    csrf_token = generate_csrf_token()

    if errors:
        response = templates.TemplateResponse(
            request,
            "business_hours_form.html",
            {
                "tenant": tenant,
                "csrf_token": csrf_token,
                "bh": None,
                "form": form,
                "errors": errors,
                "form_errors": list(errors.values()),
            },
        )
        set_csrf_cookie(response, csrf_token)
        return response

    new_bh = BusinessHours(
        tenant_id=tenant.id,
        staff_id=None,
        day_of_week=data["day_of_week"],
        start_time=data["start_time"],
        end_time=data["end_time"],
    )
    session.add(new_bh)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        errors["overlap"] = "Ya existe un horario para este día."
        response = templates.TemplateResponse(
            request,
            "business_hours_form.html",
            {
                "tenant": tenant,
                "csrf_token": csrf_token,
                "bh": None,
                "form": form,
                "errors": errors,
                "form_errors": list(errors.values()),
            },
        )
        set_csrf_cookie(response, csrf_token)
        return response

    return RedirectResponse(
        url="/panel/horarios", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/panel/horarios/{bh_id}/edit", response_class=HTMLResponse)
async def panel_business_hours_edit_form(
    request: Request,
    bh_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    bh = await session.get(BusinessHours, bh_id)
    if not bh or bh.tenant_id != tenant.id or bh.staff_id is not None:
        raise HTTPException(status_code=404, detail="Horario no encontrado")

    csrf_token = generate_csrf_token()
    form = {
        "day_of_week": str(bh.day_of_week),
        "start_time": bh.start_time.strftime("%H:%M"),
        "end_time": bh.end_time.strftime("%H:%M"),
    }
    response = templates.TemplateResponse(
        request,
        "business_hours_form.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "bh": bh,
            "form": form,
            "errors": {},
            "form_errors": [],
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.post("/panel/horarios/{bh_id}/edit", response_class=HTMLResponse)
async def panel_business_hours_edit_submit(
    request: Request,
    bh_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    bh = await session.get(BusinessHours, bh_id)
    if not bh or bh.tenant_id != tenant.id or bh.staff_id is not None:
        raise HTTPException(status_code=404, detail="Horario no encontrado")

    await validate_csrf(request)
    form = dict(await request.form())
    data, errors = _parse_business_hours_form(form)

    # Verificar solapamiento con otros horarios del mismo día (excluyendo el actual)
    if "day_of_week" in data and "start_time" in data and "end_time" in data:
        stmt = select(BusinessHours).where(
            and_(
                BusinessHours.tenant_id == tenant.id,
                BusinessHours.staff_id.is_(None),
                BusinessHours.day_of_week == data["day_of_week"],
                BusinessHours.id != bh.id,
            )
        )
        existing = (await session.execute(stmt)).scalars().all()
        for existing_bh in existing:
            if not (
                data["end_time"] <= existing_bh.start_time
                or data["start_time"] >= existing_bh.end_time
            ):
                errors["overlap"] = (
                    f"Este horario se solapa con uno existente "
                    f"({existing_bh.start_time.strftime('%H:%M')}–{existing_bh.end_time.strftime('%H:%M')})."
                )
                break

    csrf_token = generate_csrf_token()

    if errors:
        response = templates.TemplateResponse(
            request,
            "business_hours_form.html",
            {
                "tenant": tenant,
                "csrf_token": csrf_token,
                "bh": bh,
                "form": form,
                "errors": errors,
                "form_errors": list(errors.values()),
            },
        )
        set_csrf_cookie(response, csrf_token)
        return response

    bh.day_of_week = data["day_of_week"]
    bh.start_time = data["start_time"]
    bh.end_time = data["end_time"]
    session.add(bh)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        errors["overlap"] = "Ya existe un horario para este día."
        response = templates.TemplateResponse(
            request,
            "business_hours_form.html",
            {
                "tenant": tenant,
                "csrf_token": csrf_token,
                "bh": bh,
                "form": form,
                "errors": errors,
                "form_errors": list(errors.values()),
            },
        )
        set_csrf_cookie(response, csrf_token)
        return response

    return RedirectResponse(
        url="/panel/horarios", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/panel/horarios/{bh_id}/delete", response_class=HTMLResponse)
async def panel_business_hours_delete(
    request: Request,
    bh_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    bh = await session.get(BusinessHours, bh_id)
    if not bh or bh.tenant_id != tenant.id or bh.staff_id is not None:
        raise HTTPException(status_code=404, detail="Horario no encontrado")

    await validate_csrf(request)
    await session.delete(bh)
    await session.commit()
    return RedirectResponse(
        url="/panel/horarios", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Panel: Agenda (vista de turnos por día)
# ---------------------------------------------------------------------------

_AGENDA_STATUS_LABELS = {
    "pending": "Pendiente de pago",
    "confirmed": "Confirmado",
    "expired": "Expirado",
    "cancelled": "Cancelado",
    "no_show": "No se presentó",
    "completed": "Completado",
}


def _format_agenda_day_label(d: date) -> str:
    """Ej: 'lunes 29 de septiembre de 2026'."""
    weekday_names = [
        "lunes",
        "martes",
        "miércoles",
        "jueves",
        "viernes",
        "sábado",
        "domingo",
    ]
    month_names = [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ]
    return f"{weekday_names[d.weekday()]} {d.day} de {month_names[d.month - 1]} de {d.year}"


@app.get("/panel/agenda", response_class=HTMLResponse)
async def panel_agenda(
    request: Request,
    day: Annotated[str | None, Query()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """
    Vista de agenda por día: todos los turnos del tenant que tocan el día
    dado, ordenados por horario de inicio. Las horas se muestran en la
    zona horaria del tenant (Booking.start_time está en UTC en la DB).

    El filtro es por solapamiento (start < fin del día AND end > inicio
    del día) — el mismo patrón que usan los endpoints de slots — para que
    un turno que empieza 23:30 del día anterior y termina 00:30 de hoy
    aparezca también en la vista de hoy.
    """
    tenant_tz = ZoneInfo(
        tenant.timezone if tenant.timezone else "America/Argentina/Buenos_Aires"
    )
    today_local = datetime.now(tenant_tz).date()

    target_day = today_local
    if day:
        try:
            target_day = date.fromisoformat(day)
        except ValueError:
            # ?day= inválido o mal formateado → cae a hoy en lugar de 422
            target_day = today_local

    # Rango del día en el timezone del tenant → compara contra los
    # timestamptz (UTC) de la DB sin errores de conversión.
    day_start = datetime.combine(target_day, time(0, 0), tzinfo=tenant_tz)
    day_end = day_start + timedelta(days=1)

    stmt = (
        select(Booking, Service)
        .join(Service, Booking.service_id == Service.id)
        .where(
            # Solapamiento con el día mostrado: incluye turnos que
            # empiezan el día anterior pero llegan hasta hoy.
            and_(
                Booking.tenant_id == tenant.id,
                Booking.start_time < day_end,
                Booking.end_time > day_start,
            )
        )
        .order_by(Booking.start_time)
    )
    rows = (await session.execute(stmt)).all()

    agenda_items = [
        {
            "booking_id": b.id,
            "client_name": b.client_name,
            "client_phone": b.client_phone,
            "service_name": s.name,
            "start_local": b.start_time.astimezone(tenant_tz).strftime("%H:%M"),
            "end_local": b.end_time.astimezone(tenant_tz).strftime("%H:%M"),
            "status": b.status,
            "status_label": _AGENDA_STATUS_LABELS.get(b.status, b.status),
        }
        for b, s in rows
    ]

    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "agenda.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "target_day": target_day,
            "day_label": _format_agenda_day_label(target_day),
            "prev_day": (target_day - timedelta(days=1)).isoformat(),
            "next_day": (target_day + timedelta(days=1)).isoformat(),
            "is_today": target_day == today_local,
            "agenda_items": agenda_items,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


# --- PANEL AGENDA: acciones sobre turno (Tarea 8) ---

_AGENDA_ACTION_REDIRECT = "/panel/agenda"


async def _load_booking_for_tenant(
    session: AsyncSession, booking_id: int, tenant: Tenant
) -> Booking:
    booking = await session.get(Booking, booking_id)
    if not booking or booking.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    return booking


def _redirect_to_agenda(day: str | None = None) -> RedirectResponse:
    url = _AGENDA_ACTION_REDIRECT
    if day:
        url = f"{url}?day={day}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/panel/agenda/{booking_id}/confirm", response_class=HTMLResponse)
async def panel_agenda_confirm(
    request: Request,
    booking_id: int,
    day: Annotated[str | None, Form()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    booking = await _load_booking_for_tenant(session, booking_id, tenant)
    try:
        await transition_booking_status(session, booking, "confirmed", actor="owner")
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await session.commit()
    return _redirect_to_agenda(day)


@app.post("/panel/agenda/{booking_id}/cancel", response_class=HTMLResponse)
async def panel_agenda_cancel(
    request: Request,
    booking_id: int,
    reason: Annotated[str | None, Form()] = None,
    day: Annotated[str | None, Form()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    booking = await _load_booking_for_tenant(session, booking_id, tenant)
    try:
        await transition_booking_status(
            session, booking, "cancelled", actor="owner", reason=reason
        )
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await session.commit()
    return _redirect_to_agenda(day)


@app.post("/panel/agenda/{booking_id}/no-show", response_class=HTMLResponse)
async def panel_agenda_no_show(
    request: Request,
    booking_id: int,
    day: Annotated[str | None, Form()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    booking = await _load_booking_for_tenant(session, booking_id, tenant)
    try:
        await transition_booking_status(session, booking, "no_show", actor="owner")
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except BookingNotStartedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await session.commit()
    return _redirect_to_agenda(day)


@app.post("/panel/agenda/{booking_id}/complete", response_class=HTMLResponse)
async def panel_agenda_complete(
    request: Request,
    booking_id: int,
    day: Annotated[str | None, Form()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    booking = await _load_booking_for_tenant(session, booking_id, tenant)
    try:
        await transition_booking_status(session, booking, "completed", actor="owner")
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except BookingNotStartedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await session.commit()
    return _redirect_to_agenda(day)
