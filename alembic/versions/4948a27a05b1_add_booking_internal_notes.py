"""add booking internal notes

Revision ID: 4948a27a05b1
Revises: a1c2e3f4b5d6
Create Date: 2026-10-13 22:24:46.343290

Nota interna por turno (solo la ve el dueño en la agenda). Nullable, sin
default ni backfill: los turnos existentes quedan sin nota.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4948a27a05b1"
down_revision: str = "a1c2e3f4b5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking",
        sa.Column("internal_notes", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("booking", "internal_notes")
