"""Índice de precios, modelo de adjudicación y evaluación walk-forward (F-plat-4).

- `precio_indice`: índice por producto canónico × ámbito × mes (derivada; la reescribe el job).
- `modelo_adjudicacion`: coeficientes de P(ganar) (logístico y línea base), uno activo.
- `evaluacion_modelo`: métricas walk-forward por mes (las de margen, por empresa).
- `compras_agiles.comuna_inferida` y `comuna_fuente` (la API no trae comuna).

Todo nuevo o nullable: no toca datos existentes.

Revision ID: e4a7c2f9d1b3
Revises: c5f1a8d3e7b9
Create Date: 2026-11-05 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e4a7c2f9d1b3"
down_revision: str | None = "c5f1a8d3e7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("compras_agiles", sa.Column("comuna_inferida", sa.String(100), nullable=True))
    op.add_column("compras_agiles", sa.Column("comuna_fuente", sa.String(5), nullable=True))

    op.create_table(
        "precio_indice",
        sa.Column(
            "producto_id",
            sa.BigInteger(),
            sa.ForeignKey("producto_canonico.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ambito", sa.String(120), primary_key=True),
        sa.Column("mes", sa.String(7), primary_key=True),
        sa.Column("n_ganador", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("p25_ganador", sa.Float(), nullable=True),
        sa.Column("mediana_ganador", sa.Float(), nullable=True),
        sa.Column("p75_ganador", sa.Float(), nullable=True),
        sa.Column("n_ofertas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("p25_ofertas", sa.Float(), nullable=True),
        sa.Column("mediana_ofertas", sa.Float(), nullable=True),
        sa.Column("p75_ofertas", sa.Float(), nullable=True),
        sa.Column("fuente_ganador", sa.String(3), nullable=True),
    )
    op.create_index("ix_precio_indice_mes", "precio_indice", ["mes"])

    op.create_table(
        "modelo_adjudicacion",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tipo", sa.String(12), nullable=False),
        sa.Column("entrenado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("datos_hasta_mes", sa.String(7), nullable=False),
        sa.Column("n_filas", sa.Integer(), nullable=False),
        sa.Column("coeficientes", postgresql.JSONB(), nullable=False),
        sa.Column(
            "metricas", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_modelo_adjudicacion_activo", "modelo_adjudicacion", ["activo"])

    op.create_table(
        "evaluacion_modelo",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("mes", sa.String(7), nullable=False),
        sa.Column("modelo", sa.String(12), nullable=False),
        sa.Column("metrica", sa.String(40), nullable=False),
        sa.Column("valor", sa.Float(), nullable=True),
        sa.Column("n", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detalle", postgresql.JSONB(), nullable=True),
        sa.Column(
            "empresa_id",
            sa.BigInteger(),
            sa.ForeignKey("empresas.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("calculado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_evaluacion_modelo_mes", "evaluacion_modelo", ["mes"])


def downgrade() -> None:
    op.drop_index("ix_evaluacion_modelo_mes", table_name="evaluacion_modelo")
    op.drop_table("evaluacion_modelo")
    op.drop_index("ix_modelo_adjudicacion_activo", table_name="modelo_adjudicacion")
    op.drop_table("modelo_adjudicacion")
    op.drop_index("ix_precio_indice_mes", table_name="precio_indice")
    op.drop_table("precio_indice")
    op.drop_column("compras_agiles", "comuna_fuente")
    op.drop_column("compras_agiles", "comuna_inferida")
