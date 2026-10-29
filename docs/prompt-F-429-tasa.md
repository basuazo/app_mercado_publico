# Prompt de implementación — F-429-tasa (el 429 "límite de tasa" deja de bloquear el día) · 29-oct-2026

> Para Claude Code. **Modelo: Opus** (toca cuota/429 y una regla dura de `CLAUDE.md`).
> Una fase = un commit, **sin push**. Sin migración. Origen: `docs/15-diagnostico-ca-429.md`.

## 0. Antes de escribir código

### 0.a Por qué (todo [V] en logs de Actions, 18–29 oct)
- Desde ~18-oct la API responde a veces **HTTP 429 con `{"errors":[{"codigo":"429","mensaje":"Se ha
  superado el límite de tasa de solicitudes. Intente nuevamente más tarde."}]}`**, sin `Retry-After`
  ni cabeceras de cuota.
- Como no es 10500, `BaseClient._request` lo trata como tope diario → `QuotaTracker.marcar_agotado()`
  → todos los jobs del resto del día Chile cortan en `check_budget` ("quedan 0"). `ca` avanza ~10 min
  al día y el cursor de CA va ~2 días atrasado. `ciclo-match` y `nocturno` salen **verdes sin haber
  hecho nada**.
- **No es tope diario**: llegó con 8 requests (23-oct) y 21 (24-oct) en la primera corrida del día; otro
  día llegó tras ~1.700. Prueba del 29-oct: tras el 429 de las 01:05 y un `UPDATE` manual de
  `quota_log`, `ca` hizo **113 requests con 200** el mismo día y recién ahí volvió el 429.
- Forma del límite [I]: llega tras ~110–190 requests seguidas, o con pocas si hubo ráfaga en la hora
  previa. Compatible con ventana deslizante de ~1 h o con limitador por IP compartida de los runners.
- El tope diario de 10.000 **nunca se ha visto** en un 429 real: ningún código de "tope diario" está
  verificado.

### 0.b Lectura (sin cambios)
`CLAUDE.md` (regla 3); `docs/15-diagnostico-ca-429.md`. Código:
- `app/clients/base.py`: jerarquía `MPError` (`MPRateLimitError`, `MPConcurrencyError`,
  `QuotaExceededError`), `_codigo_de_error`, `_CODIGO_CONCURRENCIA`, `_handle_response` (rama 429),
  `_request` (except `MPConcurrencyError` / `MPRateLimitError`), `QuotaTracker` (`remaining`,
  `check_budget`, `marcar_agotado`), `RateLimiter.enfriar`.
- `app/ingest/compra_agil.py`: `sync_incremental` (except `MPRateLimitError`) y `_sync_en_ventanas`
  (cierre parcial ante `MPServerError` con `ventanas > 0` → `cortado_por_error`).
- `app/ingest/orchestrator.py`: `_ERRORES_DE_CANAL`, `_run_with_lock`, `_ciclo_nocturno` y el
  corte de `detalles-match` ante error de canal (≈ línea 421–483).
- `app/ingest/licitaciones.py:245`, `app/ingest/lifecycle.py:89/133/266`: cortan ante
  `(MPRateLimitError, QuotaExceededError, MPAuthError)`.
- `app/ingest/__main__.py`: salida del CLI (`[job] ERROR`, `omitido`, `print(result)`).
- `app/models/tables.py`: `SyncState` (`fuente` PK, `cursor` String(500), `notas`).
- Tests: `test_cuota.py`, `test_429_concurrencia.py`, `test_datos_1.py` (429 diario persistido),
  `test_ca_ventana_ingesta.py`, `test_detalles_match.py`.

## 1. Cambios

### 1.1 Clasificación del 429 (`base.py`)
| 429 con… | Clase | Qué hace `_request` |
|---|---|---|
| `codigo` 10500 | `MPConcurrencyError` (sin cambios) | 3 reintentos 30/60/120 s y corta |
| `codigo` 429 [V] | **`MPTasaError(MPRateLimitError)`** nueva | **no reintenta**, persiste una **pausa** de `PAUSA_TASA_MIN` (default 20) y re-lanza |
| otro código o sin código | `MPTasaError` | igual, pausa de `PAUSA_429_DESCONOCIDO_MIN` (default 60) y log **ERROR** "429 con código no visto: …" |

