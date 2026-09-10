"""initial_schema

Revision ID: 1a2b3c4d5e6f
Revises: 
Create Date: 2026-09-10 16:47:20.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision = '1a2b3c4d5e6f'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # --- PRERREQUISITO POSTGRESQL ---
    op.execute('CREATE EXTENSION IF NOT EXISTS btree_gist;')

    # --- TABLAS ---
    op.create_table('tenant',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('whatsapp_number', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('timezone', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tenant_name'), 'tenant', ['name'], unique=False)

    op.create_table('service',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('duration_minutes', sa.Integer(), nullable=False),
        sa.Column('price', sa.Float(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_service_is_active'), 'service', ['is_active'], unique=False)
    op.create_index(op.f('ix_service_tenant_id'), 'service', ['tenant_id'], unique=False)

    op.create_table('staff',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_staff_tenant_id'), 'staff', ['tenant_id'], unique=False)

    op.create_table('booking',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('service_id', sa.Integer(), nullable=False),
        sa.Column('staff_id', sa.Integer(), nullable=True),
        sa.Column('client_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('client_phone', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('start_time', sa.DateTime(), nullable=False), # Slot como DateTime
        sa.Column('end_time', sa.DateTime(), nullable=False),     # Slot como DateTime
        sa.Column('price_at_booking', sa.Float(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('idempotency_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['service_id'], ['service.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['staff_id'], ['staff.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key', name='uq_booking_idempotency_key')
    )
    
    # Índices creados explícitamente en Alembic
    op.create_index(op.f('ix_booking_end_time'), 'booking', ['end_time'], unique=False)
    op.create_index(op.f('ix_booking_idempotency_key'), 'booking', ['idempotency_key'], unique=True)
    op.create_index(op.f('ix_booking_service_id'), 'booking', ['service_id'], unique=False)
    op.create_index(op.f('ix_booking_staff_id'), 'booking', ['staff_id'], unique=False)
    op.create_index(op.f('ix_booking_start_time'), 'booking', ['start_time'], unique=False)
    op.create_index(op.f('ix_booking_status'), 'booking', ['status'], unique=False)
    op.create_index(op.f('ix_booking_tenant_id'), 'booking', ['tenant_id'], unique=False)
    
    # Red de seguridad: Exclusion Constraint en PostgreSQL
    op.execute(
        """
        ALTER TABLE booking ADD CONSTRAINT excl_overlapping_bookings 
        EXCLUDE USING gist (
            staff_id WITH =, 
            tstzrange(start_time, end_time) WITH &&
        );
        """
    )

    op.create_table('payment',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('booking_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('method', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['booking_id'], ['booking.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_payment_booking_id'), 'payment', ['booking_id'], unique=False)
    op.create_index(op.f('ix_payment_status'), 'payment', ['status'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_payment_status'), table_name='payment')
    op.drop_index(op.f('ix_payment_booking_id'), table_name='payment')
    op.drop_table('payment')
    
    op.execute('ALTER TABLE booking DROP CONSTRAINT IF EXISTS excl_overlapping_bookings;')
    
    op.drop_index(op.f('ix_booking_tenant_id'), table_name='booking')
    op.drop_index(op.f('ix_booking_status'), table_name='booking')
    op.drop_index(op.f('ix_booking_start_time'), table_name='booking')
    op.drop_index(op.f('ix_booking_staff_id'), table_name='booking')
    op.drop_index(op.f('ix_booking_service_id'), table_name='booking')
    op.drop_index(op.f('ix_booking_idempotency_key'), table_name='booking')
    op.drop_index(op.f('ix_booking_end_time'), table_name='booking')
    op.drop_table('booking')
    
    op.drop_index(op.f('ix_staff_tenant_id'), table_name='staff')
    op.drop_table('staff')
    
    op.drop_index(op.f('ix_service_tenant_id'), table_name='service')
    op.drop_index(op.f('ix_service_is_active'), table_name='service')
    op.drop_table('service')
    
    op.drop_index(op.f('ix_tenant_name'), table_name='tenant')
    op.drop_table('tenant')