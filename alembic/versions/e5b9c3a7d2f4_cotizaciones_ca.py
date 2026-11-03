"""Cotizaciones de Compra Ágil con precio por producto (F-merc-1).

Aditiva: columnas nullable en `compras_agiles` (campos del detalle que guarda la
ingesta de mercado) y dos tablas nuevas, `ca_cotizaciones` y
`ca_cotizacion_productos`, con borrado en cascada desde la CA. Las filas
existentes no se reescriben. Sin raw_json (regla 11).

Revision ID: e5b9c3a7d2f4
Revises: b8c2d5e9a1f7
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5b9c3a7d2f4"
down_revision: str | None = "b8c2d5e9a1f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columnas_ca() -> list[sa.Column]:
    """Columnas nuevas, frescas en cada llamada (un sa.Column no se reusa entre tablas)."""
    return [
        sa.Column("unidad_compra", sa.String(500), nullable=True),
        sa.Column("direccion_entrega", sa.String(1000), nullable=True),
        sa.Column("plazo_entrega_dias", sa.Integer(), nullable=True),
        sa.Column("tipo_presupuesto", sa.String(50), nullable=True),
        sa.Column("fecha_cierre_segundo_llamado", sa.DateTime(), nullable=True),
        sa.Column("motivo_desierta", sa.Text(), nullable=True),
        sa.Column("motivo_cancelacion", sa.Text(), nullable=True),
        sa.Column("cotizaciones_obtenidas_en", sa.DateTime(), nullable=True),
    ]


def upgrade() -> None:
    for columna in _columnas_ca():
        op.add_column("compras_agiles", columna)

    op.create_table(
        "ca_cotizaciones",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ca_codigo",
            sa.String(50),
            sa.ForeignKey("compras_agiles.codigo", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("llamado", sa.Integer(), nullable=True),
        sa.Column("rut_proveedor", sa.String(20), nullable=False),
        sa.Column("razon_social", sa.String(500), nullable=True),
        sa.Column("es_emt", sa.Boolean(), nullable=True),
        sa.Column("codigo_empresa", sa.String(50), nullable=True),
        sa.Column("codigo_sucursal", sa.String(50), nullable=True),
        sa.Column("id_cotizacion", sa.String(50), nullable=True),
        sa.Column("id_oc", sa.String(50), nullable=True),
        sa.Column("estado_glosa", sa.String(100), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=True),
        sa.Column("seleccionado", sa.Boolean(), nullable=True),
        sa.Column("valor_neto", sa.Float(), nullable=True),
        sa.Column("total_impuesto", sa.Float(), nullable=True),
        sa.Column("monto_despacho", sa.Float(), nullable=True),
        sa.Column("monto_total", sa.Float(), nullable=True),
        sa.Column("fecha_creacion", sa.DateTime(), nullable=True),
        sa.Column("fecha_vigencia", sa.DateTime(), nullable=True),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.UniqueConstraint("ca_codigo", "llamado", "rut_proveedor", name="uq_ca_cotizacion"),
    )
    op.create_index("ix_ca_cotizaciones_rut_proveedor", "ca_cotizaciones", ["rut_proveedor"])

    op.create_table(
        "ca_cotizacion_productos",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "cotizacion_id",
            sa.BigInteger(),
            sa.ForeignKey("ca_cotizaciones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("codigo_producto", sa.String(100), nullable=True),
        sa.Column("nombre_producto", sa.String(500), nullable=True),
        sa.Column("descripcion", sa.String(1000), nullable=True),
        sa.Column("cantidad", sa.Float(), nullable=True),
        sa.Column("precio_unitario", sa.Float(), nullable=True),
        sa.Column("monto_total_producto", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_ca_cotizacion_productos_cotizacion_id", "ca_cotizacion_productos", ["cotizacion_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_ca_cotizacion_productos_cotizacion_id", table_name="ca_cotizacion_productos")
    op.drop_table("ca_cotizacion_productos")
    op.drop_index("ix_ca_cotizaciones_rut_proveedor", table_name="ca_cotizaciones")
    op.drop_table("ca_cotizaciones")
    for columna in reversed(_columnas_ca()):
        op.drop_column("compras_agiles", columna.name)
