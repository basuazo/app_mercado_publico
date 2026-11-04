# Prompt de implementación — F-merc-1-fix (horizonte del backfill y variables en Actions) · 3-nov-2026

> Para Claude Code. **Modelo: Sonnet** (ajustes chicos con contrato claro sobre código ya auditado).
> Una fase = un commit, **sin push**. **Sin migración.** Va sobre `868ccf2` (F-merc-1, auditado por Cowork).

## 0. Antes de escribir código
Leer `CLAUDE.md` (reglas 3, 5, 20, 23, 24), `app/ingest/mercado.py` (`meses_backfill`, `Backfill._mes`,
`Backfill.correr`, `_VACIOS_SOSPECHOSOS`), `app/core/settings.py` (bloque Mercado), `.github/workflows/_job.yml`,
`tests/test_mercado.py` (tests de meses vacíos), `docs/operacion-disparos.md` (cursores del backfill).

## 1. Cambios

### 1.1 "Revisar filtros" debe contar los meses ya terminados
Hoy `correr()` pone `hubo_datos = True` solo cuando `_mes` devuelve `"datos"`. Desde la segunda noche, los meses
recientes vuelven `"saltado"` (cursor `completo`) y no cuentan, así que tres meses viejos vacíos seguidos cortan
con "revisar filtros", se resetean y se vuelven a listar cada noche (~90 requests perdidos).
- `_mes` distingue el motivo del salto: un mes con cursor `completo` o `listado` (ya tuvo CA) cuenta como
  `"datos"` para esta lógica; uno `vacio` o el mes en curso sin CA sigue como `"saltado"` y no suma.
- No cambia lo demás: con 3 meses vacíos al partir y ninguno con datos (ni ya completos), corta igual.
- Test nuevo: con 3 meses `completo` previos en `sync_state` y luego 3 meses que dan 0 CA, los 3 quedan `vacio`,
  el backfill no corta y la noche siguiente no pide ninguno de ellos.

### 1.2 Variables que faltan en `_job.yml`
Agregar al `env` y a la lista de opcionales vacías del `for v in …`: `MERCADO_BACKFILL_ESTADOS_DETALLE`,
`MERCADO_PRODUCTOS_SOLO_GRUPOS` y `MERCADO_BACKFILL_TOPE_BD_MB`. Si `tests/test_workflows.py` lista las variables
de `_job.yml`, actualizarlo.

### 1.3 Horizonte verificado
- Default de `mercado_backfill_primer_mes`: `"2024-11"` (descripción y comentario con la referencia a la sonda).
- `docs/01-analisis-api-mercado-publico.md` §10, agregar [V] (sonda `--mercado` de Boris, 3-nov-2026 ~20:35):
  región 16, días 1–7, sin `estado`: 2025-01 = 362, 2025-04 = 923, 2025-07 = 804, 2025-10 = 970; 2024-10 = 0.
  [I] el inicio de la API está entre 2024-11 y 2025-01 (un 0 no prueba ausencia antes: regla 23). Por eso
  `MERCADO_BACKFILL_PRIMER_MES = 2024-11`: el backfill marca `vacio` lo que no tenga datos (a lo más 2 meses,
  bajo el umbral de "revisar filtros").
- `docs/decisiones.md` (entrada de tamaño de F-merc-1): horizonte real ≈ 24 meses (2024-11 → mes anterior);
  se mantiene el default `MERCADO_PRODUCTOS_SOLO_GRUPOS=true` y el tope de 350 MB; la decisión de alcance
  (solo alimentos o no) se toma tras medir en `/salud` la primera noche.
- Ajustar los tests que asumen el default 2024-10.

## 2. Verificación
`ruff check .`, `python -m mypy app`, `python -m pytest -rs` contra Neon dev con `postgresql+psycopg://`
(0 fallos, 0 errores, 0 saltados). No se piden llamadas reales a la API.

## 3. Cierre
Sin entrada en `app/changelog.py` (no hay cambio visible). `docs/00-estado.md`: F-merc-1 + fix en "commit local
sin push". Commit: `F-merc-1-fix: horizonte del backfill desde 2024-11 y variables de mercado en Actions`.
`git add` solo de los archivos de la fase (no `Claude.md`, `Claude outputs/`, `docs/17-…` ni
`docs/prompt-sonda-H1-429.md`). Pegar `git show --stat HEAD` y la última línea de pytest.

## 4. Pasos de Boris (después de la auditoría)
1. Variable de GitHub `MERCADO_BACKFILL_PRIMER_MES = 2024-11` (Settings → Variables; opcional, ya es el default).
2. Migración `e5b9c3a7d2f4` en Neon prod (`$env:DATABASE_URL` = `DATABASE_URL_PROD`), push.
3. Confirmar que no queda nada más pidiendo a la API (job `ca` antiguo, puente `backfill_cot_nuble.py`, sonda H1)
   y crear en cron-job.org `GH mercado` (cada 15 min) y `GH mercado-nocturno` (22:30).
4. Tras la primera noche: tamaño en `/salud` → decidir el alcance de productos.
