"""add booking actions audit fields

Revision ID: 09efebee7cf3
Revises: f5a6b7c8d9e0
Create Date: 2026-10-02

Agrega campos de auditoría para las acciones sobre turnos (Tarea 8):
- status_changed_at: cuándo se cambió el estado
- status_changed_by: quién lo cambió (owner por ahora)
- cancellation_reason: motivo de cancelación
- no_show_at: cuándo se marcó no-show
- completed_at: cuándo se marcó como completado
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "09efebee7cf3"
down_revision: Union[str, None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "booking",
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "booking",
        sa.Column("status_changed_by", sa.String(), nullable=True),
    )
    op.add_column(
        "booking",
        sa.Column("cancellation_reason", sa.String(), nullable=True),
    )
    op.add_column(
        "booking",
        sa.Column("no_show_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "booking",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("booking", "completed_at")
    op.drop_column("booking", "no_show_at")
    op.drop_column("booking", "cancellation_reason")
    op.drop_column("booking", "status_changed_by")
    op.drop_column("booking", "status_changed_at")
