import asyncio
import csv
import io
import re
import unicodedata
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by, array_agg
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.auth import get_current_tenant_from_session
from app.booking_actions import (
    BookingNotStartedError,
    InvalidTransitionError,
    transition_booking_status,
)
from app.brand import DEFAULT_BRAND_COLOR, clean_brand_color
from app.config import settings
from app.csrf import generate_csrf_token, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.limiter import limiter
from app.logo import LOGO_UPLOAD_MAX_BYTES, logo_version, process_logo
from app.models import (
    BOOKING_HORIZON_CHOICES,
    BOOKING_NOTES_MAX,
    DEPOSIT_EXPIRATION_CHOICES,
    PUBLIC_NOTICE_MAX,
    SERVICE_BUFFER_CHOICES,
    SERVICE_QUESTION_MAX,
    SERVICE_QUESTIONS_MAX,
    BlockedPeriod,
    Booking,
    BusinessHours,
    Payment,
    Service,
    Staff,
    Tenant,
    TenantLogo,
)
from app.mp_connect import (
    clear_mp_connection,
    has_payable_mp_payment,
    mp_authorization_redirect,
)
from app.phone import InvalidPhoneError, normalize_whatsapp_phone
from app.schemas import (
    MANUAL_BOOKING_KEY_PREFIX,
    clean_business_category,
    clean_business_profile,
    clean_client_name,
)
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

    # Opt-in: sin el campo, el alta queda sin tiempo libre y la edición
    # conserva el valor guardado (formularios viejos no lo pisan).
    if "buffer_minutes" in form:
        try:
            buffer_minutes = int(form["buffer_minutes"] or 0)
            if buffer_minutes not in SERVICE_BUFFER_CHOICES:
                raise ValueError
            data["buffer_minutes"] = buffer_minutes
        except (ValueError, TypeError):
            errors["buffer_minutes"] = "Elegí un tiempo libre de la lista."

    # Opt-in como el tiempo libre: un formulario viejo no borra las preguntas.
    if any(f"question_{i}" in form for i in range(1, SERVICE_QUESTIONS_MAX + 1)):
        questions = []
        for i in range(1, SERVICE_QUESTIONS_MAX + 1):
            key = f"question_{i}"
            text, error = _clean_owner_text(form.get(key), SERVICE_QUESTION_MAX)
            if error == "too_long":
                errors[key] = (
                    f"La pregunta puede tener hasta {SERVICE_QUESTION_MAX} caracteres."
                )
            elif error:
                errors[key] = "La pregunta tiene caracteres no permitidos."
            elif text:
                # Una línea: el formulario público la muestra como etiqueta.
                questions.append(
                    {
                        "text": " ".join(text.split()),
                        "required": bool(form.get(f"{key}_required")),
                    }
                )
        data["questions"] = questions

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


