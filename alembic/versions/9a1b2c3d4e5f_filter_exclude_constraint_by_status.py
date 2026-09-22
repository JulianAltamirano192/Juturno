"""filter exclude constraint by status

Revision ID: 9a1b2c3d4e5f
Revises: 87668cdc0a01
Create Date: 2026-09-21

Filtra el ExcludeConstraint excl_overlapping_bookings para aplicarlo
solamente a bookings con estado 'pending' o 'confirmed'.
Bookings cancelados dejan de bloquear turnos solapados.
"""

from alembic import op

revision = "9a1b2c3d4e5f"
down_revision = "87668cdc0a01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE booking DROP CONSTRAINT IF EXISTS excl_overlapping_bookings;"
    )
    op.execute(
        """
        ALTER TABLE booking ADD CONSTRAINT excl_overlapping_bookings
        EXCLUDE USING gist (
            tenant_id WITH =,
            (COALESCE(staff_id, -1)) WITH =,
            tstzrange(start_time, end_time) WITH &&
        ) WHERE (status IN ('pending', 'confirmed'));
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE booking DROP CONSTRAINT IF EXISTS excl_overlapping_bookings;"
    )
    op.execute(
        """
        ALTER TABLE booking ADD CONSTRAINT excl_overlapping_bookings
        EXCLUDE USING gist (
            tenant_id WITH =,
            (COALESCE(staff_id, -1)) WITH =,
            tstzrange(start_time, end_time) WITH &&
        );
        """
    )
