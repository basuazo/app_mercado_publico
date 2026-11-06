"""Requisitos por CA, cotización formal, ventas post-adjudicación y caja (F-plat-seguimiento).

Tablas nuevas: `empresa_requisitos`, `ca_requisitos`, `ventas`, `venta_compras`.
Columnas nuevas con default (ningún dato existente cambia de significado):
`empresas.direccion|contacto|giro|condiciones_cotizacion|ultimo_correlativo|
plazo_pago_comprador_dias` y `ofertas_propias.numero_cotizacion|validez_dias|condiciones|
documento_generado_en`. La plantilla inicial de requisitos se siembra para las empresas que ya
existen (todo editable; nada se inventa como obligatorio del portal).

Revision ID: c6d9a3f1e8b2
Revises: b4e7d2a9c3f1
Create Date: 2026-11-06 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c6d9a3f1e8b2"
down_revision: str | None = "b4e7d2a9c3f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Misma lista que `app.mercado.requisitos.PLANTILLA_INICIAL` (copiada: una migración no importa
# código de la app, que cambia).
_PLANTILLA = (
    ("Cotización formal adjunta", True),
    ("Precios ingresados línea a línea en el portal", True),
    ("Plazo de entrega indicado", True),
    ("Documentos de la empresa vigentes (boleta de garantía / certificados si los pide)", False),
)


def _fk_usuario(nombre: str) -> sa.Column[int]:
    return sa.Column(
        nombre, sa.BigInteger(), sa.ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )


def _fk_empresa() -> sa.Column[int]:
    return sa.Column(
        "empresa_id",
        sa.BigInteger(),
        sa.ForeignKey("empresas.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.add_column("empresas", sa.Column("direccion", sa.String(300), nullable=True))
    op.add_column("empresas", sa.Column("contacto", sa.String(200), nullable=True))
    op.add_column("empresas", sa.Column("giro", sa.String(200), nullable=True))
    op.add_column("empresas", sa.Column("condiciones_cotizacion", sa.Text(), nullable=True))
    op.add_column(
        "empresas",
        sa.Column("ultimo_correlativo", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "empresas",
        sa.Column("plazo_pago_comprador_dias", sa.Integer(), nullable=False, server_default="30"),
    )

    op.add_column("ofertas_propias", sa.Column("numero_cotizacion", sa.String(20), nullable=True))
    op.add_column(
        "ofertas_propias",
        sa.Column("validez_dias", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column("ofertas_propias", sa.Column("condiciones", sa.Text(), nullable=True))
    op.add_column(
        "ofertas_propias", sa.Column("documento_generado_en", sa.DateTime(), nullable=True)
    )
    op.create_unique_constraint(
        "uq_oferta_propia_numero", "ofertas_propias", ["empresa_id", "numero_cotizacion"]
    )

    op.create_table(
        "empresa_requisitos",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _fk_empresa(),
        sa.Column("texto", sa.String(300), nullable=False),
        sa.Column("obligatorio", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("orden", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("empresa_id", "texto", name="uq_empresa_requisito_texto"),
    )

    op.create_table(
        "ca_requisitos",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _fk_empresa(),
        sa.Column(
            "ca_codigo",
            sa.String(50),
            sa.ForeignKey("compras_agiles.codigo"),
            nullable=False,
        ),
        sa.Column("texto", sa.String(300), nullable=False),
        sa.Column("obligatorio", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("origen", sa.String(15), nullable=False),
        sa.Column("cumplido", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _fk_usuario("cumplido_por_id"),
        sa.Column("cumplido_en", sa.DateTime(), nullable=True),
        sa.Column("orden", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("empresa_id", "ca_codigo", "texto", name="uq_ca_requisito_texto"),
        sa.CheckConstraint("origen IN ('plantilla','documento_ca','manual')", name="origen"),
    )

    op.create_table(
        "ventas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _fk_empresa(),
        sa.Column(
            "ca_codigo", sa.String(50), sa.ForeignKey("compras_agiles.codigo"), nullable=False
        ),
        sa.Column(
            "lote_id", sa.BigInteger(), sa.ForeignKey("lotes.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "oferta_id",
            sa.BigInteger(),
            sa.ForeignKey("ofertas_propias.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("estado", sa.String(15), nullable=False, server_default="adjudicada"),
        sa.Column("oc_numero", sa.String(50), nullable=True),
        sa.Column("oc_fecha", sa.Date(), nullable=True),
        sa.Column("oc_monto", sa.Float(), nullable=True),
        sa.Column("entrega_fecha_comprometida", sa.Date(), nullable=True),
        sa.Column("entrega_fecha_real", sa.Date(), nullable=True),
        sa.Column("entrega_direccion", sa.String(500), nullable=True),
        sa.Column("entrega_horario", sa.String(200), nullable=True),
        sa.Column("entrega_recibe", sa.String(200), nullable=True),
        sa.Column("guia_numero", sa.String(50), nullable=True),
        sa.Column("factura_numero", sa.String(50), nullable=True),
        sa.Column("factura_fecha", sa.Date(), nullable=True),
        sa.Column("factura_monto", sa.Float(), nullable=True),
        sa.Column("plazo_pago_dias", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("pago_esperado_en", sa.Date(), nullable=True),
        sa.Column("pago_recibido_en", sa.Date(), nullable=True),
        sa.Column("costo_real", sa.Float(), nullable=True),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("motivo_anulacion", sa.String(300), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
        _fk_usuario("actualizado_por_id"),
        sa.UniqueConstraint("empresa_id", "ca_codigo", name="uq_venta_empresa_ca"),
        sa.CheckConstraint(
            "estado IN ('adjudicada','oc_recibida','comprando','entregada','facturada',"
            "'pagada','anulada')",
            name="estado",
        ),
    )
    op.create_index("ix_ventas_empresa_estado", "ventas", ["empresa_id", "estado"])

    op.create_table(
        "venta_compras",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "venta_id", sa.BigInteger(), sa.ForeignKey("ventas.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "proveedor_id",
            sa.BigInteger(),
            sa.ForeignKey("proveedores.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "producto_id",
            sa.BigInteger(),
            sa.ForeignKey("producto_canonico.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "formato_id",
            sa.BigInteger(),
            sa.ForeignKey("producto_formato.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("descripcion", sa.String(300), nullable=False, server_default=""),
        sa.Column("cantidad", sa.Float(), nullable=False),
        sa.Column("precio_neto", sa.Float(), nullable=False),
        sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("pagada", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("pagar_en", sa.Date(), nullable=True),
        _fk_usuario("creado_por_id"),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_venta_compras_venta", "venta_compras", ["venta_id"])

    # Plantilla inicial para las empresas que ya existen (las nuevas la reciben al crearse).
    # INSERT … SELECT: funciona también con `alembic upgrade --sql` (modo offline).
    for orden, (texto, obligatorio) in enumerate(_PLANTILLA):
        literal = texto.replace("'", "''")
        op.execute(
            "INSERT INTO empresa_requisitos (empresa_id, texto, obligatorio, orden, activo) "
            f"SELECT id, '{literal}', {'true' if obligatorio else 'false'}, {orden}, true FROM empresas"
        )


def downgrade() -> None:
    op.drop_index("ix_venta_compras_venta", table_name="venta_compras")
    op.drop_table("venta_compras")
    op.drop_index("ix_ventas_empresa_estado", table_name="ventas")
    op.drop_table("ventas")
    op.drop_table("ca_requisitos")
    op.drop_table("empresa_requisitos")
    op.drop_constraint("uq_oferta_propia_numero", "ofertas_propias", type_="unique")
    for col in ("documento_generado_en", "condiciones", "validez_dias", "numero_cotizacion"):
        op.drop_column("ofertas_propias", col)
    for col in (
        "plazo_pago_comprador_dias",
        "ultimo_correlativo",
        "condiciones_cotizacion",
        "giro",
        "contacto",
        "direccion",
    ):
        op.drop_column("empresas", col)
