import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_serializer
from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.booking_actions import transition_booking_status
from app.brand import brand_theme
from app.config import settings
from app.database import get_db
from app.limiter import limiter
from app.logo import logo_version
from app.models import Booking, Payment, Service, Staff, Tenant, TenantLogo
from app.mp_connect import ERR_PAGO_NO_CONFIGURADO, resolve_mp_access_token
from app.mp_crypto import MPTokenCryptoError
from app.mp_webhooks import create_mp_preference
from app.schemas import AvailableSlotsResponse, BookingCreate, clean_client_name
from app.services import (
    active_staff,
    compute_any_staff_slots,
    compute_available_slots,
    effective_deposit,
    free_staff_for,
    has_overlapping_booking,
    is_bookable_start,
)
from app.templates import templates

logger = logging.getLogger(__name__)

_MAX_PAYMENT_LINK_LIFETIME = timedelta(hours=48)
# Upcoming confirmed no-deposit bookings a phone can hold per business.
_MAX_UPCOMING_FREE_BOOKINGS_PER_PHONE = 3

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
    status: str
    staff_name: str | None = None


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
# "Sin preferencia" cuesta unas 7 consultas por profesional: con tope por IP.
@limiter.limit("60/minute")
async def get_public_available_slots(
    request: Request,
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
    if not service or service.tenant_id != tenant_id or not service.is_active:
        raise HTTPException(status_code=404, detail="Service not found")

    # Mismo criterio que la reserva: no ofrecer horarios que darían 404.
    if staff_id is not None:
        staff = await session.get(Staff, staff_id)
        if not staff or staff.tenant_id != tenant_id or not staff.is_active:
            raise HTTPException(status_code=404, detail="Staff not found for tenant")

    common = {
        "session": session,
        "tenant_id": tenant_id,
        "service": service,
        "day": day,
        "tenant_timezone": tenant_timezone,
        "horizon_days": tenant.booking_horizon_days,
    }
    team = [] if staff_id is not None else await active_staff(session, tenant_id)
    if len(team) >= 2:
        # "Sin preferencia" con varios profesionales: vale cualquiera libre.
        slots = await compute_any_staff_slots(staff_ids=[m.id for m in team], **common)
    else:
        slots = await compute_available_slots(staff_id=staff_id, **common)
    return AvailableSlotsResponse(
        date=day,
        service_duration_min=service.duration_minutes,
        timezone=tenant.timezone,
        slots=slots,
    )


async def _idempotent_response(session: AsyncSession, booking: Booking) -> JSONResponse:
    """200 con la reserva ya creada para esa idempotency_key."""
    payment_stmt = select(Payment).where(
        and_(
            Payment.booking_id == booking.id,
            Payment.method == "mercado_pago",
            Payment.mp_checkout_url.is_not(None),
        )
    )
    existing_payment = (await session.execute(payment_stmt)).scalar_one_or_none()
    checkout_url = existing_payment.mp_checkout_url if existing_payment else ""
    staff = await session.get(Staff, booking.staff_id) if booking.staff_id else None
    return JSONResponse(
        status_code=200,
        content={
            "message": "Reserva recuperada (idempotente)",
            "booking_id": booking.id,
            "payment_url": checkout_url,
            "status": booking.status,
            "staff_name": staff.name if staff else None,
        },
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
    Si el servicio no cobra seña (seña efectiva 0), la reserva queda
    'confirmed' sin pasar por MP y payment_url es "".

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

    try:
        client_name = clean_client_name(payload.client_name)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=("Ingresá tu nombre (entre 2 y 60 caracteres, sin links)."),
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
        return await _idempotent_response(session, existing_booking)

    service = await session.get(Service, payload.service_id)
    if not service or service.tenant_id != payload.tenant_id or not service.is_active:
        raise HTTPException(status_code=404, detail="Service not found for tenant")

    if payload.staff_id is not None:
        chosen = await session.get(Staff, payload.staff_id)
        if not chosen or chosen.tenant_id != payload.tenant_id or not chosen.is_active:
            raise HTTPException(status_code=404, detail="Staff not found for tenant")
        pool = [chosen]
    else:
        # "Sin preferencia": con 2+ profesionales se asigna uno libre; con 0 o
        # 1 el turno queda sin profesional, como siempre.
        team = await active_staff(session, payload.tenant_id)
        pool = team if len(team) >= 2 else []

    tenant_timezone = ZoneInfo(
        tenant.timezone if tenant.timezone else "America/Argentina/Buenos_Aires"
    )
    start_time = payload.start_time
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=tenant_timezone)

    bookable = [
        member
        for member in (pool or [None])
        if await is_bookable_start(
            session=session,
            tenant_id=payload.tenant_id,
            service=service,
            start_time=start_time,
            staff_id=member.id if member else None,
            tenant_timezone=tenant_timezone,
            horizon_days=tenant.booking_horizon_days,
        )
    ]
    if not bookable:
        raise HTTPException(
            status_code=422,
            detail="Ese horario no está disponible. Elegí uno de los turnos ofrecidos.",
        )

    end_time = start_time + timedelta(
        minutes=service.duration_minutes + service.buffer_minutes
    )

    if pool:
        # Un turno sin profesional no choca en el EXCLUDE con los asignados:
        # free_staff_for lo descarta antes.
        candidates = await free_staff_for(
            session=session,
            tenant_id=payload.tenant_id,
            start_time=start_time,
            end_time=end_time,
            staff=bookable,
            tenant_timezone=tenant_timezone,
        )
        if not candidates:
            raise HTTPException(
                status_code=409, detail="Slot ya reservado o superpuesto"
            )
    elif await has_overlapping_booking(
        session=session,
        tenant_id=payload.tenant_id,
        start_time=start_time,
        end_time=end_time,
        staff_id=None,
    ):
        # Sin profesional ocupa a todos: no puede pisar ni a uno asignado.
        raise HTTPException(status_code=409, detail="Slot ya reservado o superpuesto")
    else:
        candidates = [None]

    deposit_decimal = effective_deposit(service.price, service.deposit_amount)

    if deposit_decimal == 0:
        # No-deposit bookings are free confirmed slots that also send a
        # WhatsApp: cap them per phone so one number cannot fill the agenda
        # or be spammed.
        # ponytail: count-then-insert, two concurrent requests can reach
        # cap+1; lock per (tenant, phone) if that ever matters.
        upcoming_free = (
            await session.execute(
                select(func.count()).where(
                    Booking.tenant_id == payload.tenant_id,
                    Booking.client_phone == client_phone,
                    Booking.status == "confirmed",
                    Booking.deposit_at_booking == 0,
                    Booking.start_time > datetime.now(timezone.utc),
                    # Los que carga el negocio desde el panel tienen su propio
                    # tope. Se usa el actor, no la clave: la clave la elige el cliente.
                    Booking.status_changed_by.is_distinct_from("owner"),
                )
            )
        ).scalar_one()
        if upcoming_free >= _MAX_UPCOMING_FREE_BOOKINGS_PER_PHONE:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Ya tenés {_MAX_UPCOMING_FREE_BOOKINGS_PER_PHONE} turnos "
                    "reservados en este negocio. Para sacar otro, comunicate "
                    "directamente con el negocio."
                ),
            )

    # Se prueba cada candidato en un SAVEPOINT: si otro lo tomó entre el
    # chequeo y el INSERT, el EXCLUDE lo rechaza y se pasa al siguiente.
    new_booking = None
    for member in candidates:
        attempt = Booking(
            tenant_id=payload.tenant_id,
            service_id=payload.service_id,
            staff_id=member.id if member else None,
            client_name=client_name,
            client_phone=client_phone,
            start_time=start_time,
            end_time=end_time,
            price_at_booking=service.price,
            deposit_at_booking=deposit_decimal,
            idempotency_key=payload.idempotency_key,
            status="pending",
        )
        try:
            async with session.begin_nested():
                session.add(attempt)
                await session.flush()
        except IntegrityError:
            existing_booking = (
                await session.execute(existing_stmt)
            ).scalar_one_or_none()
            if existing_booking is not None:
                return await _idempotent_response(session, existing_booking)
            continue
        new_booking = attempt
        staff_name = member.name if member else None
        break
    if new_booking is None:
        raise HTTPException(status_code=409, detail="Slot ya reservado o superpuesto")

    if deposit_decimal == 0:
        # Service without deposit: confirmed right away (the transition
        # enqueues the WhatsApp confirmation). No MP, no Payment.
        await transition_booking_status(
            session, new_booking, "confirmed", actor="client"
        )
        await session.commit()
        return PublicBookingResponse(
            message="Reserva confirmada",
            booking_id=new_booking.id,
            payment_url="",
            status=new_booking.status,
            staff_name=staff_name,
        )

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
            amount=deposit_decimal,
            client_name=client_name,
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
        status=new_booking.status,
        staff_name=staff_name,
    )


