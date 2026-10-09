"""default_tenant_timezone_to_buenos_aires

Revision ID: 1589d328bf07
Revises: c7d8e9f0a1b2
Create Date: 2026-10-09 17:04:32.301032

El registro no seteaba timezone y el default del modelo era "UTC": slots,
agenda y horarios de atención quedaban 3 horas corridos para negocios
argentinos. Ningún tenant pudo elegir UTC a propósito (no hay UI ni API
para cambiarlo), así que todo "UTC" existente es el default viejo.

Solo datos: los turnos ya guardados no se corrigen (al 2026-10-09 solo hay
datos de prueba). El downgrade no revierte los datos, porque no se puede
distinguir qué tenants eran "UTC".
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1589d328bf07"
down_revision: str = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE tenant SET timezone = 'America/Argentina/Buenos_Aires' "
        "WHERE timezone = 'UTC'"
    )


def downgrade() -> None:
    pass
