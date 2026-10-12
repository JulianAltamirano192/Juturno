"""add_booking_horizon_days_to_tenant

Revision ID: a1c2e3f4b5d6
Revises: 7f0ee645a6dd
Create Date: 2026-10-12 12:00:00.000000

Horizonte de reserva por negocio: hasta cuántos días hacia adelante se puede
reservar (15/30/60/90). Los tenants existentes quedan en 90 por el
server_default, el valor menos restrictivo.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c2e3f4b5d6"
down_revision: str = "7f0ee645a6dd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant",
        sa.Column(
            "booking_horizon_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("90"),
        ),
    )
    op.create_check_constraint(
        "ck_tenant_booking_horizon_days",
        "tenant",
        "booking_horizon_days IN (15, 30, 60, 90)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenant_booking_horizon_days", "tenant", type_="check")
    op.drop_column("tenant", "booking_horizon_days")
