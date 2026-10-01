"""Palabra legible en el vocabulario por rubro (F-ca-vocab).

`rubro_vocabulario.palabra`: la palabra original (minúsculas, con tildes) más
frecuente de cada lexema en su familia, para que el explorador pueda sugerirla
como filtro ("construcción" y no la raíz "constru"). Nullable: el job
`vocabulario-rubros` la llena en su próxima corrida y, mientras tanto (o si no hay
una palabra válida), la pantalla muestra el lexema.

Revision ID: b5d1f8a3c6e2
Revises: a4c9e2b7d1f3
Create Date: 2026-10-01 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b5d1f8a3c6e2"
down_revision: str | None = "a4c9e2b7d1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rubro_vocabulario", sa.Column("palabra", sa.String(60), nullable=True))


def downgrade() -> None:
    op.drop_column("rubro_vocabulario", "palabra")
