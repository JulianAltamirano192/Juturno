"""add booking extra service

Varios servicios en un mismo turno (Fase 5, ítem 43): tabla nueva con los
servicios que se suman al principal (`booking.service_id`). Los turnos
existentes no tienen filas acá y no cambian. El downgrade borra los extra
cargados desde entonces (los turnos quedan con su servicio principal).

Revision ID: e2ca7d155205
Revises: 46fd57b22b48
Create Date: 2026-10-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2ca7d155205"
down_revision: Union[str, None] = "46fd57b22b48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "booking_extra_service",
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 2", name="ck_booking_extra_service_position"
        ),
        sa.ForeignKeyConstraint(["booking_id"], ["booking.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["service.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("booking_id", "position"),
        sa.UniqueConstraint(
            "booking_id", "service_id", name="uq_booking_extra_service_service"
        ),
    )
    op.create_index(
        op.f("ix_booking_extra_service_service_id"),
        "booking_extra_service",
        ["service_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_booking_extra_service_service_id"),
        table_name="booking_extra_service",
    )
    op.drop_table("booking_extra_service")
