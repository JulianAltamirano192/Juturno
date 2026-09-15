"""add server defaults and make timestamps tz-aware

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-09-14 00:00:00.000000

Convierte columnas naive a TIMESTAMPTZ e inyecta server_default
para que INSERTs directos por SQL funcionen sin pasar los campos
que SQLModel completa en Python.
"""
from alembic import op
import sqlalchemy as sa

revision = '4d5e6f7a8b9c'
down_revision = '3c4d5e6f7a8b'
branch_labels = None
depends_on = None


def upgrade():
    # ─── booking ─────────────────────────────────────────────
    op.alter_column(
        'booking', 'created_at',
        type_=sa.DateTime(timezone=True),
        existing_type=sa.DateTime(timezone=False),
        existing_nullable=False,
        server_default=sa.text('NOW()'),
        postgresql_using="created_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        'booking', 'status',
        server_default=sa.text("'pending'"),
    )
    op.alter_column(
        'booking', 'reminder_sent',
        server_default=sa.text('false'),
    )

    # ─── notification_outbox ─────────────────────────────────
    op.alter_column(
        'notification_outbox', 'created_at',
        type_=sa.DateTime(timezone=True),
        existing_type=sa.DateTime(timezone=False),
        existing_nullable=False,
        server_default=sa.text('NOW()'),
        postgresql_using="created_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        'notification_outbox', 'status',
        server_default=sa.text("'pending'"),
    )
    op.alter_column(
        'notification_outbox', 'retry_count',
        server_default=sa.text('0'),
    )

    # ─── payment_events ──────────────────────────────────────
    op.alter_column(
        'payment_events', 'received_at',
        type_=sa.DateTime(timezone=True),
        existing_type=sa.DateTime(timezone=False),
        existing_nullable=False,
        server_default=sa.text('NOW()'),
        postgresql_using="received_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        'payment_events', 'processed_at',
        type_=sa.DateTime(timezone=True),
        existing_type=sa.DateTime(timezone=False),
        existing_nullable=True,
        postgresql_using="processed_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        'payment_events', 'status',
        server_default=sa.text("'received'"),
    )


def downgrade():
    # ─── payment_events ──────────────────────────────────────
    op.alter_column('payment_events', 'status', server_default=None)
    op.alter_column(
        'payment_events', 'processed_at',
        type_=sa.DateTime(timezone=False),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        postgresql_using="processed_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        'payment_events', 'received_at',
        type_=sa.DateTime(timezone=False),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=None,
        postgresql_using="received_at AT TIME ZONE 'UTC'",
    )

    # ─── notification_outbox ─────────────────────────────────
    op.alter_column('notification_outbox', 'retry_count', server_default=None)
    op.alter_column('notification_outbox', 'status', server_default=None)
    op.alter_column(
        'notification_outbox', 'created_at',
        type_=sa.DateTime(timezone=False),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=None,
        postgresql_using="created_at AT TIME ZONE 'UTC'",
    )

    # ─── booking ─────────────────────────────────────────────
    op.alter_column('booking', 'reminder_sent', server_default=None)
    op.alter_column('booking', 'status', server_default=None)
    op.alter_column(
        'booking', 'created_at',
        type_=sa.DateTime(timezone=False),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=None,
        postgresql_using="created_at AT TIME ZONE 'UTC'",
    )