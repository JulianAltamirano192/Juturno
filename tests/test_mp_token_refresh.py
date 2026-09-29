"""
Tarea 6: job periódico que renueva los access_token OAuth de MP de los
tenants cuando están por vencer (REFRESH_AHEAD_DAYS = 30 días).

MP rota ambos tokens en cada renovación: access_token Y refresh_token.
El job persiste ambos cifrados y recalcula mp_token_expires_at.
"""

import pytest
import redis.asyncio as redis_async
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet
from sqlalchemy import text

from app import scheduler
from app.models import Tenant
from app.mp_crypto import decrypt_token, encrypt_token
from app.scheduler import process_mp_token_refresh
from tests.conftest import TestingSessionLocal

TEST_KEY = Fernet.generate_key().decode()

FAKE_REFRESH_RESPONSE = {
    "access_token": "APP_USR-access-RENOVADO",
    "refresh_token": "TG-refresh-RENOVADO",
    "expires_in": 15552000,
    "user_id": 1234567890,
}


@pytest.fixture(autouse=True)
def _fresh_redis_client(monkeypatch):
    """redis_client de scheduler es global atado al primer loop (ver
    test_deposit_expiration); lo reparchamos con uno nuevo por test."""
    monkeypatch.setattr(
        scheduler,
        "redis_client",
        redis_async.from_url(scheduler.settings.REDIS_URL, decode_responses=True),
    )
    monkeypatch.setattr(scheduler.settings, "MP_TOKEN_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setattr(scheduler.settings, "MP_MARKETPLACE_CLIENT_ID", "5555555555")
    monkeypatch.setattr(
        scheduler.settings, "MP_MARKETPLACE_CLIENT_SECRET", "client-secret-test"
    )


async def _tenant_with_mp(
    db_session, name: str, expires_in_days: float, access_token: str
):
    tenant = Tenant(
        name=name,
        slug=name.lower().replace(" ", "-"),
        timezone="UTC",
        mp_user_id=f"user-{name}",
        mp_access_token_enc=encrypt_token(access_token),
        mp_refresh_token_enc=encrypt_token(f"TG-refresh-{name}"),
        mp_token_expires_at=datetime.now(timezone.utc)
        + timedelta(days=expires_in_days),
    )
    db_session.add(tenant)
    await db_session.commit()
    return tenant


def _patch_mp_refresh(monkeypatch, ok: bool = True):
    """Intercepta el POST a /oauth/token y responde rotación o falla."""
    calls = []

    class FakeResponse:
        @property
        def is_success(self):
            return ok

        def json(self):
            return FAKE_REFRESH_RESPONSE if ok else {"error": "invalid_grant"}

    class FakeClient:
        async def post(self, url, **kwargs):
            calls.append(kwargs["json"])
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        scheduler.refresh_tenant_mp_token.__globals__["httpx"],
        "AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    return calls


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_renews_token_that_is_about_to_expire(db_session, monkeypatch):
    """Token a 5 días de vencer → se renueva, y el refresh_token rota."""
    tenant = await _tenant_with_mp(
        db_session, "por-vencer", expires_in_days=5, access_token="viejo"
    )
    calls = _patch_mp_refresh(monkeypatch, ok=True)

    await process_mp_token_refresh(TestingSessionLocal)

    assert len(calls) == 1
    assert calls[0]["grant_type"] == "refresh_token"

    row = (
        await db_session.execute(
            text(
                "SELECT mp_access_token_enc, mp_refresh_token_enc, "
                "mp_token_expires_at FROM tenant WHERE id = :tid"
            ).bindparams(tid=tenant.id)
        )
    ).one()
    assert decrypt_token(row.mp_access_token_enc) == "APP_USR-access-RENOVADO"
    # MP rotó el refresh token también
    assert decrypt_token(row.mp_refresh_token_enc) == "TG-refresh-RENOVADO"
    assert row.mp_token_expires_at > datetime.now(timezone.utc) + timedelta(days=150)


@pytest.mark.asyncio
async def test_skips_token_with_plenty_of_life(db_session, monkeypatch):
    """Token a 150 días de vencer → no se toca."""
    tenant = await _tenant_with_mp(
        db_session, "fresco", expires_in_days=150, access_token="viejo"
    )
    calls = _patch_mp_refresh(monkeypatch, ok=True)

    await process_mp_token_refresh(TestingSessionLocal)

    assert calls == []
    could_read = decrypt_token(tenant.mp_access_token_enc)
    assert could_read == "viejo"


@pytest.mark.asyncio
async def test_skips_tenants_without_mp_connection(db_session, monkeypatch):
    """Tenant sin MP conectado no merece ni consulta."""
    tenant = Tenant(name="Sin MP", slug="sin-mp-refresh", timezone="UTC")
    db_session.add(tenant)
    await db_session.commit()
    calls = _patch_mp_refresh(monkeypatch, ok=True)

    await process_mp_token_refresh(TestingSessionLocal)

    assert calls == []


@pytest.mark.asyncio
async def test_mp_rejection_is_logged_not_fatal(db_session, monkeypatch):
    """MP rechaza el refresh (revocado) → False, tenant intacto, otro
    tenant por vencer SE RENUEVA igual en el mismo ciclo."""
    broken = await _tenant_with_mp(
        db_session, "revocado", expires_in_days=5, access_token="roto"
    )
    fine = await _tenant_with_mp(
        db_session, "bien", expires_in_days=5, access_token="bien"
    )

    original_post_calls = []

    class MixedClient:
        async def post(self, url, **kwargs):
            rt = kwargs["json"]["refresh_token"]
            original_post_calls.append(rt)
            if rt == "TG-refresh-revocado":
                return type("R", (), {"is_success": False, "json": lambda s: {}})()
            return type(
                "R",
                (),
                {
                    "is_success": True,
                    "json": lambda s: {
                        "access_token": "APP_USR-access-RENOVADO",
                        "refresh_token": "TG-refresh-OK",
                        "expires_in": 15552000,
                    },
                },
            )()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        scheduler.refresh_tenant_mp_token.__globals__["httpx"],
        "AsyncClient",
        lambda **kwargs: MixedClient(),
    )

    await process_mp_token_refresh(TestingSessionLocal)

    # Ambos fueron intentados (2 llamadas)
    assert len(original_post_calls) == 2
    # El roto quedó igual
    assert decrypt_token(broken.mp_access_token_enc) == "roto"
    # El sano se renovó
    await db_session.refresh(fine)
    # Nota: el refresh del otro tenant pisó la sesión con su commit.
    row = (
        await db_session.execute(
            text("SELECT mp_access_token_enc FROM tenant WHERE id = :tid").bindparams(
                tid=fine.id
            )
        )
    ).one()
    assert decrypt_token(row.mp_access_token_enc) == "APP_USR-access-RENOVADO"
