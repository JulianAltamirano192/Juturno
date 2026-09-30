"""add business_hours table

Revision ID: a2b3c4d5e6f7
Revises: f4a5b6c7d8e9
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa

revision = "a2b3c4d5e6f7"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_hours",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("staff_id", sa.Integer(), nullable=True),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.ForeignKeyConstraint(["staff_id"], ["staff.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "staff_id",
            "day_of_week",
            name="uq_business_hours_tenant_staff_day",
        ),
    )
    op.create_index(
        op.f("ix_business_hours_tenant_id"), "business_hours", ["tenant_id"]
    )
    op.create_index(op.f("ix_business_hours_staff_id"), "business_hours", ["staff_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_business_hours_staff_id"), table_name="business_hours")
    op.drop_index(op.f("ix_business_hours_tenant_id"), table_name="business_hours")
    op.drop_table("business_hours")
