# app/booking_actions.py
"""
Transiciones de estado de Booking (Tarea 8).

Concentra la máquina de estados y los setters de auditoría para que
cualquier caller (endpoints del panel, webhook de MP, scheduler) use
la misma lógica. Antes de este módulo, la transición se hacía inline
(ej: mp_webhooks.py asignaba booking.status = "confirmed" a mano).
"""
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Booking, NotificationOutbox

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


class InvalidTransitionError(Exception):
    """La transición pedida no está en VALID_TRANSITIONS."""

    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"Transición no permitida: {current} → {target}")


class BookingNotStartedError(Exception):
    """Se intentó marcar no_show/completed antes de start_time."""


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
               "owner" (panel), "system" (scheduler), "webhook_mp".
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

    # Al cancelar, cancelar los outbox pendientes del booking para no
    # mandar un WhatsApp de confirmación/recordatorio después de cancelar.
    if new_status == "cancelled":
        outbox_stmt = select(NotificationOutbox).where(
            NotificationOutbox.booking_id == booking.id,
            NotificationOutbox.status == "pending",
        )
        pending_outbox = (await session.execute(outbox_stmt)).scalars().all()
        for evt in pending_outbox:
            evt.status = "cancelled"
            evt.error_message = "booking_cancelled"
            session.add(evt)

    return booking
