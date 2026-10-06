"""
Manejo de cookies de sesión firmadas criptográficamente para el panel del negocio.

Diseño:
- Payload: {tenant_id}.{session_version}.{expires_at}
- Firma: HMAC-SHA256 con settings.SECRET_KEY.
- Token: {payload}.{signature}
- session_version permite invalidar todas las sesiones activas de un tenant
  inmediatamente al cambiar credenciales (D-013).
- Comparación de firma en tiempo constante (hmac.compare_digest).
- Cookie HttpOnly, SameSite=Lax, Secure en producción, duración de 14 días.
"""

import hashlib
import hmac
import time
from urllib.parse import urlparse

from fastapi import Response

from app.config import settings

SESSION_COOKIE_NAME = "juturno_session"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 3600  # 14 días


def sign_session_payload(payload: str) -> str:
    """Genera la firma HMAC-SHA256 para el payload de sesión."""
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_session_token(
    tenant_id: int, session_version: int, max_age_seconds: int = SESSION_MAX_AGE_SECONDS
) -> str:
    """Crea un token de sesión firmado con timestamp de expiración."""
    expires_at = int(time.time()) + max_age_seconds
    payload = f"{tenant_id}.{session_version}.{expires_at}"
    signature = sign_session_payload(payload)
    return f"{payload}.{signature}"


def parse_session_token(token: str | None) -> tuple[int, int] | None:
    """
    Verifica y decodifica un token de sesión.
    Devuelve (tenant_id, session_version) si es válido y no expiró, o None.
    """
    if not token or not isinstance(token, str):
        return None

    parts = token.split(".")
    if len(parts) != 4:
        return None

    tenant_id_str, version_str, expires_at_str, signature = parts

    payload = f"{tenant_id_str}.{version_str}.{expires_at_str}"
    expected_signature = sign_session_payload(payload)

    if not hmac.compare_digest(expected_signature, signature):
        return None

    try:
        tenant_id = int(tenant_id_str)
        session_version = int(version_str)
        expires_at = int(expires_at_str)
    except ValueError:
        return None

    now = int(time.time())
    if now > expires_at:
        return None

    return tenant_id, session_version


def set_session_cookie(
    response: Response,
    tenant_id: int,
    session_version: int,
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
) -> None:
    """Configura la cookie de sesión en la respuesta HTTP."""
    token = create_session_token(tenant_id, session_version, max_age_seconds)
    is_prod = settings.ENVIRONMENT.lower() == "production"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=is_prod,
        path="/",
        max_age=max_age_seconds,
    )


def delete_session_cookie(response: Response) -> None:
    """Elimina la cookie de sesión en la respuesta HTTP."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        samesite="lax",
    )


def sanitize_next_url(next_url: str | None) -> str:
    """
    Sanitiza el parámetro `next` para prevenir ataques de open redirect / phishing.
    Solo permite rutas relativas internas que comiencen con '/' y no con '//'.
    """
    if not next_url or not isinstance(next_url, str):
        return "/dashboard"

    clean = next_url.strip()
    if (
        not clean.startswith("/")
        or clean.startswith("//")
        or "\\" in clean
        or "://" in clean
    ):
        return "/dashboard"

    # Verificar que el parseo de URL no contenga esquema ni dominio (host)
    parsed = urlparse(clean)
    if parsed.scheme or parsed.netloc:
        return "/dashboard"

    return clean
