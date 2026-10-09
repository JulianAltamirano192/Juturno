"""
Flujo OAuth de Mercado Pago para que cada tenant conecte su propia cuenta
(D-012 / PLAN_MP_POR_TENANT.md).

Circuito:
  1. POST /panel/mp/connect/start (sesión del panel + CSRF) redirige a la
     URL de autorización de MP con un state de un solo uso guardado en Redis
     con TTL, y deja el mismo state en una cookie HttpOnly del navegador.
  2. El dueño del negocio inicia sesión en MP y autoriza.
  3. MP redirige a GET /mp/connect/callback?code=...&state=...
  4. El callback exige que la cookie coincida con el state, canjea el code
     por access_token + refresh_token en POST /oauth/token, los cifra
     (app/mp_crypto), los guarda en el tenant y vuelve al panel.

El state de OAuth NO lleva el tenant_id embebido: es un nonce opaco en
Redis (TTL 600s, consumido de un solo uso). Así el payload no se puede
forjar y un code interceptado no es reutilizable. La cookie ata el state al
navegador que inició el flujo: un link de autorización compartido no sirve
para vincular la cuenta MP de otra persona. Por eso no hay inicio por API
key: una URL suelta no tiene navegador al cual atarse.

Seguridad: ningún endpoint de este router expone tokens; el callback solo
redirige al panel con un flag de resultado.
"""

import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_tenant
from app.config import settings
from app.database import get_db
from app.models import Booking, Payment, Tenant
from app.mp_crypto import decrypt_token, encrypt_token

router = APIRouter(tags=["mercadopago-oauth"])

_MP_AUTH_URL = "https://auth.mercadopago.com/authorization"
_MP_TOKEN_URL = "https://api.mercadopago.com/oauth/token"
_MP_USERS_ME_URL = "https://api.mercadopago.com/users/me"

# TTL del state de OAuth — tiene que superar el tiempo que el dueño tarda
# en loguearse a MP, y quedarse corto para limitar el replay.
_STATE_TTL_SECONDS = 600
_STATE_PREFIX = "mp_connect_state:"
STATE_COOKIE_NAME = "mp_oauth_state"
_STATE_COOKIE_PATH = "/mp/connect/callback"


def _state_key(state: str) -> str:
    return f"{_STATE_PREFIX}{state}"


async def _store_state(state: str, tenant_id: int) -> None:
    """Guarda el state en Redis. Se crea el cliente por llamada para
    evitar clientes atados a otro event loop en tests (patrón health).
    La URL de vuelta la arma el callback en el servidor, nunca viaja en
    el state."""
    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await client.set(_state_key(state), str(tenant_id), ex=_STATE_TTL_SECONDS)
    finally:
        await client.aclose()


async def _consume_state(state: str) -> int | None:
    """Devuelve el tenant_id del state y lo borra (un solo uso)."""
    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        raw = await client.getdel(_state_key(state))
    finally:
        await client.aclose()
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _state_cookie_domain() -> str | None:
    """El panel (PUBLIC_BASE_URL) y el callback (MP_MARKETPLACE_REDIRECT_URL)
    pueden vivir en hosts distintos: juturno.com y api.juturno.com. Si el
    callback es un subdominio del panel, la cookie se emite para el dominio
    del panel así llega a ambos; si no, queda atada al host (local)."""
    panel_host = urlparse(settings.PUBLIC_BASE_URL).hostname or ""
    callback_host = urlparse(settings.MP_MARKETPLACE_REDIRECT_URL).hostname or ""
    if panel_host and callback_host.endswith(f".{panel_host}"):
        return panel_host
    return None


def _state_matches_cookie(request: Request, state: str) -> bool:
    cookie = request.cookies.get(STATE_COOKIE_NAME)
    if not cookie:
        return False
    return hmac.compare_digest(cookie.encode(), state.encode())


def _panel_settings_url(mp_flag: str) -> str:
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}/panel/settings?mp={mp_flag}"


def _finish_flow(mp_flag: str) -> RedirectResponse:
    """Vuelve al panel y borra la cookie del state (ya consumido)."""
    response = RedirectResponse(_panel_settings_url(mp_flag), status_code=302)
    response.delete_cookie(
        STATE_COOKIE_NAME, path=_STATE_COOKIE_PATH, domain=_state_cookie_domain()
    )
    return response


async def _exchange_code_for_tokens(code: str) -> dict[str, Any]:
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
                "reiniciá la conexión desde Configuración del panel."
            ),
        )
    return resp.json()


