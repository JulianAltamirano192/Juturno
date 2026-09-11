import hmac
import hashlib
from datetime import datetime
from fastapi import APIRouter, Request, Header, Depends, Response, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from app.models import ProcessedWebhookEvent, Payment, Booking, NotificationOutbox
from app.database import get_db
from app.config import settings

router = APIRouter()

def verify_mp_signature(x_signature: str, x_request_id: str, data_id: str) -> bool:
    """
    Verifica la autenticidad del webhook de MP mediante HMAC SHA256.
    Formato esperado en x-signature: 'ts=12345,v1=hash_hmac'
    """
    if not x_signature or not x_request_id:
        return False
    
    try:
        parts = dict(item.split('=') for item in x_signature.split(','))
        ts = parts.get('ts')
        v1 = parts.get('v1')
        
        if not ts or not v1:
            return False
            
        MP_WEBHOOK_SECRET = settings.MP_SECRET_KEY
        manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
        
        expected_hmac = hmac.new(
            MP_WEBHOOK_SECRET.encode(),
            manifest.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(expected_hmac, v1)
    except Exception:
        return False


@router.post("/webhooks/mercadopago")
async def mercadopago_webhook(
    request: Request,
    x_signature: str = Header(None, alias="x-signature"),
    x_request_id: str = Header(None, alias="x-request-id"),
    session: AsyncSession = Depends(get_db)  # Dependencia de tu sesión async de BD
):
    payload = await request.json()
    
    event_id = str(payload.get("id"))
    event_type = payload.get("action") or payload.get("type")
    data_id = str(payload.get("data", {}).get("id", ""))
    
    # 1. Verificación estricta de Firma (Si no coincide, lanza 401 Unauthorized / 403)
    if not verify_mp_signature(x_signature, x_request_id, data_id):
        raise HTTPException(status_code=401, detail="Firma de Mercado Pago inválida")
    
    # 2. Gate de Idempotencia: Intentar insertar el evento en estado 'received'/'processing'
    # Si el event_id ya existe, salta IntegrityError por la Primary Key UNIQUE.
    try:
        webhook_event = ProcessedWebhookEvent(
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            status="processing"
        )
        session.add(webhook_event)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # Evento duplicado: Devolvemos 200 inmediatamente sin reprocesar nada.
        return Response(content="DUPLICATE_EVENT_IGNORED", status_code=200)

    # 3. Procesamiento transaccional de negocio
    try:
        # Aquí consultarías el estado real a la API de MP usando data_id
        real_payment_status = "approved"  # Simulación de respuesta real de MP
        
        if real_payment_status == "approved":
            stmt = select(Payment).where(Payment.id == int(data_id))
            payment = (await session.execute(stmt)).scalar_one_or_none()
            
            if payment and payment.status != "approved":
                payment.status = "approved"
                
                booking = await session.get(Booking, payment.booking_id)
                if booking and booking.status != "confirmed":
                    booking.status = "confirmed"
                    
                    # Asociamos el booking_id al evento procesado para auditoría
                    webhook_event.booking_id = booking.id
                    
                    # Inserción en el Outbox para desacoplar el envío de WhatsApp
                    outbox_event = NotificationOutbox(
                        booking_id=booking.id,
                        notification_type="confirmation",
                        status="pending"
                    )
                    session.add(outbox_event)

        # 4. Transacción atómica final: Marcar evento como 'processed'
        webhook_event.status = "processed"
        webhook_event.processed_at = datetime.utcnow()
        session.add(webhook_event)
        
        await session.commit()
        return Response(content="EVENT_PROCESSED", status_code=200)
        
    except Exception as e:
        await session.rollback()
        # Si falla a mitad de camino, registramos el fallo para permitir trazabilidad
        webhook_event.status = "failed"
        session.add(webhook_event)
        await session.commit()
        raise e
    
