"""Guardar unificado: "me sirve" pasa a ser una oportunidad guardada (F-guardar).

Solo datos, sin cambio de esquema de la app. Por cada `match_feedback` con
`valor='sirve'`:
- sin `oportunidades_seguidas` para (usuario, fuente, código) → se crea, con
  `estado_visto` = estado ACTUAL de la oportunidad ('' si no existe) y
  `archivada=False`, para que `detectar_cambio_estado_seguidas` no alerte;
- con seguida (activa o archivada) → se deja como está;
- la fila `sirve` se borra.
Las creadas cuya oportunidad cierra en < 48 h llevan su alerta
`seguimiento_cierre` ya en `enviada`: no se manda correo (la persona ya la tenía
marcada). La migración NO borra matches: eso lo hace el primer `ciclo-match`.

Registro para el downgrade: `_mig_guardar_creadas` guarda TODAS las `sirve`
migradas; `seguida_id` es NULL cuando la seguida ya existía. El downgrade recrea
las `sirve` y borra solo las seguidas creadas acá.

Idempotente: sin filas `sirve`, el upgrade no hace nada.

Revision ID: c9e4b2f7a1d8
Revises: b5d1f8a3c6e2
Create Date: 2026-10-01 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "c9e4b2f7a1d8"
down_revision: str | None = "b5d1f8a3c6e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hora de la base como la guarda la app: naive en UTC (app/core/tiempo.ahora_utc).
_AHORA = "(now() AT TIME ZONE 'UTC')"

_CREAR_REGISTRO = """
CREATE TABLE IF NOT EXISTS _mig_guardar_creadas (
    seguida_id bigint NULL,
    usuario_id bigint NOT NULL,
    fuente varchar(30) NOT NULL,
    codigo varchar(50) NOT NULL,
    creado_en timestamp NOT NULL
)
"""

# 1. Las `sirve` que ya tenían seguida (activa o archivada): solo se registran.
_REGISTRAR_EXISTENTES = """
INSERT INTO _mig_guardar_creadas (seguida_id, usuario_id, fuente, codigo, creado_en)
SELECT NULL, f.usuario_id, f.fuente, f.codigo_oportunidad, f.creado_en
FROM match_feedback f
WHERE f.valor = 'sirve'
  AND EXISTS (
      SELECT 1 FROM oportunidades_seguidas s
      WHERE s.owner_id = f.usuario_id
        AND s.fuente = f.fuente
        AND s.codigo_oportunidad = f.codigo_oportunidad
  )
"""

# 2. Las `sirve` sin seguida: se crea la seguida con el estado actual y se registra.
_CREAR_SEGUIDAS = f"""
WITH nuevas AS (
    INSERT INTO oportunidades_seguidas
        (owner_id, fuente, codigo_oportunidad, estado_visto, archivada, creado_en, actualizado_en)
    SELECT
        f.usuario_id,
        f.fuente,
        f.codigo_oportunidad,
        COALESCE(CASE f.fuente WHEN 'licitaciones' THEN l.estado ELSE c.estado END, ''),
        false,
        f.creado_en,
        {_AHORA}
    FROM match_feedback f
    LEFT JOIN licitaciones l
        ON f.fuente = 'licitaciones' AND l.codigo = f.codigo_oportunidad
    LEFT JOIN compras_agiles c
        ON f.fuente = 'compras_agiles' AND c.codigo = f.codigo_oportunidad
    WHERE f.valor = 'sirve'
      AND NOT EXISTS (
          SELECT 1 FROM oportunidades_seguidas s
          WHERE s.owner_id = f.usuario_id
            AND s.fuente = f.fuente
            AND s.codigo_oportunidad = f.codigo_oportunidad
      )
    RETURNING id, owner_id, fuente, codigo_oportunidad, creado_en
)
INSERT INTO _mig_guardar_creadas (seguida_id, usuario_id, fuente, codigo, creado_en)
SELECT id, owner_id, fuente, codigo_oportunidad, creado_en FROM nuevas
"""

# 3. Recordatorio de cierre ya "enviado" para las creadas que cierran en < 48 h.
_CIERRE_ENVIADO = f"""
INSERT INTO alertas (match_id, seguimiento_id, tipo, enviada_en, canal, estado, intentos_envio, max_intentos)
SELECT NULL, m.seguida_id, 'seguimiento_cierre', {_AHORA}, 'email', 'enviada', 0, 3
FROM _mig_guardar_creadas m
LEFT JOIN licitaciones l ON m.fuente = 'licitaciones' AND l.codigo = m.codigo
LEFT JOIN compras_agiles c ON m.fuente = 'compras_agiles' AND c.codigo = m.codigo
WHERE m.seguida_id IS NOT NULL
  AND COALESCE(l.fecha_cierre, c.fecha_cierre)
      BETWEEN {_AHORA} AND {_AHORA} + interval '48 hours'
  AND NOT EXISTS (
      SELECT 1 FROM alertas a
      WHERE a.seguimiento_id = m.seguida_id AND a.tipo = 'seguimiento_cierre'
  )
"""

_BORRAR_SIRVE = "DELETE FROM match_feedback WHERE valor = 'sirve'"

# Downgrade: recrea las `sirve` (salvo que ya haya feedback para esa oportunidad o
# el usuario ya no exista) y borra solo las seguidas creadas por el upgrade.
_RESTAURAR_SIRVE = f"""
INSERT INTO match_feedback (usuario_id, fuente, codigo_oportunidad, valor, creado_en, actualizado_en)
SELECT m.usuario_id, m.fuente, m.codigo, 'sirve', m.creado_en, {_AHORA}
FROM _mig_guardar_creadas m
WHERE EXISTS (SELECT 1 FROM usuarios u WHERE u.id = m.usuario_id)
  AND NOT EXISTS (
      SELECT 1 FROM match_feedback f
      WHERE f.usuario_id = m.usuario_id
        AND f.fuente = m.fuente
        AND f.codigo_oportunidad = m.codigo
  )
"""

_BORRAR_CREADAS = """
DELETE FROM oportunidades_seguidas
WHERE id IN (SELECT seguida_id FROM _mig_guardar_creadas WHERE seguida_id IS NOT NULL)
"""


def migrar_sirve(conn: Connection) -> None:
    """Cuerpo del upgrade, sobre una conexión (los tests lo llaman directo)."""
    conn.execute(sa.text(_CREAR_REGISTRO))
    hay_sirve = conn.execute(
        sa.text("SELECT 1 FROM match_feedback WHERE valor = 'sirve' LIMIT 1")
    ).first()
    if hay_sirve is None:
        return
    conn.execute(sa.text(_REGISTRAR_EXISTENTES))
    conn.execute(sa.text(_CREAR_SEGUIDAS))
    conn.execute(sa.text(_CIERRE_ENVIADO))
    conn.execute(sa.text(_BORRAR_SIRVE))


def revertir_sirve(conn: Connection) -> None:
    """Cuerpo del downgrade, sobre una conexión."""
    existe = conn.execute(sa.text("SELECT to_regclass('_mig_guardar_creadas')")).scalar()
    if existe is None:
        return
    conn.execute(sa.text(_RESTAURAR_SIRVE))
    conn.execute(sa.text(_BORRAR_CREADAS))
    conn.execute(sa.text("DROP TABLE _mig_guardar_creadas"))


def upgrade() -> None:
    migrar_sirve(op.get_bind())


def downgrade() -> None:
    revertir_sirve(op.get_bind())
