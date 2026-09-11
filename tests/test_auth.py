"""
Tests de autenticación por tenant vía header X-Tenant-API-Key.
"""
import pytest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.auth import hash_api_key
from app.models import ApiKey, Tenant


# --- TESTS ---

@pytest.mark.asyncio
async def test_request_without_api_key_returns_401(client):
    """Sin header X-Tenant-API-Key → 401."""
    res = await client.get(
        "/bookings/available-slots",
        params={"tenant_id": 1, "service_id": 1, "day": "2025-06-01"},
    )
    assert res.status_code == 401
    assert "X-Tenant-API-Key" in res.json()["detail"]


@pytest.mark.asyncio
async def test_request_with_invalid_api_key_returns_401(client):
    """Header con key que no existe → 401."""
    res = await client.get(
        "/bookings/available-slots",
        params={"tenant_id": 1, "service_id": 1, "day": "2025-06-01"},
        headers={"X-Tenant-API-Key": "no-existe-esta-key"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid API key"


@pytest.mark.asyncio
async def test_request_with_revoked_api_key_returns_401(client, db_session):
    """Key válida pero revocada → 401."""
    tenant = Tenant(name="revoked-test", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    raw = "revoked-key-test"
    db_session.add(ApiKey(
        tenant_id=tenant.id,
        key_hash=hash_api_key(raw),
        revoked_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    res = await client.get(
        "/bookings/available-slots",
        params={"tenant_id": tenant.id, "service_id": 1, "day": "2025-06-01"},
        headers={"X-Tenant-API-Key": raw},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_request_with_valid_key_passes_auth(client, db_session):
    """Key válida → el endpoint pasa la auth (no 401)."""
    tenant = Tenant(name="valid-key-test", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    raw = "valid-key-test"
    db_session.add(ApiKey(tenant_id=tenant.id, key_hash=hash_api_key(raw)))
    await db_session.commit()

    res = await client.get(
        "/bookings/available-slots",
        params={"tenant_id": tenant.id, "service_id": 1, "day": "2025-06-01"},
        headers={"X-Tenant-API-Key": raw},
    )
    # 404 si el service no existe, 200 si existe.
    # Lo importante: NO debe ser 401.
    assert res.status_code != 401


@pytest.mark.asyncio
async def test_tenant_a_cannot_read_tenant_b_data(client, db_session):
    """
    El fix crítico del hallazgo #13: un tenant no puede leer datos
    de otro aunque conozca el tenant_id.
    """
    tenant_a = Tenant(name="Tenant A", timezone="UTC")
    tenant_b = Tenant(name="Tenant B", timezone="UTC")
    db_session.add_all([tenant_a, tenant_b])
    await db_session.flush()

    raw_b = "key-tenant-b"
    db_session.add(ApiKey(
        tenant_id=tenant_b.id, key_hash=hash_api_key(raw_b),
    ))
    await db_session.commit()

        # Tenant B intenta acceder a datos del tenant A pasando tenant_id=A
    res = await client.get(
        "/bookings/available-slots",
        params={
            "tenant_id": tenant_a.id,
            "service_id": 1,
            "day": "2026-12-01",
        },
        headers={"X-Tenant-API-Key": raw_b},
    )
    # Debe ser 404 (no 403, para no revelar que el tenant existe)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_last_used_at_updates_on_request(client, db_session):
    """Después de un request válido, last_used_at se actualiza."""
    tenant = Tenant(name="last-used-test", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    raw = "last-used-key-test"
    old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    api_key = ApiKey(
        tenant_id=tenant.id,
        key_hash=hash_api_key(raw),
        last_used_at=old_time,
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)

    # Request al endpoint con la key (fecha futura para pasar la validación de day)
    res = await client.get(
        "/bookings/available-slots",
        params={"tenant_id": tenant.id, "service_id": 1, "day": "2026-12-01"},
        headers={"X-Tenant-API-Key": raw},
    )
    assert res.status_code != 401

    # refresh() fuerza un SELECT y re-hidrata el objeto con el valor
    # que dejó el commit de la otra sesión (el override_get_db del endpoint).
    await db_session.refresh(api_key)

    assert api_key.last_used_at is not None
    last = api_key.last_used_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    assert last > old_time