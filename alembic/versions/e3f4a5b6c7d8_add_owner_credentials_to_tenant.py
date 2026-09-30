"""add owner_email and password_hash to tenant

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-30 14:00:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenant", sa.Column("owner_email", sa.String(), nullable=True))
    op.add_column("tenant", sa.Column("password_hash", sa.String(), nullable=True))
    op.create_index("ix_tenant_owner_email", "tenant", ["owner_email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tenant_owner_email", table_name="tenant")
    op.drop_column("tenant", "password_hash")
    op.drop_column("tenant", "owner_email")
