"""add tenant brand color

Revision ID: 2cf8d49114fa
Revises: a12e6a5a2a92
Create Date: 2026-10-15 02:42:22.168194

Color de marca de la página pública (ítem 41). Nullable, sin backfill: NULL
deja el tema de Juturno. El CHECK asegura que solo entre "#rrggbb", porque el
valor se escribe dentro de un <style>. Al crearlo Postgres recorre tenant, pero
todas las filas tienen NULL: valida al instante (tabla chica, lock breve).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2cf8d49114fa"
down_revision: str = "a12e6a5a2a92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant", sa.Column("brand_color", sa.String(7), nullable=True))
    op.create_check_constraint(
        "ck_tenant_brand_color",
        "tenant",
        "brand_color IS NULL OR brand_color ~ '^#[0123456789abcdef]{6}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenant_brand_color", "tenant", type_="check")
    op.drop_column("tenant", "brand_color")
