"""add api_key table for tenant authentication

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-09-11 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "2b3c4d5e6f7a"
down_revision = "1a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "api_key",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("key_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("label", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_api_key_tenant_id"), "api_key", ["tenant_id"], unique=False
    )
    op.create_index(op.f("ix_api_key_key_hash"), "api_key", ["key_hash"], unique=True)


def downgrade():
    op.drop_index(op.f("ix_api_key_key_hash"), table_name="api_key")
    op.drop_index(op.f("ix_api_key_tenant_id"), table_name="api_key")
    op.drop_table("api_key")
