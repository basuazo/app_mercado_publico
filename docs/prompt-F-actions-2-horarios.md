# Prompt F-actions-2 — horarios en GitHub Actions, con `ca` cada hora

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Modelo: el Opus más reciente (`/model`).
>
> **Reescrito el 24-sep-2026.** Reemplaza la versión del 23-sep, que se escribió antes de los
> canarios y suponía un `ciclo-ca` único cada 2 h. Desde entonces:
> - En `main`: F-actions-1 (`4e83a3a`), F-raw-json (`1faf78d`), F-detalles-match (`15b9b0b`) y
>   F-ca-ventana-volumen (`71e4a89`).
> - **Canario 6 (24-sep, 19:38–20:24 UTC): primer ciclo en verde** [V]. `ca` terminó en OK
>   parcial a los 23 min (con `cortado_por_error` tras un 504). `match` tardó 23 s, `alerts`
>   terminó OK y `detalles-match` cortó por tiempo a los 20 min. Conexión Actions ↔ Neon
>   production probada, y ningún secreto en los logs.
> - **Capacidad [V]:** la API entrega ~42 ítems/min. En la mañana hubo ~2000 cambios/h de CA
>   (512 en 15 min). En hora punta, `ca` apenas le gana al ritmo de cambios aunque corra sin
>   parar. Con un ciclo cada 2 h y nada de noche, el atraso crece (6,75 h en el canario 6).
>   Por eso `ca` pasa a su propio workflow, **cada hora, las 24 h**. Los jobs que dependen de los
>   datos (`match`, `alerts`, `detalles-match`) van en otro workflow.
> - **Concurrencia [V, código]:** hay UNA sola llave de advisory lock para todos los jobs (la
>   API cuenta por ticket y castiga lo simultáneo con 429/10500). Hoy, un job que encuentra el
>   lock ocupado sale `omitido` sin esperar. Además, en GitHub un grupo de `concurrency` guarda
>   una sola corrida pendiente: si llega otra, la anterior pendiente se cancela. Con 7 workflows
>   en un solo grupo `mp-jobs` y `ca` cada hora, `resumen` o `nocturno` podrían cancelarse en
>   silencio. Esta fase lo resuelve: un grupo por workflow, y el CLI **espera** el lock un tiempo
>   acotado en vez de omitir de inmediato.
> - cron-job.org ya está pausado (24-sep): hoy nada dispara la ingesta sola.
>
> **Paso 0 (Boris, antes de pegar el prompt):** corre
> `.venv\Scripts\python.exe data\paso0_duraciones.py` y pega la salida al final del mensaje
> para Claude Code. Muestra la duración real de cada job en los últimos 7 días (sobre todo
> `activas`, `detalles`, `nocturno`, `datos_abiertos`, `lifecycle` y `competencia`, que
> todavía no se han medido en Actions). Con eso se ajustan los timeouts.

---

