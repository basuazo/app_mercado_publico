"""Licitaciones con nombre de organismo y región (F-datos-1).

Replica las columnas de `compras_agiles`: `organismo_nombre` y `region`, con
índice en `region`. Llegan con el detalle v1, bajo `Comprador` [V, sonda
claves-lic]. Las vigentes que ya tienen detalle las rellena el paso
`rellenar-organismo` de `nocturno`.

Revision ID: a4e7c2d9f1b3
Revises: c9e4b2f7a1d8
Create Date: 2026-10-08 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4e7c2d9f1b3"
down_revision: str | None = "c9e4b2f7a1d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("licitaciones", sa.Column("organismo_nombre", sa.String(500), nullable=True))
    op.add_column("licitaciones", sa.Column("region", sa.Integer(), nullable=True))
    op.create_index("ix_licitaciones_region", "licitaciones", ["region"])


def downgrade() -> None:
    op.drop_index("ix_licitaciones_region", table_name="licitaciones")
    op.drop_column("licitaciones", "region")
    op.drop_column("licitaciones", "organismo_nombre")
