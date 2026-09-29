"""
Flujo OAuth de Mercado Pago para que cada tenant conecte su propia cuenta
(D-012 / PLAN_MP_POR_TENANT.md).

Circuito:
  1. GET /mp/connect/start (autenticado con X-Tenant-API-Key) → devuelve la
     URL de autorización de MP con un state de un solo uso (anti-CSRF)
     guardado en Redis con TTL.
  2. El dueño del negocio abre esa URL, inicia sesión en MP y autoriza.
  3. MP redirige a GET /mp/connect/callback?code=...&state=...
  4. El callback canjea el code por access_token + refresh_token en
     POST /oauth/token, los cifra (app/mp_crypto) y los guarda en el tenant.

El state de OAuth NO lleva el tenant_id embebido: es un nonce opaco en
Redis (TTL 600s, consumido de un solo uso). Así el payload no se puede
forjar y un code interceptado no es reutilizable.

Seguridad: ningún endpoint de este router expone tokens; la respuesta del
callback trae únicamente datos de estado (conectado, user_id, alias).
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_tenant
from app.config import settings
from app.database import get_db
from app.models import Tenant
from app.mp_crypto import decrypt_token, encrypt_token

router = APIRouter(tags=["mercadopago-oauth"])

_MP_AUTH_URL = "https://auth.mercadopago.com/authorization"
_MP_TOKEN_URL = "https://api.mercadopago.com/oauth/token"
_MP_USERS_ME_URL = "https://api.mercadopago.com/users/me"

# TTL del state de OAuth — tiene que superar el tiempo que el dueño tarda
# en loguearse a MP, y quedarse corto para limitar el replay.
_STATE_TTL_SECONDS = 600
_STATE_PREFIX = "mp_connect_state:"


def _state_key(state: str) -> str:
    return f"{_STATE_PREFIX}{state}"


async def _store_state(state: str, tenant_id: int) -> None:
    """Guarda el state en Redis. Se crea el cliente por llamada para
    evitar clientes atados a otro event loop en tests (patrón health)."""
    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await client.set(_state_key(state), str(tenant_id), ex=_STATE_TTL_SECONDS)
    finally:
        await client.aclose()


async def _consume_state(state: str) -> Optional[int]:
    """Devuelve el tenant_id del state y lo borra (un solo uso), o None."""
    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        raw = await client.getdel(_state_key(state))
    finally:
        await client.aclose()
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _exchange_code_for_tokens(code: str) -> Dict[str, Any]:
    """
    Canjea el authorization_code por tokens en el endpoint de MP.
    MP exige redirect_uri idéntico al de la URL de autorización; en sandbox
    hay que pedir test_token=true para recibir credenciales de prueba.
    Respuesta típica: access_token, refresh_token, expires_in, user_id, scope.
    """
    if (
        not settings.MP_MARKETPLACE_CLIENT_ID
        or not settings.MP_MARKETPLACE_CLIENT_SECRET
    ):
        raise HTTPException(
            status_code=503,
            detail="OAuth de Mercado Pago no está configurado en la plataforma.",
        )

    body = {
        "client_id": settings.MP_MARKETPLACE_CLIENT_ID,
        "client_secret": settings.MP_MARKETPLACE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.MP_MARKETPLACE_REDIRECT_URL,
        # MP decide credenciales de prueba vs producción por este flag
        # (el Authorization code flow de usuarios TEST falla sin él).
        "test_token": "true" if settings.MP_SANDBOX else "false",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_MP_TOKEN_URL, json=body)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504, detail="Timeout canjeando el código con Mercado Pago"
        ) from exc

    if not resp.is_success:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Mercado Pago rechazó el canje del código "
                f"(HTTP {resp.status_code}). El code vence a los ~10 minutos: "
                "reiniciá la conexión desde /mp/connect/start."
            ),
        )
    return resp.json()


async def _fetch_mp_profile(
    access_token: str,
) -> tuple[Optional[str], Optional[str]]:
    """user_id y alias (nickname) de la cuenta recién autorizada.
    No es crítico: si falla, el tenant queda conectado igual."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _MP_USERS_ME_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if not resp.is_success:
            return None, None
        data = resp.json()
        user_id = data.get("id")
        return (str(user_id) if user_id is not None else None, data.get("nickname"))
    except (httpx.HTTPError, ValueError):
        return None, None


