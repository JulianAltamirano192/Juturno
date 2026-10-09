"""WhatsAppService error logging must not leak client data."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.whatsapp_service import WhatsAppService


@pytest.mark.asyncio
async def test_meta_4xx_logs_error_code_without_body(caplog):
    """Meta error bodies can echo the recipient phone; the log (which also
    reaches Sentry as an ERROR event) keeps only the identifiers."""
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {
        "error": {
            "message": "Recipient 5491112345678 not in allowed list",
            "type": "OAuthException",
            "code": 131030,
            "error_data": {"details": "phone 5491112345678"},
            "fbtrace_id": "TRACE123",
        }
    }
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    service = WhatsAppService(phone_number_id="123", access_token="token")
    with (
        patch(
            "app.whatsapp_service.WhatsAppClientSingleton.get_client",
            return_value=client,
        ),
        caplog.at_level(logging.ERROR, logger="app.whatsapp_service"),
    ):
        await service._send_request_with_retry({"to": "5491112345678"})

    assert "131030" in caplog.text
    assert "TRACE123" in caplog.text
    assert "5491112345678" not in caplog.text
