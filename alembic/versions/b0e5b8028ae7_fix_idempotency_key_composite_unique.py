"""fix_idempotency_key_composite_unique

Revision ID: b0e5b8028ae7
Revises: 55526fb8c0f9
Create Date: 2026-10-06 12:57:13.740817

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b0e5b8028ae7"
down_revision: Union[str, None] = "55526fb8c0f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f("ix_booking_idempotency_key"), table_name="booking")
    op.drop_constraint("uq_booking_idempotency_key", "booking", type_="unique")
    op.create_unique_constraint(
        "uq_booking_idempotency_key", "booking", ["tenant_id", "idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_booking_idempotency_key", "booking", type_="unique")
    op.create_unique_constraint(
        "uq_booking_idempotency_key", "booking", ["idempotency_key"]
    )
    op.create_index(
        op.f("ix_booking_idempotency_key"), "booking", ["idempotency_key"], unique=True
    )
