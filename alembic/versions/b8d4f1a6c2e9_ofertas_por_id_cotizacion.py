"""Una fila por OFERTA de Compra Ágil, no por RUT (F-merc-1c §3.1).

Un mismo RUT manda varias ofertas válidas y distintas a una CA [V, sonda 4-nov-2026]; el
UNIQUE (ca_codigo, llamado, rut_proveedor) descartaba precios reales. Pasa a
(ca_codigo, llamado, id_cotizacion). Las filas sin `id_cotizacion` reciben la clave
sustituta "rut:<rut>" (la misma que usa la escritura): con una fila por RUT, como hasta
hoy, no puede chocar. `llamado` nulo sigue sin chocar en el UNIQUE, igual que antes.

`batch_alter_table`: en Postgres son ALTER directos; en SQLite (tests) recrea la tabla.

Downgrade: restaura el UNIQUE por RUT y falla con mensaje claro, sin borrar nada, si hay
ofertas que lo violarían; vuelve a NULL las claves sustitutas.

Revision ID: b8d4f1a6c2e9
Revises: a7c3e9d1b4f2
Create Date: 2026-11-04 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d4f1a6c2e9"
down_revision: str | None = "a7c3e9d1b4f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLA = "ca_cotizaciones"
_UQ_RUT = "uq_ca_cotizacion"
_UQ_OFERTA = "uq_ca_cotizacion_oferta"
# Clave sustituta de `id_cotizacion` nulo: misma que `app/ingest/mercado.py`.
_PREFIJO_SUSTITUTA = "rut:"


def _repetidos(columna: str) -> int:
    """Grupos (ca_codigo, llamado, `columna`) con más de una fila; llamado nulo no choca."""
    sql = sa.text(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM {_TABLA} "  # nombres fijos, sin entrada externa
        f"WHERE llamado IS NOT NULL AND {columna} IS NOT NULL "
        f"GROUP BY ca_codigo, llamado, {columna} HAVING COUNT(*) > 1) AS r"
    )
    return int(op.get_bind().execute(sql).scalar_one())


def upgrade() -> None:
    op.execute(
        sa.text(
            f"UPDATE {_TABLA} SET id_cotizacion = :prefijo || rut_proveedor "
            "WHERE id_cotizacion IS NULL"
        ).bindparams(prefijo=_PREFIJO_SUSTITUTA)
    )
    n = _repetidos("id_cotizacion")
    if n:
        raise RuntimeError(
            f"{n} grupos (ca_codigo, llamado, id_cotizacion) repetidos en {_TABLA}: "
            "revisar a mano antes de crear el UNIQUE por oferta (nada se cambió)."
        )
    with op.batch_alter_table(_TABLA) as batch:
        batch.drop_constraint(_UQ_RUT, type_="unique")
        batch.create_unique_constraint(_UQ_OFERTA, ["ca_codigo", "llamado", "id_cotizacion"])


def downgrade() -> None:
    n = _repetidos("rut_proveedor")
    if n:
        raise RuntimeError(
            f"No se puede volver al UNIQUE por RUT: {n} grupos (ca_codigo, llamado, "
            f"rut_proveedor) tienen más de una oferta en {_TABLA}. El downgrade no borra "
            "datos: decidir a mano qué ofertas quitar y repetir."
        )
    with op.batch_alter_table(_TABLA) as batch:
        batch.drop_constraint(_UQ_OFERTA, type_="unique")
        batch.create_unique_constraint(_UQ_RUT, ["ca_codigo", "llamado", "rut_proveedor"])
    op.execute(
        sa.text(f"UPDATE {_TABLA} SET id_cotizacion = NULL WHERE id_cotizacion LIKE :patron")
        .bindparams(patron=_PREFIJO_SUSTITUTA + "%")
    )