def _public_page_csp(nonce: str | None = None) -> str:
    """CSP de la página pública: solo recursos propios y nuestro <script> (nonce).

    Si algo llegara a inyectarse (HTML o CSS), no puede cargar ni mandar nada a
    otro sitio, ni correr otro script, ni meter la página en un iframe ajeno.
    El estilo inline queda permitido: la página y el color de marca lo usan.
    """
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    return (
        f"default-src 'none'; script-src {script}; "
        "style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'; object-src 'none'"
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
            headers={
                "Content-Security-Policy": _public_page_csp(),
                "Cache-Control": "no-store",
            },
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
    team = await active_staff(session, tenant.id)
    # Con un solo profesional no hay nada que elegir: no se muestra el paso.
    staff_data = [{"id": m.id, "name": m.name} for m in team] if len(team) >= 2 else []
    nonce = secrets.token_urlsafe(16)
    response = templates.TemplateResponse(
        request,
        "public_booking.html",
        {
            "csp_nonce": nonce,
            "tenant_name": tenant.name,
            "tenant_id": tenant.id,
            "public_notice": tenant.public_notice,
            "slug": tenant.slug,
            "locality": tenant.locality,
            "instagram": tenant.instagram,
            "facebook": tenant.facebook,
            "logo_version": logo_version(tenant.logo_updated_at),
            "brand_theme": brand_theme(tenant.brand_color),
            "timezone": tenant.timezone or "America/Argentina/Buenos_Aires",
            "services": services_data,
            "staff": staff_data,
        },
    )
    response.headers["Content-Security-Policy"] = _public_page_csp(nonce)
    # Ninguna caché compartida puede guardarla: repetiría el nonce para todos
    # (y mostraría servicios o avisos viejos).
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/t/{slug}/logo")
@limiter.limit("120/minute")
async def public_logo(
    slug: str, request: Request, session: AsyncSession = Depends(get_db)
):
    """Logo del negocio: siempre un WebP que re-codificamos nosotros (app/logo.py)."""
    data = (
        await session.execute(
            select(col(TenantLogo.data))
            .join(Tenant, Tenant.id == TenantLogo.tenant_id)
            .where(Tenant.slug == slug)
        )
    ).scalar_one_or_none()
    if data is None:
        raise HTTPException(status_code=404, detail="Logo not found")
    return Response(
        content=data,
        media_type="image/webp",
        headers={
            # Corto: un logo quitado deja de verse en una hora.
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            # Abierto directo en el navegador no puede ejecutar nada.
            # frame-ancestors no lo cubre default-src: va explícito.
            "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
        },
    )
