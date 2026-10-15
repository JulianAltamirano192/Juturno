"""add business profile and tenant logo

Revision ID: a12e6a5a2a92
Revises: dcb3cce62e10
Create Date: 2026-10-15 02:06:25.417916

Perfil del negocio (ítem 15): localidad y usuarios de Instagram/Facebook en
tenant (nullable, sin backfill) y el logo en una tabla aparte, así los bytes
no viajan en cada query de tenant. Tabla nueva y vacía: no bloquea nada.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a12e6a5a2a92"
down_revision: str = "dcb3cce62e10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant", sa.Column("locality", sa.String(60), nullable=True))
    op.add_column("tenant", sa.Column("instagram", sa.String(30), nullable=True))
    op.add_column("tenant", sa.Column("facebook", sa.String(100), nullable=True))
    op.create_table(
        "tenant_logo",
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tenant_logo")
    op.drop_column("tenant", "facebook")
    op.drop_column("tenant", "instagram")
    op.drop_column("tenant", "locality")
