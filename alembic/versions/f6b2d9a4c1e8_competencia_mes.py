"""Análisis de competencia (F-plat-comp).

- `competencia_mes`: agregado por RUT × comprador × familia × mes (derivada; la reescribe el job
  `mercado-competencia`).
- `competencia_comprador_mes`: agregado por comprador × familia × mes (CA, desiertas, oferentes,
  un oferente, «no ganó el más barato»), para el mapa de mercado.

Solo tablas nuevas: no toca datos existentes.

Revision ID: f6b2d9a4c1e8
Revises: e4a7c2f9d1b3
Create Date: 2026-11-05 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f6b2d9a4c1e8"
down_revision: str | None = "e4a7c2f9d1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _int(nombre: str) -> sa.Column:
    return sa.Column(nombre, sa.Integer(), nullable=False, server_default="0")


def _float(nombre: str) -> sa.Column:
    return sa.Column(nombre, sa.Float(), nullable=False, server_default="0")


def upgrade() -> None:
    op.create_table(
        "competencia_mes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("mes", sa.String(7), nullable=False),
        sa.Column("region", sa.Integer(), nullable=True),
        sa.Column("rut", sa.String(20), nullable=False),
        sa.Column("clave_comprador", sa.String(600), nullable=False),
        sa.Column("organismo_rut", sa.String(20), nullable=False, server_default=""),
        sa.Column("familia", sa.String(100), nullable=False),
        sa.Column("razon_social", sa.String(300), nullable=True),
        sa.Column("es_emt", sa.Boolean(), nullable=True),
        sa.Column("comuna_proveedor", sa.String(100), nullable=True),
        sa.Column("region_proveedor", sa.Integer(), nullable=True),
        _int("n_ca"),
        _int("n_ofertas"),
        _int("n_ganadas"),
        _int("n_sin_dato"),
        _float("suma_log_ratio"),
        _int("n_log_ratio"),
        sa.Column("p50_log_ratio", sa.Float(), nullable=True),
        _float("monto_ganado"),
        _int("n_ca_un_oferente"),
    )
    op.create_index("ix_competencia_mes_rut", "competencia_mes", ["rut"])
    # Sin índice (organismo_rut, familia): ninguna consulta lo usa (regla 11).
    op.create_index(
        "ix_competencia_mes_comprador_familia", "competencia_mes", ["clave_comprador", "familia"]
    )

    op.create_table(
        "competencia_comprador_mes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("mes", sa.String(7), nullable=False),
        sa.Column("region", sa.Integer(), nullable=True),
        sa.Column("clave_comprador", sa.String(600), nullable=False),
        sa.Column("organismo_rut", sa.String(20), nullable=False, server_default=""),
        sa.Column("comprador_nombre", sa.String(600), nullable=True),
        sa.Column("familia", sa.String(100), nullable=False),
        _int("n_ca"),
        _int("n_desiertas"),
        _int("n_con_ofertas"),
        _int("suma_oferentes"),
        sa.Column("p50_oferentes", sa.Float(), nullable=True),
        _int("n_un_oferente"),
        _int("n_con_ganador"),
        _int("n_2mas_con_ganador"),
        _int("n_no_gano_mas_barato"),
        _float("monto_adjudicado"),
    )
    op.create_index(
        "ix_competencia_comprador_mes_comprador_familia",
        "competencia_comprador_mes",
        ["clave_comprador", "familia"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_competencia_comprador_mes_comprador_familia", table_name="competencia_comprador_mes"
    )
    op.drop_table("competencia_comprador_mes")
    op.drop_index("ix_competencia_mes_comprador_familia", table_name="competencia_mes")
    op.drop_index("ix_competencia_mes_rut", table_name="competencia_mes")
    op.drop_table("competencia_mes")
