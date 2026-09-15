"""
Autenticación por tenant vía header X-Tenant-API-Key.

Diseño:
- Hash SHA-256 determinístico (no bcrypt/argon2): las keys son
  secretos de alta entropía generados por CLI (256 bits), no
  contraseñas elegidas por humanos. No hace falta un hash lento, y
  el determinismo permite buscar por índice único.
- Cache en Redis con TTL corto para evitar un round-trip a Postgres
  en cada request. Trade-off: una key recién revocada puede seguir
  aceptándose hasta 60s.
- last_used_at se actualiza con throttle (máximo una vez cada 5 min).
"""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import redis.asyncio as redis
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import ApiKey, Tenant

import asyncio

_redis_clients: dict[int, "redis.Redis"] = {}

_CACHE_TTL_SECONDS = 60
_CACHE_PREFIX = "auth:apikey:"
_LAST_USED_THROTTLE = timedelta(minutes=5)


def _get_redis_client() -> "redis.Redis":
    """
    Devuelve un cliente Redis atado al event loop actual.
    Necesario para tests: pytest-asyncio crea un loop por test, y un
    cliente global queda atado al loop del primer test.
    """
    loop = asyncio.get_running_loop()
    key = id(loop)
    if key not in _redis_clients:
        _redis_clients[key] = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_clients[key]


def hash_api_key(key: str) -> str:
    """Hash determinístico de una API key en texto plano."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def _get_cached_tenant_id(key_hash: str) -> Optional[int]:
    cached = await _get_redis_client().get(_CACHE_PREFIX + key_hash)
    return int(cached) if cached is not None else None


async def _cache_tenant_id(key_hash: str, tenant_id: int) -> None:
    await _get_redis_client().set(
        _CACHE_PREFIX + key_hash, str(tenant_id), ex=_CACHE_TTL_SECONDS
    )


async def get_current_tenant(
    x_tenant_api_key: Annotated[Optional[str], Header(alias="X-Tenant-API-Key")] = None,
    session: AsyncSession = Depends(get_db),
) -> Tenant:
    """
    Dependencia de FastAPI: valida X-Tenant-API-Key y devuelve el
    Tenant dueño de esa key.
    """
    if not x_tenant_api_key:
        raise HTTPException(
            status_code=401,
            detail="Falta el header X-Tenant-API-Key",
        )

    key_hash = hash_api_key(x_tenant_api_key)

    # 1. Intento rápido por cache
    cached_tenant_id = await _get_cached_tenant_id(key_hash)
    if cached_tenant_id is not None:
        tenant = await session.get(Tenant, cached_tenant_id)
        if tenant is not None:
            return tenant

    # 2. Búsqueda real en DB por índice único
    stmt = select(ApiKey).where(ApiKey.key_hash == key_hash)
    api_key = (await session.execute(stmt)).scalar_one_or_none()

    # Mensaje genérico: no distinguimos "no existe" de "revocada"
    if api_key is None or api_key.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    tenant = await session.get(Tenant, api_key.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # 3. Throttle de last_used_at: solo escribimos si pasaron > 5 min
    now = datetime.now(timezone.utc)
    last = api_key.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is None or (now - last) > _LAST_USED_THROTTLE:
        api_key.last_used_at = now
        session.add(api_key)
        await session.commit()

    await _cache_tenant_id(key_hash, tenant.id)

    return tenant
