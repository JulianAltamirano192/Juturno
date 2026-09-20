"""add ondelete cascade to booking fks

Revision ID: 87668cdc0a01
Revises: 4d5e6f7a8b9c
Create Date: 2026-09-20

Cambia las FKs de notification_outbox y payment_events hacia booking
para que sean ON DELETE CASCADE. Sin esto, borrar un booking con
notificaciones asociadas falla con FK violation.
"""

from alembic import op

revision = "87668cdc0a01"
down_revision = "4d5e6f7a8b9c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # notification_outbox
    op.drop_constraint(
        "notification_outbox_booking_id_fkey",
        "notification_outbox",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "notification_outbox_booking_id_fkey",
        "notification_outbox",
        "booking",
        ["booking_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # payment_events
    op.drop_constraint(
        "payment_events_booking_id_fkey",
        "payment_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "payment_events_booking_id_fkey",
        "payment_events",
        "booking",
        ["booking_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # payment_events
    op.drop_constraint(
        "payment_events_booking_id_fkey",
        "payment_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "payment_events_booking_id_fkey",
        "payment_events",
        "booking",
        ["booking_id"],
        ["id"],
    )

    # notification_outbox
    op.drop_constraint(
        "notification_outbox_booking_id_fkey",
        "notification_outbox",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "notification_outbox_booking_id_fkey",
        "notification_outbox",
        "booking",
        ["booking_id"],
        ["id"],
    )
