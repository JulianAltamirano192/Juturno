# app/booking_actions.py
"""
Transiciones de estado de Booking (Tarea 8).

Concentra la máquina de estados y los setters de auditoría para que
cualquier caller (endpoints del panel, webhook de MP, scheduler) use
la misma lógica. Antes de este módulo, la transición se hacía inline
(ej: mp_webhooks.py asignaba booking.status = "confirmed" a mano).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Booking, NotificationOutbox, Tenant

logger = logging.getLogger(__name__)

# Transiciones permitidas. Cualquier otra es error 409.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"confirmed", "cancelled", "expired"},
    "confirmed": {"cancelled", "no_show", "completed"},
    "expired": {"confirmed"},
    "cancelled": set(),
    "no_show": set(),
    "completed": set(),
}


# Estados que requieren que el turno ya haya empezado.
REQUIRE_STARTED = {"no_show", "completed"}

# Tope de avisos al dueño por tenant en 24 h. El número del dueño todavía no se
# verifica (OTP pendiente): sin tope, alguien podría registrar el WhatsApp de
# un tercero y usar el número de la plataforma para mandarle mensajes en masa.
OWNER_ALERTS_PER_DAY = 30


class InvalidTransitionError(Exception):
    """La transición pedida no está en VALID_TRANSITIONS."""

    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"Transición no permitida: {current} → {target}")


class BookingNotStartedError(Exception):
    """Se intentó marcar no_show/completed antes de start_time."""


async def _enqueue_once(
    session: AsyncSession, booking: Booking, notification_type: str
) -> None:
    """Encola el aviso si el booking todavía no tiene uno de ese tipo (en
    cualquier estado: uno solo por booking y tipo, ni siquiera si el anterior
    quedó failed/cancelled)."""
    stmt = select(NotificationOutbox.id).where(
        NotificationOutbox.booking_id == booking.id,
        NotificationOutbox.notification_type == notification_type,
    )
    if (await session.execute(stmt)).first() is None:
        session.add(
            NotificationOutbox(
                booking_id=booking.id,
                notification_type=notification_type,
                status="pending",
            )
        )


async def enqueue_confirmation(session: AsyncSession, booking: Booking) -> None:
    """Encola la confirmación por WhatsApp al cliente (una sola por booking)."""
    await _enqueue_once(session, booking, "confirmation")


async def enqueue_owner_alert(session: AsyncSession, booking: Booking) -> None:
    """Encola el aviso de turno nuevo al dueño (uno solo por booking), solo si
    el negocio cargó su número de WhatsApp: vacío desactiva los avisos. Con el
    tope diario del tenant alcanzado no se encola (cuenta cualquier estado)."""
    tenant = await session.get(Tenant, booking.tenant_id)
    if tenant is None or not tenant.whatsapp_number:
        return
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    count_stmt = (
        select(func.count())
        .select_from(NotificationOutbox)
        .join(Booking, Booking.id == NotificationOutbox.booking_id)
        .where(
            Booking.tenant_id == booking.tenant_id,
            NotificationOutbox.notification_type == "owner_new_booking",
            NotificationOutbox.created_at >= since,
        )
    )
    if (await session.execute(count_stmt)).scalar_one() >= OWNER_ALERTS_PER_DAY:
        logger.warning(
            "Tope diario de avisos al dueño alcanzado (tenant_id=%s)",
            booking.tenant_id,
        )
        return
    await _enqueue_once(session, booking, "owner_new_booking")


async def transition_booking_status(
    session: AsyncSession,
    booking: Booking,
    new_status: str,
    actor: str = "system",
    reason: str | None = None,
) -> Booking:
    """
    Aplica una transición de estado al booking y registra la auditoría.

    NO hace commit: el caller decide cuándo commitear (permite agrupar
    la transición con otras operaciones en una misma transacción).

    Args:
        session: sesión async.
        booking: instancia cargada del Booking.
        new_status: uno de "confirmed" | "cancelled" | "completed" |
                    "no_show" | "expired".
        actor: quién dispara la acción. Valores típicos:
               "owner" (panel), "system" (scheduler), "webhook_mp",
               "client" (reserva pública sin seña). Al confirmar se avisa
               al dueño salvo que la acción la haya hecho él ("owner").
        reason: motivo. Solo se persiste si new_status == "cancelled".

    Raises:
        InvalidTransitionError: si la transición no está permitida.
        BookingNotStartedError: si new_status ∈ {no_show, completed}
            y el turno todavía no empezó.

    Returns:
        El mismo booking (mutado), ya agregado a la sesión.
    """
    if new_status not in VALID_TRANSITIONS.get(booking.status, set()):
        raise InvalidTransitionError(booking.status, new_status)

    now = datetime.now(timezone.utc)

    if new_status in REQUIRE_STARTED and booking.start_time > now:
        raise BookingNotStartedError(
            f"No se puede marcar '{new_status}' antes de que empiece el turno."
        )

    booking.status = new_status
    booking.status_changed_at = now
    booking.status_changed_by = actor

    if new_status == "cancelled":
        booking.cancellation_reason = reason
    elif new_status == "no_show":
        booking.no_show_at = now
    elif new_status == "completed":
        booking.completed_at = now

    session.add(booking)

    if new_status == "confirmed":
        await enqueue_confirmation(session, booking)
        if actor != "owner":
            await enqueue_owner_alert(session, booking)

    # Al cancelar, cancelar los outbox sin enviar del booking (pending y los
    # failed que process_outbox reintenta) para no mandar un WhatsApp de
    # confirmación/recordatorio después de cancelar. SKIP LOCKED: el que
    # process_outbox tiene tomado se está enviando a Meta (hasta ~33 s); no se
    # lo espera ni se pisa su resultado, y si falla el worker no lo reintenta
    # porque el turno ya no está confirmado.
    if new_status == "cancelled":
        outbox_stmt = (
            select(NotificationOutbox)
            .where(
                NotificationOutbox.booking_id == booking.id,
                NotificationOutbox.status.in_(("pending", "failed")),
            )
            .with_for_update(skip_locked=True)
        )
        pending_outbox = (await session.execute(outbox_stmt)).scalars().all()
        for evt in pending_outbox:
            evt.status = "cancelled"
            evt.error_message = "booking_cancelled"
            session.add(evt)

    return booking
