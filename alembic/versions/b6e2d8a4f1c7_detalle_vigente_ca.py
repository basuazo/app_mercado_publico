"""Detalle de las CA vigentes: `detalle_vigente_en` y `n_documentos` (F-merc-3).

Dos columnas nullable en `compras_agiles`; no tocan datos existentes.

Revision ID: b6e2d8a4f1c7
Revises: a9d4c7e1f2b6
Create Date: 2026-11-04 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6e2d8a4f1c7"
down_revision: str | None = "a9d4c7e1f2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("compras_agiles", sa.Column("detalle_vigente_en", sa.DateTime(), nullable=True))
    op.add_column("compras_agiles", sa.Column("n_documentos", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("compras_agiles", "n_documentos")
    op.drop_column("compras_agiles", "detalle_vigente_en")
