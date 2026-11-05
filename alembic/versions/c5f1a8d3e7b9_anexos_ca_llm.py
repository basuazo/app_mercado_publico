"""Anexos de Compra Ágil y uso de OpenRouter (F-plat-2).

- `ca_anexos`: metadatos y texto extraído de cada anexo; SIN el binario (reglas 10 y 11).
- `llm_uso` (presupuesto del mes) y `llm_cache` (respuestas ya pagadas), en Postgres (regla 10).
- `ca_lineas.anexo_id` (de qué anexo salió la línea) y `compras_agiles.anexo_resuelto_en`.

Todo nuevo o nullable: no toca datos existentes.

Revision ID: c5f1a8d3e7b9
Revises: b6e2d8a4f1c7
Create Date: 2026-11-04 23:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c5f1a8d3e7b9"
down_revision: str | None = "b6e2d8a4f1c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ca_anexos",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ca_codigo",
            sa.String(50),
            sa.ForeignKey("compras_agiles.codigo", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("nombre_original", sa.String(255), nullable=False, server_default=""),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("tamano", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("paginas", sa.Integer(), nullable=True),
        sa.Column("escaneado", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("texto_extraido", sa.Text(), nullable=True),
        sa.Column(
            "subido_por",
            sa.BigInteger(),
            sa.ForeignKey("usuarios.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("subido_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("extraccion_estado", sa.String(16), nullable=False, server_default="pendiente"),
        sa.Column("extraccion_modelo", sa.String(200), nullable=True),
        sa.Column("extraccion_costo_usd", sa.Float(), nullable=True),
        sa.Column("extraccion_detalle", sa.String(500), nullable=True),
        sa.CheckConstraint(
            "extraccion_estado IN ('pendiente', 'hecha', 'sin_presupuesto', 'error')",
            name="estado",
        ),
    )
    op.create_index("ix_ca_anexos_ca_codigo", "ca_anexos", ["ca_codigo"])

    op.create_table(
        "llm_uso",
        sa.Column("mes", sa.String(7), primary_key=True),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_in", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("costo_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("estimado", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "llm_cache",
        sa.Column("clave", sa.String(64), primary_key=True),
        sa.Column("modelo", sa.String(200), nullable=False),
        sa.Column("respuesta", postgresql.JSONB(), nullable=False),
        sa.Column("creado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.add_column(
        "ca_lineas",
        sa.Column(
            "anexo_id",
            sa.BigInteger(),
            sa.ForeignKey("ca_anexos.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_ca_lineas_anexo_id", "ca_lineas", ["anexo_id"])
    op.add_column("compras_agiles", sa.Column("anexo_resuelto_en", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("compras_agiles", "anexo_resuelto_en")
    op.drop_index("ix_ca_lineas_anexo_id", table_name="ca_lineas")
    op.drop_column("ca_lineas", "anexo_id")
    op.drop_table("llm_cache")
    op.drop_table("llm_uso")
    op.drop_index("ix_ca_anexos_ca_codigo", table_name="ca_anexos")
    op.drop_table("ca_anexos")
