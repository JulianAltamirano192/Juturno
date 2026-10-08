import hashlib
import hmac
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking_actions import transition_booking_status
from app.config import settings
from app.database import get_db
from app.models import (
    Booking,
    NotificationOutbox,
    Payment,
    ProcessedWebhookEvent,
    Service,
    Tenant,
)
from app.mp_crypto import decrypt_token
from app.services import effective_deposit

logger = logging.getLogger(__name__)

router = APIRouter()

# Ventana de tolerancia para timestamps de webhooks (en segundos)
_WEBHOOK_TS_TOLERANCE = 300  # 5 minutos


# ─────────────────────────────────────────────────────────────────
# Integración con la API de Mercado Pago
# ─────────────────────────────────────────────────────────────────


async def get_payment_details(
    data_id: str, access_token: str | None = None
) -> dict[str, Any] | None:
    """
    Consulta la API de MP y devuelve el JSON completo del pago.
    Devuelve None si MP responde 404 (pago no existe en su sistema).
    Lanza HTTPException 504 si hay timeout.

    access_token: token OAuth del tenant dueño de la reserva. Si no viene
    se usa el de la plataforma (tenants sin cuenta conectada — ver D-012).
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payment_response = await client.get(
                f"https://api.mercadopago.com/v1/payments/{data_id}",
                headers={
                    "Authorization": f"Bearer {access_token or settings.MP_ACCESS_TOKEN}"
                },
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504, detail="Timeout consultando Mercado Pago"
        ) from exc

    if payment_response.status_code == 404:
        return None
    payment_response.raise_for_status()
    return payment_response.json()


async def create_mp_preference(
    booking_id: int,
    amount: float,
    client_name: str,
    notification_url: str = "https://api.juturno.com/webhooks/mercadopago",
    back_url: str | None = None,
    access_token: str | None = None,
) -> dict[str, str]:
    """
    Crea una preferencia de pago en Mercado Pago y devuelve
    {"preference_id": ..., "init_point": ..., "sandbox_init_point": ...,
     "checkout_url": ...}.

    access_token: token OAuth del tenant dueño de la reserva — el dinero
    aterriza en SU cuenta. Si no viene, se usa el de la plataforma (solo
    válido en sandbox según la regla de cobro de D-012).

    "checkout_url" es la URL que debe abrir el cliente para pagar:
    sandbox_init_point si MP_SANDBOX=true (credenciales de prueba)
    o init_point si MP_SANDBOX=false (credenciales de producción).
    Enviar al cliente la URL del ambiente equivocado hace que el pago
    sea imposible.

    Si se pasa back_url, configura back_urls (success/failure/pending)
    y auto_return para que MP redirija al cliente de vuelta a la página
    de reserva tras el pago.

    Lanza HTTPException 502 si la API de MP responde con error,
    para que el caller pueda hacer rollback del booking.
    """
    body = {
        "items": [
            {
                "title": f"Reserva #{booking_id}",
                "quantity": 1,
                "unit_price": amount,
                "currency_id": "ARS",
            }
        ],
        "external_reference": f"booking-{booking_id}",
        "notification_url": notification_url,
        "payer": {"name": client_name},
    }

    if back_url:
        separator = "&" if "?" in back_url else "?"
        body["back_urls"] = {
            "success": back_url,
            "failure": f"{back_url}{separator}result=failure",
            "pending": f"{back_url}{separator}result=pending",
        }
        body["auto_return"] = "approved"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.mercadopago.com/checkout/preferences",
                headers={
                    "Authorization": f"Bearer {access_token or settings.MP_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=502, detail="Timeout creando preferencia en Mercado Pago"
        ) from exc

    if not response.is_success:
        raise HTTPException(
            status_code=502,
            detail=f"Mercado Pago rechazó la preferencia: {response.status_code}",
        )

    data = response.json()
    init_point = data["init_point"]
    sandbox_init_point = data.get("sandbox_init_point", init_point)
    return {
        "preference_id": data["id"],
        "init_point": init_point,
        "sandbox_init_point": sandbox_init_point,
        "checkout_url": sandbox_init_point if settings.MP_SANDBOX else init_point,
    }


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
    external_ref: str | None,
) -> int | None:
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


def _parse_mp_datetime(value: str | None) -> datetime | None:
    """Convierte un datetime ISO de MP a datetime tz-aware."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def _slot_still_free(session: AsyncSession, booking: Booking) -> bool:
    """
    True si ningún otro booking pending/confirmed pisa el horario.
    Misma semántica que el ExcludeConstraint excl_overlapping_bookings
    (mismo tenant, mismo staff vía COALESCE, solapamiento de rangos).
    """
    staff_key = booking.staff_id if booking.staff_id is not None else -1
    stmt = select(Booking).where(
        and_(
            Booking.tenant_id == booking.tenant_id,
            Booking.id != booking.id,
            Booking.status.in_(("pending", "confirmed")),
            func.coalesce(Booking.staff_id, -1) == staff_key,
            Booking.start_time < booking.end_time,
            Booking.end_time > booking.start_time,
        )
    )
    return (await session.execute(stmt)).scalars().first() is None


