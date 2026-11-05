"""Trabajo en equipo sobre las CA (F-equipo).

- `usuarios.nombre` y `usuarios.actividad_leida_hasta` (nullable).
- `ca_trabajo`: estado compartido de una CA por empresa (UNIQUE empresa × CA, `version`).
- `ca_eventos`: registro que solo se agrega; los de sistema (usuario_id NULL) son únicos por
  (empresa, CA, tipo) con un índice único parcial.
- `ca_comentarios`: hilo por CA y empresa (eliminar es lógico).
- `preferencias_alarma`: interruptores por usuario (sin fila = activa).

Solo columnas nullable y tablas nuevas: no toca datos existentes.

Revision ID: a8d4f2c6e9b1
Revises: f6b2d9a4c1e8
Create Date: 2026-11-05 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a8d4f2c6e9b1"
down_revision: str | None = "f6b2d9a4c1e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fk_usuario(nombre: str, ondelete: str, nullable: bool = True) -> sa.Column:
    return sa.Column(
        nombre,
        sa.BigInteger(),
        sa.ForeignKey("usuarios.id", ondelete=ondelete),
        nullable=nullable,
    )


def _fk_empresa() -> sa.Column:
    return sa.Column(
        "empresa_id",
        sa.BigInteger(),
        sa.ForeignKey("empresas.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.add_column("usuarios", sa.Column("nombre", sa.String(100), nullable=True))
    op.add_column("usuarios", sa.Column("actividad_leida_hasta", sa.DateTime(), nullable=True))

    op.create_table(
        "ca_trabajo",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _fk_empresa(),
        sa.Column(
            "ca_codigo", sa.String(50), sa.ForeignKey("compras_agiles.codigo"), nullable=False
        ),
        sa.Column("estado", sa.String(20), nullable=False),
        _fk_usuario("responsable_id", "SET NULL"),
        sa.Column("motivo_descarte", sa.String(300), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("resultado", sa.String(20), nullable=True),
        sa.Column("resultado_en", sa.DateTime(), nullable=True),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
        _fk_usuario("actualizado_por_id", "SET NULL"),
        sa.UniqueConstraint("empresa_id", "ca_codigo", name="uq_ca_trabajo_empresa_ca"),
    )
    op.create_index("ix_ca_trabajo_empresa_estado", "ca_trabajo", ["empresa_id", "estado"])
    op.create_index(
        "ix_ca_trabajo_empresa_responsable", "ca_trabajo", ["empresa_id", "responsable_id"]
    )

    op.create_table(
        "ca_eventos",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _fk_empresa(),
        sa.Column("ca_codigo", sa.String(50), nullable=False),
        _fk_usuario("usuario_id", "CASCADE"),
        sa.Column("tipo", sa.String(20), nullable=False),
        _fk_usuario("destinatario_id", "CASCADE"),
        sa.Column("datos", postgresql.JSONB(), nullable=True),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_ca_eventos_empresa_creado",
        "ca_eventos",
        ["empresa_id", sa.text("creado_en DESC")],
    )
    op.create_index("ix_ca_eventos_empresa_ca", "ca_eventos", ["empresa_id", "ca_codigo"])
    op.create_index(
        "uq_ca_eventos_sistema",
        "ca_eventos",
        ["empresa_id", "ca_codigo", "tipo"],
        unique=True,
        postgresql_where=sa.text("usuario_id IS NULL"),
    )

    op.create_table(
        "ca_comentarios",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _fk_empresa(),
        sa.Column("ca_codigo", sa.String(50), nullable=False),
        _fk_usuario("usuario_id", "CASCADE", nullable=False),
        sa.Column("texto", sa.String(2000), nullable=False),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("editado_en", sa.DateTime(), nullable=True),
        sa.Column("eliminado", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index(
        "ix_ca_comentarios_empresa_ca_creado",
        "ca_comentarios",
        ["empresa_id", "ca_codigo", "creado_en"],
    )

    op.create_table(
        "preferencias_alarma",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _fk_usuario("usuario_id", "CASCADE", nullable=False),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("activa", sa.Boolean(), nullable=False, server_default="true"),
        sa.UniqueConstraint("usuario_id", "tipo", name="uq_preferencia_alarma"),
    )


def downgrade() -> None:
    op.drop_table("preferencias_alarma")
    op.drop_index("ix_ca_comentarios_empresa_ca_creado", table_name="ca_comentarios")
    op.drop_table("ca_comentarios")
    op.drop_index("uq_ca_eventos_sistema", table_name="ca_eventos")
    op.drop_index("ix_ca_eventos_empresa_ca", table_name="ca_eventos")
    op.drop_index("ix_ca_eventos_empresa_creado", table_name="ca_eventos")
    op.drop_table("ca_eventos")
    op.drop_index("ix_ca_trabajo_empresa_responsable", table_name="ca_trabajo")
    op.drop_index("ix_ca_trabajo_empresa_estado", table_name="ca_trabajo")
    op.drop_table("ca_trabajo")
    op.drop_column("usuarios", "actividad_leida_hasta")
    op.drop_column("usuarios", "nombre")
