"""Anexos: carga masiva por código, prioridad del anexo sobre la API, token externo (F-plat-anexos-2).

- `ca_lineas.estado`: String(10) → String(12) para el estado nuevo `reemplazada` (11 letras).
- `ca_anexos`: `fuente` (manual | masiva | api_externa | api_mp | descarga_directa; default
  manual), `empresa_id` (quién lo subió; las líneas siguen siendo comunes), `extraccion_respaldo`
  (lo leyó el modelo de respaldo) y UNIQUE (ca_codigo, sha256): el mismo archivo dos veces no se
  procesa dos veces. Antes del UNIQUE se juntan los duplicados que ya hubiera: las líneas del
  repetido pasan al anexo que se conserva (el de id menor) y el repetido se borra.
- `empresas.token_anexos_hash`: sha256 del token de la API externa (el token en claro no se guarda).
- `compras_agiles.documentos`: `[{id, nombre}]` de `documentos[]` del detalle (antes solo se
  guardaba `n_documentos`): los lista `GET /api/anexos/pendientes` y los usa la descarga directa.

Revision ID: e7c2a9f4b1d8
Revises: a3c8e5d1b7f2
Create Date: 2026-11-05 23:59:50.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7c2a9f4b1d8"
down_revision: str | None = "a3c8e5d1b7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUENTES = "('manual', 'masiva', 'api_externa', 'api_mp', 'descarga_directa')"


def upgrade() -> None:
    op.alter_column(
        "ca_lineas",
        "estado",
        type_=sa.String(12),
        existing_type=sa.String(10),
        existing_nullable=False,
    )

    op.add_column(
        "ca_anexos",
        sa.Column("fuente", sa.String(16), nullable=False, server_default="manual"),
    )
    op.create_check_constraint(op.f("ck_ca_anexos_fuente"), "ca_anexos", f"fuente IN {_FUENTES}")
    op.add_column(
        "ca_anexos",
        sa.Column(
            "empresa_id",
            sa.BigInteger(),
            sa.ForeignKey("empresas.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "ca_anexos",
        sa.Column("extraccion_respaldo", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Duplicados previos (mismo archivo subido dos veces a la misma CA): sus líneas pasan al
    # anexo que se conserva y el repetido se borra. Sin esto el UNIQUE no se puede crear.
    op.execute(
        """
        WITH conservado AS (
            SELECT id, MIN(id) OVER (PARTITION BY ca_codigo, sha256) AS keep_id
            FROM ca_anexos
        )
        UPDATE ca_lineas l SET anexo_id = c.keep_id
        FROM conservado c
        WHERE l.anexo_id = c.id AND c.id <> c.keep_id
        """
    )
    op.execute(
        """
        DELETE FROM ca_anexos a
        USING ca_anexos b
        WHERE a.ca_codigo = b.ca_codigo AND a.sha256 = b.sha256 AND a.id > b.id
        """
    )
    op.create_unique_constraint(
        op.f("uq_ca_anexos_ca_codigo"), "ca_anexos", ["ca_codigo", "sha256"]
    )

    op.add_column("empresas", sa.Column("token_anexos_hash", sa.String(64), nullable=True))
    op.add_column("compras_agiles", sa.Column("documentos", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("compras_agiles", "documentos")
    op.drop_column("empresas", "token_anexos_hash")
    op.drop_constraint(op.f("uq_ca_anexos_ca_codigo"), "ca_anexos", type_="unique")
    op.drop_column("ca_anexos", "extraccion_respaldo")
    op.drop_column("ca_anexos", "empresa_id")
    op.drop_constraint(op.f("ck_ca_anexos_fuente"), "ca_anexos", type_="check")
    op.drop_column("ca_anexos", "fuente")
    # `reemplazada` no cabe en String(10): vuelve a `ok` (la línea de la API, tal como estaba).
    op.execute("UPDATE ca_lineas SET estado = 'ok' WHERE estado = 'reemplazada'")
    op.alter_column(
        "ca_lineas",
        "estado",
        type_=sa.String(10),
        existing_type=sa.String(12),
        existing_nullable=False,
    )
