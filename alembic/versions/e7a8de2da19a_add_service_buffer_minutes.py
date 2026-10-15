"""add service buffer_minutes

Tiempo libre después de cada turno, por servicio (Fase 5, ítem 32). Opt-in:
los servicios existentes quedan en 0 (sin tiempo libre), igual que hoy.

Revision ID: e7a8de2da19a
Revises: f4e2a9d38afb
Create Date: 2026-10-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a8de2da19a"
down_revision: Union[str, None] = "f4e2a9d38afb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "service",
        sa.Column(
            "buffer_minutes", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_service_buffer_minutes",
        "service",
        "buffer_minutes IN (0, 5, 10, 15, 20, 30, 45, 60)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_service_buffer_minutes", "service", type_="check")
    op.drop_column("service", "buffer_minutes")
