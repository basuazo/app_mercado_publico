# Prompt F-invertir-modelo — el cron dispara los jobs, el proceso duerme

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Diagnóstico que lo origina: `docs/11-auditoria-ping-y-arquitectura.md` §3 (invertir el
> modelo) y §4 [BLOQUEANTE-5]. Prerrequisito ya cumplido: F-observabilidad desplegada
> (dead-man's switch en `/api/salud/jobs`), y los 12 jobs ya expuestos con advisory lock en
> `POST /api/jobs/run` (`fd78ff0`).
> **Zona sensible:** el arranque. El 4-sep-2026 producción se cayó por tocar el `_make_app` /
> `--factory` (ver `docs/00-estado-actual.md`). Este cambio NO toca eso: solo condiciona el
> arranque del scheduler DENTRO del `lifespan`. No tocar `_make_app`, ni el factory, ni el
> startCommand del dashboard de Render.

---

```
Fase F-invertir-modelo. Hoy el scheduler interno (APScheduler BackgroundScheduler dentro del
proceso web) es el mecanismo real de ejecución, y para que corra hay que mantener el proceso
despierto 24/7 con un pinger externo — un patrón frágil (fue la causa del corte de julio).
Damos vuelta el modelo: el cron externo dispara los jobs directo contra el endpoint que ya
existe, y el proceso duerme entre disparos. Lee CLAUDE.md antes de empezar; las reglas 5, 10
y 13 están en juego. No cambies la lógica de negocio de ningún runner.

CONTEXTO
- El endpoint POST /api/jobs/run ya expone todos los jobs con advisory lock (ca, activas,
  detalles, datos-abiertos, lifecycle, match, competencia, alerts, resumen, retencion,
  catalogos, nocturno, más job=all y job=nocturno). Eso ya está y NO se toca su lógica.
- El scheduler interno se arma en el lifespan de create_app (app/api/main.py): llama
  build_scheduler(), copia los jobs a un BackgroundScheduler y lo arranca. ESO es lo que hay
  que apagar en producción.

1. CONDICIONAR EL SCHEDULER INTERNO A UNA FLAG (default OFF)
   Objetivo: que en producción el proceso NO levante el BackgroundScheduler, así puede
   dormirse cuando no hay tráfico. Sin borrar nada — reversible y testeable.
   - En app/core/settings.py, agregar un campo booleano:
       scheduler_en_proceso: bool = Field(
           default=False,
           description="Arranca el APScheduler dentro del proceso web. En producción va en "
                       "False: los jobs los dispara el cron externo contra /api/jobs/run.",
       )
     (pydantic lo mapea a la env var SCHEDULER_EN_PROCESO, sin distinción de mayúsculas.)
   - En app/api/main.py, dentro del lifespan de create_app, envolver TODO el bloque que arma
     y arranca el BackgroundScheduler en `if settings.scheduler_en_proceso:`. Cuando la flag
     está en False: no importar ni instanciar el scheduler, y dejar
     `app.state.scheduler = None`.
   - El shutdown al final del lifespan debe tolerar que no haya scheduler: llamar
     `bg_sched.shutdown(wait=False)` SOLO si se arrancó (guardar la referencia o chequear
     `app.state.scheduler is not None`).
   - NO tocar nada más de main.py: ni `_make_app`, ni `create_app` fuera del bloque del
     scheduler, ni el seed, ni `_wait_for_db`, ni los routers, ni el middleware. El factory y
     el startCommand quedan exactamente igual.

2. DOS JOBS COMPUESTOS EN EL ENDPOINT (paridad con la cadencia del scheduler)
   El scheduler agrupaba pasos que el endpoint hoy solo tiene sueltos. Para que el cron pueda
   disparar la misma secuencia con una sola llamada, agregar dos entradas al dict `_jobs` de
   app/api/routes/api.py, REUSANDO las entradas ya envueltas en `_locked` (no re-implementar
   ni re-envolver: cada paso ya toma el lock por separado, igual que hace `_full_cycle`):
   - "ciclo-ca"      → ejecuta en orden: _jobs["ca"](), _jobs["match"](), _jobs["alerts"]()
   - "ciclo-activas" → en orden: _jobs["activas"](), _jobs["detalles"](), _jobs["match"](),
                       _jobs["alerts"]()
   Definirlos como funciones que llaman esas entradas en secuencia (mirá cómo está hecho
   `_full_cycle`). Van a `background_tasks.add_task(...)` igual que el resto.
   - Como reutilizan las entradas `_locked` existentes, job_runs sigue registrando cada paso
     con su nombre propio ("ca", "match", "activas"…), así que el dead-man's switch
     (/api/salud/jobs) sigue funcionando sin cambios en su watchlist. No toques esa constante.
   - NO tocar `job=all` (`_CICLO_COMPLETO`), ni `job=nocturno`, ni `run_resumen`, ni el
     `_ciclo_nocturno`. `nocturno` conserva su guard de ventana 22:00–07:00 intacto (regla 5):
     es la red de seguridad si el cron dispara a una hora equivocada por UTC/DST.

3. TESTS
   - Con la flag por defecto (False): el lifespan NO arranca el scheduler y
     `app.state.scheduler is None`. (Se puede testear el lifespan con el TestClient o
     construyendo la app y chequeando el estado.)
   - Con `scheduler_en_proceso=True`: sí lo arranca (mockear/inyectar para no correr jobs
     reales; basta con verificar que se instancia y queda en app.state).
   - "ciclo-ca" y "ciclo-activas": encolan y ejecutan sus sub-jobs en el orden correcto
     (verificar la secuencia de llamadas; podés inyectar try_lock_fn/unlock_fn como en los
     tests existentes de jobs).
   - Regresión: `job=all` y `job=nocturno` siguen comportándose igual que antes.

4. VERIFICACIÓN Y LÍMITES
   - `ruff check .`, `python -m mypy app`, `python -m pytest` (como módulo). Suite verde.
   - NO tocar el factory `_make_app`, el startCommand ni render.yaml.
   - NO cambiar la lógica de negocio de ningún runner ni la cadencia del scheduler interno
     (build_scheduler queda igual; solo deja de arrancarse en prod por la flag).
   - Fuera de alcance (NO tocar acá): exit codes del CLI, desacoplar `resumen` de
     `_full_cycle`, y escalonar horarios — son los ítems de "F-actions" en
     `docs/prompt-F-secretos.md`. Y la rotación de secretos.
   - Un commit, prefijo `F-invertir-modelo:`. Agregar entrada a app/changelog.py en el mismo
     commit.
```

---

## Paso operativo (Boris, después del deploy)

**1. Confirmar la flag en Render.** Que `SCHEDULER_EN_PROCESO` **no exista** (o esté en
`false`) en las env vars del servicio. Con eso el proceso deja de correr el scheduler interno
y puede dormirse. (En local, para tener el scheduler como antes: `SCHEDULER_EN_PROCESO=true`
en tu `.env`, o correr `python -m app.ingest run-scheduler`.)

**2. Crons en cron-job.org.** Poné la **zona horaria de cada cron en `America/Santiago`**
(cron-job.org lo permite) — así usás hora de Chile y te olvidás del UTC y el cambio de hora.
Cadencia propuesta (horarios **escalonados a propósito**: el advisory lock es una sola llave
global, dos jobs distintos en el mismo minuto colisionan y uno se omite):

| Cron | job= | Horario Chile |
|---|---|---|
| CA + match + alertas | `ciclo-ca` | cada 2 h, 08:00→20:00 |
| Licitaciones activas + detalles + match + alertas | `ciclo-activas` | 08:10, 13:10, 18:10 |
| Ciclo nocturno (datos abiertos, lifecycle, competencia, backfill) | `nocturno` | 01:00 |
| Resumen por correo | `resumen` | 08:30 |
| Retención (purga) | `retencion` | 03:00 |
| Catálogos | `catalogos` | lunes 02:30 |

Todos son **POST** a `https://app-mercado-publico.onrender.com/api/jobs/run?job=<nombre>` con
el header `X-Jobs-Token`. Cada disparo despierta el servicio (~1 min de arranque en frío),
corre el job y lo deja dormirse ~15 min después.

**3. Apagar el keep-alive.** Ya no hace falta mantener el proceso despierto 24/7: desactivá el
pinger a `/api/salud/ping` en cron-job.org. (El proceso ahora despierta solo cuando hay job.)

**4. El monitor del dead-man's switch, con tolerancia a arranque en frío.** Con el proceso
durmiendo, la propia request del monitor a `/api/salud/jobs` puede caer sobre un arranque en
frío y recibir un 503 pasajero → falsa alarma. Mitigación en cron-job.org: subile el
**timeout** (30–60 s) y activá **reintentos ante fallo** antes de notificar. El "stale" real
(los jobs no corren) persiste entre reintentos y sí dispara el aviso; el cold-start pasajero
no.

**Nota sobre jobs largos (BLOQUEANTE-5).** Render duerme el proceso 15 min después de la
última request ENTRANTE, sin importar si hay un job corriendo en background. El `nocturno`
puede pasarse de ese plazo y recibir SIGTERM a mitad. No corrompe nada: los jobs son
idempotentes y con cursor en Postgres, así que la corrida siguiente retoma. Si en la práctica
el `nocturno` queda cortándose seguido, la solución es partirlo en disparos separados (fase
aparte, no ahora).
