"""add tenant business category

Revision ID: dcb3cce62e10
Revises: 82b61531a397
Create Date: 2026-10-15 01:28:32.354296

Rubro del negocio (ítem 14): clave de la lista genérica y texto libre para
"Otro". Nullable, sin default ni backfill: los negocios existentes quedan sin
rubro hasta que lo elijan en Ajustes.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dcb3cce62e10"
down_revision: str = "82b61531a397"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant",
        sa.Column("business_category", sa.String(20), nullable=True),
    )
    op.add_column(
        "tenant",
        sa.Column("business_category_other", sa.String(60), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant", "business_category_other")
    op.drop_column("tenant", "business_category")
