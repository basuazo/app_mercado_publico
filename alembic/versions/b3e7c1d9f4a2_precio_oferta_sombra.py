"""Precio, margen y mi oferta en la mesa; modo sombra (F-plat-5).

- `evaluaciones_cache`: última evaluación de cada CA vigente por empresa (PK CA × empresa).
- `predicciones`: inmutables, UNA por CA y empresa (UNIQUE sin `modelo_id`: el modelo se
  reentrena a diario y no debe abrir una predicción nueva cada día).
- `ofertas_propias`: contenido de la oferta (líneas, total, plazo), con `version`. Sin estado:
  el flujo vive en `ca_trabajo` (F-equipo).

Solo tablas nuevas: no toca datos existentes.

Revision ID: b3e7c1d9f4a2
Revises: a8d4f2c6e9b1
Create Date: 2026-11-05 23:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b3e7c1d9f4a2"
down_revision: str | None = "a8d4f2c6e9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fk_empresa(primary_key: bool = False) -> sa.Column:
    return sa.Column(
        "empresa_id",
        sa.BigInteger(),
        sa.ForeignKey("empresas.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=primary_key,
    )


def _fk_modelo() -> sa.Column:
    return sa.Column(
        "modelo_id",
        sa.BigInteger(),
        sa.ForeignKey("modelo_adjudicacion.id", ondelete="SET NULL"),
        nullable=True,
    )


def upgrade() -> None:
    op.create_table(
        "evaluaciones_cache",
        sa.Column(
            "ca_codigo",
            sa.String(50),
            sa.ForeignKey("compras_agiles.codigo", ondelete="CASCADE"),
            primary_key=True,
        ),
        _fk_empresa(primary_key=True),
        sa.Column("evaluada_en", sa.DateTime(), nullable=False),
        _fk_modelo(),
        sa.Column("datos", postgresql.JSONB(), nullable=False),
    )

    op.create_table(
        "predicciones",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ca_codigo", sa.String(50), sa.ForeignKey("compras_agiles.codigo"), nullable=False
        ),
        _fk_empresa(),
        sa.Column("creada_en", sa.DateTime(), nullable=False),
        sa.Column("cierre_ca", sa.DateTime(), nullable=True),
        _fk_modelo(),
        sa.Column("modelo_tipo", sa.String(12), nullable=False),
        sa.Column("precio_sugerido", sa.Float(), nullable=False),
        sa.Column("intervalo_80_min", sa.Float(), nullable=True),
        sa.Column("intervalo_80_max", sa.Float(), nullable=True),
        sa.Column("p_ganar_sugerido", sa.Float(), nullable=True),
        sa.Column("margen_esperado", sa.Float(), nullable=True),
        sa.Column("cobertura", sa.Float(), nullable=True),
        sa.Column("detalle", postgresql.JSONB(), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="abierta"),
        sa.Column("resultado_precio_ganador", sa.Float(), nullable=True),
        sa.Column("resultado_en_intervalo", sa.Boolean(), nullable=True),
        sa.Column("resultado_propio", sa.String(20), nullable=True),
        sa.Column("cerrada_en", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("ca_codigo", "empresa_id", name="uq_prediccion_ca_empresa"),
    )
    op.create_index("ix_predicciones_empresa_estado", "predicciones", ["empresa_id", "estado"])

    op.create_table(
        "ofertas_propias",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ca_codigo", sa.String(50), sa.ForeignKey("compras_agiles.codigo"), nullable=False
        ),
        _fk_empresa(),
        sa.Column(
            "usuario_id",
            sa.BigInteger(),
            sa.ForeignKey("usuarios.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("lineas", postgresql.JSONB(), nullable=False),
        sa.Column("total_neto", sa.Float(), nullable=False),
        sa.Column("plazo_entrega_dias", sa.Integer(), nullable=True),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("guardada_en", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("precio_ganador_real", sa.Float(), nullable=True),
        sa.Column("margen_realizado", sa.Float(), nullable=True),
        sa.UniqueConstraint("ca_codigo", "empresa_id", name="uq_oferta_propia_ca_empresa"),
    )


def downgrade() -> None:
    op.drop_table("ofertas_propias")
    op.drop_index("ix_predicciones_empresa_estado", table_name="predicciones")
    op.drop_table("predicciones")
    op.drop_table("evaluaciones_cache")
