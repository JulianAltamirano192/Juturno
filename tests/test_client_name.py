"""client_name llega al template de WhatsApp y al pagador de MP: largo
acotado, sin links y sin saltos de línea (Meta rechaza parámetros con \\n)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models import Service, Tenant
from app.schemas import clean_client_name
from tests.test_integration import _auth_headers, _create_api_key
from tests.test_public_endpoints import FAKE_MP_RESULT, FUTURE_DAY, MP_PATCH


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("María José O'Brien-López", "María José O'Brien-López"),
        ("  Ana   Pérez  ", "Ana Pérez"),
        ("Ana\nPérez\t", "Ana Pérez"),
        ("Ma. José", "Ma. José"),
        ("Cliente 1", "Cliente 1"),
        ("J.Perez", "J.Perez"),
        ("Sr.Gomez", "Sr.Gomez"),
        ("Ma.Jose", "Ma.Jose"),
        ("Dr.Cohen", "Dr.Cohen"),
    ],
)
def test_clean_client_name_accepts(raw, expected):
    assert clean_client_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "A",
        "x" * 61,
        "Promo https://spam.example",
        "www.spam.ar",
        "Escribime a spam.com.ar",
        "info@spam.com",
        "SPAM.COM",
        "spam\uff0ecom",  # punto de ancho completo
        "spam\u3002com",  # punto ideográfico
        "spam\u200b.com",  # zero-width space
        "Ana\u202eoreP",  # bidi override
        "Ana\x00",
        "\u200b\u200b",
        "12",
        "a" * 100_000,  # corta por largo antes de la regex
    ],
)
def test_clean_client_name_rejects(raw):
    with pytest.raises(ValueError):
        clean_client_name(raw)


async def _service(db_session):
    tenant = Tenant(name="Negocio", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()
    service = Service(tenant_id=tenant.id, name="Corte", duration_minutes=30, price=100)
    db_session.add(service)
    await db_session.commit()
    return tenant, service


def _payload(tenant, service, name, key):
    return {
        "tenant_id": tenant.id,
        "service_id": service.id,
        "client_name": name,
        "client_phone": "5491188776655",
        "start_time": f"{FUTURE_DAY}T15:00:00Z",
        "idempotency_key": key,
    }


@pytest.mark.asyncio
async def test_public_booking_rejects_link_in_name_with_message(client, db_session):
    tenant, service = await _service(db_session)

    with patch(MP_PATCH, new=AsyncMock(return_value=FAKE_MP_RESULT)):
        res = await client.post(
            "/public/bookings",
            json=_payload(tenant, service, "visitá spam.com", "name-1"),
        )

    assert res.status_code == 422
    # La página pública solo muestra detail si es texto.
    assert isinstance(res.json()["detail"], str)


@pytest.mark.asyncio
async def test_public_booking_stores_normalized_name(client, db_session):
    tenant, service = await _service(db_session)

    with patch(MP_PATCH, new=AsyncMock(return_value=FAKE_MP_RESULT)):
        res = await client.post(
            "/public/bookings",
            json=_payload(tenant, service, " Ana\n Pérez ", "name-2"),
        )

    assert res.status_code == 201
    from app.models import Booking

    booking = await db_session.get(Booking, res.json()["booking_id"])
    assert booking.client_name == "Ana Pérez"


@pytest.mark.asyncio
async def test_api_booking_rejects_long_name(client, db_session):
    tenant, service = await _service(db_session)
    key = await _create_api_key(db_session, tenant.id)

    res = await client.post(
        "/bookings",
        json=_payload(tenant, service, "x" * 61, "name-3"),
        headers=_auth_headers(key),
    )

    assert res.status_code == 422