```
Fase F-actions-2. Lee Claude.md antes de empezar: las reglas 3, 5, 10, 13 y 14 están en juego.
Los workflows existentes: `_job.yml` (reutilizable) y `ciclo-ca.yml` (hoy solo manual, corre
"ca match alerts detalles-match"). Contexto y evidencia arriba de este bloque, en
docs/prompt-F-actions-2-horarios.md. Si pegué la salida del Paso 0 (duración de cada job en
producción), úsala para los timeouts. Si no, usa los valores de abajo y dilo en el resumen.

1. ESPERAR EL LOCK EN VEZ DE OMITIR (único cambio en app/).
   a) `_run_with_lock` recibe `esperar_lock_s: int = 0`. Con 0 se comporta exactamente como
      hoy (scheduler, endpoint y Render no cambian). Con > 0, reintenta `pg_try_advisory_lock`
      cada 30 s hasta ese plazo, con log INFO al empezar a esperar y al conseguirlo (con los
      segundos esperados). Si vence el plazo, sale `omitido` como hoy y lo deja escrito en el
      log. No uses el `pg_advisory_lock` bloqueante: sin timeout, un lock colgado dejaría el
      workflow esperando hasta que lo mate su propio timeout.
   b) Mientras espera no debe haber una transacción abierta (el motivo del AUTOCOMMIT de
      F-cuota: Neon mata "idle in transaction").
   c) CLI: `run-once --esperar-lock-min N` (default 0). Aplícalo también a los pasos internos de
      `nocturno` (`_ciclo_nocturno` toma el lock por paso).
   d) `_job.yml`: input nuevo `esperar_lock_min` (number, default 30) que se pasa al CLI en cada
      job. Documenta en el YAML que `timeout_min` debe cubrir la espera más el trabajo.
   e) Tests: con 0 omite de inmediato, como hoy. Con N > 0 consigue el lock cuando se libera
      dentro del plazo, y omite cuando no. Reloj y sleep inyectables, sin esperas reales.

2. CONCURRENCIA DE GITHUB. Cada workflow con su propio grupo:
   `concurrency: { group: mp-${{ github.workflow }}, cancel-in-progress: false }`.
   Así, lo único que GitHub descarta es una segunda corrida pendiente del MISMO workflow (que
   haría lo mismo). La serialización entre workflows distintos la hace el advisory lock con la
   espera del punto 1. Explícalo en un comentario de `_job.yml`.

3. WORKFLOWS Y HORARIOS (cron en UTC, conservar `workflow_dispatch` en todos).

   | Archivo | `jobs:` | cron (UTC) | Chile UTC−3 (hoy) | `timeout_min` | `esperar_lock_min` |
   |---|---|---|---|---|---|
   | `ca.yml` (nuevo)            | `"ca"` | `5 * * * *` | cada hora :05 | 75 | 25 |
   | `ciclo-match.yml` (nuevo)   | `"match alerts detalles-match"` | `50 11,13,15,17,19,21,23 * * *` | 08:50→20:50 c/2 h | 60 | 25 |
   | `ciclo-activas.yml` (nuevo) | `"activas detalles match alerts"` | `15 13,17,22 * * *` | 10:15 / 14:15 / 19:15 | 150 | 30 |
   | `nocturno.yml` (nuevo)      | `"nocturno"` | `10 4 * * *` | 01:10 | 210 | 30 |
   | `retencion.yml` (nuevo)     | `"retencion"` | `40 8 * * *` | 05:40 | 45 | 30 |
   | `catalogos.yml` (nuevo)     | `"catalogos"` | `35 9 * * 1` | lun 06:35 | 45 | 30 |
   | `resumen.yml` (nuevo)       | `"resumen"` | `30 11 * * *` y `30 12 * * *` | 08:30 | 60 | 45 |
   | `ciclo-ca.yml` (existe)     | sin cambios | SIN schedule (queda manual, para canarios) | — | 90 | 0 |

   - Minutos escalonados: ninguna combinación de minuto y hora se repite entre workflows (`ca`
     ocupa el :05 de todas las horas). Déjalo como comentario en cada archivo.
   - Por qué esas horas: `nocturno` puede tener el lock ~3 h desde las 01:10 Chile, así que
     `retencion` y `catalogos` van después, a las 05:40 y 06:35 (06:35 sigue dentro de la
     ventana 22:00–07:00 con UTC−3, y con UTC−4 cae 05:35). `ciclo-activas` no puede estar
     antes de `resumen`: con 150 min de timeout lo dejaría sin lock. Por eso parte a las 10:15.
   - `resumen` no usa la API, pero toma el mismo lock. A las 08:30 casi siempre va a
     encontrar corriendo el `ca` de las 08:05, que dura hasta ~40 min. Por eso espera hasta 45
     min, y el correo puede llegar entre 08:30 y ~08:50. Es aceptable; no cambies la
     semántica del lock para evitarlo. Déjalo como comentario.
   - `nocturno`: 04:10 UTC cae dentro de 22:00–07:00 con los dos offsets de Chile. Verifica
     `en_ventana_nocturna()` antes de darlo por bueno. Su timeout cubre 120 min de
     `detalles-match` (DETALLES_MINUTOS_NOCHE) + datos abiertos, lifecycle, competencia y el
     backfill de ayer + la espera. Ajústalo con el Paso 0: el docstring de `_ciclo_nocturno`
     ya lo pide.
   - `resumen` es el único a prueba del cambio de hora: dos crons y `guard_hora_chile: "08"`
     (ya soportado por `_job.yml`), así que solo corre la entrada que cae a las 08 hora Chile.
     Confirma que concuerda con DIGEST_HOUR=8 y deja un comentario avisando que, si alguien
     cambia DIGEST_HOUR, el guardia queda desfasado.
   - Los demás aceptan el corrimiento de 1 h cuando cambia la hora de Chile. Déjalo dicho en un
     comentario.
   - `ca` cada hora: con CA_MAX_REQUESTS_POR_CORRIDA=150 (~40 min en punta) cabe en la hora. En
     la noche, con poco volumen, se pone al día en pocas corridas. Si una corrida de `ca` espera
     el lock (p. ej. durante `nocturno` o `ciclo-activas`) y se vence el plazo, sale `omitido`:
     es aceptable, porque la siguiente hora vuelve a intentarlo.

4. MONITOR Y CUOTA (solo verificar, no cambiar código salvo que encuentres un error).
   - `/api/salud/jobs` vigila activas, ca, datos-abiertos, resumen, match y alerts. Confirma que
     los nombres con que los graba el CLI en job_runs calzan con esos alias, incluidos los pasos
     internos de `nocturno` (`datos_abiertos`).
   - Estima el consumo diario de requests con el nuevo horario (`ca` hasta 150 × 24 en el peor
     caso, más detalles, activas, etc.) contra el presupuesto de 9.000 (regla 3), y dilo en el
     resumen. Si el peor caso lo pasa, propón el ajuste; no lo apliques.

5. TESTS (tests/test_workflows.py, sin PyYAML, con regex como hoy).
   - Todo job nombrado en un workflow existe en el CLI (ya existe; que siga verde con 8 archivos).
   - Todos tienen `workflow_dispatch`, y todos menos `ciclo-ca.yml` tienen `schedule`.
   - No se repite una combinación de minuto y hora entre workflows distintos (expandiendo listas
     de horas y el `*` de `ca`).
   - Cada workflow usa `group: mp-${{ github.workflow }}` y ninguno comparte grupo fijo.
   - `timeout_min` ≥ `esperar_lock_min` + 10 en cada uno.
   - Ningún workflow trae SECRET_KEY ni JOBS_TOKEN desde secrets (ya existe).

6. LÍMITES.
   - En app/ solo el punto 1. No toques render.yaml, el endpoint ni la lógica de los runners.
   - No apagues el endpoint `/api/jobs/run` ni el scheduler en proceso: eso es F-actions-3.
   - `ruff check .`, `python -m mypy app`, `python -m pytest`. Un commit con prefijo
     `F-actions-2:` y una entrada en app/changelog.py. No toques docs/. No hagas push.
   - NO uses `git add -A` (`_to_delete/` tiene un `.env` con secretos de producción).
   - En el resumen: horario final (UTC y Chile), timeouts y de dónde salió cada uno, estimación
     de requests/día y qué no pudiste verificar.

     ob                       corr   ok  err omit min_prom  min_max  ultima(UTC)
activas                     12    8    0    4      8.9     10.6  2026-09-23 21:10
alerts                      22   12    0   10      0.0      0.1  2026-09-24 20:03
alerts_post_ca               3    3    0    0      0.0      0.0  2026-09-21 00:05
ca                          31   12   10    9     39.3     80.1  2026-09-24 19:38
ca_incremental               3    3    0    0      0.1      0.1  2026-09-21 00:04
catalogos                    1    1    0    0      0.0      0.0  2026-09-21 00:38
competencia                  1    1    0    0      0.0      0.0  2026-09-20 22:37
datos-abiertos               1    1    0    0      1.9      1.9  2026-09-20 22:32
detalles                     7    3    1    3     18.6     44.2  2026-09-23 21:10
detalles-match               3    2    0    1     20.1     20.1  2026-09-24 20:04
lifecycle                    1    1    0    0      2.1      2.1  2026-09-20 22:34
match                       22   12    0   10      1.1      1.6  2026-09-24 20:01
match_post_ca                3    3    0    0      1.1      1.3  2026-09-21 00:04
resumen                      2    2    0    0      0.0      0.0  2026-09-21 00:35
retencion                    1    1    0    0      0.0      0.0  2026-09-21 00:37
```

