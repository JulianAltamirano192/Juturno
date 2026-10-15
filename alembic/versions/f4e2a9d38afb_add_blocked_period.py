"""add blocked_period

Días bloqueados (feriados, vacaciones) del negocio o de un profesional
(Fase 5, ítem 31). Tabla nueva: no toca datos existentes.

Revision ID: f4e2a9d38afb
Revises: 9a9d04b9ff5e
Create Date: 2026-10-15 19:07:47.817808

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4e2a9d38afb"
down_revision: Union[str, None] = "9a9d04b9ff5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blocked_period",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("staff_id", sa.Integer(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
        sa.CheckConstraint(
            "end_date >= start_date", name="ck_blocked_period_date_order"
        ),
        sa.ForeignKeyConstraint(["staff_id"], ["staff.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_blocked_period_tenant_id"), "blocked_period", ["tenant_id"]
    )
    op.create_index(op.f("ix_blocked_period_staff_id"), "blocked_period", ["staff_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_blocked_period_staff_id"), table_name="blocked_period")
    op.drop_index(op.f("ix_blocked_period_tenant_id"), table_name="blocked_period")
    op.drop_table("blocked_period")
