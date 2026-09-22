import hmac
import hashlib
import json
from fastapi import APIRouter, Request, Query, HTTPException, Response
from fastapi.responses import PlainTextResponse
import logging
from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/webhooks/whatsapp")
async def verify_webhook(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    """
    Paso 1 del diseño: Handshake con Meta.
    Verifica que el endpoint es tuyo comparando el verify_token.
    """
    if mode and token:
        if mode == "subscribe" and token == settings.META_VERIFY_TOKEN:
            logger.info("Webhook verificado exitosamente por Meta.")
            # Obligatorio: devolver el challenge en texto plano con status 200
            return PlainTextResponse(content=challenge, status_code=200)
        else:
            # Token incorrecto
            raise HTTPException(status_code=403, detail="Forbidden: Token mismatch")

    raise HTTPException(status_code=400, detail="Bad Request")


@router.post("/webhooks/whatsapp")
async def receive_whatsapp_event(request: Request):
    """
    Paso 4 del diseño: Recepción asíncrona de eventos y mensajes.
    Verifica la firma HMAC SHA256 (X-Hub-Signature-256) enviada por Meta.
    Garantiza un response 200 en <5s.
    """
    raw_body = await request.body()

    if settings.META_APP_SECRET:
        signature_header = request.headers.get("x-hub-signature-256")
        if not signature_header or not signature_header.startswith("sha256="):
            raise HTTPException(
                status_code=401, detail="Missing or invalid X-Hub-Signature-256 header"
            )

        given_signature = signature_header.split("sha256=", 1)[1]
        expected_signature = hmac.new(
            settings.META_APP_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(given_signature, expected_signature):
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        # Extraemos el payload completo
        body = json.loads(raw_body) if raw_body else {}

        # Validamos estructura básica
        if body.get("object") != "whatsapp_business_account":
            return Response(status_code=404)

        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # A) Escenario: El usuario nos envió un mensaje de respuesta
                if "messages" in value:
                    for msg in value["messages"]:
                        # Ruta de acceso exacta según el diseño
                        wa_id = msg.get("from")
                        msg_text = msg.get("text", {}).get("body", "")
                        msg_id = msg.get("id")
                        logger.info(f"Nuevo mensaje de {wa_id} ({msg_id}): {msg_text}")

                # B) Escenario: Meta nos avisa del cambio de estado (sent, delivered, read)
                elif "statuses" in value:
                    for status in value["statuses"]:
                        msg_id = status.get("id")
                        estado = status.get("status")  # sent, delivered, read, failed
                        logger.info(f"Status del mensaje {msg_id} cambió a: {estado}")

        # Siempre devolver 200 OK inmediatamente (Paso 2 del diseño)
        return Response(content="EVENT_RECEIVED", status_code=200)

    except Exception as e:
        logger.error(f"Error procesando webhook de Meta: {e}")
        # Incluso si falla el parsing, devolvemos 200 para que Meta no reintente infinitamente un payload que no entendemos.
        return Response(content="ERROR_PARSING_BUT_RECEIVED", status_code=200)
