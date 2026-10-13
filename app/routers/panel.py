from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, func, select
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
from app.models import (
    BOOKING_HORIZON_CHOICES,
    DEPOSIT_EXPIRATION_CHOICES,
    Booking,
    BusinessHours,
    Service,
    Staff,
    Tenant,
)
from app.mp_connect import (
    clear_mp_connection,
    has_payable_mp_payment,
    mp_authorization_redirect,
)
from app.phone import InvalidPhoneError, normalize_whatsapp_phone
from app.schemas import MANUAL_BOOKING_KEY_PREFIX, clean_client_name
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
# Turnos manuales por tenant en 24 h móviles (cada uno manda un WhatsApp).
MANUAL_BOOKINGS_PER_DAY = 50


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
    if not form.get("charge_deposit"):
        # "Cobrar seña al reservar" unchecked: no deposit, amount ignored.
        data["deposit_amount"] = Decimal(0)
    elif deposit_raw == "":
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

    if (
        form.get("charge_deposit")
        and "price" in data
        and "deposit_amount" in data
        and effective_deposit(data["price"], data["deposit_amount"]) == 0
    ):
        # Checked with an effective deposit of 0 (typed 0, or 30% rounds to 0)
        # would silently mean "no deposit": that is chosen by unchecking.
        del data["deposit_amount"]
        errors["deposit_amount"] = (
            "La seña tiene que ser mayor a 0. Para no cobrar seña, "
            "desmarcá «Cobrar seña al reservar»."
        )

    return data, errors


_DAY_NAMES = [
    "Lunes",
    "Martes",
    "Miércoles",
    "Jueves",
    "Viernes",
    "Sábado",
    "Domingo",
]


def _parse_business_hours_form(
    form: dict, days: list[str] | None = None
) -> tuple[dict, dict]:
    """Parsea y valida el formulario de horarios de atención.

    Sin `days` (edición) valida un único `day_of_week` → data["day_of_week"].
    Con `days` (alta, checkboxes repetidos) → data["days"], ordenados y sin
    duplicados; exige al menos uno.

    Returns (data, errors). Si errors está vacío, data es usable para DB.
    """
    errors: dict = {}
    data: dict = {}

    if days is None:
        try:
            dow = int(form.get("day_of_week", ""))
            if not 0 <= dow <= 6:
                raise ValueError
            data["day_of_week"] = dow
        except (ValueError, TypeError):
            errors["day_of_week"] = "Seleccioná un día de la semana válido."
    else:
        try:
            parsed = sorted({int(d) for d in days if d.strip()})
            if not all(0 <= d <= 6 for d in parsed):
                raise ValueError
        except ValueError:
            errors["day_of_week"] = "Seleccioná un día de la semana válido."
        else:
            if parsed:
                data["days"] = parsed
            else:
                errors["day_of_week"] = "Seleccioná al menos un día."

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
    # FOR UPDATE: el webhook de MP y el job de expiración también bloquean el
    # booking; sin el lock la acción decide sobre un estado viejo y lo pisa.
    stmt = (
        select(Booking)
        .where(Booking.id == booking_id, Booking.tenant_id == tenant.id)
        .with_for_update()
    )
    booking = (await session.execute(stmt)).scalar_one_or_none()
    if booking is None:
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
    csrf_token = generate_csrf_token(request)
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

_HORIZON_FLASH = {
    "saved": "Guardamos hasta cuándo pueden reservar tus clientes.",
    "invalid": "Elegí una de las opciones de la lista.",
}

_WHATSAPP_FLASH = {
    "saved": "Guardamos el WhatsApp para avisos de turnos nuevos.",
    "removed": "Desactivamos los avisos por WhatsApp.",
    "invalid": (
        "Ese número no parece un celular válido. Probá con código de área, "
        "por ejemplo 11 5555 5555."
    ),
}

_DEPOSIT_FLASH = {
    "saved": "Guardamos el plazo para pagar la seña.",
    "invalid": "Elegí una de las opciones de la lista.",
}


