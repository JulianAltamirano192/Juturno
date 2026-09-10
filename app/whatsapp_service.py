import asyncio
import logging
from typing import Any, Dict
import httpx

logger = logging.getLogger(__name__)

class WhatsAppClientSingleton:
    """
    Singleton para mantener un único httpx.AsyncClient compartiendo
    el pool de conexiones y evitando overhead de latencia.
    """
    _client: httpx.AsyncClient | None = None

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        if cls._client is None:
            # Configurado con timeouts razonables para no colgar el worker
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
                
                # Si es 429 (Rate limit) o 5xx (Meta caído), lanzamos error para forzar retry
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                    
                # Si llega a un 2xx o un 4xx que no es rate limit (ej. mal formato), retornamos
                return response
                
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                logger.warning(f"Error en Meta API (intento {attempt + 1}/{max_retries}): {str(e)}")
                if attempt == max_retries - 1:
                    raise e # Falla definitiva, el worker deberá reintentar más tarde o marcar como 'failed'
                
                # Backoff exponencial: 1s, 2s, 4s...
                await asyncio.sleep(2 ** attempt)

    async def send_confirmation(self, phone: str, booking_id: int, nombre: str, fecha: str):
        """
        Template Message (Utility). Se dispara asíncronamente desde el worker.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "template",
            "template": {
                "name": "booking_confirmation", # Nombre de tu plantilla aprobada en Meta
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
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "template",
            "template": {
                "name": "booking_reminder", # Nombre de tu plantilla aprobada
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