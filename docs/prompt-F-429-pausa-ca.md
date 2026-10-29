# Prompt de implementación — F-429-pausa-ca (un job que arranca en pausa espera o se omite, no falla) · 29-oct-2026

> Para Claude Code. **Modelo: Opus** (cuota/429). Una fase = un commit, **sin push**. Sin migración.
> Requisito: F-429-tasa (`5ae770d`) en `main`. Origen: auditoría de F-429-tasa, 29-oct.

## 0. Antes de escribir código

### 0.a Por qué
Con F-429-tasa, un 429 de tasa deja `sync_state['api_pausa']` vigente 20 min (60 si el código es
desconocido). Si el job siguiente **arranca** dentro de esa pausa, `check_budget` lanza
`APIPausadaError` en la primera request: `_sync_en_ventanas` la re-lanza (`ventanas == 0`), el CLI
sale 1 y Actions queda en **rojo con 0 requests**, aunque el sistema hizo exactamente lo que debía.

Caso esperable [I]: el límite parece ser de volumen por hora (~110–190 requests, ver
`docs/15-diagnostico-ca-429.md` §6). `ca` (:05) gasta ~110; `ciclo-match` (:50) corre
`detalles-match`, recibe el 429 y deja pausa hasta ~:10 → el `ca` de las :05 siguiente **pierde la
hora entera** y sale rojo. Repetido, `ca` avanza cada 2 h en vez de cada hora y el rojo deja de
significar "algo anda mal".

### 0.b Lectura (sin cambios)
- `app/ingest/__main__.py`: `cmd_run_once`, `_locked`, `dispatch`, la salida `omitido` (hoy solo por
  lock) y `_cortes` (F-429-tasa).
- `app/ingest/orchestrator.py`: `_run_with_lock` (`propagar`, fila `omitido` en `job_runs`,
  `_registrar_corrida_segura`), `_adquirir_lock` (patrón de espera sin retener conexión),
  `_make_clients`, `run_sync_ca`.
- `app/clients/base.py`: `QuotaTracker.pausada_hasta()`, `check_budget`, `APIPausadaError`.
- `app/ingest/compra_agil.py`: `sync_incremental` y el cierre parcial de `_sync_en_ventanas`.
- `.github/workflows/ca.yml` (`timeout_min: 90`, `esperar_lock_min: 25`), `ciclo-match.yml`,
  `ciclo-activas.yml`, `_job.yml` (loop de jobs, `sleep 60` entre jobs).

## 1. Cambios

### 1.1 Chequeo de pausa antes del lock (CLI)
En `cmd_run_once`, **antes** de `_run_with_lock`, para los jobs que usan la API:
- Leer `pausada_hasta()` con un `QuotaTracker` (o una función de módulo que lea la misma fila; no
  duplicar el SQL).
- Sin pausa vigente → corre como siempre.
- Pausa vigente que vence en ≤ `ESPERA_PAUSA_MAX_MIN` (nuevo en `Settings`, default **15**) →
  log INFO "API en pausa hasta HH:MM Chile; espero N min", dormir hasta el vencimiento + 30 s y
  correr. **Sin retener conexión** mientras duerme (mismo criterio que `_adquirir_lock`: Neon y
  `pool_size ≤ 5`). Releer la pausa al despertar: si otro proceso la alargó, volver a decidir con
  el tiempo que quede (no esperar más de `ESPERA_PAUSA_MAX_MIN` en total).
- Pausa que vence más tarde → **omitido**: `::warning::[job] omitido: API en pausa hasta HH:MM Chile`,
  fila `omitido` en `job_runs` (por el camino de `_registrar_corrida_segura`, no con SQL nuevo),
  sale **0**.
- Lista explícita `_JOBS_CON_API` en `__main__.py`, verificada contra cada runner: al menos `ca`,
  `activas`, `detalles`, `lifecycle`, `detalles-match`. **No** incluir `nocturno` ni
  `estados-vencidos` (tienen pasos sin API —datos abiertos, competencia— que deben correr igual; sus
  pasos con API ya cortan limpio ante `APIPausadaError` desde F-429-tasa). Tampoco `match`, `alerts`,
  `resumen`, `retencion`, `datos-abiertos`, `competencia`, `vocabulario-rubros`, `catalogos` si se
  confirma que no llaman a la API (`catalogos` y `plan-anual`: verificar).
- La espera cuenta para el `timeout_min` del workflow: `ca` tiene 90 min, 25 de lock + 15 de pausa +
  ~40 de trabajo cabe. Verificar que `ciclo-match` y `ciclo-activas` también tengan margen; si no,
  anotar en el resumen de la fase (no tocar timeouts sin decirlo).

### 1.2 Pausa que aparece entre el chequeo y la primera request
Si la pausa la pone otro proceso **después** del chequeo de §1.1 y `ca` recibe `APIPausadaError` con
`ventanas == 0`, el resultado debe ser el mismo **omitido** (sale 0 con `::warning::`), no error.
En cambio, un **`MPTasaError` con `ventanas == 0`** (la API nos rechazó en nuestra propia primera
request) **sigue siendo error** (rojo): es la señal de que algo cambió afuera.
Implementarlo sin que `_run_with_lock` conozca `APIPausadaError` si se puede (p. ej. el runner
devuelve un resultado `{"omitido_por": "APIPausadaError", ...}` y el CLI lo trata como omitido); si
no, un `except APIPausadaError` explícito en `_run_with_lock` solo cuando `propagar=True`, con fila
`omitido`. Elegir lo más simple y justificarlo en el commit.

### 1.3 Docs (mismo commit)
- `docs/operacion.md` §7 y `docs/02-arquitectura-y-operacion.md`: un job con API que arranca en pausa
  espera hasta 15 min o se omite (sale 0, `::warning::`); `nocturno` no espera, sus pasos cortan.
- `docs/00-estado.md`: fase en producción al cerrar; sacar del backlog lo que esta fase resuelva.
- `CLAUDE.md`: **sin cambios** (la regla 3 ya dice "cortar la corrida guardando el avance").
- `app/changelog.py`: sin entrada (no hay cambio visible en la web).

## 2. Tests (sin red; reloj y `sleep` inyectables, como en `_adquirir_lock`)
1. Sin pausa → el job corre, no duerme.
2. Pausa que vence en 10 min → duerme ~10,5 min (sleep falso) y corre; ninguna conexión retenida
   durante la espera (contar checkouts del pool o el patrón que usen los tests de `_adquirir_lock`).
3. Pausa que vence en 40 min → no corre, `job_runs` = `omitido`, `::warning::` en stdout, exit 0.
4. Pausa alargada por otro proceso mientras se espera → recalcula; si el total pasa de 15 min, omite.
5. `ca` con `APIPausadaError` en la primera request (pausa puesta después del chequeo) → omitido, exit 0.
6. `ca` con `MPTasaError` en la primera request → error, exit 1 (no cambia).
7. `nocturno` con pausa vigente → no espera ni se omite; los pasos sin API corren.
8. Job sin API (`match`) con pausa vigente → corre normal.

## 3. Cierre
- `ruff check .`, `python -m mypy app`, `python -m pytest -rs` contra Neon dev con
  `DATABASE_URL=postgresql+psycopg://…`: 0 fallos, 0 errores, 0 saltados.
- `git add` solo de los archivos de la fase y este prompt.
- Commit: `F-429-pausa-ca: un job con API que arranca en pausa espera hasta 15 min o se omite`. Sin push.

*Fuente de los datos de dominio: Dirección ChileCompra.*
