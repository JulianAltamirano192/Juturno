from fastapi import APIRouter, Request, Query, HTTPException, Response
from fastapi.responses import PlainTextResponse
import logging
from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

# Este token debe coincidir EXACTAMENTE con el que configures en el panel de Meta
META_VERIFY_TOKEN = settings.META_VERIFY_TOKEN


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
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
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
    Garantiza un response 200 en <5s.
    """
    try:
        # Extraemos el payload completo
        body = await request.json()

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

                        # ACA: Idealmente encolarías la respuesta o la enviarías a un LLM.
                        # No procesar pesadamente aquí para asegurar el 200 rápido.

                # B) Escenario: Meta nos avisa del cambio de estado (sent, delivered, read)
                elif "statuses" in value:
                    for status in value["statuses"]:
                        msg_id = status.get("id")
                        estado = status.get("status")  # sent, delivered, read, failed
                        logger.info(f"Status del mensaje {msg_id} cambió a: {estado}")

                        # ACA: Actualizar el estado en tu tabla NotificationOutbox

        # Siempre devolver 200 OK inmediatamente (Paso 2 del diseño)
        return Response(content="EVENT_RECEIVED", status_code=200)

    except Exception as e:
        logger.error(f"Error procesando webhook de Meta: {e}")
        # Incluso si falla el parsing, devolvemos 200 para que Meta no reintente infinitamente un payload que no entendemos.
        return Response(content="ERROR_PARSING_BUT_RECEIVED", status_code=200)
