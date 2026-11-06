"""Impuestos específicos en el catálogo: neto sin impuestos, bruto con todos (F-plat-impuestos).

- Tabla `impuesto_especifico` (código, nombre, tasa, recuperable, vigente_desde, fuente y notas).
  Una tasa nueva es una fila nueva con el mismo código: UNIQUE (codigo, vigente_desde). Seed
  VACÍO: Boris carga las tasas verificadas con `scripts/mercado/sembrar_impuestos.py`.
- Tabla `familia_impuesto` (familia → código y modo `aplica` | `clasificar`).
- `producto_canonico.impuesto_codigo` y `proveedor_productos.impuesto_codigo` (la pista de la
  planilla mientras no hay vínculo). Nullable y sin default: ninguna fila existente cambia.

Revision ID: e9a4c2f7b1d3
Revises: d8f1b6c4a2e9
Create Date: 2026-11-06 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e9a4c2f7b1d3"
down_revision: str | None = "d8f1b6c4a2e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "impuesto_especifico",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("codigo", sa.String(40), nullable=False),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("tasa", sa.Float(), nullable=False),
        sa.Column("recuperable", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("vigente_desde", sa.Date(), nullable=False),
        sa.Column("fuente_url", sa.String(500), nullable=True),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("creado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("codigo", "vigente_desde", name="uq_impuesto_codigo_vigencia"),
        sa.CheckConstraint("tasa >= 0 AND tasa < 1", name="tasa"),
    )
    op.create_table(
        "familia_impuesto",
        sa.Column("familia", sa.String(100), primary_key=True),
        sa.Column("impuesto_codigo", sa.String(40), nullable=False),
        sa.Column("modo", sa.String(10), nullable=False, server_default="aplica"),
        sa.Column(
            "actualizado_por",
            sa.BigInteger(),
            sa.ForeignKey("usuarios.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("modo IN ('aplica','clasificar')", name="modo"),
    )
    op.add_column("producto_canonico", sa.Column("impuesto_codigo", sa.String(40), nullable=True))
    op.add_column(
        "proveedor_productos", sa.Column("impuesto_codigo", sa.String(40), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("proveedor_productos", "impuesto_codigo")
    op.drop_column("producto_canonico", "impuesto_codigo")
    op.drop_table("familia_impuesto")
    op.drop_table("impuesto_especifico")
