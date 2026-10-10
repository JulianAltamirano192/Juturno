from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_tenant
from app.database import get_db
from app.models import Booking, Service, Staff, Tenant
from app.phone import InvalidPhoneError, normalize_whatsapp_phone
from app.schemas import AvailableSlotsResponse, BookingCreate
from app.services import (
    compute_available_slots,
    effective_deposit,
    is_bookable_start,
)

router = APIRouter()


class TenantSettingsUpdate(BaseModel):
    """Campos de configuración que el tenant puede actualizar de sí mismo."""

    deposit_expiration_minutes: int | None = Field(default=None, ge=1)


@router.get("/bookings/available-slots", response_model=AvailableSlotsResponse)
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


@router.post("/bookings", status_code=201)
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
    if not service or service.tenant_id != payload.tenant_id or not service.is_active:
        raise HTTPException(status_code=404, detail="Service not found for tenant")

    if payload.staff_id is not None:
        staff = await session.get(Staff, payload.staff_id)
        if not staff or staff.tenant_id != payload.tenant_id or not staff.is_active:
            raise HTTPException(status_code=404, detail="Staff not found for tenant")

    tenant_timezone = ZoneInfo(tenant.timezone)
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
            detail=(
                "start_time no es un turno disponible: debe ser futuro y uno "
                "de los que devuelve GET /bookings/available-slots."
            ),
        )

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


@router.patch("/tenants/me")
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
