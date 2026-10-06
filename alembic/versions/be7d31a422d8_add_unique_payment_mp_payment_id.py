"""add_unique_payment_mp_payment_id

Revision ID: be7d31a422d8
Revises: 346cd2b92a65
Create Date: 2026-10-06 01:01:42.740319

"""

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "be7d31a422d8"
down_revision: str = "346cd2b92a65"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_payment_mp_payment_id",
        "payment",
        ["mp_payment_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_payment_mp_payment_id",
        "payment",
        type_="unique",
    )
