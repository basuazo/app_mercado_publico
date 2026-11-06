"""Cotización por lote, solicitudes de precio a proveedores y precios vencidos (F-plat-lote).

Tablas nuevas: `lotes`, `lote_cas`, `lote_lineas`, `solicitudes_precio`, `solicitud_lineas`.
Columnas nuevas con default (ningún dato existente cambia de significado):
`empresas.dias_vigencia_precio` (15, decisión de Boris 5-nov), `empresas.margen_objetivo` (0,15) y
`proveedor_productos.tramos` (JSONB nullable: precio por volumen).

Revision ID: b4e7d2a9c3f1
Revises: e7c2a9f4b1d8
Create Date: 2026-11-05 23:59:50.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4e7d2a9c3f1"
down_revision: str | None = "e7c2a9f4b1d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fk_usuario(nombre: str) -> sa.Column[int]:
    return sa.Column(
        nombre, sa.BigInteger(), sa.ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )


def upgrade() -> None:
    op.add_column(
        "empresas",
        sa.Column("dias_vigencia_precio", sa.Integer(), nullable=False, server_default="15"),
    )
    op.add_column(
        "empresas",
        sa.Column("margen_objetivo", sa.Float(), nullable=False, server_default="0.15"),
    )
    op.add_column(
        "proveedor_productos",
        sa.Column("tramos", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.create_table(
        "lotes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "empresa_id",
            sa.BigInteger(),
            sa.ForeignKey("empresas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("estado", sa.String(15), nullable=False, server_default="abierto"),
        _fk_usuario("creado_por_id"),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("fecha_limite", sa.DateTime(), nullable=True),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "estado IN ('abierto','solicitado','con_precios','ofertado','cerrado')", name="estado"
        ),
    )
    op.create_index("ix_lotes_empresa_estado", "lotes", ["empresa_id", "estado"])

    op.create_table(
        "lote_cas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "lote_id",
            sa.BigInteger(),
            sa.ForeignKey("lotes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ca_codigo", sa.String(50), sa.ForeignKey("compras_agiles.codigo"), nullable=False
        ),
        _fk_usuario("agregada_por_id"),
        sa.Column("agregada_en", sa.DateTime(), nullable=False),
        sa.Column("estado_previo", sa.String(20), nullable=True),
        sa.UniqueConstraint("lote_id", "ca_codigo", name="uq_lote_ca"),
    )
    op.create_index("ix_lote_cas_ca", "lote_cas", ["ca_codigo"])

    op.create_table(
        "lote_lineas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "lote_id",
            sa.BigInteger(),
            sa.ForeignKey("lotes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "producto_id", sa.BigInteger(), sa.ForeignKey("producto_canonico.id"), nullable=False
        ),
        sa.Column(
            "formato_id", sa.BigInteger(), sa.ForeignKey("producto_formato.id"), nullable=False
        ),
        sa.Column("cantidad_base", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cantidad_formato", sa.Float(), nullable=False, server_default="0"),
        sa.Column("en_stock_base", sa.Float(), nullable=False, server_default="0"),
        sa.Column("a_comprar_base", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "proveedor_sugerido_id",
            sa.BigInteger(),
            sa.ForeignKey("proveedores.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "proveedor_producto_id",
            sa.BigInteger(),
            sa.ForeignKey("proveedor_productos.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "proveedor_fijado", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("costo_unitario_sugerido", sa.Float(), nullable=True),
        sa.Column("precio_vencido", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dias_precio", sa.Integer(), nullable=True),
        sa.Column(
            "detalle",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.UniqueConstraint("lote_id", "producto_id", "formato_id", name="uq_lote_linea"),
    )

    op.create_table(
        "solicitudes_precio",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "lote_id",
            sa.BigInteger(),
            sa.ForeignKey("lotes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "proveedor_id",
            sa.BigInteger(),
            sa.ForeignKey("proveedores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("estado", sa.String(15), nullable=False, server_default="borrador"),
        sa.Column("canal", sa.String(15), nullable=True),
        sa.Column("enviada_en", sa.DateTime(), nullable=True),
        _fk_usuario("enviada_por_id"),
        sa.Column("respondida_en", sa.DateTime(), nullable=True),
        sa.Column("texto_generado", sa.Text(), nullable=True),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("lote_id", "proveedor_id", name="uq_solicitud_lote_proveedor"),
        sa.CheckConstraint(
            "estado IN ('borrador','enviada','respondida','sin_respuesta')", name="estado"
        ),
    )

    op.create_table(
        "solicitud_lineas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "solicitud_id",
            sa.BigInteger(),
            sa.ForeignKey("solicitudes_precio.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "lote_linea_id",
            sa.BigInteger(),
            sa.ForeignKey("lote_lineas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cantidad_formato", sa.Float(), nullable=False, server_default="0"),
        sa.Column("precio_respuesta", sa.Float(), nullable=True),
        sa.Column("tramos", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("disponible", sa.Boolean(), nullable=True),
        sa.Column("plazo_dias", sa.Integer(), nullable=True),
        sa.Column("respondida_en", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("solicitud_id", "lote_linea_id", name="uq_solicitud_linea"),
    )


def downgrade() -> None:
    op.drop_table("solicitud_lineas")
    op.drop_table("solicitudes_precio")
    op.drop_table("lote_lineas")
    op.drop_index("ix_lote_cas_ca", table_name="lote_cas")
    op.drop_table("lote_cas")
    op.drop_index("ix_lotes_empresa_estado", table_name="lotes")
    op.drop_table("lotes")
    op.drop_column("proveedor_productos", "tramos")
    op.drop_column("empresas", "margen_objetivo")
    op.drop_column("empresas", "dias_vigencia_precio")
