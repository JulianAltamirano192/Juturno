import logging
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, literal_column, or_, select
from sqlmodel import col

from app.config import settings
from app.models import Booking, NotificationOutbox, Service, Tenant
from app.phone import InvalidPhoneError, normalize_whatsapp_phone
from app.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)

# Intentos totales por evento. Tras n fallos el próximo intento es a los
# 2^n - 1 minutos de encolado (1, 3, 7, 15, 31, 63): cubre ~1 h de caída de Meta.
MAX_OUTBOX_ATTEMPTS = 7

# ponytail: backoff calculado desde created_at para no agregar columna;
# migrar a next_attempt_at si hace falta un calendario de reintentos propio.
_retry_due = (
    NotificationOutbox.created_at
    + literal_column("interval '1 minute'")
    * (func.power(2, NotificationOutbox.retry_count) - 1)
    <= func.now()
)
_ELIGIBLE = or_(
    NotificationOutbox.status == "pending",
    and_(
        NotificationOutbox.status == "failed",
        NotificationOutbox.retry_count < MAX_OUTBOX_ATTEMPTS,
        _retry_due,
        # Fallidos viejos (p. ej. anteriores a los reintentos) no se reenvían:
        # avisarían de un turno con horas o días de atraso.
        NotificationOutbox.created_at
        >= func.now() - literal_column("interval '2 hours'"),
    ),
)


def format_booking_datetime(dt, tenant_timezone: str) -> str:
    """
    Formatea un datetime UTC al timezone del tenant (por defecto America/Argentina/Buenos_Aires) en formato legible.
    Ejemplo: '15/09/2026 a las 15:00'.
    """
    if dt.tzinfo is None:
        from datetime import timezone as _tz

        dt = dt.replace(tzinfo=_tz.utc)

    tz_name = tenant_timezone or "America/Argentina/Buenos_Aires"
    local_dt = dt.astimezone(ZoneInfo(tz_name))
    return local_dt.strftime("%d/%m/%Y a las %H:%M")


async def process_outbox(async_session_maker) -> None:
    """
    Envía eventos pendientes y reintenta los fallidos con backoff; el
    scheduler registra este job cada minuto.

    Cada evento va en su propia transacción: un error (de Meta o de datos,
    p. ej. una timezone inválida) marca solo ese evento como fallido, y lo
    ya enviado queda commiteado aunque el proceso muera a mitad del lote.
    Si `_send_event` devuelve un motivo, el evento queda `cancelled` con ese
    motivo en `error_message` (no se reintenta).
    """
    whatsapp = WhatsAppService(
        phone_number_id=settings.WHATSAPP_PHONE_NUMBER_ID,
        access_token=settings.WHATSAPP_TOKEN,
    )

    async with async_session_maker() as session:
        event_ids = (
            (
                await session.execute(
                    select(col(NotificationOutbox.id))
                    .where(_ELIGIBLE)
                    .order_by(col(NotificationOutbox.id))
                )
            )
            .scalars()
            .all()
        )

    for event_id in event_ids:
        async with async_session_maker() as session, session.begin():
            event = (
                await session.execute(
                    select(NotificationOutbox)
                    .where(NotificationOutbox.id == event_id, _ELIGIBLE)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if event is None:  # otro worker lo tomó o ya no corresponde
                continue

            try:
                # Savepoint: si falla la base adentro, se puede igual marcar failed.
                async with session.begin_nested():
                    reason = await _send_event(session, whatsapp, event)
                if reason is not None:
                    event.status = "cancelled"
                    event.error_message = reason
                    continue
                event.status = "sent"
                event.error_message = None
                logger.info(
                    "Outbox event %s enviado OK (booking_id=%s, type=%s)",
                    event.id,
                    event.booking_id,
                    event.notification_type,
                )
            except Exception as exc:
                event.status = "failed"
                event.retry_count += 1
                event.error_message = str(exc)
                logger.exception(
                    "No se pudo enviar outbox event %s (intento %s/%s)",
                    event.id,
                    event.retry_count,
                    MAX_OUTBOX_ATTEMPTS,
                )


async def _send_event(session, whatsapp: WhatsAppService, event) -> str | None:
    """
    Envía el evento. Devuelve None si se envió, o el motivo por el que no
    corresponde mandarlo: el turno ya no está confirmado, o (aviso al dueño)
    el negocio no tiene número o el guardado no es un teléfono válido.
    """
    booking = await session.get(Booking, event.booking_id)
    if booking is None:
        raise RuntimeError("Booking not found")
    if booking.status != "confirmed":
        return "booking_not_confirmed"

    tenant = await session.get(Tenant, booking.tenant_id)
    tenant_tz = tenant.timezone if tenant else "America/Argentina/Buenos_Aires"
    fecha_legible = format_booking_datetime(booking.start_time, tenant_tz)

    if event.notification_type == "owner_new_booking":
        if tenant is None or not tenant.whatsapp_number:
            return "owner_whatsapp_missing"
        try:
            # Normaliza también los números viejos guardados sin normalizar.
            phone = normalize_whatsapp_phone(tenant.whatsapp_number)
        except InvalidPhoneError:
            return "owner_whatsapp_invalid"
        service = await session.get(Service, booking.service_id)
        response = await whatsapp.send_owner_new_booking(
            phone,
            tenant.name,
            booking.client_name,
            service.name if service else "",
            fecha_legible,
        )
        response.raise_for_status()
        return None

    send = (
        whatsapp.send_confirmation
        if event.notification_type == "confirmation"
        else whatsapp.send_reminder
    )
    response = await send(
        booking.client_phone, booking.id, booking.client_name, fecha_legible
    )
    response.raise_for_status()
    return None
