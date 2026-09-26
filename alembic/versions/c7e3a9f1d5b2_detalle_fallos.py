"""Contador de fallos de detalle por oportunidad (F-detalles-fallos).

Aditiva: dos columnas en `licitaciones` y `compras_agiles`. Las filas existentes
toman el server_default (0 fallos, sin último fallo); no se reescribe nada más.
Sin índices: las columnas solo ordenan y filtran una cola ya acotada por
raw_json nulo, estado publicada y match.

Revision ID: c7e3a9f1d5b2
Revises: b8d4e2a91c07
Create Date: 2026-09-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7e3a9f1d5b2"
down_revision: str | None = "b8d4e2a91c07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLAS = ("licitaciones", "compras_agiles")


def upgrade() -> None:
    for tabla in _TABLAS:
        op.add_column(
            tabla,
            sa.Column("detalle_fallos", sa.Integer(), nullable=False, server_default="0"),
        )
        op.add_column(tabla, sa.Column("detalle_ultimo_fallo", sa.DateTime(), nullable=True))


def downgrade() -> None:
    for tabla in _TABLAS:
        op.drop_column(tabla, "detalle_ultimo_fallo")
        op.drop_column(tabla, "detalle_fallos")
