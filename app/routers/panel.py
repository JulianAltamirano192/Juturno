from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_tenant_from_session
from app.booking_actions import (
    BookingNotStartedError,
    InvalidTransitionError,
    transition_booking_status,
)
from app.config import settings
from app.csrf import generate_csrf_token, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.models import Booking, BusinessHours, Payment, Service, Staff, Tenant
from app.mp_connect import mp_authorization_redirect
from app.services import effective_deposit
from app.templates import templates

router = APIRouter()

_AGENDA_STATUS_LABELS = {
    "pending": "Pendiente de pago",
    "confirmed": "Confirmado",
    "expired": "Expirado",
    "cancelled": "Cancelado",
    "no_show": "No se presentó",
    "completed": "Completado",
}

_AGENDA_ACTION_REDIRECT = "/panel/agenda"


# Máximo de Numeric(10,2); un valor mayor hace fallar el INSERT con 500.
_MAX_AMOUNT = Decimal("99999999.99")


def _parse_amount(raw: str) -> Decimal:
    """Monto del form redondeado como lo guarda Numeric(10,2).

    Lanza ValueError/InvalidOperation si no es un número finito.
    """
    amount = Decimal(raw.replace(",", "."))
    if not amount.is_finite():
        raise ValueError
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
        price = _parse_amount(form.get("price", ""))
        if not Decimal("0.01") <= price <= _MAX_AMOUNT:
            raise ValueError
        data["price"] = price
    except Exception:
        errors["price"] = (
            "El precio debe ser un número entre 0.01 y 99999999.99 (ej: 5000.00)."
        )

    deposit_raw = form.get("deposit_amount", "").strip()
    if deposit_raw == "":
        data["deposit_amount"] = None
    else:
        try:
            deposit = _parse_amount(deposit_raw)
            if not Decimal(0) <= deposit <= _MAX_AMOUNT:
                raise ValueError
        except Exception:
            errors["deposit_amount"] = (
                "La seña debe ser un número entre 0 y 99999999.99 (ej: 1500.00)."
            )
        else:
            if "price" in data and deposit > data["price"]:
                errors["deposit_amount"] = "La seña no puede ser mayor que el precio."
            else:
                data["deposit_amount"] = deposit

    return data, errors


def _parse_business_hours_form(form: dict) -> tuple[dict, dict]:
    """Parsea y valida el formulario de horarios de atención.

    Returns (data, errors). Si errors está vacío, data es usable para DB.
    """
    errors: dict = {}
    data: dict = {}

    try:
        dow = int(form.get("day_of_week", ""))
        if not 0 <= dow <= 6:
            raise ValueError
        data["day_of_week"] = dow
    except (ValueError, TypeError):
        errors["day_of_week"] = "Seleccioná un día de la semana válido."

    start_raw = form.get("start_time", "").strip()
    if not start_raw:
        errors["start_time"] = "La hora de apertura es obligatoria."
    else:
        try:
            data["start_time"] = time.fromisoformat(start_raw)
        except ValueError:
            errors["start_time"] = "Formato de hora inválido (use HH:MM)."

    end_raw = form.get("end_time", "").strip()
    if not end_raw:
        errors["end_time"] = "La hora de cierre es obligatoria."
    else:
        try:
            data["end_time"] = time.fromisoformat(end_raw)
        except ValueError:
            errors["end_time"] = "Formato de hora inválido (use HH:MM)."

    if (
        "start_time" in data
        and "end_time" in data
        and data["start_time"] >= data["end_time"]
    ):
        errors["order"] = "La hora de apertura debe ser anterior a la de cierre."

    return data, errors


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


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def _tenant_tz(tenant: Tenant) -> ZoneInfo:
    return ZoneInfo(
        tenant.timezone if tenant.timezone else "America/Argentina/Buenos_Aires"
    )


async def _bookings_for_day(
    session: AsyncSession, tenant: Tenant, target_day: date
) -> list[tuple[Booking, Service]]:
    """Turnos del tenant que tocan el día dado (en su zona horaria), por inicio."""
    tenant_tz = _tenant_tz(tenant)
    day_start = datetime.combine(target_day, time(0, 0), tzinfo=tenant_tz)
    day_end = day_start + timedelta(days=1)
    stmt = (
        select(Booking, Service)
        .join(Service, Booking.service_id == Service.id)
        .where(
            and_(
                Booking.tenant_id == tenant.id,
                Service.tenant_id == tenant.id,
                Booking.start_time < day_end,
                Booking.end_time > day_start,
            )
        )
        .order_by(Booking.start_time)
    )
    return [(b, s) for b, s in (await session.execute(stmt)).all()]