- Si viene `Retry-After`, la pausa es `max(Retry-After, pausa configurada)`.
- **Ningún 429 llama a `marcar_agotado()`**. Motivo: el código de tope diario no está verificado y un
  falso positivo cuesta un día entero de ingesta. `marcar_agotado` se queda (con su test) y se
  documenta como "sin uso hasta verificar un código de tope diario". El 429 sigue contando en
  `quota_log` como cualquier request emitida.
- `MPTasaError` es subclase de `MPRateLimitError` **a propósito** (mismo patrón que `MPConcurrencyError`):
  los runners que ya cortan ante `MPRateLimitError` siguen cortando sin conocerla.
- Constantes nuevas en `Settings`: `pausa_tasa_min: int = 20`, `pausa_429_desconocido_min: int = 60`
  (no se agregan a `_job.yml`; bastan los defaults).

### 1.2 Pausa persistida (`QuotaTracker`)
- La pausa vive en Postgres (regla 10: el proceso es desechable): fila de `sync_state` con
  `fuente="api_pausa"`, `cursor` = ISO-8601 UTC de "pausada hasta", `notas` = causa
  (`"429/429"`, `"429/desconocido:<código>"`). **Sin migración.** Upsert idempotente; una pausa más
  corta nunca acorta una más larga en curso (igual que `RateLimiter.enfriar`).
- `QuotaTracker.pausar(hasta, causa)` y `QuotaTracker.pausada_hasta() -> datetime | None`.
- `check_budget` revisa la pausa **antes** del presupuesto: si `ahora < pausada_hasta` lanza
  **`APIPausadaError(QuotaExceededError)`** nueva ("API en pausa por 429 hasta HH:MM Chile"), sin emitir.
  Subclase de `QuotaExceededError` a propósito: todos los llamadores ya cortan limpio ante ella.
- Además, `_request` enfría el `RateLimiter` del proceso por la duración de la pausa (por si el
  mismo proceso sigue con otro paso del nocturno). **No** se duerme esperando: dormir 20 min en un
  runner quema minutos de Actions; el job corta y la siguiente corrida retoma.
- SQLite (tests) y Postgres: misma lógica; reloj inyectable (`now_fn`) como ya tiene `QuotaTracker`.

### 1.3 `ca` termina con avance parcial (`compra_agil.py`)
- En `_sync_en_ventanas`, `MPTasaError` y `APIPausadaError` se tratan como el `MPServerError` de hoy:
  con `ventanas > 0` la corrida termina **OK** con `cortado_por_error="MPTasaError"` (o
  `"APIPausadaError"`), cursor en la última ventana completa; con `ventanas == 0` se re-lanza.
- `sync_incremental`: el log del `except MPRateLimitError` deja de decir "tope diario"; distinguir
  tasa vs concurrencia en el mensaje.

### 1.4 Que el corte se vea (CLI)
- Hoy `detalles-match` y los pasos del `nocturno` cortan por cuota/pausa y el job sale verde sin decir
  nada. En `app/ingest/__main__.py`: si el resultado (o alguno de los resultados de pasos del
  nocturno, si están a mano sin refactor grande) trae `cortado_por_error` u otra marca de corte por
  canal, imprimir `::warning::[job] cortado por <causa>; avance parcial guardado`. Sale 0.
- Si para `detalles-match` y `lifecycle` hoy no hay marca en el resultado, agregar
  `cortado_por: str | None` al dict que devuelven (nombre del tipo de excepción de canal). Alcance
  mínimo: `ca`, `detalles-match`, `estados-vencidos`. Si algún runner obliga a refactor grande,
  dejarlo anotado en el backlog de `00-estado.md` en vez de hacerlo.

### 1.5 Docs (mismo commit)
- `CLAUDE.md` regla 3, reemplazar la parte de 429 por:
  > 429 con Codigo 10500 (peticiones simultáneas) = concurrencia: máx. 3 reintentos con backoff
  > (30/60/120 s) y luego cortar el job. 429 con codigo 429 ("límite de tasa", verificado 29-oct-2026)
  > = transitorio: sin reintento, pausa persistida en Postgres de 20 min, cortar la corrida guardando
  > el avance. 429 con cualquier otro código = igual que el de tasa con pausa de 60 min y log ERROR.
  > Ningún 429 marca el día como agotado: no hay código de tope diario verificado. Tras un 504 o un
  > timeout, enfriar 60 s antes de cualquier request.
  (Mantener la frase del 504/timeout y el presupuesto de 9.000.)
