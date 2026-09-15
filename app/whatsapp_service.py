import asyncio
import logging
from typing import Any, Dict
import httpx

logger = logging.getLogger(__name__)


def normalize_phone_for_meta(phone: str) -> str:
    """
    Meta rechaza números argentinos con el '9' después del '54' cuando
    el número fue verificado en su formato sin el 9.
    Si el número empieza con '549', se remueve el '9'.
    Otros países pasan sin cambios.
    """
    if phone.startswith("549"):
        return "54" + phone[3:]
    return phone


class WhatsAppClientSingleton:
    """
    Singleton para mantener un único httpx.AsyncClient compartiendo
    el pool de conexiones y evitando overhead de latencia.
    """
    _client: httpx.AsyncClient | None = None

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        if cls._client is None:
            cls._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return cls._client


class WhatsAppService:
    def __init__(self, phone_number_id: str, access_token: str):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.base_url = f"https://graph.facebook.com/v19.0/{self.phone_number_id}/messages"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

    async def _send_request_with_retry(self, payload: Dict[str, Any], max_retries: int = 3) -> httpx.Response:
        """
        Envía la petición a Meta manejando Timeouts, 429 (Rate Limit) y 5xx.
        Implementa backoff exponencial (ej: 1s, 2s, 4s).
        """
        client = WhatsAppClientSingleton.get_client()

        for attempt in range(max_retries):
            try:
                response = await client.post(self.base_url, headers=self.headers, json=payload)

                # 429 (rate limit) o 5xx (Meta caído) → forzar retry
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()

                # 4xx que no es rate limit: loggear el body completo antes de retornar
                if 400 <= response.status_code < 500:
                    try:
                        error_body = response.json()
                    except Exception:
                        error_body = response.text
                    logger.error(
                        "Meta API rechazó la request (status=%s): %s",
                        response.status_code,
                        error_body,
                    )

                return response

            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                logger.warning(f"Error en Meta API (intento {attempt + 1}/{max_retries}): {str(e)}")
                if attempt == max_retries - 1:
                    raise e

                await asyncio.sleep(2 ** attempt)

    async def send_confirmation(self, phone: str, booking_id: int, nombre: str, fecha: str):
        """
        Template Message (Utility). Se dispara asíncronamente desde el worker.
        Requiere que la plantilla 'booking_confirmation' esté aprobada en Meta.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone_for_meta(phone),
            "type": "template",
            "template": {
                "name": "booking_confirmation",
                "language": {"code": "es"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": nombre},
                            {"type": "text", "text": fecha},
                            {"type": "text", "text": str(booking_id)}
                        ]
                    }
                ]
            }
        }
        return await self._send_request_with_retry(payload)

    async def send_reminder(self, phone: str, booking_id: int, nombre: str, fecha: str):
        """
        Template Message (Utility) para recordar 24hs antes.
        Requiere que la plantilla 'booking_reminder' esté aprobada en Meta.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone_for_meta(phone),
            "type": "template",
            "template": {
                "name": "booking_reminder",
                "language": {"code": "es"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": nombre},
                            {"type": "text", "text": fecha}
                        ]
                    }
                ]
            }
        }
        return await self._send_request_with_retry(payload)