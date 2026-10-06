"""add_check_deposit_amount_non_negative

Revision ID: 346cd2b92a65
Revises: 09efebee7cf3
Create Date: 2026-10-06 00:53:04.284452

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "346cd2b92a65"
down_revision: Union[str, None] = "09efebee7cf3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_service_deposit_amount_non_negative",
        "service",
        "deposit_amount IS NULL OR deposit_amount >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_service_deposit_amount_non_negative",
        "service",
        type_="check",
    )
