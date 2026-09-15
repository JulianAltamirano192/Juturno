"""
Valida que los server_default estén aplicados a nivel DB.
Un INSERT directo (sin pasar los campos que SQLModel completa en
Python) debe funcionar y tomar los valores por defecto de Postgres.
"""
import pytest
from sqlalchemy import text

from app.models import Service, Tenant


@pytest.mark.asyncio
async def test_booking_server_defaults(db_session):
    """INSERT directo a booking sin status, reminder_sent ni created_at."""
    tenant = Tenant(name="default-test", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="svc", duration_minutes=60, price=100
    )
    db_session.add(service)
    await db_session.commit()

    # INSERT directo: sin status, reminder_sent, created_at
    await db_session.execute(
        text("""
            INSERT INTO booking (
                tenant_id, service_id, staff_id, client_name, client_phone,
                start_time, end_time, price_at_booking, idempotency_key
            ) VALUES (
                :tid, :sid, NULL, 'Default Test', '5491100000000',
                NOW() + interval '1 day', NOW() + interval '1 day 1 hour',
                100.00, 'default-test-' || extract(epoch from now())
            )
        """),
        {"tid": tenant.id, "sid": service.id},
    )
    await db_session.commit()

    result = await db_session.execute(
        text(
            "SELECT status, reminder_sent, created_at FROM booking "
            "WHERE idempotency_key LIKE 'default-test-%' "
            "ORDER BY id DESC LIMIT 1"
        )
    )
    row = result.fetchone()

    assert row[0] == "pending", "status debe defaultear a 'pending'"
    assert row[1] is False, "reminder_sent debe defaultear a false"
    assert row[2] is not None, "created_at debe defaultear a NOW()"


@pytest.mark.asyncio
async def test_notification_outbox_server_defaults(db_session):
    """INSERT directo a notification_outbox sin status, retry_count, created_at."""
    tenant = Tenant(name="default-outbox-test", timezone="UTC")
    db_session.add(tenant)
    await db_session.flush()

    service = Service(
        tenant_id=tenant.id, name="svc", duration_minutes=60, price=100
    )
    db_session.add(service)
    await db_session.flush()

    await db_session.execute(
        text("""
            INSERT INTO booking (
                tenant_id, service_id, staff_id, client_name, client_phone,
                start_time, end_time, price_at_booking, idempotency_key
            ) VALUES (
                :tid, :sid, NULL, 'Outbox Test', '5491100000001',
                NOW() + interval '2 days', NOW() + interval '2 days 1 hour',
                100.00, 'outbox-default-' || extract(epoch from now())
            )
        """),
        {"tid": tenant.id, "sid": service.id},
    )
    await db_session.commit()

    booking_id = (
        await db_session.execute(
            text(
                "SELECT id FROM booking WHERE idempotency_key LIKE 'outbox-default-%' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
    ).scalar()

    # INSERT directo: sin status, retry_count, created_at
    await db_session.execute(
        text("""
            INSERT INTO notification_outbox (booking_id, notification_type)
            VALUES (:bid, 'confirmation')
        """),
        {"bid": booking_id},
    )
    await db_session.commit()

    result = await db_session.execute(
        text("""
            SELECT status, retry_count, created_at FROM notification_outbox
            WHERE booking_id = :bid
            ORDER BY id DESC LIMIT 1
        """),
        {"bid": booking_id},
    )
    row = result.fetchone()

    assert row[0] == "pending", "status debe defaultear a 'pending'"
    assert row[1] == 0, "retry_count debe defaultear a 0"
    assert row[2] is not None, "created_at debe defaultear a NOW()"


@pytest.mark.asyncio
async def test_payment_events_server_defaults(db_session):
    """INSERT directo a payment_events sin status, received_at."""
    await db_session.execute(
        text("""
            INSERT INTO payment_events (event_id, event_type, payload)
            VALUES (
                'evt-default-' || extract(epoch from now()),
                'payment.updated',
                '{}'::json
            )
        """)
    )
    await db_session.commit()

    result = await db_session.execute(
        text("""
            SELECT status, received_at FROM payment_events
            WHERE event_id LIKE 'evt-default-%'
            ORDER BY received_at DESC LIMIT 1
        """)
    )
    row = result.fetchone()

    assert row[0] == "received", "status debe defaultear a 'received'"
    assert row[1] is not None, "received_at debe defaultear a NOW()"