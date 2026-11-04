"""Órdenes de compra de Compra Ágil de las regiones del piloto (F-merc-2).

Aditiva: tablas nuevas `oc_lineas_ca` y `precio_referencia`. No toca filas existentes.

Revision ID: a7c3e9d1b4f2
Revises: e5b9c3a7d2f4
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c3e9d1b4f2"
down_revision: str | None = "e5b9c3a7d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oc_lineas_ca",
        sa.Column("codigo_oc", sa.String(50), primary_key=True),
        sa.Column("id_item", sa.String(30), primary_key=True),
        sa.Column("mes", sa.String(7), nullable=False),
        sa.Column("fecha_creacion", sa.DateTime(), nullable=True),
        sa.Column("codigo_estado", sa.String(10), nullable=True),
        sa.Column("moneda", sa.String(10), nullable=True),
        sa.Column("rut_unidad", sa.String(20), nullable=True),
        sa.Column("unidad_compra", sa.Text(), nullable=True),
        sa.Column("organismo", sa.Text(), nullable=True),
        sa.Column("sector", sa.String(100), nullable=True),
        sa.Column("comuna_compra", sa.String(100), nullable=True),
        sa.Column("region_compra", sa.Integer(), nullable=True),
        sa.Column("rut_proveedor", sa.String(20), nullable=True),
        sa.Column("proveedor", sa.Text(), nullable=True),
        sa.Column("comuna_proveedor", sa.String(100), nullable=True),
        sa.Column("region_proveedor", sa.Integer(), nullable=True),
        sa.Column("proveedor_local", sa.Boolean(), nullable=True),
        sa.Column("codigo_producto", sa.String(20), nullable=True),
        sa.Column("producto_generico", sa.Text(), nullable=True),
        sa.Column("grupo", sa.String(100), nullable=False),
        sa.Column("especificacion", sa.Text(), nullable=True),
        sa.Column("cantidad", sa.Float(), nullable=True),
        sa.Column("unidad_medida", sa.String(50), nullable=True),
        sa.Column("precio_neto", sa.Float(), nullable=True),
        sa.Column("total_linea_neto", sa.Float(), nullable=True),
        sa.Column("precio_base", sa.Float(), nullable=True),
        sa.Column("base", sa.String(3), nullable=True),
    )
    op.create_index("ix_oc_lineas_ca_mes", "oc_lineas_ca", ["mes"])
    op.create_index("ix_oc_lineas_ca_region_mes", "oc_lineas_ca", ["region_compra", "mes"])

    op.create_table(
        "precio_referencia",
        sa.Column("clave", sa.String(30), primary_key=True),
        sa.Column("mes", sa.String(7), primary_key=True),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("p25", sa.Float(), nullable=False),
        sa.Column("mediana", sa.Float(), nullable=False),
        sa.Column("p75", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("precio_referencia")
    op.drop_index("ix_oc_lineas_ca_region_mes", table_name="oc_lineas_ca")
    op.drop_index("ix_oc_lineas_ca_mes", table_name="oc_lineas_ca")
    op.drop_table("oc_lineas_ca")