---

## Paso operativo (Boris, después del commit)

**1. Push, y los `schedule` corren solos desde `main`.** La primera corrida programada puede
llegar con varios minutos de atraso: GitHub encola los `schedule` y en horas punta se demora.
Si haces el merge un lunes después de las 06:35 Chile, `catalogos` recién corre el lunes
siguiente. No es un bug.

**2. Durante el primer día, mira Actions un par de veces:**
- `ca` corre cada hora y su `atraso_horas` baja, sobre todo de noche;
- las corridas que esperaron el lock lo dicen en el log ("esperando lock… conseguido tras N s");
- ninguna corrida sale cancelada por GitHub (eso indicaría un grupo de concurrencia mal puesto).

**3. Al día siguiente:**
- el correo de resumen llegó **entre las 08:30 y ~08:50 hora Chile** (puede esperar a que
  termine el `ca` de las 08:05). Eso prueba el guardia del cambio de hora;
- `/api/salud/jobs` muestra frescos `activas`, `ca`, `datos-abiertos`, `resumen`, `match` y `alerts`;
- la cuota en `/api/salud` está lejos de 9.000. Si se acerca, para y lo revisamos.

**4. cron-job.org:** los crons de ingesta quedan pausados, como ya están. Deja vivo solo el
monitor de `GET /api/salud/jobs`. Borrarlos y apagar el endpoint es F-actions-3.
