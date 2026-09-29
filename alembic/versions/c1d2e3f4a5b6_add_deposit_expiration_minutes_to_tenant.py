"""add deposit_expiration_minutes to tenant

Revision ID: c1d2e3f4a5b6
Revises: b2c3d4e5f6a7
Create Date: 2026-09-29 03:30:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default llena también a los tenants existentes con el
    # default del producto (15 minutos).
    op.add_column(
        "tenant",
        sa.Column(
            "deposit_expiration_minutes",
            sa.Integer(),
            nullable=True,
            server_default="15",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant", "deposit_expiration_minutes")
