"""add service questions and booking answers

Preguntas personalizadas por servicio (Fase 5, ítem 42). Los servicios
existentes quedan sin preguntas ([]) y los turnos existentes sin respuestas
(NULL): el upgrade no cambia ningún dato guardado. El downgrade borra las
preguntas y respuestas cargadas desde entonces (solo se recuperan del backup).

Revision ID: 46fd57b22b48
Revises: e7a8de2da19a
Create Date: 2026-10-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "46fd57b22b48"
down_revision: Union[str, None] = "e7a8de2da19a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "service",
        sa.Column(
            "questions", sa.JSON(), server_default=sa.text("'[]'"), nullable=False
        ),
    )
    op.add_column("booking", sa.Column("answers", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("booking", "answers")
    op.drop_column("service", "questions")
