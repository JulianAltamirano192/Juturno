"""
Protección CSRF basada en el patrón Double-Submit Cookie.

Diseño:
- En peticiones GET que renderizan formularios, se genera un token pseudoaleatorio
  criptográficamente seguro (secrets.token_hex(32)).
- Se envía el token en una cookie 'csrf_token' (no HttpOnly para que pueda leerse si se necesita,
  SameSite=Lax, Path=/) y también se inyecta en el campo oculto del formulario HTML.
- Al recibir el POST, se compara el token del formulario contra la cookie mediante
  hmac.compare_digest a tiempo constante.
"""

import hmac
import secrets

from fastapi import Response
from app.config import settings

CSRF_COOKIE_NAME = "csrf_token"


def generate_csrf_token() -> str:
    """Genera un token CSRF criptográficamente seguro."""
    return secrets.token_hex(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    """Configura la cookie CSRF en la respuesta HTTP."""
    is_prod = settings.ENVIRONMENT.lower() == "production"
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,  # Double-submit cookie requiere que no sea HttpOnly
        samesite="lax",
        secure=is_prod,
        path="/",
        max_age=7200,  # 2 horas
    )


def validate_csrf_double_submit(
    form_token: str | None, cookie_token: str | None
) -> bool:
    """
    Valida que el token enviado en el formulario coincida exactamente con
    la cookie CSRF recibida en el request mediante comparación a tiempo constante.
    """
    if not form_token or not cookie_token:
        return False
    if not isinstance(form_token, str) or not isinstance(cookie_token, str):
        return False
    return hmac.compare_digest(form_token, cookie_token)


async def validate_csrf(request) -> bool:
    """
    Valida CSRF para peticiones del panel usando el patrón double-submit cookie.
    Lee el campo 'csrf_token' del form body y lo compara contra la cookie.
    Lanza HTTPException 403 si falta o no coincide.
    """
    from fastapi import HTTPException, status

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="CSRF cookie missing"
        )

    form_data = await request.form()
    form_token = form_data.get(CSRF_COOKIE_NAME)

    if not validate_csrf_double_submit(form_token, cookie_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token invalid"
        )

    return True
