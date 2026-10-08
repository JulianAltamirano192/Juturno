"""add_unique_tenant_mp_user_id

Revision ID: c7d8e9f0a1b2
Revises: b0e5b8028ae7
Create Date: 2026-10-07 12:00:00.000000

Una cuenta de MP pertenece a un solo negocio: el webhook resuelve el tenant
por mp_user_id y con duplicados falla con MultipleResultsFound (500).
Índice parcial: los tenants sin MP conectado (NULL) no cuentan.

Si ya hay duplicados, CREATE UNIQUE INDEX falla y la migración no se aplica.
Revisar antes con:
    SELECT mp_user_id, count(*) FROM tenant
    WHERE mp_user_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1;
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: str = "b0e5b8028ae7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_tenant_mp_user_id",
        "tenant",
        ["mp_user_id"],
        unique=True,
        postgresql_where=sa.text("mp_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_tenant_mp_user_id", table_name="tenant")