- `docs/decisiones.md`: decisión nueva con fecha, la evidencia de §0.a y "reemplaza a 'otro 429 = tope
  diario'".
- `docs/01-analisis-api-mercado-publico.md`: tabla de errores (línea ~144) y §10 hallazgo [V] del
  429/429. `docs/02-arquitectura-y-operacion.md` (~45) y `docs/operacion.md` §7: misma corrección.
- `docs/00-estado.md`: F-429-tasa en producción (al cerrar) y pendiente operativo "decidir si se
  adelanta el cursor de CA" (ver §3).
- `app/changelog.py`: **no** lleva entrada (no hay cambio visible en la web), salvo que se decida
  mostrar la pausa en `/salud` (fuera de alcance; anotar en backlog).

## 2. Tests (respx, sin red; SQLite y PG dev según el patrón de cada archivo)
1. 429 con `{"errors":[{"codigo":"429",…}]}` → `MPTasaError`, sin reintento, cuenta 1 en `quota_log`,
   **no** sube el día a 9.000, deja `sync_state['api_pausa']` en ahora + 20 min.
2. Un `QuotaTracker` nuevo (otra corrida) dentro de la pausa → `check_budget` lanza `APIPausadaError`
   sin emitir request; con el reloj 21 min después → no lanza y emite normal el mismo día Chile.
3. 429 sin código / con código raro (p. ej. 9999) → `MPTasaError`, pausa 60 min, log ERROR con el
   código, día no agotado.
4. 429/10500 sin cambios (los tests de `test_429_concurrencia.py` pasan tal cual).
5. `Retry-After: 3600` en un 429/429 → pausa de 60 min; `Retry-After: 5` → se queda en 20.
6. Pausa más corta no acorta una más larga vigente.
7. `ca`: dos ventanas OK y 429/429 en la tercera → resultado OK con
   `cortado_por_error="MPTasaError"`, cursor en el fin de la segunda; 429/429 en la primera → re-lanza.
8. `ca` arranca dentro de una pausa → `APIPausadaError`, 0 requests emitidas.
9. `detalles-match` ante `MPTasaError` corta el loop y guarda lo hecho (adaptar el parametrizado de
   `test_fetch_detalles_corta_ante_error_de_canal` / `test_lifecycle_corta_ante_error_de_canal`).
10. CLI: resultado con `cortado_por_error` imprime `::warning::` y sale 0.
11. Ajustar (no borrar sin reemplazo) los tests que hoy afirman "429 no-10500 = día agotado"
    (`test_datos_1.py`, `test_cuota.py::test_el_429_sigue_sin_reintentarse`,
    `test_sin_retry_after_se_mantiene_el_fallback_de_medianoche`): ahora afirman la pausa.

## 3. Fuera de alcance (decide Boris)
- **Atraso del cursor de CA** (~2 días al 29-oct): adelantarlo a "ahora − 2 h" y dejar el hueco para un
  relleno nocturno, o dejar que se ponga al día solo (~110 requests/h con `CA_TAMANO_PAGINA=10`).
- Probar H1 (límite por IP de runners): cuando `ca` reciba un 429/429, correr
  `python scripts/smoke_test.py` desde el PC en ese minuto.
- Mostrar "API en pausa hasta …" en `/salud`.

## 4. Cierre
- `ruff check .`, `python -m mypy app`, `python -m pytest -rs` contra Neon dev con
  `DATABASE_URL=postgresql+psycopg://…`: 0 fallos, 0 errores, 0 saltados.
- `git add` solo de los archivos de la fase (código, tests, `CLAUDE.md`, docs tocados y este prompt).
- Commit: `F-429-tasa: el 429 de límite de tasa pausa 20 min en vez de bloquear el día`. Sin push.
- Si se despliega el mismo día de un 429 ya marcado, Boris corre una vez en Neon:
  `UPDATE quota_log SET requests_usadas = <consumo real> WHERE fecha = '<hoy>';`

*Fuente de los datos de dominio: Dirección ChileCompra.*