_MONTH_NAMES = [
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
    return f"{weekday_names[d.weekday()]} {d.day} de {_MONTH_NAMES[d.month - 1]} de {d.year}"


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
    return await _bookings_for_days(session, tenant, target_day, 1)


async def _bookings_for_days(
    session: AsyncSession, tenant: Tenant, first_day: date, days: int
) -> list[tuple[Booking, Service]]:
    """Turnos del tenant que tocan `days` días locales desde `first_day`, por inicio."""
    tenant_tz = _tenant_tz(tenant)
    day_start = datetime.combine(first_day, time(0, 0), tzinfo=tenant_tz)
    # Fin en hora local (no +N*24h): respeta un eventual cambio de horario.
    day_end = datetime.combine(
        first_day + timedelta(days=days), time(0, 0), tzinfo=tenant_tz
    )
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


async def _approved_payments(
    session: AsyncSession, tenant: Tenant, booking_ids: list[int]
) -> dict[int, Decimal]:
    """Suma de pagos aprobados por turno (solo turnos del tenant).

    Seña cobrada = pagos aprobados (no deposit_at_booking: un turno confirmado
    a mano puede no tener seña, y un reembolso deja el pago refunded).
    """
    if not booking_ids:
        return {}
    stmt = (
        select(col(Payment.booking_id), func.sum(Payment.amount))
        .join(Booking, Payment.booking_id == Booking.id)
        .where(
            Booking.tenant_id == tenant.id,
            Payment.status == "approved",
            col(Payment.booking_id).in_(booking_ids),
        )
        .group_by(Payment.booking_id)
    )
    return {booking_id: total for booking_id, total in await session.execute(stmt)}


def _summarize_money(
    tenant: Tenant,
    rows: list[tuple[Booking, Service]],
    paid: dict[int, Decimal],
    first_day: date,
) -> dict[str, Decimal | int]:
    """Señas cobradas y total a cobrar de lo vendido (confirmed + completed) y
    cantidad de cancelados / no se presentaron (expired no: nunca pagó).

    Cada turno cuenta en el día local en que empieza: uno que cruza la
    medianoche desde el día anterior al período no se cuenta dos veces.
    """
    tenant_tz = _tenant_tz(tenant)
    deposits = local = Decimal(0)
    lost = 0
    for b, _ in rows:
        if b.start_time.astimezone(tenant_tz).date() < first_day:
            continue
        if b.status in ("confirmed", "completed"):
            booking_paid = paid.get(b.id, Decimal(0))
            deposits += booking_paid
            local += max(b.price_at_booking - booking_paid, Decimal(0))
        elif b.status in ("cancelled", "no_show"):
            lost += 1
    return {
        "deposits": deposits,
        "local": local,
        "total": deposits + local,
        "lost": lost,
    }


_STATS_STATUSES = [
    ("confirmed", "Confirmados"),
    ("completed", "Completados"),
    ("no_show", "No se presentaron"),
    ("cancelled", "Cancelados"),
]
_STATS_TOP_SERVICES = 5


def _pct(count: int, top: int) -> int:
    """Largo de una barra: % del máximo del gráfico, entero de 0 a 100.
    Un conteo mayor a 0 nunca queda en 0 (la barra se vería vacía)."""
    if not count or not top:
        return 0
    return max(1, round(count * 100 / top))


def _bar(label: str, count: int, top: int, tag: str = "") -> dict:
    return {"label": label, "count": count, "width": _pct(count, top), "tag": tag}


def _summarize_activity(
    tenant: Tenant, rows: list[tuple[Booking, Service]], first_day: date, days: int
) -> dict:
    """Datos de los gráficos de Estadísticas.

    La barra de estados cuenta confirmados, completados, no se presentaron y
    cancelados (expired no: nunca pagó). Servicios, días de la semana y días
    del mes cuentan lo atendido o por atender (confirmed + completed), como el
    resumen de plata. Cada turno cuenta en el día local en que empieza.
    """
    tenant_tz = _tenant_tz(tenant)
    statuses: dict[str, int] = {key: 0 for key, _ in _STATS_STATUSES}
    services: dict[tuple[str, int | None], int] = {}
    weekdays = [0] * 7
    by_day = [0] * days
    for b, s in rows:
        start = b.start_time.astimezone(tenant_tz).date()
        if start < first_day:
            continue
        if b.status in statuses:
            statuses[b.status] += 1
        if b.status in ("confirmed", "completed"):
            services[(s.name, s.id)] = services.get((s.name, s.id), 0) + 1
            weekdays[start.weekday()] += 1
            by_day[(start - first_day).days] += 1

    top_services = sorted(services.items(), key=lambda kv: (-kv[1], kv[0][0]))
    top_services = top_services[:_STATS_TOP_SERVICES]
    services_top = top_services[0][1] if top_services else 0
    day_top = max(by_day)

    # "Más tranquilo" = menos turnos entre los días con alguno: un día cerrado
    # (0) no dice nada. Sin diferencia entre días, no se marca ninguno; en un
    # empate se marcan todos los días empatados.
    busiest = max(weekdays)
    quietest = min((n for n in weekdays if n), default=0)
    tags = {}
    if busiest != quietest:
        tags = {i: "Más movido" for i, n in enumerate(weekdays) if n == busiest}
        tags |= {i: "Más tranquilo" for i, n in enumerate(weekdays) if n == quietest}

    return {
        "total": sum(statuses.values()),
        "statuses": [
            {"key": key, "label": label, "count": statuses[key]}
            for key, label in _STATS_STATUSES
        ],
        "services": [_bar(name, n, services_top) for (name, _), n in top_services],
        "weekdays": (
            [
                _bar(_DAY_NAMES[i], n, busiest, tags.get(i, ""))
                for i, n in enumerate(weekdays)
            ]
            if busiest
            else []
        ),
        "by_day": [
            {
                "day": first_day + timedelta(days=i),
                "count": n,
                "height": _pct(n, day_top),
            }
            for i, n in enumerate(by_day)
        ],
    }


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

_CATEGORY_FLASH = {
    "saved": "Guardamos el rubro de tu negocio.",
    "invalid": (
        "Elegí un rubro de la lista o, si elegiste Otro, escribí el tuyo "
        "(entre 2 y 60 caracteres)."
    ),
}

_NOTICE_FLASH = {
    "saved": "Guardamos el aviso de tu página de reservas.",
    "removed": "Sacamos el aviso de tu página de reservas.",
    "too_long": f"El aviso puede tener hasta {PUBLIC_NOTICE_MAX} caracteres.",
    "invalid": "El aviso tiene caracteres no permitidos.",
}

_PROFILE_FLASH = {
    "saved": "Guardamos el perfil de tu negocio.",
    "invalid_locality": "La localidad tiene que tener entre 2 y 60 caracteres.",
    "invalid_instagram": (
        "Ese usuario de Instagram no es válido. Escribí solo el usuario "
        "(por ejemplo @minegocio) o pegá el link de tu perfil."
    ),
    "invalid_facebook": (
        "Esa página de Facebook no es válida. Escribí el usuario o pegá el "
        "link de tu página."
    ),
}

_LOGO_FLASH = {
    "saved": "Guardamos el logo de tu negocio.",
    "removed": "Sacamos el logo de tu negocio.",
    "not_image": "No pudimos leer la imagen. Subí un archivo JPG, PNG o WebP.",
    "too_big": (
        f"La imagen pesa más de {LOGO_UPLOAD_MAX_BYTES // (1024 * 1024)} MB. "
        "Probá con una más liviana."
    ),
}

_BRAND_FLASH = {
    "saved": "Guardamos el color de tu página de reservas.",
    "removed": "Volvimos al color de Juturno en tu página de reservas.",
    "invalid": "Elegí un color con el selector.",
    "too_dark": (
        "Ese color casi no se ve sobre el fondo oscuro de tu página. "
        "Elegí uno más claro."
    ),
}

_FLASH_ERRORS = {
    "invalid",
    "too_dark",
    "too_long",
    "invalid_locality",
    "invalid_instagram",
    "invalid_facebook",
    "not_image",
    "too_big",
}


def _clean_owner_text(raw: str | None, max_len: int) -> tuple[str | None, str | None]:
    """Texto libre del dueño (nota de turno, aviso público) → (texto, error).

    Vacío = None. Error "too_long" o "invalid" (caracteres de control).
    """
    # El textarea manda CRLF pero su maxlength cuenta cada salto como uno.
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None, None
    if len(text) > max_len:
        return None, "too_long"
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        # Caracteres de control (p. ej. NUL, que Postgres rechaza → 500). Solo
        # Cc: el zero-width joiner (Cf) de los emoji compuestos se permite.
        return None, "invalid"
    return text, None


@router.get("/panel/settings", response_class=HTMLResponse)
async def panel_settings(
    request: Request,
    mp: str | None = None,
    horizon: str | None = None,
    deposit: str | None = None,
    whatsapp: str | None = None,
    notice: str | None = None,
    category: str | None = None,
    profile: str | None = None,
    logo: str | None = None,
    brand: str | None = None,
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
        (notice, _NOTICE_FLASH),
        (category, _CATEGORY_FLASH),
        (profile, _PROFILE_FLASH),
        (logo, _LOGO_FLASH),
        (brand, _BRAND_FLASH),
    ):
        flash = messages.get(value or "")
        if flash:
            flash_error = value in _MP_FLASH_ERRORS or value in _FLASH_ERRORS
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
            "notice_max": PUBLIC_NOTICE_MAX,
            "logo_version": logo_version(tenant.logo_updated_at),
            "logo_max_mb": LOGO_UPLOAD_MAX_BYTES // (1024 * 1024),
            "brand_color": tenant.brand_color or DEFAULT_BRAND_COLOR,
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


@router.post("/panel/settings/category")
async def panel_business_category(
    request: Request,
    business_category: Annotated[str | None, Form()] = None,
    business_category_other: Annotated[str | None, Form()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """Rubro del negocio: uno de la lista o "otro" con texto libre."""
    await validate_csrf(request)
    try:
        category, other = clean_business_category(
            business_category, business_category_other
        )
    except ValueError:
        return RedirectResponse(
            url="/panel/settings?category=invalid",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    tenant.business_category = category
    tenant.business_category_other = other
    session.add(tenant)
    await session.commit()
    return RedirectResponse(
        url="/panel/settings?category=saved", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/panel/settings/notice")
async def panel_public_notice(
    request: Request,
    public_notice: Annotated[str | None, Form()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """Aviso destacado de la página pública; vacío = sin aviso."""
    await validate_csrf(request)
    cleaned, error = _clean_owner_text(public_notice, PUBLIC_NOTICE_MAX)
    if error:
        return RedirectResponse(
            url=f"/panel/settings?notice={error}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    tenant.public_notice = cleaned
    session.add(tenant)
    await session.commit()
    result = "saved" if cleaned else "removed"
    return RedirectResponse(
        url=f"/panel/settings?notice={result}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/panel/settings/profile")
async def panel_business_profile(
    request: Request,
    locality: Annotated[str | None, Form()] = None,
    instagram: Annotated[str | None, Form()] = None,
    facebook: Annotated[str | None, Form()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """Localidad y redes de la página pública; vacío = sin dato."""
    await validate_csrf(request)
    try:
        tenant.locality, tenant.instagram, tenant.facebook = clean_business_profile(
            locality, instagram, facebook
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/panel/settings?profile=invalid_{exc.args[0]}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    session.add(tenant)
    await session.commit()
    return RedirectResponse(
        url="/panel/settings?profile=saved", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/panel/settings/brand-color")
async def panel_brand_color(
    request: Request,
    brand_color: Annotated[str | None, Form()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """Color de la página pública; vacío = vuelve al tema de Juturno."""
    await validate_csrf(request)
    try:
        color = clean_brand_color(brand_color)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/panel/settings?brand={exc.args[0]}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    # El <input type="color"> siempre manda valor y arranca en el cian por
    # defecto: guardarlo sin tocarlo deja el tema de Juturno (con su degradé).
    tenant.brand_color = None if color == DEFAULT_BRAND_COLOR else color
    session.add(tenant)
    await session.commit()
    result = "saved" if tenant.brand_color else "removed"
    return RedirectResponse(
        url=f"/panel/settings?brand={result}", status_code=status.HTTP_303_SEE_OTHER
    )


def _logo_redirect(result: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/panel/settings?logo={result}", status_code=status.HTTP_303_SEE_OTHER
    )


# Decodificar imágenes es CPU y RAM: pocas a la vez para no tirar la API.
_LOGO_SLOTS = asyncio.Semaphore(2)


@router.post("/panel/settings/logo")
@limiter.limit("10/minute")
async def panel_logo_upload(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """Sube o reemplaza el logo; se guarda re-codificado (app/logo.py)."""
    # Antes de leer el body: el form lo bajaría entero a disco. Sin
    # Content-Length (chunked) no se sabe cuánto es, así que tampoco se acepta.
    length = request.headers.get("content-length", "")
    if (
        not re.fullmatch(r"[0-9]+", length)
        or int(length) > LOGO_UPLOAD_MAX_BYTES + 64 * 1024
    ):
        return _logo_redirect("too_big")
    await validate_csrf(request)
    upload = (await request.form()).get("logo")
    if not isinstance(upload, UploadFile):
        return _logo_redirect("not_image")
    raw = await upload.read(LOGO_UPLOAD_MAX_BYTES + 1)
    if len(raw) > LOGO_UPLOAD_MAX_BYTES:
        return _logo_redirect("too_big")
    try:
        async with _LOGO_SLOTS:
            webp = await run_in_threadpool(process_logo, raw)
    except ValueError:
        return _logo_redirect("not_image")
    now = datetime.now(timezone.utc)
    await session.execute(
        pg_insert(TenantLogo)
        .values(tenant_id=tenant.id, data=webp, updated_at=now)
        .on_conflict_do_update(
            index_elements=["tenant_id"], set_={"data": webp, "updated_at": now}
        )
    )
    tenant.logo_updated_at = now
    session.add(tenant)
    await session.commit()
    return _logo_redirect("saved")


@router.post("/panel/settings/logo/remove")
async def panel_logo_remove(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    await validate_csrf(request)
    await session.execute(delete(TenantLogo).where(TenantLogo.tenant_id == tenant.id))
    tenant.logo_updated_at = None
    session.add(tenant)
    await session.commit()
    return _logo_redirect("removed")


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
        buffer_minutes=data.get("buffer_minutes", 0),
        price=data["price"],
        deposit_amount=data.get("deposit_amount"),
        questions=data.get("questions", []),
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
        "buffer_minutes": str(service.buffer_minutes),
        "price": str(service.price),
        "deposit_amount": (
            str(service.deposit_amount) if service.deposit_amount else ""
        ),
    }
    if service.deposit_amount != 0:
        form["charge_deposit"] = "on"
    for i, question in enumerate(service.questions, start=1):
        form[f"question_{i}"] = question["text"]
        if question["required"]:
            form[f"question_{i}_required"] = "on"
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
    service.buffer_minutes = data.get("buffer_minutes", service.buffer_minutes)
    service.price = data["price"]
    service.deposit_amount = data.get("deposit_amount")
    service.questions = data.get("questions", service.questions)
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


async def _hours_owner(
    session: AsyncSession, tenant: Tenant, staff_id: int | None
) -> Staff | None:
    """Profesional dueño de los horarios (None = el negocio); 404 si es ajeno."""
    if staff_id is None:
        return None
    staff = await session.get(Staff, staff_id)
    if not staff or staff.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Miembro no encontrado")
    return staff


def _hours_of(staff_id: int | None):
    """Filtro de las filas de un dueño: el negocio (NULL) o un profesional."""
    if staff_id is None:
        return BusinessHours.staff_id.is_(None)
    return BusinessHours.staff_id == staff_id


def _hours_context(owner: Staff | None) -> dict:
    """URLs y nombre del dueño para las plantillas de horarios."""
    query = f"?staff={owner.id}" if owner else ""
    return {
        "owner": owner,
        "staff_q": query,
        "hours_url": f"/panel/horarios{query}",
    }


@router.get("/panel/horarios", response_class=HTMLResponse)
async def panel_business_hours_list(
    request: Request,
    staff: Annotated[int | None, Query()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    owner = await _hours_owner(session, tenant, staff)
    csrf_token = generate_csrf_token(request)
    stmt = (
        select(BusinessHours)
        .where(and_(BusinessHours.tenant_id == tenant.id, _hours_of(staff)))
        .order_by(BusinessHours.day_of_week, BusinessHours.start_time)
    )
    business_hours = (await session.execute(stmt)).scalars().all()
    staff_members = (
        (
            await session.execute(
                select(Staff).where(Staff.tenant_id == tenant.id).order_by(Staff.id)
            )
        )
        .scalars()
        .all()
    )
    today = datetime.now(_tenant_tz(tenant)).date()
    blocked_periods = (
        (
            await session.execute(
                select(BlockedPeriod)
                .where(
                    BlockedPeriod.tenant_id == tenant.id,
                    _blocked_of(staff),
                    BlockedPeriod.end_date >= today,
                )
                .order_by(BlockedPeriod.start_date)
            )
        )
        .scalars()
        .all()
    )

    response = templates.TemplateResponse(
        request,
        "business_hours_list.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "business_hours": business_hours,
            "blocked_periods": blocked_periods,
            "day_names": _DAY_NAMES,
            "staff_members": staff_members,
            **_hours_context(owner),
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.get("/panel/horarios/new", response_class=HTMLResponse)
async def panel_business_hours_new_form(
    request: Request,
    staff: Annotated[int | None, Query()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    owner = await _hours_owner(session, tenant, staff)
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
            **_hours_context(owner),
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/panel/horarios/new", response_class=HTMLResponse)
async def panel_business_hours_new_submit(
    request: Request,
    staff: Annotated[int | None, Query()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    owner = await _hours_owner(session, tenant, staff)
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
                    _hours_of(staff),
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
                **_hours_context(owner),
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
                staff_id=staff,
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
        url=_hours_context(owner)["hours_url"], status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/panel/horarios/{bh_id}/edit", response_class=HTMLResponse)
async def panel_business_hours_edit_form(
    request: Request,
    bh_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    bh = await session.get(BusinessHours, bh_id)
    if not bh or bh.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Horario no encontrado")
    owner = await _hours_owner(session, tenant, bh.staff_id)

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
            **_hours_context(owner),
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
    if not bh or bh.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Horario no encontrado")
    owner = await _hours_owner(session, tenant, bh.staff_id)

    await validate_csrf(request)
    form = dict(await request.form())
    data, errors = _parse_business_hours_form(form)

    if "day_of_week" in data and "start_time" in data and "end_time" in data:
        stmt = select(BusinessHours).where(
            and_(
                BusinessHours.tenant_id == tenant.id,
                _hours_of(bh.staff_id),
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
                **_hours_context(owner),
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
                **_hours_context(owner),
            },
        )
        set_csrf_cookie(response, csrf_token)
        return response

    return RedirectResponse(
        url=_hours_context(owner)["hours_url"], status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/panel/horarios/{bh_id}/delete", response_class=HTMLResponse)
async def panel_business_hours_delete(
    request: Request,
    bh_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    bh = await session.get(BusinessHours, bh_id)
    if not bh or bh.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Horario no encontrado")
    owner = await _hours_owner(session, tenant, bh.staff_id)

    await validate_csrf(request)
    await session.delete(bh)
    await session.commit()
    return RedirectResponse(
        url=_hours_context(owner)["hours_url"], status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Panel: Días bloqueados (feriados, vacaciones) — ítem 31
# ---------------------------------------------------------------------------

BLOCKED_REASON_MAX = 80
BLOCKED_DAYS_AHEAD_MAX = 365
_BLOCKING_BOOKINGS_SHOWN = 5


def _blocked_of(staff_id: int | None):
    """Filtro de los bloqueos de un dueño: el negocio (NULL) o un profesional."""
    if staff_id is None:
        return BlockedPeriod.staff_id.is_(None)
    return BlockedPeriod.staff_id == staff_id


def _parse_blocked_period_form(form: dict, today: date) -> tuple[dict, list[str]]:
    """Valida el rango de fechas (inclusive) y el motivo. Returns (data, errors)."""
    try:
        start = date.fromisoformat(str(form.get("start_date", "")).strip())
        end = date.fromisoformat(str(form.get("end_date", "")).strip())
    except ValueError:
        return {}, ["Elegí una fecha válida."]
    errors = []
    if end < start:
        errors.append("La fecha de fin no puede ser anterior a la de inicio.")
    if start < today:
        errors.append("No se pueden bloquear fechas pasadas.")
    if end > today + timedelta(days=BLOCKED_DAYS_AHEAD_MAX):
        errors.append("Solo se pueden bloquear fechas del próximo año.")
    reason = str(form.get("reason", "")).strip()
    if len(reason) > BLOCKED_REASON_MAX:
        errors.append(f"El motivo puede tener hasta {BLOCKED_REASON_MAX} caracteres.")
    return {"start_date": start, "end_date": end, "reason": reason or None}, errors


async def _blocking_bookings_error(
    session: AsyncSession,
    tenant: Tenant,
    staff_id: int | None,
    start: date,
    end: date,
) -> str | None:
    """Mensaje si hay turnos pendientes o confirmados en el rango, o None.

    El bloqueo del negocio mira todos los turnos; el de un profesional, solo
    los suyos. ponytail: chequeo y alta no son atómicos; una reserva que entra
    en ese instante queda viva y se ve en la agenda.
    """
    tenant_tz = _tenant_tz(tenant)
    # Desde ahora: los turnos de hoy que ya terminaron no impiden bloquear hoy.
    range_start = max(
        datetime.combine(start, time(0), tzinfo=tenant_tz), datetime.now(tenant_tz)
    )
    range_end = datetime.combine(end + timedelta(days=1), time(0), tzinfo=tenant_tz)
    stmt = (
        select(Booking)
        .where(
            Booking.tenant_id == tenant.id,
            col(Booking.status).in_(["pending", "confirmed"]),
            Booking.start_time < range_end,
            Booking.end_time > range_start,
        )
        .order_by(Booking.start_time)
    )
    if staff_id is not None:
        stmt = stmt.where(Booking.staff_id == staff_id)
    bookings = (await session.execute(stmt)).scalars().all()
    if not bookings:
        return None
    shown = [
        f"{b.start_time.astimezone(tenant_tz).strftime('%d/%m %H:%M')} {b.client_name}"
        for b in bookings[:_BLOCKING_BOOKINGS_SHOWN]
    ]
    rest = len(bookings) - len(shown)
    more = f" y {rest} más" if rest else ""
    return (
        f"Hay turnos en esas fechas: {', '.join(shown)}{more}. "
        "Cancelalos o movelos desde la Agenda antes de bloquear."
    )


def _render_blocked_form(
    request: Request,
    tenant: Tenant,
    owner: Staff | None,
    form: dict,
    form_errors: list[str],
) -> HTMLResponse:
    today = datetime.now(_tenant_tz(tenant)).date()
    csrf_token = generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "blocked_period_form.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "form": form,
            "form_errors": form_errors,
            "min_date": today.isoformat(),
            "max_date": (today + timedelta(days=BLOCKED_DAYS_AHEAD_MAX)).isoformat(),
            "reason_max": BLOCKED_REASON_MAX,
            **_hours_context(owner),
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.get("/panel/horarios/bloqueos/new", response_class=HTMLResponse)
async def panel_blocked_period_new_form(
    request: Request,
    staff: Annotated[int | None, Query()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    owner = await _hours_owner(session, tenant, staff)
    return _render_blocked_form(request, tenant, owner, {}, [])


@router.post("/panel/horarios/bloqueos/new", response_class=HTMLResponse)
async def panel_blocked_period_new_submit(
    request: Request,
    staff: Annotated[int | None, Query()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    owner = await _hours_owner(session, tenant, staff)
    await validate_csrf(request)
    form = dict(await request.form())
    today = datetime.now(_tenant_tz(tenant)).date()
    data, errors = _parse_blocked_period_form(form, today)
    if not errors:
        busy = await _blocking_bookings_error(
            session, tenant, staff, data["start_date"], data["end_date"]
        )
        if busy:
            errors.append(busy)
    if errors:
        return _render_blocked_form(request, tenant, owner, form, errors)

    session.add(BlockedPeriod(tenant_id=tenant.id, staff_id=staff, **data))
    await session.commit()
    return RedirectResponse(
        url=_hours_context(owner)["hours_url"], status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/panel/horarios/bloqueos/{block_id}/delete", response_class=HTMLResponse)
async def panel_blocked_period_delete(
    request: Request,
    block_id: int,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    block = await session.get(BlockedPeriod, block_id)
    if not block or block.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Bloqueo no encontrado")
    owner = await _hours_owner(session, tenant, block.staff_id)

    await validate_csrf(request)
    await session.delete(block)
    await session.commit()
    return RedirectResponse(
        url=_hours_context(owner)["hours_url"], status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Panel: Agenda (vista de turnos por día)
# ---------------------------------------------------------------------------


@router.get("/panel/agenda", response_class=HTMLResponse)
async def panel_agenda(
    request: Request,
    day: Annotated[str | None, Query()] = None,
    view: Annotated[str | None, Query()] = None,
    notes: Annotated[str | None, Query()] = None,
    export: Annotated[str | None, Query()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """
    Vista de agenda por día: todos los turnos del tenant que tocan el día
    dado, ordenados por horario de inicio. Las horas se muestran en la
    zona horaria del tenant (Booking.start_time está en UTC en la DB).
    Con `view=week`, vista de solo lectura de la semana (lunes a domingo).
    """
    tenant_tz = _tenant_tz(tenant)
    today_local = datetime.now(tenant_tz).date()

    target_day = today_local
    if day:
        try:
            target_day = date.fromisoformat(day)
        except ValueError:
            target_day = today_local

    if view == "week":
        return await _render_week_agenda(
            request, tenant, session, target_day, today_local
        )

    rows = await _bookings_for_day(session, tenant, target_day)

    paid = await _approved_payments(session, tenant, [b.id for b, _ in rows])
    due = {
        b.id: max(b.price_at_booking - paid.get(b.id, Decimal(0)), Decimal(0))
        for b, _ in rows
    }

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
            "internal_notes": b.internal_notes,
            "answers": b.answers or [],
            "price": b.price_at_booking,
            "deposit_paid": paid.get(b.id, Decimal(0)),
            "balance_due": due[b.id],
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
            "day_summary": _summarize_money(tenant, rows, paid, target_day),
            # Mensaje fijo: nunca se refleja el valor del query param.
            "notes_too_long": notes == "too_long",
            "notes_invalid": notes == "invalid",
            "notes_max": BOOKING_NOTES_MAX,
            "export_invalid": export == "invalid",
            # Por defecto se exporta el mes del día que se está viendo.
            "export_from": target_day.replace(day=1).isoformat(),
            "export_to": target_day.replace(
                day=monthrange(target_day.year, target_day.month)[1]
            ).isoformat(),
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


async def _render_week_agenda(
    request: Request,
    tenant: Tenant,
    session: AsyncSession,
    target_day: date,
    today_local: date,
) -> HTMLResponse:
    """Semana de lunes a domingo que contiene `target_day`, agrupada por día."""
    tenant_tz = _tenant_tz(tenant)
    monday = target_day - timedelta(days=target_day.weekday())
    week = [
        {
            "date": monday + timedelta(days=i),
            "label": _format_agenda_day_label(monday + timedelta(days=i)),
            "items": [],
        }
        for i in range(7)
    ]
    rows = await _bookings_for_days(session, tenant, monday, 7)
    for b, s in rows:
        start_local = b.start_time.astimezone(tenant_tz)
        # Un turno que empezó el domingo anterior y cruza la medianoche va al lunes.
        index = max((start_local.date() - monday).days, 0)
        week[index]["items"].append(
            {
                "client_name": b.client_name,
                "service_name": s.name,
                "start_local": start_local.strftime("%H:%M"),
                "end_local": b.end_time.astimezone(tenant_tz).strftime("%H:%M"),
                "status": b.status,
                "status_label": _AGENDA_STATUS_LABELS.get(b.status, b.status),
            }
        )

    sunday = monday + timedelta(days=6)
    is_current = monday <= today_local <= sunday
    # Sin acciones, pero el layout tiene el formulario de "Cerrar sesión".
    csrf_token = generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "agenda.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "view": "week",
            "week": week,
            "week_label": f"{monday.day}/{monday.month} al {sunday.day}/{sunday.month}",
            "week_count": sum(
                1 for b, _ in rows if b.status not in ("cancelled", "expired")
            ),
            "prev_week": (monday - timedelta(days=7)).isoformat(),
            "next_week": (monday + timedelta(days=7)).isoformat(),
            "is_current_week": is_current,
            # El selector "Día" lleva a hoy si es esta semana; si no, al lunes.
            "day_view_day": (today_local if is_current else monday).isoformat(),
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


# ---------------------------------------------------------------------------
# Panel: Estadísticas (resumen de plata por semana o mes)
# ---------------------------------------------------------------------------


@router.get("/panel/estadisticas", response_class=HTMLResponse)
async def panel_stats(
    request: Request,
    period: Annotated[str | None, Query()] = None,
    day: Annotated[str | None, Query()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """Señas cobradas, cancelados y total a cobrar de la semana (lunes a
    domingo) o del mes que contiene `day`, más los gráficos de turnos por
    estado, servicios y días (ítem 18). Período desconocido = semana."""
    target_day = datetime.now(_tenant_tz(tenant)).date()
    if day:
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            parsed = None
        # Fuera de este rango, el período anterior/siguiente desborda `date` (500).
        if parsed and 2000 <= parsed.year <= 2100:
            target_day = parsed

    if period == "month":
        first_day = target_day.replace(day=1)
        days = monthrange(first_day.year, first_day.month)[1]
        label = f"{_MONTH_NAMES[first_day.month - 1].capitalize()} {first_day.year}"
        prev_day = (first_day - timedelta(days=1)).replace(day=1)
    else:
        period = "week"
        first_day = target_day - timedelta(days=target_day.weekday())
        days = 7
        last_day = first_day + timedelta(days=6)
        label = (
            f"Semana del {first_day.day}/{first_day.month}"
            f" al {last_day.day}/{last_day.month}"
        )
        prev_day = first_day - timedelta(days=7)

    rows = await _bookings_for_days(session, tenant, first_day, days)
    paid = await _approved_payments(session, tenant, [b.id for b, _ in rows])

    # El layout tiene el formulario de "Cerrar sesión".
    csrf_token = generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "stats.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "period": period,
            "period_label": label,
            "target_day": target_day.isoformat(),
            "prev_day": prev_day.isoformat(),
            "next_day": (first_day + timedelta(days=days)).isoformat(),
            "summary": _summarize_money(tenant, rows, paid, first_day),
            "activity": _summarize_activity(tenant, rows, first_day, days),
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


# ---------------------------------------------------------------------------
# Panel: Clientes (historial agrupado por teléfono, sin tabla Client)
# ---------------------------------------------------------------------------

_CLIENTS_LIST_MAX = 50
_CLIENT_HISTORY_MAX = 200
_CLIENT_SEARCH_MAX = 60


@router.get("/panel/clientes", response_class=HTMLResponse)
async def panel_clients(
    request: Request,
    q: Annotated[str | None, Query()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """Un cliente por teléfono, el de turno más reciente primero. `q` busca por
    nombre o por los dígitos del teléfono. Los turnos vencidos sin pagar no
    cuentan: son reservas abandonadas."""
    # Postgres rechaza el byte NUL en un texto (500): se descarta.
    search = (q or "").replace("\x00", "").strip()[:_CLIENT_SEARCH_MAX]
    filters = [Booking.tenant_id == tenant.id, Booking.status != "expired"]
    if search:
        digits = re.sub(r"\D", "", search)
        match = col(Booking.client_name).icontains(search, autoescape=True)
        if digits:
            match = match | col(Booking.client_phone).contains(digits)
        filters.append(match)

    last_start = func.max(Booking.start_time)
    stmt = (
        select(
            col(Booking.client_phone),
            # Nombre del turno más reciente: el cliente puede escribirlo distinto.
            array_agg(
                aggregate_order_by(
                    col(Booking.client_name), col(Booking.start_time).desc()
                )
            )[1],
            func.count(),
            func.count().filter(col(Booking.status) == "no_show"),
            last_start,
        )
        .where(*filters)
        .group_by(col(Booking.client_phone))
        .order_by(last_start.desc())
        .limit(_CLIENTS_LIST_MAX)
    )
    tenant_tz = _tenant_tz(tenant)
    clients = [
        {
            "phone": phone,
            "name": name,
            "bookings": bookings,
            "no_shows": no_shows,
            "last_day": last.astimezone(tenant_tz).strftime("%d/%m/%Y"),
        }
        for phone, name, bookings, no_shows, last in await session.execute(stmt)
    ]

    # El layout tiene el formulario de "Cerrar sesión".
    csrf_token = generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "clients.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "q": search,
            "clients": clients,
            "is_capped": len(clients) == _CLIENTS_LIST_MAX,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.get("/panel/clientes/{phone}", response_class=HTMLResponse)
async def panel_client_detail(
    request: Request,
    phone: str,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """Todos los turnos del tenant con ese teléfono, el más reciente primero."""
    # Los teléfonos se guardan normalizados (solo dígitos); otra cosa no existe
    # y un byte NUL haría fallar la query con 500.
    if not (phone.isascii() and phone.isdigit()):
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    stmt = (
        select(Booking, Service, Staff)
        .join(Service, Booking.service_id == Service.id)
        .outerjoin(Staff, Booking.staff_id == Staff.id)
        .where(Booking.tenant_id == tenant.id, Booking.client_phone == phone)
        .order_by(col(Booking.start_time).desc())
        .limit(_CLIENT_HISTORY_MAX)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    tenant_tz = _tenant_tz(tenant)
    history = []
    for b, s, st in rows:
        start_local = b.start_time.astimezone(tenant_tz)
        history.append(
            {
                "day": start_local.date().isoformat(),
                "day_label": start_local.strftime("%d/%m/%Y"),
                "time_label": start_local.strftime("%H:%M"),
                "service_name": s.name,
                "staff_name": st.name if st else None,
                "status": b.status,
                "status_label": _AGENDA_STATUS_LABELS.get(b.status, b.status),
                "price": b.price_at_booking,
                "internal_notes": b.internal_notes,
                "answers": b.answers or [],
            }
        )
    counts = {key: 0 for key in ("completed", "no_show", "cancelled")}
    for b, _, _ in rows:
        if b.status in counts:
            counts[b.status] += 1

    csrf_token = generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "client_detail.html",
        {
            "tenant": tenant,
            "csrf_token": csrf_token,
            "client_name": rows[0][0].client_name,
            "client_phone": phone,
            "history": history,
            "counts": counts,
            "is_capped": len(rows) == _CLIENT_HISTORY_MAX,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


# Máximo de días por descarga: 366 inclusive (un año bisiesto).
_EXPORT_MAX_DAYS = 366
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: str) -> str:
    """Evita que Excel/Sheets interpreten la celda como fórmula."""
    return f"'{value}" if value.startswith(_CSV_FORMULA_PREFIXES) else value


def _csv_amount(value: Decimal) -> str:
    """Monto con coma decimal (planillas en es-AR)."""
    return f"{value:.2f}".replace(".", ",")


# Sin extensión ".csv": Cloudflare cachea esa extensión por defecto y la URL
# del mes es la misma para todos los tenants.
@router.get("/panel/agenda/export")
async def panel_agenda_export_csv(
    date_from: Annotated[str | None, Query(alias="from")] = None,
    date_to: Annotated[str | None, Query(alias="to")] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """
    Descarga en CSV los turnos del tenant cuyo inicio cae entre `from` y `to`
    (ambas inclusive, en la zona horaria del negocio), en cualquier estado.
    """
    tenant_tz = _tenant_tz(tenant)
    try:
        first = date.fromisoformat(date_from or "")
        last = date.fromisoformat(date_to or "")
        valid = 0 <= (last - first).days < _EXPORT_MAX_DAYS
        range_start = datetime.combine(first, time(0, 0), tzinfo=tenant_tz)
        # OverflowError si "to" es 9999-12-31 (no hay día siguiente).
        range_end = datetime.combine(
            last + timedelta(days=1), time(0, 0), tzinfo=tenant_tz
        )
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        # Mensaje fijo en la agenda: nunca se refleja la entrada.
        return RedirectResponse(
            url=f"{_AGENDA_ACTION_REDIRECT}?export=invalid",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    stmt = (
        select(Booking, col(Service.name), col(Staff.name))
        .join(Service, Booking.service_id == Service.id)
        .outerjoin(
            Staff, and_(Booking.staff_id == Staff.id, Staff.tenant_id == tenant.id)
        )
        .where(
            Booking.tenant_id == tenant.id,
            Service.tenant_id == tenant.id,
            Booking.start_time >= range_start,
            Booking.start_time < range_end,
        )
        .order_by(Booking.start_time)
    )
    rows = (await session.execute(stmt)).all()

    buffer = io.StringIO()
    # ";" y comillas en todas las celdas: Excel es-AR usa ";" como separador y
    # un ";" suelto en un nombre partiría la celda (y abriría una fórmula).
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writerow(
        [
            "Fecha",
            "Hora",
            "Servicio",
            "Personal",
            "Cliente",
            "Teléfono",
            "Estado",
            "Precio",
            "Seña",
            "Notas",
            "Respuestas",
        ]
    )
    for booking, service_name, staff_name in rows:
        start_local = booking.start_time.astimezone(tenant_tz)
        deposit = booking.deposit_at_booking or Decimal(0)
        writer.writerow(
            [
                start_local.strftime("%d/%m/%Y"),
                start_local.strftime("%H:%M"),
                _csv_cell(service_name),
                _csv_cell(staff_name or ""),
                _csv_cell(booking.client_name),
                _csv_cell(booking.client_phone),
                _AGENDA_STATUS_LABELS.get(booking.status, booking.status),
                _csv_amount(booking.price_at_booking),
                _csv_amount(deposit),
                _csv_cell(booking.internal_notes or ""),
                _csv_cell(
                    " | ".join(
                        f"{a['question']}: {a['answer']}" for a in booking.answers or []
                    )
                ),
            ]
        )

    filename = f"turnos_{first.isoformat()}_{last.isoformat()}.csv"
    # BOM: sin él Excel abre el UTF-8 como Latin-1 y rompe los acentos.
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Datos personales: ningún caché (ni Cloudflare) puede guardarlo.
            "Cache-Control": "no-store, private",
        },
    )


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
                end_time = start_time + timedelta(
                    minutes=service.duration_minutes + service.buffer_minutes
                )
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


@router.post("/panel/agenda/{booking_id}/notes", response_class=HTMLResponse)
async def panel_agenda_notes(
    request: Request,
    booking_id: int,
    internal_notes: Annotated[str | None, Form()] = None,
    day: Annotated[str | None, Form()] = None,
    tenant: Tenant = Depends(get_current_tenant_from_session),
    session: AsyncSession = Depends(get_db),
):
    """Guarda la nota interna del turno; vacía = sin nota. No cambia el estado."""
    await validate_csrf(request)
    booking = await _load_booking_for_tenant(session, booking_id, tenant)
    cleaned, error = _clean_owner_text(internal_notes, BOOKING_NOTES_MAX)
    if error:
        params = {"day": day} if day else {}
        params["notes"] = error
        return RedirectResponse(
            url=f"{_AGENDA_ACTION_REDIRECT}?{urlencode(params)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    booking.internal_notes = cleaned
    await session.commit()
    return _redirect_to_agenda(day)
