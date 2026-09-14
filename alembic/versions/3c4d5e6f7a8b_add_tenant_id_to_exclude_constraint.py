"""add tenant_id to exclude constraint

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
Create Date: 2026-09-14 20:25:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '3c4d5e6f7a8b'
down_revision = '2b3c4d5e6f7a'
branch_labels = None
depends_on = None


def upgrade():
    # Drop the old constraint (without tenant_id)
    op.execute('ALTER TABLE booking DROP CONSTRAINT IF EXISTS excl_overlapping_bookings;')

    # Recreate with tenant_id as the first dimension
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


def downgrade():
    # Revert to the original constraint (without tenant_id)
    op.execute('ALTER TABLE booking DROP CONSTRAINT IF EXISTS excl_overlapping_bookings;')

    op.execute(
        """
        ALTER TABLE booking ADD CONSTRAINT excl_overlapping_bookings
        EXCLUDE USING gist (
            (COALESCE(staff_id, -1)) WITH =,
            tstzrange(start_time, end_time) WITH &&
        );
        """
    )
