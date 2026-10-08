# Prompt F-jobs-endpoint — hacer los jobs disparables desde afuera, con lock

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Diagnóstico que lo origina: `docs/11-auditoria-ping-y-arquitectura.md` §4 y §7,
> `docs/12-free-vs-pago.md` §2.
> Es una fase **neutral a la arquitectura**: arregla bugs que existen hoy y habilita tanto el
> modelo "cron dispara el endpoint" como "GitHub Actions corre el CLI". No decide nada.

---

```
Fase F-jobs-endpoint. Arregla los caminos de disparo de jobs de este repo. Son bugs
reales en producción hoy, no una refactorización preventiva. Lee CLAUDE.md antes de
empezar; las reglas 5, 10 y 13 son las que están en juego.

CONTEXTO DEL PROBLEMA
El scheduler interno (APScheduler dentro del proceso web) es hoy el único camino real
de ejecución, y depende de que Render free mantenga el proceso despierto. Vamos a
mover los jobs a un disparador externo. Antes de eso hay cinco defectos que lo
impiden. Arréglalos sin cambiar la lógica de negocio de ningún job.

1. ADVISORY LOCK EN TODOS LOS CAMINOS (regla 13)
   `POST /api/jobs/run` en app/api/routes/api.py llama los runners de
   app/ingest/orchestrator.py DIRECTO (run_sync_ca, run_sync_activas, etc.), sin pasar
   por `_run_with_lock`. El scheduler interno sí lo hace. Resultado: un disparo externo
   puede solaparse con el ciclo interno y gastar cuota API dos veces sobre el mismo
   trabajo.
   - Envuelve CADA entrada del dict `_jobs` del endpoint en `_run_with_lock(<nombre>, ...)`.
   - Envuelve también cada paso de `_full_cycle`.
   - Envuelve cada job del dispatch de `cmd_run_once` en app/ingest/__main__.py, por la
     misma razón: el CLI va a pasar a ser un camino de producción.
   - `_run_with_lock` ya devuelve None cuando el lock está ocupado. Respeta eso: no lo
     conviertas en excepción ni en reintento. Saltarse el ciclo es el comportamiento
     correcto.

2. JOBS INALCANZABLES DESDE AFUERA
   `retencion` y `catalogos` existen en el scheduler (orchestrator.py, jobs id
   "retencion" y "catalogos") pero NO en el dict `_jobs` del endpoint, así que solo
   corren si el proceso está despierto a las 03:00 y los lunes a las 02:00. Sin la
   purga de retención los 0,5 GB de Neon se llenan solos.
   - Agrégalos al dict `_jobs` del endpoint: `"retencion"` → `run_retencion(engine)`,
     `"catalogos"` → `run_catalogos(settings, engine)`.
   - En el CLI ya existen ambos; solo les falta el lock del punto 1.

3. EL CICLO NOCTURNO NO ES DISPARABLE Y SU GUARD SE PUEDE SALTEAR (regla 5)
   `_ciclo_nocturno` (orchestrator.py) encadena datos_abiertos → lifecycle →
   competencia → backfill del día anterior, y valida la ventana 22:00–07:00 con
   `en_ventana_nocturna` (ZoneInfo America/Santiago). No está expuesto ni en el
   endpoint ni en el CLI. Llamar los pasos por separado desde un cron externo saltea el
   guard Y deja fuera `run_backfill_fecha`, que hoy no tiene NINGÚN camino de disparo
   externo.
   - Agrega `"nocturno"` al dict `_jobs` del endpoint → `_ciclo_nocturno(settings, engine)`.
   - Agrega `"nocturno"` a `_JOBS` y al dispatch del CLI.
   - `_ciclo_nocturno` ya llama `_run_with_lock` por paso: NO lo envuelvas otra vez
     (tomaría el lock por fuera y cada paso interno lo encontraría ocupado; el ciclo
     entero se volvería no-op silencioso). Este es el único caso que NO se envuelve.
   - El guard `en_ventana_nocturna` se mantiene tal cual. Los crons externos corren en
     UTC y no se les cree la hora.

4. `_full_cycle` NO ENVÍA EL RESUMEN
   `job=all` corre activas → detalles → datos-abiertos → lifecycle → match →
   competencia → alerts y termina. Si el disparo externo con `job=all` pasa a ser el
   mecanismo principal, el correo-resumen nunca se envía (hoy queda cubierto solo por
   el job "resumen" del scheduler interno).
   - Agrega `run_resumen` al final de `_full_cycle`, después de `alerts`.

5. `_make_app()` SE EJECUTA DOS VECES
   app/api/main.py termina con `app = _make_app()` a nivel de módulo, y render.yaml
   arranca con `--factory app.api.main:_make_app`, que la crea otra vez. La instancia de
   módulo nunca sirve tráfico y su lifespan nunca corre (no arranca un segundo
   scheduler), pero duplica el objeto FastAPI, el registro de rutas y el Engine en una
   instancia de 512 MB, y obliga a tener todas las env vars presentes para cualquier
   import del módulo.
   - Elimina la línea `app = _make_app()` y su comentario.
   - Verifica que nada más en el repo importe `app.api.main:app`. Si algún test lo
     hace, migralo a `_make_app()` o al fixture que ya construye la app con settings de
     test.
   - No toques el startCommand de render.yaml: `--factory _make_app` sigue siendo correcto.

DOCUMENTACIÓN (parte de esta fase, no opcional)
- docs/operacion.md §9 documenta `POST /api/jobs/run?job=retencion` como el modo de
  forzar la purga. Hoy eso devuelve 400. Con el punto 2 pasa a ser verdad: deja el texto
  y verifica que el ejemplo de curl sea correcto.
- docs/operacion.md §9 afirma que "El pinger en /api/salud/ping la mantiene activa"
  refiriéndose a Neon. Es falso: `/api/salud/ping` devuelve un dict literal y no toca la
  base (api.py, handler `ping`). Ese es el diseño CORRECTO — un ping que consultara la
  base quemaría CU-horas de Neon 24/7 y agotaría el plan free. Corrige el texto para que
  diga eso explícitamente, con una nota de "no agregar una consulta a la base a este
  endpoint", para que nadie lo "mejore" en la dirección equivocada.
- docs/operacion.md §14 (tabla de jobs y cadencia): agrega las filas de `nocturno` y
  aclara qué jobs son disparables por `POST /api/jobs/run` ahora que son todos.
- NO toques docs/despliegue.md §3 (el pinger) en esta fase. La decisión de eliminar el
  keepalive es de la fase siguiente.

TESTS
- El endpoint acepta los nombres nuevos: `retencion`, `catalogos`, `nocturno` → 200 con
  `{"queued": true, "job": ...}`; un nombre inventado sigue dando 400.
- Cada camino del endpoint pasa por `_run_with_lock`: inyecta un doble que registre las
  llamadas y afirma que se tomó el lock. Los parámetros `try_lock_fn`/`unlock_fn` de
  `_run_with_lock` ya son inyectables — úsalos, no parchees por monkeypatch de módulo.
- Lock ocupado (`try_lock_fn` devuelve False) → el job no corre y no se propaga
  excepción al cliente HTTP.
- `_full_cycle` invoca `run_resumen` (mockea los runners, verifica el orden de llamadas).
- `nocturno` fuera de la ventana horaria no ejecuta ningún paso (congela el reloj con
  freezegun o usa el `now_fn` inyectable que ya existe).
- El CLI: `run-once --job=nocturno` existe y respeta el guard.
- Toda llamada de red mockeada con respx. Ninguna llamada real (regla del CLAUDE.md).

RESTRICCIONES
- No agregues dependencias.
- No cambies la lógica interna de ningún runner ni el scoring del matching.
- No toques app/models ni crees migraciones: esta fase no tiene cambio de esquema.
- No ejecutes `alembic upgrade`. Las migraciones las corro yo.
- Una sola fase, un solo commit, mensaje en español con prefijo "F-jobs-endpoint:".
- Agrega la entrada correspondiente a app/changelog.py en el MISMO commit (convención
  de changelog acumulativo, ver F-onboarding).

Al terminar: `ruff check .`, y déjame el diff junto con los comandos exactos de
`python -m mypy app` y `python -m pytest` para que los corra yo (los ejecutables del
venv están bloqueados por antivirus, siempre como módulo).
```

---

## Checklist de auditoría del resultado

Para la conversación limpia de auditoría, después del commit:

- [ ] `_ciclo_nocturno` **no** quedó envuelto en un `_run_with_lock` externo. Si lo está, el
      ciclo nocturno completo es un no-op silencioso: el lock exterior lo tiene el mismo
      proceso y cada paso interno lo encuentra ocupado. Es el error más probable de esta fase.
- [ ] `retencion` en el endpoint llama `run_retencion(engine)` y no
      `run_retencion(settings, engine)` — su firma toma solo el engine.
- [ ] `_full_cycle` sigue respetando el orden: datos-abiertos antes de lifecycle/match,
      competencia después de lifecycle. `resumen` va al final.
- [ ] Ningún test nuevo pega a la red. `grep` por `httpx` y por dominios reales en `tests/`.
- [ ] Borrar `app = _make_app()` no rompió ningún import. Buscar `main:app` y `from app.api.main
      import app` en todo el repo, incluidos `tests/` y `scripts/`.
- [ ] La entrada de `app/changelog.py` describe el cambio en lenguaje de usuario, no de
      implementación (el panel de novedades lo lee gente, no devs).
