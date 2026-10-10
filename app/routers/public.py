import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_serializer
from sqlalchemy import and_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.limiter import limiter
from app.models import Booking, Payment, Service, Staff, Tenant
from app.mp_connect import ERR_PAGO_NO_CONFIGURADO, resolve_mp_access_token
from app.mp_crypto import MPTokenCryptoError
from app.mp_webhooks import create_mp_preference
from app.schemas import AvailableSlotsResponse, BookingCreate
from app.services import (
    compute_available_slots,
    effective_deposit,
    is_bookable_start,
)
from app.templates import templates

logger = logging.getLogger(__name__)

_MAX_PAYMENT_LINK_LIFETIME = timedelta(hours=48)

router = APIRouter()


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


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing_page(request: Request):
    return templates.TemplateResponse(request, "landing.html")


@router.get("/health")
async def health(session: AsyncSession = Depends(get_db)):
    """Health check profundo: verifica API, DB y Redis."""
    checks = {
        "api": "ok",
        "database": "unknown",
        "redis": "unknown",
    }
    is_healthy = True

    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        is_healthy = False

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


@router.get("/public/tenants/{identifier}", response_model=PublicTenantDetailResponse)
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


@router.get("/public/available-slots", response_model=AvailableSlotsResponse)
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


@router.post("/public/bookings", status_code=201)
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

    from app.phone import InvalidPhoneError, normalize_whatsapp_phone

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
    if not service or service.tenant_id != payload.tenant_id or not service.is_active:
        raise HTTPException(status_code=404, detail="Service not found for tenant")

    if payload.staff_id is not None:
        staff = await session.get(Staff, payload.staff_id)
        if not staff or staff.tenant_id != payload.tenant_id or not staff.is_active:
            raise HTTPException(status_code=404, detail="Staff not found for tenant")

    tenant_timezone = ZoneInfo(
        tenant.timezone if tenant.timezone else "America/Argentina/Buenos_Aires"
    )
    start_time = payload.start_time
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=tenant_timezone)

    if not await is_bookable_start(
        session=session,
        tenant_id=payload.tenant_id,
        service=service,
        start_time=start_time,
        staff_id=payload.staff_id,
        tenant_timezone=tenant_timezone,
    ):
        raise HTTPException(
            status_code=422,
            detail="Ese horario no está disponible. Elegí uno de los turnos ofrecidos.",
        )

    end_time = start_time + timedelta(minutes=service.duration_minutes)

    deposit_decimal = effective_deposit(service.price, service.deposit_amount)

    new_booking = Booking(
        tenant_id=payload.tenant_id,
        service_id=payload.service_id,
        staff_id=payload.staff_id,
        client_name=payload.client_name,
        client_phone=client_phone,
        start_time=start_time,
        end_time=end_time,
        price_at_booking=service.price,
        deposit_at_booking=deposit_decimal,
        idempotency_key=payload.idempotency_key,
        status="pending",
    )

    session.add(new_booking)
    try:
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

    # El link vence junto con la reserva: al expirar la seña o, sin
    # expiración configurada, a la hora del turno. Con tope de 48 h: un
    # turno lejano no puede mantener vivo el link (ni bloquear la
    # desconexión de MP) hasta esa fecha.
    now = datetime.now(timezone.utc)
    link_expires_at = min(start_time, now + _MAX_PAYMENT_LINK_LIFETIME)
    if tenant.deposit_expiration_minutes is not None:
        link_expires_at = min(
            link_expires_at,
            now + timedelta(minutes=tenant.deposit_expiration_minutes),
        )

    try:
        mp_result = await create_mp_preference(
            booking_id=new_booking.id,
            amount=float(deposit_decimal),
            client_name=payload.client_name,
            expires_at=link_expires_at,
            back_url=(
                f"{settings.PUBLIC_BASE_URL}/t/{tenant.slug}"
                f"?booking={new_booking.id}"
            ),
            access_token=mp_access_token,
        )
    except HTTPException:
        await session.rollback()
        raise

    new_payment = Payment(
        booking_id=new_booking.id,
        amount=deposit_decimal,
        method="mercado_pago",
        status="pending",
        mp_preference_id=mp_result["preference_id"],
        mp_checkout_url=mp_result["checkout_url"],
        mp_expires_at=link_expires_at,
    )
    session.add(new_payment)

    await session.commit()

    return PublicBookingResponse(
        message="Reserva creada",
        booking_id=new_booking.id,
        payment_url=mp_result["checkout_url"],
    )


@router.get("/t/{slug}", response_class=HTMLResponse)
async def public_booking_page(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Página pública de reserva (mobile-first) de un negocio."""
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
