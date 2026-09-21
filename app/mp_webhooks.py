import hmac
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import httpx
from fastapi import APIRouter, Request, Header, Depends, Response, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from app.models import ProcessedWebhookEvent, Payment, Booking, NotificationOutbox
from app.database import get_db
from app.config import settings

router = APIRouter()

# Ventana de tolerancia para timestamps de webhooks (en segundos)
_WEBHOOK_TS_TOLERANCE = 300  # 5 minutos


# ─────────────────────────────────────────────────────────────────
# Integración con la API de Mercado Pago
# ─────────────────────────────────────────────────────────────────


async def get_payment_details(data_id: str) -> Optional[Dict[str, Any]]:
    """
    Consulta la API de MP y devuelve el JSON completo del pago.
    Devuelve None si MP responde 404 (pago no existe en su sistema).
    Lanza HTTPException 504 si hay timeout.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payment_response = await client.get(
                f"https://api.mercadopago.com/v1/payments/{data_id}",
                headers={"Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}"},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504, detail="Timeout consultando Mercado Pago"
        ) from exc

    if payment_response.status_code == 404:
        return None
    payment_response.raise_for_status()
    return payment_response.json()


# ─────────────────────────────────────────────────────────────────
# Verificación de firma y timestamp
# ─────────────────────────────────────────────────────────────────


def verify_mp_signature(x_signature: str, x_request_id: str, data_id: str) -> bool:
    """
    Verifica la autenticidad del webhook de MP mediante HMAC SHA256.
    Formato esperado en x-signature: 'ts=12345,v1=hash_hmac'
    """
    if not x_signature or not x_request_id:
        return False

    try:
        parts = dict(item.split("=") for item in x_signature.split(","))
        ts = parts.get("ts")
        v1 = parts.get("v1")

        if not ts or not v1:
            return False

        manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"

        expected_hmac = hmac.new(
            settings.MP_SECRET_KEY.encode(), manifest.encode(), hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected_hmac, v1)
    except Exception:
        return False


def verify_timestamp_freshness(x_signature: str) -> bool:
    """
    Verifica que el timestamp del webhook esté dentro de la ventana de
    tolerancia (±5 min). Previene ataques de replay.
    """
    try:
        parts = dict(item.split("=") for item in x_signature.split(","))
        ts = parts.get("ts")
        if not ts:
            return False
        webhook_time = int(ts)
        now = int(datetime.now(timezone.utc).timestamp())
        return abs(now - webhook_time) <= _WEBHOOK_TS_TOLERANCE
    except (ValueError, TypeError):
        return False


# ─────────────────────────────────────────────────────────────────
# Helpers para extracción de datos del webhook
# ─────────────────────────────────────────────────────────────────


def _extract_data_id(payload: dict, request: Request) -> str:
    """
    Extrae el ID del recurso afectado. MP manda dos formatos:
    - Nuevo: ?data.id=X&type=payment  → payload["data"]["id"] o query param
    - Viejo: ?id=X&topic=payment      → payload["id"] o query param
    """
    data_id = payload.get("data", {}).get("id")
    if data_id is None:
        data_id = request.query_params.get("data.id")
    if data_id is None:
        data_id = payload.get("id")
    if data_id is None:
        data_id = request.query_params.get("id")
    return str(data_id) if data_id else ""


def _extract_event_id(payload: dict, request: Request) -> str:
    """
    Devuelve un identificador único para idempotencia.
    Si no hay un id global, sintetiza uno con data_id + tipo de evento.
    """
    event_id = (
        payload.get("id")
        or request.query_params.get("id")
        or payload.get("data", {}).get("id")
        or request.query_params.get("data.id")
    )
    if event_id is not None:
        return str(event_id)
    # Fallback: usar data_id + action como clave sintética
    data_id = _extract_data_id(payload, request)
    action = (
        payload.get("action")
        or payload.get("type")
        or request.query_params.get("topic")
        or "unknown"
    )
    return f"{data_id}:{action}"


def _extract_event_type(payload: dict, request: Request) -> str:
    return str(
        payload.get("action")
        or payload.get("type")
        or request.query_params.get("topic")
        or "payment"
    )


def _parse_booking_id_from_external_reference(
    external_ref: Optional[str],
) -> Optional[int]:
    """
    Extrae el booking_id del external_reference.
    Formato esperado: 'booking-23' → 23
    """
    if not external_ref:
        return None
    prefix = "booking-"
    if not external_ref.startswith(prefix):
        return None
    try:
        return int(external_ref[len(prefix) :])
    except (ValueError, TypeError):
        return None


def _parse_mp_datetime(value: Optional[str]) -> Optional[datetime]:
    """Convierte un datetime ISO de MP a datetime tz-aware."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ─────────────────────────────────────────────────────────────────
# Endpoint del webhook
# ─────────────────────────────────────────────────────────────────