async def _fetch_mp_profile(
    access_token: str,
) -> tuple[str | None, str | None]:
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


def resolve_mp_access_token(tenant: Tenant) -> str | None:
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
# Renovación de tokens OAuth (Tarea 6)
# ─────────────────────────────────────────────────────────────────

# MP renueva con anticipación: si quedan menos de estos días para el
# vencimiento (~180 días de vida), el job pide un access_token nuevo.
REFRESH_AHEAD_DAYS = 30


async def refresh_tenant_mp_token(session: AsyncSession, tenant: Tenant) -> bool:
    """
    Renueva el access_token OAuth del tenant con su refresh_token.
    Devuelve True si el tenant quedó con token nuevo persistido.

    MP ROTA el par completo: access_token Y refresh_token — ambos se
    reemplazan cifrados y se recalcula mp_token_expires_at.

    Fallas NO lanzan excepción: devuelve False (MP rechazó el refresh,
    p.ej. el dueño revocó el acceso). La reconexión es manual.
    """
    if not tenant.mp_refresh_token_enc:
        return False

    body = {
        "client_id": settings.MP_MARKETPLACE_CLIENT_ID,
        "client_secret": settings.MP_MARKETPLACE_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": decrypt_token(tenant.mp_refresh_token_enc),
        "test_token": "true" if settings.MP_SANDBOX else "false",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_MP_TOKEN_URL, json=body)
    except httpx.TimeoutException:
        return False

    if not resp.is_success:
        return False

    data = resp.json()
    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token")
    if not new_access:
        return False

    tenant.mp_access_token_enc = encrypt_token(new_access)
    if new_refresh:
        # El refresh token viejo queda inválido ante MP una vez usado
        tenant.mp_refresh_token_enc = encrypt_token(new_refresh)

    expires_in = data.get("expires_in")
    if expires_in:
        tenant.mp_token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(expires_in)
        )

    session.add(tenant)
    await session.commit()
    return True


# ─────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────


async def mp_authorization_redirect(tenant_id: int) -> RedirectResponse:
    """
    Redirige a la autorización de MP: registra el state en Redis y lo deja
    en una cookie HttpOnly del navegador que inicia el flujo. El callback
    solo acepta el state si vuelve con esa cookie.
    """
    if not settings.MP_MARKETPLACE_CLIENT_ID:
        raise HTTPException(
            status_code=503,
            detail="OAuth de Mercado Pago no está configurado en la plataforma.",
        )
    state = secrets.token_urlsafe(32)
    await _store_state(state, tenant_id)
    params = urlencode(
        {
            "client_id": settings.MP_MARKETPLACE_CLIENT_ID,
            "response_type": "code",
            "platform_id": "mp",
            "state": state,
            "redirect_uri": settings.MP_MARKETPLACE_REDIRECT_URL,
        }
    )
    response = RedirectResponse(url=f"{_MP_AUTH_URL}?{params}", status_code=302)
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=state,
        max_age=_STATE_TTL_SECONDS,
        path=_STATE_COOKIE_PATH,
        domain=_state_cookie_domain(),
        httponly=True,
        # Lax: MP vuelve con una navegación GET de nivel superior.
        samesite="lax",
        secure=settings.is_production,
    )
    return response


@router.get("/mp/connect/callback")
async def mp_connect_callback(
    request: Request,
    session: AsyncSession = Depends(get_db),
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """
    Destino del redirect_uri registrado en MP Developers. Recibe el code,
    valida el state de un solo uso contra la cookie del navegador, canjea
    por tokens, los persiste cifrados en el tenant y vuelve al panel.
    Nunca devuelve tokens.
    """
    if error is not None:
        # El dueño canceló o MP rechazó la autorización (p. ej. access_denied).
        # Solo el navegador que inició el flujo puede descartar su state.
        if state and _state_matches_cookie(request, state):
            await _consume_state(state)
            return _finish_flow("error")
        return RedirectResponse(_panel_settings_url("error"), status_code=302)
    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail="Callback de Mercado Pago incompleto (falta code o state).",
        )
    if not _state_matches_cookie(request, state):
        # El flujo no se inició en este navegador (un link de autorización
        # compartido, o en el celular MP volvió por otra app/navegador):
        # no se consume ni se vincula nada.
        return RedirectResponse(_panel_settings_url("other_browser"), status_code=302)

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
    mp_user_id = token_resp.get("user_id") or mp_user_id
    if not mp_user_id:
        # Sin la cuenta no hay unicidad ni guard de collector_id posibles.
        return _finish_flow("error")

    tenant.mp_access_token_enc = encrypt_token(access_token)
    tenant.mp_refresh_token_enc = (
        encrypt_token(refresh_token) if refresh_token else None
    )
    tenant.mp_user_id = str(mp_user_id)
    tenant.mp_alias = mp_alias

    expires_in = token_resp.get("expires_in")
    if expires_in:
        tenant.mp_token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(expires_in)
        )

    session.add(tenant)
    try:
        await session.commit()
    except IntegrityError:
        # uq_tenant_mp_user_id: la cuenta de MP ya está vinculada a otro
        # negocio. El rollback descarta los tokens; el otro tenant no cambia.
        await session.rollback()
        return _finish_flow("account_in_use")
    await session.refresh(tenant)

    return _finish_flow("connected")


