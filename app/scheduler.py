import logging
from uuid import uuid4
from datetime import datetime, timedelta, timezone
import redis.asyncio as redis
from sqlalchemy import select, and_

from app.models import Booking, Tenant, NotificationOutbox
from app.config import settings

logger = logging.getLogger(__name__)

# Conexión asíncrona a Redis
redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)


async def process_reminders(async_session_maker):
    """
    Job periódico que busca turnos próximos a cumplirse y encola recordatorios.
    Protegido por Lock distribuido de Redis con TTL (EX 30) para evitar deadlocks de instancia.
    """
    lock_key = "reminder-job-lock"
    lock_value = uuid4().hex

    # 1. Lock distribuido: SET NX con expiración de seguridad de 30 segundos
    lock_acquired = await redis_client.set(lock_key, lock_value, nx=True, ex=30)

    if not lock_acquired:
        logger.debug(
            "Lock de recordatorios ocupado por otra instancia. Omitiendo ejecución."
        )
        return

    try:
        logger.info("Lock adquirido exitosamente. Buscando turnos para recordatorio...")

        now = datetime.now(timezone.utc)
        target_start = now + timedelta(hours=24)
        target_end = target_start + timedelta(minutes=5)

        async with async_session_maker() as session:
            # 2. Buscar bookings confirmados cuyo recordatorio aún no fue enviado
            stmt = select(Booking).where(
                and_(
                    Booking.start_time >= target_start,
                    Booking.start_time <= target_end,
                    Booking.status == "confirmed",
                    Booking.reminder_sent.is_(False),
                )
            )
            result = await session.execute(stmt)
            bookings = result.scalars().all()

            # 3. Marcar flag y encolar en el Outbox en lote atómico
            for booking in bookings:
                outbox_event = NotificationOutbox(
                    booking_id=booking.id,
                    notification_type="reminder",
                    status="pending",
                )
                session.add(outbox_event)

                booking.reminder_sent = True
                session.add(booking)

            if bookings:
                await session.commit()
                logger.info(f"Se encolaron {len(bookings)} recordatorios en el Outbox.")
    finally:
        if await redis_client.get(lock_key) == lock_value:
            await redis_client.delete(lock_key)


async def process_deposit_expiration(async_session_maker):
    """
    Job periódico que expira reservas 'pending' cuyo límite de pago de seña
    (deposit_expiration_minutes del tenant) venció. Al marcarlas 'expired'
    liberan el horario (el ExcludeConstraint solo bloquea pending/confirmed).
    Mismo patrón de lock distribuido que los demás jobs.
    """
    lock_key = "deposit-expiration-job-lock"
    lock_value = uuid4().hex

    # 1. Lock distribuido: SET NX con expiración de seguridad de 30 segundos
    lock_acquired = await redis_client.set(lock_key, lock_value, nx=True, ex=30)

    if not lock_acquired:
        logger.debug(
            "Lock de expiración ocupado por otra instancia. Omitiendo ejecución."
        )
        return

    try:
        now = datetime.now(timezone.utc)

        async with async_session_maker() as session:
            # 2. Pendientes de tenants con límite configurado
            stmt = (
                select(Booking, Tenant)
                .join(Tenant, Booking.tenant_id == Tenant.id)
                .where(
                    and_(
                        Booking.status == "pending",
                        Tenant.deposit_expiration_minutes.is_not(None),
                    )
                )
            )
            result = await session.execute(stmt)
            rows = result.all()

            # 3. Vencer solo los que superaron su deadline
            expired_count = 0
            for booking, tenant in rows:
                deadline = booking.created_at + timedelta(
                    minutes=tenant.deposit_expiration_minutes
                )
                if now > deadline:
                    booking.status = "expired"
                    session.add(booking)
                    expired_count += 1

            if expired_count:
                await session.commit()
                logger.info(
                    f"Se expiraron {expired_count} reservas por seña no pagada."
                )
    finally:
        if await redis_client.get(lock_key) == lock_value:
            await redis_client.delete(lock_key)
