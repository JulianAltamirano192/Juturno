import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import redis.asyncio as redis
from sqlalchemy import and_, select

from app.booking_actions import transition_booking_status
from app.config import settings
from app.models import Booking, NotificationOutbox, Tenant
from app.mp_connect import REFRESH_AHEAD_DAYS, refresh_tenant_mp_token

logger = logging.getLogger(__name__)

# ============================================================================
# Redis client — per-loop
# ----------------------------------------------------------------------------
# Necesario para tests: pytest-asyncio crea un loop por test, y un cliente
# global queda atado al loop del primer test. Cuando el loop se cierra y se
# crea uno nuevo, la conexión queda muerta ("got Future attached to a
# different loop"). Mismo patrón que app/auth.py.
# ============================================================================
_redis_clients: dict[int, "redis.Redis"] = {}


def _get_redis_client() -> "redis.Redis":
    """Devuelve un cliente Redis atado al event loop actual."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    if key not in _redis_clients:
        _redis_clients[key] = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_clients[key]


async def process_reminders(async_session_maker):
    """
    Job periódico que busca turnos próximos a cumplirse y encola recordatorios.
    Protegido por Lock distribuido de Redis con TTL (EX 30) para evitar deadlocks de instancia.
    """
    redis_client = _get_redis_client()
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
    redis_client = _get_redis_client()
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
                .with_for_update(skip_locked=True, of=Booking)
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
                    await transition_booking_status(
                        session, booking, "expired", actor="system"
                    )
                    expired_count += 1

            if expired_count:
                await session.commit()
                logger.info(
                    f"Se expiraron {expired_count} reservas por seña no pagada."
                )
    finally:
        if await redis_client.get(lock_key) == lock_value:
            await redis_client.delete(lock_key)


async def process_mp_token_refresh(async_session_maker):
    """
    Job diario que renueva los access_token OAuth de Mercado Pago de los
    tenants cuyo token vence en menos de REFRESH_AHEAD_DAYS días. MP los
    emite con vida de ~180 días; con renovación proactiva el dueño del
    negocio no tiene que reconectar su cuenta manualmente.

    Mismo patrón de lock distribuido en Redis que los demás jobs.
    """
    redis_client = _get_redis_client()
    lock_key = "mp-token-refresh-job-lock"
    lock_value = uuid4().hex

    lock_acquired = await redis_client.set(lock_key, lock_value, nx=True, ex=30)
    if not lock_acquired:
        logger.debug(
            "Lock de refresh de tokens MP ocupado por otra instancia. Omitiendo."
        )
        return

    try:
        # Solo tenants con refresh token y vencimiento dentro de la ventana.
        # Se recolectan solo IDs: cada renovación usa su propia sesión
        # (refresh_tenant_mp_token commitea, y el commit expiraría los demás
        # objetos si compartiéramos sesión entre tenants).
        threshold = datetime.now(timezone.utc) + timedelta(days=REFRESH_AHEAD_DAYS)
        async with async_session_maker() as session:
            stmt = select(Tenant.id).where(
                and_(
                    Tenant.mp_refresh_token_enc.is_not(None),
                    Tenant.mp_token_expires_at.is_not(None),
                    Tenant.mp_token_expires_at <= threshold,
                )
            )
            tenant_ids = (await session.execute(stmt)).scalars().all()

        if not tenant_ids:
            return

        refreshed = 0
        failed = 0
        for tenant_id in tenant_ids:
            async with async_session_maker() as session:
                tenant = await session.get(Tenant, tenant_id)
                if tenant is None:
                    continue
                ok = await refresh_tenant_mp_token(session, tenant)
                if ok:
                    refreshed += 1
                else:
                    failed += 1

        logger.info(
            f"Refresh de tokens MP: {refreshed} renovados, {failed} fallidos "
            "(reconexión manual)."
        )
    finally:
        if await redis_client.get(lock_key) == lock_value:
            await redis_client.delete(lock_key)
