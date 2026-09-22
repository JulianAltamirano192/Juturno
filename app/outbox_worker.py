import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.models import Booking, NotificationOutbox, Tenant
from app.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)


def format_booking_datetime(dt, tenant_timezone: str) -> str:
    """
    Formatea un datetime UTC al timezone del tenant (por defecto America/Argentina/Buenos_Aires) en formato legible.
    Ejemplo: '15/09/2026 a las 15:00'.
    """
    if dt.tzinfo is None:
        from datetime import timezone as _tz

        dt = dt.replace(tzinfo=_tz.utc)

    tz_name = (
        tenant_timezone
        if (tenant_timezone and tenant_timezone != "UTC")
        else "America/Argentina/Buenos_Aires"
    )
    local_dt = dt.astimezone(ZoneInfo(tz_name))
    return local_dt.strftime("%d/%m/%Y a las %H:%M")


async def process_outbox(async_session_maker) -> None:
    """Envía eventos pendientes; el scheduler registra este job cada minuto."""
    whatsapp = WhatsAppService(
        phone_number_id=settings.WHATSAPP_PHONE_NUMBER_ID,
        access_token=settings.WHATSAPP_TOKEN,
    )

    async with async_session_maker() as session:
        async with session.begin():
            result = await session.execute(
                select(NotificationOutbox)
                .where(NotificationOutbox.status == "pending")
                .with_for_update(skip_locked=True)
            )
            events = result.scalars().all()

            for event in events:
                booking = await session.get(Booking, event.booking_id)
                if booking is None:
                    event.status = "failed"
                    event.retry_count += 1
                    event.error_message = "Booking not found"
                    continue

                # Cargar el tenant para obtener su timezone
                tenant = await session.get(Tenant, booking.tenant_id)
                tenant_tz = tenant.timezone if tenant else "UTC"

                # Formatear la fecha en el timezone del tenant
                fecha_legible = format_booking_datetime(booking.start_time, tenant_tz)

                try:
                    if event.notification_type == "confirmation":
                        response = await whatsapp.send_confirmation(
                            booking.client_phone,
                            booking.id,
                            booking.client_name,
                            fecha_legible,
                        )
                    else:
                        response = await whatsapp.send_reminder(
                            booking.client_phone,
                            booking.id,
                            booking.client_name,
                            fecha_legible,
                        )
                    response.raise_for_status()
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
                    logger.exception("No se pudo enviar outbox event %s", event.id)
