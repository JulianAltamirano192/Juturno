"""move_deposit_at_booking_to_booking

Revision ID: 55526fb8c0f9
Revises: be7d31a422d8
Create Date: 2026-10-06 01:30:24.482268

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "55526fb8c0f9"
down_revision: Union[str, None] = "be7d31a422d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "booking",
        sa.Column("deposit_at_booking", sa.Numeric(10, 2), nullable=True),
    )
    # Backfill existing rows using current service price so old pending bookings
    # don't get rejected if the service price changes after this migration.
    op.execute(
        """
        UPDATE booking b
        SET deposit_at_booking = COALESCE(
            s.deposit_amount,
            ROUND(s.price * 0.30, 2)
        )
        FROM service s
        WHERE b.service_id = s.id
          AND b.deposit_at_booking IS NULL
        """
    )
    op.create_check_constraint(
        "ck_booking_deposit_at_booking_non_negative",
        "booking",
        "deposit_at_booking >= 0",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE booking DROP CONSTRAINT IF EXISTS ck_booking_deposit_at_booking_non_negative"
    )
    op.drop_column("booking", "deposit_at_booking")
