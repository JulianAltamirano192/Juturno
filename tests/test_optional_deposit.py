"""
Phase 3, item 11: optional deposit per service.

A service with ``deposit_amount == 0`` is booked without Mercado Pago: the
public booking is confirmed right away, the WhatsApp confirmation is enqueued
in the same transaction and no Payment row is created. It works even in
production for a tenant without MP connected.
"""

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import main as main_module
from app.models import Booking, NotificationOutbox, Payment, Service, Tenant
from app.phone import normalize_whatsapp_phone


@pytest.fixture(autouse=True)
def _production(monkeypatch):
    # Production: a deposit service would need the tenant's own MP account.
    monkeypatch.setattr(main_module.settings, "MP_SANDBOX", False)


@pytest.fixture
def fake_mp(monkeypatch):
    calls: list[dict] = []

    async def fake_create_mp_preference(**kwargs):
        calls.append(kwargs)
        return {
            "preference_id": "pref-test",
            "checkout_url": "https://www.mercadopago.com/init",
        }

    monkeypatch.setattr(
        "app.routers.public.create_mp_preference", fake_create_mp_preference
    )
    return calls


async def _tenant_with_service(db_session, deposit: Decimal | None):
    tenant = Tenant(name="Negocio Demo", slug="negocio-sin-sena", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()
    service = Service(
        tenant_id=tenant.id,
        name="Servicio",
        duration_minutes=30,
        price=Decimal("1500.00"),
        deposit_amount=deposit,
    )
    db_session.add(service)
    await db_session.commit()
    return tenant, service


def _payload(tenant_id: int, service_id: int, key: str = "no-deposit-1") -> dict:
    start = datetime.combine(
        datetime.now(timezone.utc).date() + timedelta(days=1),
        time(10),
        tzinfo=timezone.utc,
    )
    return {
        "tenant_id": tenant_id,
        "service_id": service_id,
        "client_name": "Cliente Prueba",
        "client_phone": "1134567890",
        "start_time": start.isoformat(),
        "idempotency_key": key,
    }


async def _count(db_session, model, *where) -> int:
    stmt = select(func.count()).select_from(model).where(*where)
    return (await db_session.execute(stmt)).scalar_one()


@pytest.mark.asyncio
async def test_no_deposit_booking_is_confirmed_without_mp(client, db_session, fake_mp):
    tenant, service = await _tenant_with_service(db_session, Decimal(0))

    res = await client.post("/public/bookings", json=_payload(tenant.id, service.id))

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["payment_url"] == ""
    assert body["status"] == "confirmed"
    booking = await db_session.get(Booking, body["booking_id"])
    assert booking.status == "confirmed"
    assert booking.status_changed_by == "client"
    assert booking.deposit_at_booking == Decimal(0)
    assert fake_mp == []
    assert await _count(db_session, Payment, Payment.booking_id == booking.id) == 0
    assert (
        await _count(
            db_session,
            NotificationOutbox,
            NotificationOutbox.booking_id == booking.id,
            NotificationOutbox.notification_type == "confirmation",
        )
        == 1
    )


@pytest.mark.asyncio
async def test_no_deposit_retry_is_idempotent(client, db_session, fake_mp):
    tenant, service = await _tenant_with_service(db_session, Decimal(0))
    payload = _payload(tenant.id, service.id)

    first = await client.post("/public/bookings", json=payload)
    retry = await client.post("/public/bookings", json=payload)

    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json()["booking_id"] == first.json()["booking_id"]
    assert retry.json()["payment_url"] == ""
    assert retry.json()["status"] == "confirmed"
    assert (
        await _count(
            db_session,
            NotificationOutbox,
            NotificationOutbox.booking_id == first.json()["booking_id"],
        )
        == 1
    )
    assert fake_mp == []


@pytest.mark.asyncio
@pytest.mark.parametrize("deposit", [None, Decimal("500.00")])
async def test_deposit_service_still_requires_mp_in_production(
    client, db_session, fake_mp, deposit
):
    """Deposit > 0 (or the 30% default) keeps the old behaviour: without MP
    connected in production the booking is refused and nothing is stored."""
    tenant, service = await _tenant_with_service(db_session, deposit)

    res = await client.post("/public/bookings", json=_payload(tenant.id, service.id))

    assert res.status_code == 422
    assert "Mercado Pago" in res.json()["detail"]
    assert fake_mp == []
    assert await _count(db_session, Booking, Booking.tenant_id == tenant.id) == 0


async def _seed_booking(
    db_session,
    tenant,
    service,
    *,
    days,
    status,
    deposit,
    phone=None,
    key="seed",
    actor=None,
):
    start = datetime.combine(
        datetime.now(timezone.utc).date() + timedelta(days=days),
        time(15),
        tzinfo=timezone.utc,
    )
    db_session.add(
        Booking(
            tenant_id=tenant.id,
            service_id=service.id,
            client_name="Cliente Prueba",
            client_phone=phone or normalize_whatsapp_phone("1134567890"),
            start_time=start,
            end_time=start + timedelta(minutes=30),
            price_at_booking=service.price,
            deposit_at_booking=deposit,
            idempotency_key=f"{key}-{days}-{status}",
            status=status,
            status_changed_by=actor,
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_fourth_upcoming_no_deposit_booking_per_phone_is_refused(
    client, db_session, fake_mp
):
    """A phone can hold at most 3 upcoming confirmed no-deposit bookings per
    business: free bookings must not fill the agenda or spam one number."""
    tenant, service = await _tenant_with_service(db_session, Decimal(0))
    for days in (2, 3, 4):
        await _seed_booking(
            db_session, tenant, service, days=days, status="confirmed", deposit=0
        )

    # Same number written differently: the cap uses the normalized phone.
    payload = _payload(tenant.id, service.id)
    payload["client_phone"] = "+54 9 11 3456-7890"
    res = await client.post("/public/bookings", json=payload)

    assert res.status_code == 422
    assert "3 turnos" in res.json()["detail"]
    assert await _count(db_session, Booking, Booking.tenant_id == tenant.id) == 3
    assert await _count(db_session, NotificationOutbox) == 0


@pytest.mark.asyncio
async def test_no_deposit_cap_ignores_past_cancelled_paid_and_other_phones(
    client, db_session, fake_mp
):
    tenant, service = await _tenant_with_service(db_session, Decimal(0))
    await _seed_booking(
        db_session, tenant, service, days=2, status="confirmed", deposit=0
    )
    await _seed_booking(
        db_session, tenant, service, days=3, status="confirmed", deposit=0
    )
    await _seed_booking(
        db_session, tenant, service, days=-1, status="confirmed", deposit=0
    )
    await _seed_booking(
        db_session, tenant, service, days=4, status="cancelled", deposit=0
    )
    await _seed_booking(
        db_session, tenant, service, days=5, status="confirmed", deposit=Decimal(500)
    )
    await _seed_booking(
        db_session,
        tenant,
        service,
        days=6,
        status="confirmed",
        deposit=0,
        phone=normalize_whatsapp_phone("1199998888"),
    )

    other = Tenant(name="Otro Negocio", slug="otro-negocio", timezone="UTC")
    db_session.add(other)
    await db_session.flush()
    other_service = Service(
        tenant_id=other.id,
        name="Servicio",
        duration_minutes=30,
        price=Decimal("1500.00"),
        deposit_amount=Decimal(0),
    )
    db_session.add(other_service)
    await db_session.commit()
    await _seed_booking(
        db_session, other, other_service, days=7, status="confirmed", deposit=0
    )

    res = await client.post("/public/bookings", json=_payload(tenant.id, service.id))

    assert res.status_code == 201, res.text
    assert res.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_no_deposit_cap_ignores_manual_panel_bookings(
    client, db_session, fake_mp
):
    # Los turnos que carga el negocio desde el panel (actor `owner`) tienen su
    # propio tope diario; no deben impedir que el cliente reserve online.
    tenant, service = await _tenant_with_service(db_session, Decimal(0))
    for days in (2, 3, 4):
        await _seed_booking(
            db_session,
            tenant,
            service,
            days=days,
            status="confirmed",
            deposit=0,
            key="panel",
            actor="owner",
        )

    res = await client.post("/public/bookings", json=_payload(tenant.id, service.id))

    assert res.status_code == 201, res.text
    assert res.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_no_deposit_cap_is_not_bypassed_by_panel_like_key(
    client, db_session, fake_mp
):
    # El cliente elige su idempotency_key: una clave `panel-*` no lo saca del tope.
    tenant, service = await _tenant_with_service(db_session, Decimal(0))
    for days in (2, 3, 4):
        await _seed_booking(
            db_session,
            tenant,
            service,
            days=days,
            status="confirmed",
            deposit=0,
            key="panel",
            actor="client",
        )

    payload = _payload(tenant.id, service.id, key="panel-cliente")
    res = await client.post("/public/bookings", json=payload)

    assert res.status_code == 422