async def _tenant_has(
    session: AsyncSession, model, tenant_id: int, *conditions
) -> bool:
    stmt = select(model.id).where(model.tenant_id == tenant_id, *conditions).limit(1)
    return (await session.execute(stmt)).first() is not None


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """Vista principal del panel: resumen del día, próximos turnos y checklist."""
    tenant_tz = _tenant_tz(tenant)
    now = datetime.now(tenant_tz)
    rows = await _bookings_for_day(session, tenant, now.date())

    def _count(state: str) -> int:
        return sum(1 for b, _ in rows if b.status == state)

    stats = {
        "total": sum(1 for b, _ in rows if b.status not in ("cancelled", "expired")),
        "confirmed": _count("confirmed"),
        "pending": _count("pending"),
        "completed": _count("completed"),
    }
    upcoming = [
        {
            "client_name": b.client_name,
            "service_name": s.name,
            "start_local": b.start_time.astimezone(tenant_tz).strftime("%H:%M"),
            "status": b.status,
            "status_label": _AGENDA_STATUS_LABELS.get(b.status, b.status),
        }
        for b, s in rows
        if b.status in ("pending", "confirmed") and b.end_time > now
    ][:3]

    setup = [
        {
            "key": "services",
            "label": "Cargá tus servicios",
            "href": "/panel/services",
            "done": await _tenant_has(session, Service, tenant.id),
        },
        {
            "key": "hours",
            "label": "Definí tus horarios",
            "href": "/panel/horarios",
            "done": await _tenant_has(
                session,
                BusinessHours,
                tenant.id,
                BusinessHours.staff_id.is_(None),
            ),
        },
        {
            "key": "staff",
            "label": "Sumá a tu personal",
            "href": "/panel/staff",
            "done": await _tenant_has(session, Staff, tenant.id),
        },
        {
            "key": "mp",
            "label": "Conectá Mercado Pago",
            "href": "/panel/settings",
            "done": tenant.mp_access_token_enc is not None,
        },
    ]

    public_base = settings.PUBLIC_BASE_URL.rstrip("/")
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "day_label": _format_agenda_day_label(now.date()),
            "stats": stats,
            "upcoming": upcoming,
            "setup": setup,
            "setup_done": sum(1 for item in setup if item["done"]),
            "public_url": f"{public_base}/t/{tenant.slug}" if tenant.slug else None,
            "public_url_display": f"{public_base.split('://', 1)[-1]}/t/",
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


# Panel: ajustes y conexión con Mercado Pago
# ---------------------------------------------------------------------------

_MP_FLASH = {
    "connected": "Cuenta de Mercado Pago conectada.",
    "disconnected": "Cuenta de Mercado Pago desconectada.",
    "pending": (
        "No podés desconectar Mercado Pago: tenés señas pendientes de pago. "
        "Esperá a que se paguen o venzan."
    ),
    "error": "No se pudo conectar Mercado Pago. Probá de nuevo.",
    "other_browser": (
        "No se pudo conectar Mercado Pago: la autorización terminó en otro "
        "navegador. Volvé a intentarlo y completala en el mismo navegador."
    ),
    "account_in_use": (
        "Esa cuenta de Mercado Pago ya está vinculada a otro negocio. "
        "Conectá una cuenta distinta."
    ),
}

# Mensajes de error: se muestran con banner de error, no de éxito.
_MP_FLASH_ERRORS = {"pending", "error", "other_browser", "account_in_use"}


@router.get("/panel/settings", response_class=HTMLResponse)
async def panel_settings(
    request: Request,
    mp: str | None = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
):
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "settings.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "mp_connected": tenant.mp_access_token_enc is not None,
            # Solo mensajes fijos: nunca se refleja el query param.
            "flash": _MP_FLASH.get(mp or ""),
            "flash_error": mp in _MP_FLASH_ERRORS,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/panel/mp/connect/start")
async def panel_mp_connect_start(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
):
    await validate_csrf(request)
    # El state queda atado a este navegador por cookie; la URL de vuelta la
    # arma el callback en el servidor (sin open redirect a través del state).
    return await mp_authorization_redirect(tenant.id)


