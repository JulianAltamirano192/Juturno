"""add deposit_amount to service and mp preference fields to payment

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-22 04:37:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "service", sa.Column("deposit_amount", sa.Numeric(10, 2), nullable=True)
    )
    op.add_column("payment", sa.Column("mp_preference_id", sa.String(), nullable=True))
    op.add_column("payment", sa.Column("mp_checkout_url", sa.String(), nullable=True))
    op.create_index(
        op.f("ix_payment_mp_preference_id"),
        "payment",
        ["mp_preference_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_payment_mp_preference_id"), table_name="payment")
    op.drop_column("payment", "mp_checkout_url")
    op.drop_column("payment", "mp_preference_id")
    op.drop_column("service", "deposit_amount")
