# Prompt de implementación — F-indices (índices y escrituras del matching) · 09-oct-2026

> Para Claude Code. **Modelo: Sonnet.** Una fase = un commit, **sin push**. Origen:
> `docs/14-auditoria-integral.md` §3.4. **No cambia ningún resultado**: mismas oportunidades,
> mismos scores y mismo orden. Solo hace que la base trabaje menos.
> Lee `CLAUDE.md` antes de empezar (reglas 11, 13 y 15; solo `app/models` define el esquema).

## 0. Antes de escribir código
Lee estos archivos:
- `app/models/tables.py`: modelos y el bloque "Índices adicionales" al final.
- `alembic/versions/d7f2a4c8b6e1_plan_anual_busqueda.py`: es el patrón para un índice GIN de expresión. Ese índice se crea en la migración, no en el ORM.
- `app/matching/engine.py`: `_FTS_LIC_INCLUDE/EXCLUDE`, `_FTS_CA_PRODUCTO`, `_FTS_CA_INCLUDE/EXCLUDE`, `_upsert_match`, `match_perfil` y `limpiar_matches_perfil`.
- `app/core/vigencia.py`: `condicion_lic_vigente` y `condicion_ca_vigente`.
- `app/core/retencion.py`.
- `app/alerts/` (lo que borra o lee `alertas`).

## 1. Cambios

### 1.1 Migración solo de índices (la única de la fase)
Los índices se crean con `CREATE INDEX IF NOT EXISTS` y el downgrade los borra con `DROP INDEX IF EXISTS`. Los índices simples se declaran **también** en el bloque "Índices adicionales" de `tables.py`, para que el ORM y la base coincidan. Los de expresión van solo en la migración, con un comentario en `tables.py` que diga dónde viven (mismo criterio que `ix_plan_compra_lineas_desc_tsv`).

| Índice | Para qué |
|---|---|
| `licitacion_items (licitacion_codigo)` | `EXISTS` del recall, `selectinload` y el borrado en cascada (retención y `upsert_detalle`, que borra y reinserta los ítems) |
| `ca_productos (ca_codigo)` | lo mismo para CA |
| `alertas (match_id)` y `alertas (seguimiento_id)` | borrado en cascada desde `oportunidades_match` (limpieza de matches) y desde `oportunidades_seguidas` |
| `oportunidades_match (fuente, codigo_oportunidad)` | búsquedas por oportunidad sin perfil (feed, resumen, Mi registro, detalles-match). `uq_match` empieza por `perfil_id` y no sirve para eso |
| GIN `licitacion_items` sobre `to_tsvector('spanish', inmutable_unaccent(nombre))` | FTS de ítems en el recall y en la exclusión |
| GIN `ca_productos` sobre `to_tsvector('spanish', inmutable_unaccent(nombre \|\| ' ' \|\| descripcion))` | FTS de productos de CA |
| `compras_agiles (fecha_cierre)` y `compras_agiles (fecha_publicacion)` | vigencia de CA: matching, explorador y feed |

- La expresión de cada GIN tiene que ser **textualmente idéntica** a la de `engine.py` (`_FTS_CA_PRODUCTO` y la de ítems). Si no, el planner no usa el índice.
- Verifícalo en dev con `EXPLAIN` sobre la consulta de candidatos de un perfil real y pega el plan en el reporte.
- Si el planner no usa el GIN dentro del `OR EXISTS`, **no** reescribas el recall: anótalo en el reporte y sigue. La reescritura puede ir en otra fase.
- **No** crees índices sobre `lower(trim(estado))`. Normalizar el estado al escribir queda para otra fase.