@router.post("/panel/mp/disconnect")
async def panel_mp_disconnect(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    # Con señas de MP pendientes y todavía pagables, desconectar dejaría el
    # webhook sin token para verificar el pago: se bloquea.
    pending_mp = (
        select(Booking)
        .join(Payment, Payment.booking_id == Booking.id)
        .where(
            Booking.tenant_id == tenant.id,
            Booking.status == "pending",
            Payment.mp_preference_id.is_not(None),
        )
        .limit(1)
    )
    if tenant.deposit_expiration_minutes is not None:
        # Mismo deadline que process_deposit_expiration (created_at + minutos)
        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=tenant.deposit_expiration_minutes
        )
        pending_mp = pending_mp.where(Booking.created_at >= cutoff)
    if (await session.execute(pending_mp)).first() is not None:
        return RedirectResponse(
            url="/panel/settings?mp=pending", status_code=status.HTTP_302_FOUND
        )
    # ponytail: solo borra los tokens locales; no revoca la autorización en MP.
    tenant.mp_access_token_enc = None
    tenant.mp_refresh_token_enc = None
    tenant.mp_token_expires_at = None
    tenant.mp_user_id = None
    tenant.mp_public_key = None
    tenant.mp_alias = None
    session.add(tenant)
    await session.commit()
    return RedirectResponse(
        url="/panel/settings?mp=disconnected", status_code=status.HTTP_302_FOUND
    )


# ---------------------------------------------------------------------------
# Panel: CRUD de servicios
# ---------------------------------------------------------------------------


@router.get("/panel/services", response_class=HTMLResponse)
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


@router.get("/panel/services/new", response_class=HTMLResponse)
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


@router.post("/panel/services/new", response_class=HTMLResponse)
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


@router.get("/panel/services/{service_id}/edit", response_class=HTMLResponse)
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


@router.post("/panel/services/{service_id}/edit", response_class=HTMLResponse)
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


@router.post("/panel/services/{service_id}/toggle", response_class=HTMLResponse)
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


# ---------------------------------------------------------------------------
# Panel: CRUD de staff
# ---------------------------------------------------------------------------


@router.get("/panel/staff", response_class=HTMLResponse)
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


@router.get("/panel/staff/new", response_class=HTMLResponse)
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


@router.post("/panel/staff/new", response_class=HTMLResponse)
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


@router.get("/panel/staff/{staff_id}/edit", response_class=HTMLResponse)
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


@router.post("/panel/staff/{staff_id}/edit", response_class=HTMLResponse)
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


@router.post("/panel/staff/{staff_id}/toggle", response_class=HTMLResponse)
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


# ---------------------------------------------------------------------------
# Panel: Business Hours (horarios del negocio)
# ---------------------------------------------------------------------------


@router.get("/panel/horarios", response_class=HTMLResponse)
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


@router.get("/panel/horarios/new", response_class=HTMLResponse)
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


@router.post("/panel/horarios/new", response_class=HTMLResponse)
async def panel_business_hours_new_submit(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    form = dict(await request.form())
    data, errors = _parse_business_hours_form(form)

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


@router.get("/panel/horarios/{bh_id}/edit", response_class=HTMLResponse)
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


@router.post("/panel/horarios/{bh_id}/edit", response_class=HTMLResponse)
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


@router.post("/panel/horarios/{bh_id}/delete", response_class=HTMLResponse)
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


@router.get("/panel/agenda", response_class=HTMLResponse)
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
    """
    tenant_tz = _tenant_tz(tenant)
    today_local = datetime.now(tenant_tz).date()

    target_day = today_local
    if day:
        try:
            target_day = date.fromisoformat(day)
        except ValueError:
            target_day = today_local

    rows = await _bookings_for_day(session, tenant, target_day)

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


# ---------------------------------------------------------------------------
# Panel Agenda: acciones sobre turno
# ---------------------------------------------------------------------------


@router.post("/panel/agenda/{booking_id}/confirm", response_class=HTMLResponse)
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


@router.post("/panel/agenda/{booking_id}/cancel", response_class=HTMLResponse)
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


@router.post("/panel/agenda/{booking_id}/no-show", response_class=HTMLResponse)
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


@router.post("/panel/agenda/{booking_id}/complete", response_class=HTMLResponse)
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