# ─────────────────────────────────────────────────────────────────
# Estado y desconexión (Tarea 7)
# ─────────────────────────────────────────────────────────────────


@router.get("/tenants/me/mp")
async def get_my_mp_connection(
    current_tenant: Tenant = Depends(get_current_tenant),
):
    """
    Estado de la cuenta MP del tenant autenticado. Solo expone datos de
    lectura — nunca access_token ni refresh_token, ni siquiera cifrados.
    """
    connected = current_tenant.mp_access_token_enc is not None
    return {
        "connected": connected,
        "mp_user_id": current_tenant.mp_user_id if connected else None,
        "mp_alias": current_tenant.mp_alias if connected else None,
        "mp_token_expires_at": (
            current_tenant.mp_token_expires_at.isoformat()
            if connected and current_tenant.mp_token_expires_at
            else None
        ),
    }


# Margen para webhooks demorados (MP reintenta durante horas) de pagos
# hechos justo antes del vencimiento del link.
_PAYMENT_WEBHOOK_GRACE = timedelta(hours=2)
# Pagos de MP que todavía esperan un webhook con su estado final.
_IN_FLIGHT_PAYMENT_STATUSES = ("pending", "in_process", "authorized")


async def has_payable_mp_payment(session: AsyncSession, tenant_id: int) -> bool:
    """True si al tenant le puede llegar un pago de MP que el webhook tenga
    que verificar con su token: un link de pago vigente sin pagar, o un pago
    de MP sin estado final (p. ej. tarjeta en revisión)."""
    link_cutoff = datetime.now(timezone.utc) - _PAYMENT_WEBHOOK_GRACE
    stmt = (
        select(Payment)
        .join(Booking, Booking.id == Payment.booking_id)
        .where(
            Booking.tenant_id == tenant_id,
            or_(
                # La fila de la preferencia queda "pending" aunque se pague
                # (el webhook crea otra): cuenta solo si el turno todavía
                # puede confirmarse con un pago.
                and_(
                    Payment.mp_preference_id.is_not(None),
                    Booking.status.in_(("pending", "expired")),
                    Payment.mp_expires_at > link_cutoff,
                ),
                and_(
                    Payment.mp_payment_id.is_not(None),
                    Payment.status.in_(_IN_FLIGHT_PAYMENT_STATUSES),
                ),
            ),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


def clear_mp_connection(tenant: Tenant) -> None:
    """Borra las credenciales locales; no revoca la autorización en MP."""
    tenant.mp_user_id = None
    tenant.mp_alias = None
    tenant.mp_public_key = None
    tenant.mp_access_token_enc = None
    tenant.mp_refresh_token_enc = None
    tenant.mp_token_expires_at = None


@router.delete("/tenants/me/mp")
async def disconnect_my_mp_connection(
    current_tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """
    Desconecta la cuenta MP del tenant: borra credenciales y metadata.
    Es idempotente (borrar dos veces no falla). 409 mientras pueda llegar
    un pago que el webhook necesite verificar con el token.

    Tras desconectarse, la regla D-012 le bloquea el cobro en producción
    hasta que vuelva a conectarse por OAuth.
    """
    if await has_payable_mp_payment(session, current_tenant.id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Hay señas de Mercado Pago que todavía pueden pagarse; "
                "reintentá cuando venzan."
            ),
        )
    clear_mp_connection(current_tenant)
    session.add(current_tenant)
    await session.commit()

    return {"disconnected": True}
