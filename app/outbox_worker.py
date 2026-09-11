import logging

from sqlalchemy import select

from app.config import settings
from app.models import Booking, NotificationOutbox
from app.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)


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

                try:
                    if event.notification_type == "confirmation":
                        response = await whatsapp.send_confirmation(
                            booking.client_phone, booking.id, booking.client_name,
                            booking.start_time.isoformat(),
                        )
                    else:
                        response = await whatsapp.send_reminder(
                            booking.client_phone, booking.id, booking.client_name,
                            booking.start_time.isoformat(),
                        )
                    response.raise_for_status()
                    event.status = "sent"
                    event.error_message = None
                except Exception as exc:
                    event.status = "failed"
                    event.retry_count += 1
                    event.error_message = str(exc)
                    logger.exception("No se pudo enviar outbox event %s", event.id)