# ─────────────────────────────────────────────────────────────────
# Resolución de token de cobro (regla de dinero de D-012)
# ─────────────────────────────────────────────────────────────────

# Mensaje que ve el cliente si el negocio no puede cobrar en producción.
ERR_PAGO_NO_CONFIGURADO = (
    "Este negocio todavía no configuró su cuenta de Mercado Pago. "
    "Avisale al local para que conecte su cuenta y vuelvas a reservar."
)


def resolve_mp_access_token(tenant: Tenant) -> Optional[str]:
    """
    Token OAuth con el que cobra este tenant, según la regla de D-012:

      - Tenant con cuenta conectada → su access_token (descifrado). Fallas
        de cifrado propagan MPTokenCryptoError (clave rota → 502 arriba).
      - Sandbox + sin cuenta → token de la plataforma (plata de prueba).
      - Producción + sin cuenta → None: el negocio aún no puede cobrar;
        el caller responde 422 ERR_PAGO_NO_CONFIGURADO sin crear la reserva.
    """
    if tenant.mp_access_token_enc:
        return decrypt_token(tenant.mp_access_token_enc)
    if settings.MP_SANDBOX:
        return settings.MP_ACCESS_TOKEN
    return None


# ─────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────


@router.get("/mp/connect/start")
async def mp_connect_start(
    current_tenant: Tenant = Depends(get_current_tenant),
):
    """
    Devuelve la URL de autorización de Mercado Pago para el tenant
    autenticado. El dueño del negocio abre esa URL en el navegador,
    autoriza, y MP redirige al callback.
    """
    if not settings.MP_MARKETPLACE_CLIENT_ID:
        raise HTTPException(
            status_code=503,
            detail="OAuth de Mercado Pago no está configurado en la plataforma.",
        )

    state = secrets.token_urlsafe(32)
    await _store_state(state, current_tenant.id)

    params = urlencode(
        {
            "client_id": settings.MP_MARKETPLACE_CLIENT_ID,
            "response_type": "code",
            "platform_id": "mp",
            "state": state,
            "redirect_uri": settings.MP_MARKETPLACE_REDIRECT_URL,
        }
    )
    return {
        "authorization_url": f"{_MP_AUTH_URL}?{params}",
        "expires_in_seconds": _STATE_TTL_SECONDS,
    }


@router.get("/mp/connect/callback")
async def mp_connect_callback(
    session: AsyncSession = Depends(get_db),
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    """
    Destino del redirect_uri registrado en MP Developers. Recibe el code,
    valida el state de un solo uso, canjea por tokens y los persiste
    cifrados en el tenant. Nunca devuelve tokens.
    """
    if error is not None:
        # El dueño canceló o MP rechazó la autorización (p. ej. access_denied)
        raise HTTPException(
            status_code=400,
            detail=f"Autorización de Mercado Pago cancelada o rechazada: {error}.",
        )
    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail="Callback de Mercado Pago incompleto (falta code o state).",
        )

    tenant_id = await _consume_state(state)
    if tenant_id is None:
        raise HTTPException(
            status_code=400,
            detail="state de autorización inválido, vencido o ya utilizado.",
        )

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404, detail="No existe el tenant para este state."
        )

    token_resp = await _exchange_code_for_tokens(code)
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    if not access_token:
        raise HTTPException(
            status_code=502,
            detail="Mercado Pago no devolvió access_token en el canje.",
        )

    mp_user_id, mp_alias = await _fetch_mp_profile(access_token)

    tenant.mp_access_token_enc = encrypt_token(access_token)
    tenant.mp_refresh_token_enc = (
        encrypt_token(refresh_token) if refresh_token else None
    )
    tenant.mp_user_id = str(token_resp.get("user_id") or mp_user_id)
    tenant.mp_alias = mp_alias

    expires_in = token_resp.get("expires_in")
    if expires_in:
        tenant.mp_token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(expires_in)
        )

    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)

    return {
        "connected": True,
        "tenant_id": tenant.id,
        "mp_user_id": tenant.mp_user_id,
        "mp_alias": tenant.mp_alias,
        "mp_token_expires_at": (
            tenant.mp_token_expires_at.isoformat()
            if tenant.mp_token_expires_at
            else None
        ),
    }