@router.get("/panel/settings", response_class=HTMLResponse)
async def panel_settings(
    request: Request,
    mp: str | None = None,
    horizon: str | None = None,
    deposit: str | None = None,
    whatsapp: str | None = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
):
    csrf_token = generate_csrf_token(request)
    # Solo mensajes fijos: nunca se refleja el query param.
    flash, flash_error = None, False
    for value, messages in (
        (mp, _MP_FLASH),
        (horizon, _HORIZON_FLASH),
        (deposit, _DEPOSIT_FLASH),
        (whatsapp, _WHATSAPP_FLASH),
    ):
        flash = messages.get(value or "")
        if flash:
            flash_error = value in _MP_FLASH_ERRORS or value == "invalid"
            break
    response = templates.TemplateResponse(
        request,
        "settings.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "mp_connected": tenant.mp_access_token_enc is not None,
            "flash": flash,
            "flash_error": flash_error,
            "horizon_choices": BOOKING_HORIZON_CHOICES,
            "deposit_choices": DEPOSIT_EXPIRATION_CHOICES,
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
    # Si todavía puede llegar un pago, el webhook necesita el token para
    # verificarlo: se bloquea.
    if await has_payable_mp_payment(session, tenant.id):
        return RedirectResponse(
            url="/panel/settings?mp=pending", status_code=status.HTTP_302_FOUND
        )
    clear_mp_connection(tenant)
    session.add(tenant)
    await session.commit()
    return RedirectResponse(
        url="/panel/settings?mp=disconnected", status_code=status.HTTP_302_FOUND
    )


@router.post("/panel/settings/booking-horizon")
async def panel_booking_horizon(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    form = await request.form()
    value = str(form.get("booking_horizon_days", ""))
    choices = {str(days): days for days in BOOKING_HORIZON_CHOICES}
    if value not in choices:
        return RedirectResponse(
            url="/panel/settings?horizon=invalid",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    tenant.booking_horizon_days = choices[value]
    session.add(tenant)
    await session.commit()
    return RedirectResponse(
        url="/panel/settings?horizon=saved", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/panel/settings/whatsapp")
async def panel_whatsapp_number(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    form = await request.form()
    raw = str(form.get("whatsapp_number", "")).strip()
    if not raw:
        # Vacío = sin avisos por WhatsApp.
        tenant.whatsapp_number = None
        result = "removed"
    else:
        try:
            tenant.whatsapp_number = normalize_whatsapp_phone(raw)
        except InvalidPhoneError:
            return RedirectResponse(
                url="/panel/settings?whatsapp=invalid",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        result = "saved"
    session.add(tenant)
    await session.commit()
    return RedirectResponse(
        url=f"/panel/settings?whatsapp={result}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/panel/settings/deposit-expiration")
async def panel_deposit_expiration(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    form = await request.form()
    value = str(form.get("deposit_expiration_minutes", ""))
    choices = {str(minutes): minutes for minutes in DEPOSIT_EXPIRATION_CHOICES}
    if value not in choices:
        return RedirectResponse(
            url="/panel/settings?deposit=invalid",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    # Aplica a las reservas nuevas: las pendientes ya creadas vencen con su
    # link de MP (Payment.mp_expires_at, D-031).
    tenant.deposit_expiration_minutes = choices[value]
    session.add(tenant)
    await session.commit()
    return RedirectResponse(
        url="/panel/settings?deposit=saved", status_code=status.HTTP_303_SEE_OTHER
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
    csrf_token = generate_csrf_token(request)
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
    csrf_token = generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "service_form.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "service": None,
            "form": {"charge_deposit": "on"},
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

    csrf_token = generate_csrf_token(request)

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

    csrf_token = generate_csrf_token(request)
    deposit_preview = effective_deposit(service.price, None)
    form = {
        "name": service.name,
        "duration_minutes": str(service.duration_minutes),
        "price": str(service.price),
        "deposit_amount": (
            str(service.deposit_amount) if service.deposit_amount else ""
        ),
    }
    if service.deposit_amount != 0:
        form["charge_deposit"] = "on"
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

    csrf_token = generate_csrf_token(request)

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
    csrf_token = generate_csrf_token(request)
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
    csrf_token = generate_csrf_token(request)
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

    csrf_token = generate_csrf_token(request)

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

    csrf_token = generate_csrf_token(request)
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

    csrf_token = generate_csrf_token(request)

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
    csrf_token = generate_csrf_token(request)
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

    response = templates.TemplateResponse(
        request,
        "business_hours_list.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "business_hours": business_hours,
            "day_names": _DAY_NAMES,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.get("/panel/horarios/new", response_class=HTMLResponse)
async def panel_business_hours_new_form(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
):
    csrf_token = generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "business_hours_form.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "bh": None,
            "form": {},
            "selected_days": ["0"],
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
    form_data = await request.form()
    form = dict(form_data)
    # dict() keeps only the last value of repeated keys: read the days apart.
    selected_days = [str(d) for d in form_data.getlist("day_of_week")]
    data, errors = _parse_business_hours_form(form, days=selected_days)

    if "days" in data and "start_time" in data and "end_time" in data:
        stmt = (
            select(BusinessHours)
            .where(
                and_(
                    BusinessHours.tenant_id == tenant.id,
                    BusinessHours.staff_id.is_(None),
                    BusinessHours.day_of_week.in_(data["days"]),
                )
            )
            .order_by(BusinessHours.day_of_week, BusinessHours.start_time)
        )
        existing = (await session.execute(stmt)).scalars().all()
        conflicts = [
            f"{_DAY_NAMES[existing_bh.day_of_week]} "
            f"({existing_bh.start_time.strftime('%H:%M')}–{existing_bh.end_time.strftime('%H:%M')})"
            for existing_bh in existing
            if not (
                data["end_time"] <= existing_bh.start_time
                or data["start_time"] >= existing_bh.end_time
            )
        ]
        if conflicts:
            errors["overlap"] = (
                f"Se solapa con un horario existente: {', '.join(conflicts)}."
            )

    csrf_token = generate_csrf_token(request)

    def render_with_errors() -> HTMLResponse:
        response = templates.TemplateResponse(
            request,
            "business_hours_form.html",
            {
                "tenant": tenant,
                "csrf_token": csrf_token,
                "bh": None,
                "form": form,
                "selected_days": selected_days,
                "errors": errors,
                "form_errors": list(errors.values()),
            },
        )
        set_csrf_cookie(response, csrf_token)
        return response

    if errors:
        return render_with_errors()

    for dow in data["days"]:
        session.add(
            BusinessHours(
                tenant_id=tenant.id,
                staff_id=None,
                day_of_week=dow,
                start_time=data["start_time"],
                end_time=data["end_time"],
            )
        )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        errors["overlap"] = "Ya existe un horario en alguno de los días elegidos."
        return render_with_errors()

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

    csrf_token = generate_csrf_token(request)
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

    csrf_token = generate_csrf_token(request)

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

    csrf_token = generate_csrf_token(request)
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
# Panel Agenda: alta manual de turno (reserva telefónica o sin turno previo)
# ---------------------------------------------------------------------------


async def _render_manual_booking_form(
    request: Request,
    session: AsyncSession,
    tenant: Tenant,
    form: dict[str, str],
    errors: dict[str, str] | None = None,
    form_errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    services = (
        (
            await session.execute(
                select(Service)
                .where(Service.tenant_id == tenant.id, Service.is_active)
                .order_by(Service.name)
            )
        )
        .scalars()
        .all()
    )
    staff_members = (
        (
            await session.execute(
                select(Staff)
                .where(Staff.tenant_id == tenant.id, Staff.is_active)
                .order_by(Staff.name)
            )
        )
        .scalars()
        .all()
    )
    csrf_token = generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "manual_booking_form.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "services": services,
            "staff_members": staff_members,
            "form": form,
            "errors": errors or {},
            "form_errors": form_errors or [],
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.get("/panel/agenda/new", response_class=HTMLResponse)
async def panel_agenda_new_form(
    request: Request,
    day: Annotated[str | None, Query()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    target_day = datetime.now(_tenant_tz(tenant)).date()
    if day:
        try:
            target_day = date.fromisoformat(day)
        except ValueError:
            pass
    return await _render_manual_booking_form(
        request, session, tenant, {"date": target_day.isoformat()}
    )


@router.post("/panel/agenda/new", response_class=HTMLResponse)
async def panel_agenda_new_submit(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """
    El dueño carga un turno a mano: nace confirmado, sin seña ni MP, y encola
    la confirmación por WhatsApp. Horario libre (sin grilla, horario de
    atención ni horizonte); solo el EXCLUDE evita superposiciones.
    """
    await validate_csrf(request)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    errors: dict[str, str] = {}

    # Cada turno manual manda un WhatsApp desde el número de la plataforma y el
    # registro no verifica email: tope por tenant (no por IP).
    # ponytail: contar y después insertar; envíos concurrentes pueden pasar el
    # tope por pocos. Lock por tenant si hiciera falta exactitud.
    manual_last_day = (
        await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.tenant_id == tenant.id,
                Booking.idempotency_key.startswith(MANUAL_BOOKING_KEY_PREFIX),
                # La clave la elige también la reserva pública; el actor no.
                Booking.status_changed_by == "owner",
                Booking.created_at > datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
    ).scalar_one()
    if manual_last_day >= MANUAL_BOOKINGS_PER_DAY:
        return await _render_manual_booking_form(
            request,
            session,
            tenant,
            form,
            form_errors=[
                (
                    f"Llegaste al límite de {MANUAL_BOOKINGS_PER_DAY} turnos "
                    "manuales en 24 horas. Probá más tarde."
                )
            ],
            status_code=400,
        )

    service = (
        await session.execute(
            select(Service).where(
                Service.tenant_id == tenant.id,
                Service.is_active,
                Service.id == _form_int(form.get("service_id")),
            )
        )
    ).scalar_one_or_none()
    if service is None:
        errors["service_id"] = "Elegí un servicio."

    staff_id: int | None = None
    if form.get("staff_id"):
        staff = (
            await session.execute(
                select(Staff).where(
                    Staff.tenant_id == tenant.id,
                    Staff.is_active,
                    Staff.id == _form_int(form.get("staff_id")),
                )
            )
        ).scalar_one_or_none()
        if staff is None:
            errors["staff_id"] = "Elegí un miembro del personal válido."
        else:
            staff_id = staff.id

    start_time: datetime | None = None
    end_time: datetime | None = None
    local_day: date | None = None
    try:
        local_day = date.fromisoformat(form.get("date", ""))
    except ValueError:
        errors["date"] = "Ingresá una fecha válida."
    try:
        # Algunos navegadores mandan HH:MM:SS; el turno empieza en el minuto.
        local_time = time.fromisoformat(form.get("time", "")).replace(
            second=0, microsecond=0
        )
    except ValueError:
        errors["time"] = "Ingresá una hora válida (HH:MM)."
    if local_day is not None and "time" not in errors:
        try:
            start_time = datetime.combine(
                local_day, local_time, tzinfo=_tenant_tz(tenant)
            ).astimezone(timezone.utc)
            if service is not None:
                end_time = start_time + timedelta(minutes=service.duration_minutes)
        except OverflowError:
            start_time = None
            errors["date"] = "Ingresá una fecha válida."
        if start_time is not None and start_time <= datetime.now(timezone.utc):
            errors["time"] = "El turno tiene que ser en el futuro."

    try:
        client_name = clean_client_name(form.get("client_name", ""))
    except ValueError:
        errors["client_name"] = (
            "Ingresá un nombre (entre 2 y 60 caracteres, sin links)."
        )

    try:
        client_phone = normalize_whatsapp_phone(form.get("client_phone", ""))
    except InvalidPhoneError:
        errors["client_phone"] = (
            "Ingresá un WhatsApp válido con característica (ej: 11 5555 5555)."
        )

    if (
        errors
        or service is None
        or start_time is None
        or end_time is None
        or local_day is None
    ):
        return await _render_manual_booking_form(
            request, session, tenant, form, errors=errors, status_code=400
        )

    booking = Booking(
        tenant_id=tenant.id,
        service_id=service.id,
        staff_id=staff_id,
        client_name=client_name,
        client_phone=client_phone,
        start_time=start_time,
        end_time=end_time,
        price_at_booking=service.price,
        deposit_at_booking=Decimal(0),
        idempotency_key=f"{MANUAL_BOOKING_KEY_PREFIX}{uuid4().hex}",
        status="pending",
    )
    try:
        session.add(booking)
        await session.flush()
        await transition_booking_status(session, booking, "confirmed", actor="owner")
        await session.commit()
    except IntegrityError:
        # EXCLUDE excl_overlapping_bookings: el horario choca con otro turno.
        await session.rollback()
        await session.refresh(tenant)
        return await _render_manual_booking_form(
            request,
            session,
            tenant,
            form,
            form_errors=["Ese horario se superpone con otro turno."],
            status_code=409,
        )

    return _redirect_to_agenda(local_day.isoformat())


def _form_int(raw: str | None) -> int:
    """Id numérico del form; -1 (no matchea ningún id) si es inválido o no
    entra en un INTEGER de Postgres."""
    try:
        value = int(raw or "")
    except ValueError:
        return -1
    return value if 0 < value < 2**31 else -1


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
        await session.commit()
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except IntegrityError:
        # expired → confirmed: el horario ya lo tomó otro turno (EXCLUDE).
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="El horario ya está ocupado por otro turno."
        )
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
