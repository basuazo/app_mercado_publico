# Prompt — F-plat-6-fix (inserciones por lote en una sola sentencia, avance visible y carga completa desde Actions) · 5-nov-2026

> Para Claude Code. **Modelo: Sonnet** (arreglo acotado, sin esquema). Un commit, **sin migración**, sin push.
> Lo marcado [V] está verificado; [I] es inferido.

## Contexto
- `mercado-demanda --completo` corrido desde el PC de Boris contra Neon prod (5-nov, 18:58) pasó 24 min sin terminar
  y se cortó con Ctrl+C dentro de `recalcular_series`, en `session.execute(insert(DemandaSerie), filas[i : i + LOTE])`
  → `do_executemany` → `psycopg cursor.executemany` [V, traceback]. No imprimió nada entre «iniciando» y el corte.
- Mismo patrón en `mercado-competencia --completo` desde el PC: 7.307 + 1.412 filas en 434 s de recálculo ≈ 50 ms por
  fila [V, log del 5-nov], que calza con **un viaje de ida y vuelta Chile → Neon (us-east) por fila** [I].
  `demanda_series` de 26 meses tiene muchas más filas, de ahí las horas.
- Los workflows no permiten pedir la carga completa: `_job.yml` no tiene input para `--completo` [V] y
  `mercado-indices.yml` dice que la carga completa se corre desde el PC.

## 1. Inserción en una sentencia por lote
- Reemplazar `session.execute(insert(Modelo), lista_de_dicts)` (executemany) por
  `session.execute(insert(Modelo).values(lote))` (un solo `INSERT ... VALUES (...), (...), ...` por lote) en:
  `app/mercado/demanda.py` (`DemandaSerie`, `DemandaNombre`, `Pronostico`, `EvaluacionModelo`) y
  `app/mercado/competencia.py` (`CompetenciaMes`, `CompetenciaCompradorMes` si usa el mismo patrón,
  `EvaluacionModelo`). Buscar el patrón en `app/mercado/` y `app/ingest/` (acotar rutas; no `grep` sobre `.venv`) y
  aplicarlo donde se inserten más de ~100 filas; anotar en el commit la lista.
- Tamaño de lote: que `filas × columnas` quede bajo el límite de parámetros (Postgres 65.535; SQLite según versión):
  usar una función común `lotes_para(modelo, n_columnas)` o un tope fijo (p. ej. 500 filas) y documentarlo.
- Revisar también actualizaciones fila por fila en caminos de `--completo` (p. ej. comunas de `mercado-indices`:
  7.969 «cambiadas» en la carga del 5-nov, que tardó 19 min desde el PC). Si son `UPDATE` uno por uno, pasarlos a un
  `UPDATE ... FROM (VALUES ...)` por lote o anotarlo como pendiente si el cambio no es trivial.
- Test: con un listener `before_cursor_execute`, insertar N > lote filas genera `ceil(N/lote)` sentencias y ninguna
  `executemany`.

## 2. Avance visible
Logs INFO por etapa en `run_demanda` (y en `recalcular_competencia`): recorrido de OC (cada 50.000 líneas y al
final, con conteos), series por mes («2025-03: 4.210 filas, 3,1 s»), nombres, pronósticos, backtest por mes,
recomendaciones por empresa, y el tiempo de cada etapa en el resultado (`segundos_por_etapa`). Así una corrida larga
se puede seguir y cortar sabiendo dónde iba.

## 3. Carga completa desde GitHub Actions
- `_job.yml`: input opcional `completo` (boolean, default false) que agrega `--completo` a los jobs que lo aceptan
  (`mercado-indices`, `mercado-competencia`, `mercado-demanda`, `mercado-catalogo` con `--inicial` NO: no mezclar).
  Los demás jobs lo ignoran sin error.
- `mercado-indices.yml`: `workflow_dispatch` con input `completo` (boolean, default false) pasado a `_job.yml`;
  cuando es true, `timeout_min` 180. cron-job.org sigue disparando sin inputs (false).
- Actualizar el comentario del workflow y `operacion-disparos.md`: la carga completa se lanza desde la pestaña
  Actions → mercado-indices → Run workflow → completo ✔ (de día, sin migración pendiente).
- `tests/test_workflows.py`: el input existe, default false, y no cambia lo que hace el disparo de cron-job.org.

## 4. Cierre
`ruff check .`, `python -m mypy app`, `python -m pytest -rs` completo contra Postgres local (lo corre Boris), sin
saltados. Sin changelog (no es visible). Commit: `F-plat-6-fix: inserciones por lote, avance visible y carga completa
desde Actions`. Incluir este prompt en el commit.

## 5. Pasos de Boris
1. Push (sin migración: no hace falta pausar nada).
2. GitHub → Actions → `mercado-indices` → Run workflow → marcar `completo` → Run. Mirar el log por etapas; pegar el
   resumen de `mercado-demanda` (`medicion`, `backtest`, `segundos_por_etapa`, `duracion_s`).
