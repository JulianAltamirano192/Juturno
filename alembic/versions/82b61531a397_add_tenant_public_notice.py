"""add tenant public notice

Revision ID: 82b61531a397
Revises: 4948a27a05b1
Create Date: 2026-10-15 00:47:51.852910

Aviso destacado del negocio en la página pública (ítem 39). Nullable, sin
default ni backfill: los negocios existentes quedan sin aviso.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "82b61531a397"
down_revision: str = "4948a27a05b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant",
        sa.Column("public_notice", sa.String(280), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant", "public_notice")