### 1.2 Upsert de matches en una sola sentencia (`_upsert_match`)
- Hoy se hace un `SELECT` por candidato antes de cada insert o update: hasta ~1.000 por perfil y por ciclo.
- Reemplazarlo por `INSERT … ON CONFLICT ON CONSTRAINT uq_match DO UPDATE SET score = EXCLUDED.score, razones = EXCLUDED.razones`.
- Agregar `WHERE oportunidades_match.score IS DISTINCT FROM EXCLUDED.score OR oportunidades_match.razones IS DISTINCT FROM EXCLUDED.razones`, para no reescribir filas que no cambiaron.
- `RETURNING (xmax = 0) AS insertado` mantiene el contrato de devolver si es nuevo. Ojo: con el `WHERE`, una fila sin cambios no devuelve nada; trátalo como "no nuevo".
- **`fecha_match` solo se fija al insertar**, como hoy. Es un invariante del resumen y tiene que tener test.
- **SQLite (tests):** si la suite usa SQLite para esta ruta, usa el `insert` del dialecto (`sqlalchemy.dialects.postgresql.insert` / `sqlite.insert`) según `session.bind.dialect.name`, o mantén el camino viejo solo para SQLite. Lo que importa es el comportamiento en Postgres.
- Si `match_perfil` acumula los upserts, pueden ir en lotes (`executemany`) de hasta 500 filas.

### 1.3 Medición antes y después
Agrega `data/paso0_indices.py` (gitignored, solo lectura), con el mismo patrón que `data/paso0_auditoria.py`:
- tamaño de cada índice nuevo con `pg_relation_size`;
- tamaño total de la base;
- `EXPLAIN (ANALYZE, BUFFERS)` de la consulta de candidatos de los perfiles 5 y 7. Usa las funciones de `engine.py` para armar el SQL; no lo copies a mano.

Boris lo corre en dev antes y después de la migración, y en producción después del deploy.

## 2. Fuera de alcance
- Fórmula del score, `dias_al_cierre` en `razones`, feed y resumen: eso es **F-match-1**. Ojo: mientras `razones.dias_al_cierre` cambie en cada ciclo, el `WHERE … IS DISTINCT FROM` de §1.2 ahorra poco. El ahorro completo llega con F-match-1.
- Reescribir la limpieza para no repetir el FTS (auditoría §3.4): otra fase.
- N+1 del resumen y de `detector.py`: F-match-1 / backlog.

## 3. Tests
- La migración sube y baja limpia en dev: `alembic upgrade head`, luego `downgrade -1`, luego `upgrade head`.
- `_upsert_match` en Postgres (`@needs_postgres`):
  - inserta y devuelve `True`;
  - un segundo upsert con otro score actualiza, devuelve `False` y **no** cambia `fecha_match`;
  - un tercero con los mismos valores no reescribe (comprobar `xmin` sin cambio o contar filas afectadas).
- `match_perfil` da exactamente los mismos matches, scores y razones que antes sobre el fixture existente de matching. Si hace falta, agrega un test de regresión que compare con un snapshot del resultado anterior.
- Toda la suite de matching y de retención pasa sin cambios en sus aserciones.

## 4. Cierre
- Correr `ruff check .`, `python -m mypy app` y `python -m pytest -rs` completo contra dev con `postgresql+psycopg://` (`alembic upgrade head` antes): 0 fallos, 0 errores, 0 saltados.
- Changelog: **ninguno**, porque el cambio no es visible para la persona. Esta es una excepción explícita a la regla.
- `git add` solo de lo tocado. Commit: `F-indices: índices de FK, FTS de ítems y productos, fechas de CA y upsert de matches en una sentencia`. **Sin push.**
- **Reporte:** hash, archivos tocados, plan `EXPLAIN` antes y después de un perfil, tamaño de los índices en dev, resultado de la suite y "Desvío del prompt".
- **Push (lo hace Boris tras la auditoría):** tiene migración, así que va justo después de un `ca` de los :05. Confirmar `Running upgrade a4e7c2d9f1b3 -> <nueva>` en Render.

*Fuente de los datos de dominio: Dirección ChileCompra.*
