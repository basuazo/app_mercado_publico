"""Resumen por usuario y sin frecuencia por perfil.

Revision ID: d2f8a6c1b9e0
Revises: e1f4a7c9b2d6
Create Date: 2026-07-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2f8a6c1b9e0"
down_revision: str | None = "e1f4a7c9b2d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "usuarios",
        sa.Column("dias_resumen", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "usuarios",
        sa.Column("ultimo_resumen_en", sa.DateTime(), nullable=True),
    )
    op.drop_column("perfiles_busqueda", "frecuencia_alerta")


def downgrade() -> None:
    op.add_column(
        "perfiles_busqueda",
        sa.Column(
            "frecuencia_alerta",
            sa.String(length=20),
            nullable=False,
            server_default="digest",
        ),
    )
    op.drop_column("usuarios", "ultimo_resumen_en")
    op.drop_column("usuarios", "dias_resumen")
