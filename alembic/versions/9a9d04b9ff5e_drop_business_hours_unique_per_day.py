"""drop business hours unique per day

Revision ID: 9a9d04b9ff5e
Revises: 58769fb62c2a
Create Date: 2026-10-15 04:52:54.341965

Horarios por profesional (ítem 30): el UNIQUE (tenant_id, staff_id,
day_of_week) solo frenaba a los profesionales (los NULL del negocio cuentan
como distintos), así que no podían tener horario partido. Se borra y los
solapamientos los valida el panel, igual que con el horario del negocio.
El downgrade lo recrea y falla si ya hay dos franjas el mismo día para un
profesional: hay que borrar las sobrantes antes.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a9d04b9ff5e"
down_revision: str = "58769fb62c2a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_business_hours_tenant_staff_day", "business_hours", type_="unique"
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_business_hours_tenant_staff_day",
        "business_hours",
        ["tenant_id", "staff_id", "day_of_week"],
    )