# ─────────────────────────────────────────────────────────────────
# Resolución de token de cobro para el webhook (D-012)
# ─────────────────────────────────────────────────────────────────


async def _resolve_token_for_payment(
    session: AsyncSession, payload: dict[str, Any]
) -> str | None:
    """
    Determina con qué token consultar a MP este pago (webhook recibido).

    El payload de MP trae "user_id" = la cuenta de MP que recibió el pago.
    Si ese user_id coincide con el mp_user_id de un tenant conectado,
    el dinero está en ESA cuenta → se usa su token OAuth (descifrado).

    Si no hay user_id o no matchea ningún tenant (puede ser un pago de la
    cuenta de la plataforma, o un tenant viejo sin conectar), se cae al
    token global de MP — el comportamiento de la Fase 1.

    Si el token existe pero no se puede descifrar (clave rota), la excepción
    de MPTokenCryptoError se propaga: MP reintentará el webhook.
    """
    mp_user_id = payload.get("user_id") or payload.get("data", {}).get("user_id")
    if not mp_user_id:
        return None  # caller usa el fallback

    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.mp_user_id == str(mp_user_id))
        )
    ).scalar_one_or_none()

    if tenant is not None and tenant.mp_access_token_enc:
        return decrypt_token(tenant.mp_access_token_enc)

    # Tenant encontrado sin credenciales (conexión cortada), o no hay
    # tenant para este user_id (pago de la plataforma, u otro collector)
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
    # IPN (?id=X&topic=...) no trae firma validable; el mismo evento llega
    # también como Webhook firmado (?data.id=X&type=...). Se confirma la
    # recepción para cortar los reintentos de MP, sin procesar nada.
    query = request.query_params
    if "topic" in query and "data.id" not in query:
        return Response(content="IPN_IGNORED", status_code=200)

    try:
        payload = await request.json()
    except (ValueError, RecursionError):  # vacío, inválido, no UTF-8, anidado
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("data", {}), dict):
        raise HTTPException(status_code=400, detail="Payload de webhook inválido")

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
        # D-012: si el pago aterrizó en la cuenta de un tenant conectado,
        # MP solo deja leerlo con el token de ESA cuenta.
        token = await _resolve_token_for_payment(session, payload)
        details = await get_payment_details(data_id, access_token=token)

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

        # SELECT FOR UPDATE before the guards to serialize concurrent updates
        # on the same mp_payment_id. The UNIQUE constraint handles concurrent INSERTs.
        stmt_pay = (
            select(Payment).where(Payment.mp_payment_id == data_id).with_for_update()
        )
        payment = (await session.execute(stmt_pay)).scalar_one_or_none()

        # Guards run BEFORE creating the Payment record so a rejected webhook
        # never commits a Payment row for a fraudulent/invalid payment.
        # Guard 1 (tenant) applies to ALL statuses to prevent cross-tenant data injection.
        # Guards 2 and 3 (currency, amount) only apply to approved payments.
        stmt_booking = select(Booking).where(Booking.id == booking_id).with_for_update()
        booking = (await session.execute(stmt_booking)).scalar_one_or_none()
        if booking is not None:
            webhook_event.booking_id = booking.id

            # Guard 1: collector_id from the authenticated MP response must match
            # the tenant that owns this booking. Applies to every payment status.
            booking_tenant = await session.get(Tenant, booking.tenant_id)
            collector_id = str(details.get("collector_id") or "")
            if booking_tenant is None or not booking_tenant.mp_user_id:
                if settings.is_production:
                    logger.warning(
                        "Webhook MP: tenant %s sin mp_user_id en producción (booking %s)",
                        booking.tenant_id,
                        booking_id,
                    )
                    webhook_event.status = "processed"
                    webhook_event.processed_at = datetime.now(timezone.utc)
                    session.add(webhook_event)
                    await session.commit()
                    return Response(content="TENANT_MISMATCH", status_code=200)
                # sandbox/dev: skip guard (platform-account fallback is expected)
            elif collector_id != booking_tenant.mp_user_id:
                logger.warning(
                    "Webhook MP: tenant mismatch — collector_id %s, "
                    "booking %s pertenece a tenant %s (mp_user_id %s)",
                    collector_id,
                    booking_id,
                    booking.tenant_id,
                    booking_tenant.mp_user_id,
                )
                webhook_event.status = "processed"
                webhook_event.processed_at = datetime.now(timezone.utc)
                session.add(webhook_event)
                await session.commit()
                return Response(content="TENANT_MISMATCH", status_code=200)

            if payment_status == "approved":
                # Guard 2: only ARS payments are valid for Juturno bookings.
                if details.get("currency_id") != "ARS":
                    logger.warning(
                        "Webhook MP: moneda no soportada — %s (booking %s)",
                        details.get("currency_id"),
                        booking_id,
                    )
                    webhook_event.status = "processed"
                    webhook_event.processed_at = datetime.now(timezone.utc)
                    session.add(webhook_event)
                    await session.commit()
                    return Response(content="CURRENCY_NOT_SUPPORTED", status_code=200)

                # Guard 3: paid amount must cover the effective deposit.
                # Use booking.deposit_at_booking (snapshotted at reservation time) so
                # a service price change after the booking doesn't reject a valid payment.
                # Fall back to effective_deposit() for older bookings without the snapshot.
                if booking.deposit_at_booking is not None:
                    deposit = booking.deposit_at_booking
                else:
                    service = await session.get(Service, booking.service_id)
                    if service is None:
                        # Defensive: CASCADE makes this unreachable normally; fail closed.
                        logger.warning(
                            "Webhook MP: service %s no encontrado para booking %s (fail closed)",
                            booking.service_id,
                            booking_id,
                        )
                        webhook_event.status = "processed"
                        webhook_event.processed_at = datetime.now(timezone.utc)
                        session.add(webhook_event)
                        await session.commit()
                        return Response(content="AMOUNT_INSUFFICIENT", status_code=200)
                    deposit = effective_deposit(service.price, service.deposit_amount)
                paid_amount = Decimal(str(details.get("transaction_amount") or 0))
                if not paid_amount.is_finite() or paid_amount < deposit:
                    logger.warning(
                        "Webhook MP: monto insuficiente — pagado %s, seña requerida %s (booking %s)",
                        paid_amount,
                        deposit,
                        booking_id,
                    )
                    webhook_event.status = "processed"
                    webhook_event.processed_at = datetime.now(timezone.utc)
                    session.add(webhook_event)
                    await session.commit()
                    return Response(content="AMOUNT_INSUFFICIENT", status_code=200)

        # All guards passed. Create or update the Payment record.
        # (payment already fetched via SELECT FOR UPDATE above)
        # For concurrent INSERTs the UNIQUE constraint on mp_payment_id is the
        # safety net; the IntegrityError handler below covers that path.
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
            try:
                await session.flush()
            except IntegrityError:
                # A concurrent webhook beat us to the INSERT for this mp_payment_id.
                # Treat as idempotent: mark this event processed and return.
                await session.rollback()
                stmt_ev = select(ProcessedWebhookEvent).where(
                    ProcessedWebhookEvent.event_id == event_id
                )
                wh = (await session.execute(stmt_ev)).scalar_one_or_none()
                if wh is not None:
                    wh.status = "processed"
                    wh.processed_at = datetime.now(timezone.utc)
                    session.add(wh)
                    await session.commit()
                return Response(content="EVENT_PROCESSED", status_code=200)
        else:
            # Actualizar el estado si cambió
            if payment.status != payment_status:
                payment.status = payment_status
                if payment_status == "approved" and payment.paid_at is None:
                    payment.paid_at = _parse_mp_datetime(
                        details.get("date_approved")
                    ) or datetime.now(timezone.utc)
                session.add(payment)

        # Confirm booking if approved (booking already fetched above).
        if payment_status == "approved" and booking is not None:
            # Solo cambiar estado a confirmed si estaba en pending
            if (
                booking.status == "pending"
                or booking.status == "expired"
                and await _slot_still_free(session, booking)
            ):
                await transition_booking_status(
                    session, booking, "confirmed", actor="webhook_mp"
                )

            # Generar outbox de confirmación si el booking está confirmado y no existe previa
            if booking.status == "confirmed":
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