@router.post("/webhooks/mercadopago")
async def mercadopago_webhook(
    request: Request,
    x_signature: str = Header(None, alias="x-signature"),
    x_request_id: str = Header(None, alias="x-request-id"),
    session: AsyncSession = Depends(get_db),
):
    payload = await request.json()

    event_id = _extract_event_id(payload, request)
    event_type = _extract_event_type(payload, request)
    data_id = _extract_data_id(payload, request)

    # 1. Verificación estricta de firma HMAC
    if not verify_mp_signature(x_signature, x_request_id, data_id):
        raise HTTPException(status_code=401, detail="Firma de Mercado Pago inválida")

    # 1b. Protección contra replay: timestamps fuera de ±5 min
    if not verify_timestamp_freshness(x_signature):
        raise HTTPException(
            status_code=403,
            detail="Timestamp del webhook fuera de ventana de tolerancia",
        )

    # Si no hay data_id extraíble (ej. merchant_order mal formado), salimos limpio
    if not data_id:
        return Response(content="EVENT_IGNORED_NO_DATA_ID", status_code=200)

    # 2. Gate de idempotencia con soporte de reintentos
    try:
        webhook_event = ProcessedWebhookEvent(
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            status="processing",
        )
        session.add(webhook_event)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        stmt = select(ProcessedWebhookEvent).where(
            ProcessedWebhookEvent.event_id == event_id
        )
        webhook_event = (await session.execute(stmt)).scalar_one_or_none()
        if webhook_event is None or webhook_event.status == "processed":
            return Response(content="DUPLICATE_EVENT_IGNORED", status_code=200)
        # Estado 'processing' o 'failed' → reintento legítimo
        webhook_event.status = "processing"
        session.add(webhook_event)
        await session.commit()

    # 3. Procesamiento del pago
    try:
        details = await get_payment_details(data_id)

        if details is None:
            # MP no encuentra el pago. Puede ser un ID del simulador o un evento viejo.
            webhook_event.status = "processed"
            webhook_event.processed_at = datetime.now(timezone.utc)
            session.add(webhook_event)
            await session.commit()
            return Response(content="PAYMENT_NOT_FOUND_ON_MP", status_code=200)

        payment_status = details.get(
            "status"
        )  # approved, pending, rejected, in_process, ...
        external_reference = details.get("external_reference") or ""
        booking_id = _parse_booking_id_from_external_reference(external_reference)

        if booking_id is None:
            # El pago no está vinculado a un booking de Juturno
            webhook_event.status = "processed"
            webhook_event.processed_at = datetime.now(timezone.utc)
            session.add(webhook_event)
            await session.commit()
            return Response(content="NO_BOOKING_LINKED", status_code=200)

        # Buscar el Payment por mp_payment_id
        stmt = select(Payment).where(Payment.mp_payment_id == data_id)
        payment = (await session.execute(stmt)).scalar_one_or_none()

        if payment is None:
            # Auto-crear el Payment con los datos de MP
            transaction_amount = details.get("transaction_amount") or 0
            payment_method_id = details.get("payment_method_id") or "unknown"
            paid_at = _parse_mp_datetime(details.get("date_approved"))

            payment = Payment(
                booking_id=booking_id,
                amount=transaction_amount,
                mp_payment_id=data_id,
                method=payment_method_id,
                status=payment_status,
                paid_at=paid_at if payment_status == "approved" else None,
            )
            session.add(payment)
            await session.flush()
        else:
            # Actualizar el estado si cambió
            if payment.status != payment_status:
                payment.status = payment_status
                if payment_status == "approved" and payment.paid_at is None:
                    payment.paid_at = _parse_mp_datetime(
                        details.get("date_approved")
                    ) or datetime.now(timezone.utc)
                session.add(payment)

        # Si el pago está aprobado, confirmar el booking y encolar WhatsApp
        if payment_status == "approved":
            booking = await session.get(Booking, booking_id)
            if booking is not None:
                webhook_event.booking_id = booking.id

                if booking.status != "confirmed":
                    booking.status = "confirmed"
                    session.add(booking)

                    # Evitar duplicar outbox si ya hay uno para esta confirmación
                    outbox_stmt = select(NotificationOutbox).where(
                        NotificationOutbox.booking_id == booking.id,
                        NotificationOutbox.notification_type == "confirmation",
                    )
                    existing_outbox = (
                        await session.execute(outbox_stmt)
                    ).scalar_one_or_none()

                    if existing_outbox is None:
                        outbox_event = NotificationOutbox(
                            booking_id=booking.id,
                            notification_type="confirmation",
                            status="pending",
                        )
                        session.add(outbox_event)

        # 4. Marcar evento como processed y commitear
        webhook_event.status = "processed"
        webhook_event.processed_at = datetime.now(timezone.utc)
        session.add(webhook_event)
        await session.commit()

        return Response(content="EVENT_PROCESSED", status_code=200)

    except HTTPException:
        # Errores esperados (timeout, etc.) — marcar como failed y propagar
        await session.rollback()
        stmt = select(ProcessedWebhookEvent).where(
            ProcessedWebhookEvent.event_id == event_id
        )
        fresh = (await session.execute(stmt)).scalar_one_or_none()
        if fresh is not None:
            fresh.status = "failed"
            session.add(fresh)
            await session.commit()
        raise

    except Exception:
        await session.rollback()
        # Re-fetch para no reusar un objeto invalidado por el rollback
        stmt = select(ProcessedWebhookEvent).where(
            ProcessedWebhookEvent.event_id == event_id
        )
        fresh = (await session.execute(stmt)).scalar_one_or_none()
        if fresh is not None:
            fresh.status = "failed"
            session.add(fresh)
            await session.commit()
        raise
