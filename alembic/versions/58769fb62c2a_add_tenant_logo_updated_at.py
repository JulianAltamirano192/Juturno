"""add tenant logo_updated_at

Revision ID: 58769fb62c2a
Revises: 2cf8d49114fa
Create Date: 2026-10-15 04:35:05.521595

Versión del logo en tenant, así el header del panel lo muestra sin consultar
tenant_logo en cada página. Nullable (NULL = sin logo); el backfill la copia de
tenant_logo.updated_at para los logos ya subidos (pocas filas: un logo por
negocio).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "58769fb62c2a"
down_revision: str = "2cf8d49114fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant",
        sa.Column("logo_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE tenant SET logo_updated_at = tenant_logo.updated_at "
        "FROM tenant_logo WHERE tenant_logo.tenant_id = tenant.id"
    )


def downgrade() -> None:
    op.drop_column("tenant", "logo_updated_at")
