"""add MP OAuth credential fields to tenant

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-09-30 12:00:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Token columns store Fernet ciphertext (opaque strings) — MP tokens
    # never persist in plaintext. All nullable: existing tenants have no
    # MP account connected yet (D-012).
    op.add_column("tenant", sa.Column("mp_user_id", sa.String(), nullable=True))
    op.add_column("tenant", sa.Column("mp_public_key", sa.String(), nullable=True))
    op.add_column("tenant", sa.Column("mp_alias", sa.String(), nullable=True))
    op.add_column(
        "tenant", sa.Column("mp_access_token_enc", sa.String(), nullable=True)
    )
    op.add_column(
        "tenant", sa.Column("mp_refresh_token_enc", sa.String(), nullable=True)
    )
    op.add_column(
        "tenant",
        sa.Column(
            "mp_token_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant", "mp_token_expires_at")
    op.drop_column("tenant", "mp_refresh_token_enc")
    op.drop_column("tenant", "mp_access_token_enc")
    op.drop_column("tenant", "mp_alias")
    op.drop_column("tenant", "mp_public_key")
    op.drop_column("tenant", "mp_user_id")
