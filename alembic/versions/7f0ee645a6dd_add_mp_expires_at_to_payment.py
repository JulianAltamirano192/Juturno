"""add_mp_expires_at_to_payment

Revision ID: 7f0ee645a6dd
Revises: 1589d328bf07
Create Date: 2026-10-09 19:30:00.000000

Vencimiento del link de pago de MP (la preferencia ahora se crea con
expiration_date_to). El guard de desconexión de MP lo usa para saber cuándo
ya no puede llegar un pago que el webhook tenga que verificar con el token.

Nullable y sin backfill: los Payment previos no tienen vencimiento conocido
y el guard los ignora (al 2026-10-09 solo hay datos de prueba).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f0ee645a6dd"
down_revision: str = "1589d328bf07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payment",
        sa.Column("mp_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payment", "mp_expires_at")
