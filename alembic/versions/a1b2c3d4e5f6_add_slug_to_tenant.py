"""add slug to tenant

Revision ID: a1b2c3d4e5f6
Revises: 9a1b2c3d4e5f
Create Date: 2026-09-22

Agrega la columna slug a la tabla tenant con índice único.
"""

from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "9a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant", sa.Column("slug", sa.String(), nullable=True))
    op.create_index("ix_tenant_slug", "tenant", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tenant_slug", table_name="tenant")
    op.drop_column("tenant", "slug")
