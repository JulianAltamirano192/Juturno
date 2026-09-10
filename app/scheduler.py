import logging
from datetime import datetime, timedelta
import redis.asyncio as redis
from sqlalchemy import select, and_

from app.models import Booking, NotificationOutbox

logger = logging.getLogger(__name__)

# Conexión asíncrona a Redis
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

async def process_reminders(async_session_maker):
    """
    Job periódico que busca turnos próximos a cumplirse y encola recordatorios.
    Protegido por Lock distribuido de Redis con TTL (EX 30) para evitar deadlocks de instancia.
    """
    lock_key = "reminder-job-lock"
    
    # 1. Lock distribuido: SET NX con expiración de seguridad de 30 segundos
    lock_acquired = await redis_client.set(lock_key, "locked", nx=True, ex=30)
    
    if not lock_acquired:
        logger.debug("Lock de recordatorios ocupado por otra instancia. Omitiendo ejecución.")
        return

    logger.info("Lock adquirido exitosamente. Buscando turnos para recordatorio...")
    
    now = datetime.utcnow()
    target_start = now + timedelta(hours=24)
    target_end = target_start + timedelta(minutes=5)
    
    async with async_session_maker() as session:
        # 2. Buscar bookings confirmados cuyo recordatorio aún no fue enviado
        stmt = select(Booking).where(
            and_(
                Booking.start_time >= target_start,
                Booking.start_time <= target_end,
                Booking.status == 'confirmed',
                Booking.reminder_sent == False
            )
        )
        result = await session.execute(stmt)
        bookings = result.scalars().all()
        
        # 3. Marcar flag y encolar en el Outbox en lote atómico
        for booking in bookings:
            outbox_event = NotificationOutbox(
                booking_id=booking.id,
                notification_type="reminder",
                status="pending"
            )
            session.add(outbox_event)
            
            booking.reminder_sent = True
            session.add(booking)
            
        if bookings:
            await session.commit()
            logger.info(f"Se encolaron {len(bookings)} recordatorios en el Outbox.")