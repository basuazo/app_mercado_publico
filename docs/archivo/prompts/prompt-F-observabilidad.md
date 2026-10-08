# Prompt F-observabilidad — dead-man's switch de los jobs

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Diagnóstico que lo origina: `docs/11-auditoria-ping-y-arquitectura.md` §6, confirmado
> en `docs/00-estado-actual.md` (el corte del 17-jul-2026 se descubrió ~2 meses después
> porque nada avisa cuando los jobs se detienen).
> Es una fase **neutral a la arquitectura**: funciona igual con el scheduler interno de hoy
> y con el modelo invertido de mañana (§3), porque instrumenta el punto único por donde
> pasan todos los disparos: `_run_with_lock`. Migración **additiva** (segura, corre sola
> en el deploy). No cambia la lógica de negocio de ningún job.

---

```
Fase F-observabilidad. Dale memoria y un dead-man's switch a los jobs de este repo. Hoy
un job que falla de forma persistente se ve idéntico a un job que no tuvo trabajo, y no
hay nada que avise: por eso la ingesta estuvo caída desde el 17-jul-2026 y se notó
recién en septiembre. Lee CLAUDE.md antes de empezar; las reglas 5, 10, 11, 13 y 23
están en juego. No cambies la lógica de negocio de ningún runner.

CONTEXTO DEL PROBLEMA
`_run_with_lock` (app/ingest/orchestrator.py) captura TODA excepción, la loguea y
devuelve None. Es el único camino por el que pasan todos los disparos —scheduler
interno, POST /api/jobs/run y el CLI—, así que es también el único lugar donde hay que
instrumentar. No hay historial de corridas ni un endpoint que grite cuando algo se
detuvo.

1. MODELO + TABLA job_runs
   En app/models/tables.py, junto a SyncState, agrega:

       class JobRun(Base):
           __tablename__ = "job_runs"

           id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
           job: Mapped[str] = mapped_column(String(50), nullable=False)
           iniciado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
           terminado_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
           estado: Mapped[str] = mapped_column(String(20), nullable=False)  # ok | error | omitido
           resultado_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
           error: Mapped[str | None] = mapped_column(Text, nullable=True)

   Usa BigInt/JSONB (los variants ya definidos arriba en el módulo) para que la tabla
   funcione en Postgres Y en el SQLite de los tests. Agrega un índice compuesto para la
   consulta del switch y la purga:

       Index("ix_job_runs_job_iniciado", JobRun.job, JobRun.iniciado_en.desc())

   Estados: "ok" (fn retornó sin excepción), "error" (fn lanzó), "omitido" (el advisory
   lock estaba ocupado; NO es fallo). Una sola fila por corrida.

2. MIGRACIÓN ALEMBIC (additiva)
   El head actual es f3a9b8c7d6e5 (verifícalo con `python -m alembic heads`). Crea una
   migración nueva con down_revision = "f3a9b8c7d6e5" que cree la tabla job_runs y su
   índice. Puedes usar --autogenerate, pero REVISA a mano el archivo resultante: copia el
   patrón de tipos de las migraciones existentes en alembic/versions/ (el variant JSONB
   suele necesitar ajuste manual). No apliques la migración a producción: en prod corre
   sola en el deploy (startCommand `alembic upgrade head`). En local, la credencial de la
   branch dev de Neon puede estar vencida (ver docs/00-estado-actual.md) — si upgrade
   falla por auth, déjalo anotado y no lo fuerces; los tests corren en SQLite igual.

3. _run_with_lock GRABA UNA FILA POR CORRIDA
   Instrumenta app/ingest/orchestrator.py::_run_with_lock para que registre cada corrida
   en job_runs, respetando estas reglas duras:
   - La escritura usa su PROPIA sesión/conexión de vida corta (Session(engine) nueva), NO
     la `conn` del advisory lock. Escribir sobre `conn` puede ensuciar su transacción, y
     un rollback ahí soltaría el lock. El lock vive atado a `conn`.
   - TODO el registro va envuelto en try/except que loguea y NO propaga: si grabar falla,
     el job NO se cae. Esto es telemetría, no puede tumbar producción.
   - Marca iniciado_en al entrar. Al terminar, inserta UNA fila con iniciado_en,
     terminado_en, estado y resultado/error:
       · éxito → estado="ok", resultado_json = el dict que retornó fn (es dict[str,int]).
       · excepción → estado="error", error = traceback.format_exc() (ya se captura ahí).
       · lock ocupado (la rama que hoy retorna None temprano) → estado="omitido".
   - Extrae la escritura a un helper chico, p. ej.
     `_registrar_corrida(engine, job, iniciado_en, estado, resultado, error)`, y llámalo
     en las tres ramas. El comportamiento de retorno de _run_with_lock queda idéntico
     (None en lock ocupado y en error; el dict en éxito).

   NOTA sobre nombres de job: el mismo job lógico llega con nombres distintos según el
   disparador (endpoint: "activas", "ca", "datos-abiertos"; scheduler: "sync_activas",
   "ca_incremental"; nocturno: "datos_abiertos"). Graba el nombre tal cual llega; los
   alias se resuelven en el punto 4.

4. ENDPOINT GET /api/salud/jobs (dead-man's switch)
   En app/api/routes/api.py, agrega un endpoint PÚBLICO (sin auth, sin secretos, mismo
   criterio que /api/salud/ping — NO lo pongas detrás de api_require_admin). Devuelve 200
   si todos los jobs críticos tienen una corrida "ok" dentro de su ventana, y HTTP 503 si
   alguno está atrasado o nunca corrió OK. El 503 es lo que el monitor externo detecta
   como no-2xx para disparar el aviso.

   Define el watchlist como constante de módulo, con alias y umbral por job. Críticos
   (disparan 503):
     - "activas": alias {"activas","sync_activas"}, umbral 30 h
     - "ca": alias {"ca","ca_incremental"}, umbral 30 h
     - "datos-abiertos": alias {"datos-abiertos","datos_abiertos"}, umbral 36 h
   Informativos (se muestran, NO disparan 503):
     - "resumen": alias {"resumen"}, umbral 30 h
   Los umbrales son amplios a propósito: el switch detecta "la ingesta se detuvo" (el
   incidente real fue de semanas), no "un job se atrasó una hora". 30 h cubre el hueco
   nocturno sin falsos positivos. Déjalos en la constante para poder ajustarlos.

   Por cada job del watchlist, busca la última corrida con estado="ok" cuyo `job` esté en
   su set de alias (usa el índice). Calcula edad_horas = ahora - iniciado_en; si no hay
   ninguna, stale=True. Respuesta:
     {
       "status": "ok" | "stale",
       "generado_en": "<iso>",
       "jobs": [
         {"job":"activas","ultimo_ok":"<iso|null>","edad_horas":3.2,"umbral_horas":30,
          "critico":true,"stale":false},
         ...
       ]
     }
   status="stale" y HTTP 503 si algún job crítico está stale; si no, "ok" y 200. La
   consulta debe ser liviana (regla 11: este endpoint SÍ toca la base, a diferencia de
   /ping). No incluyas MP_TICKET, SECRET_KEY ni JOBS_TOKEN en la respuesta.

5. RETENCIÓN DE job_runs
   job_runs crece con cada corrida. Suma su purga a run_retencion (app/core/retencion.py
   / el runner que ya existe): borrar filas con iniciado_en más viejo que ~90 días.
   Mantiene la tabla acotada (regla 11, Neon 0,5 GB). Devuelve el conteo borrado en el
   dict de resultado de retención.

6. TESTS (que corran offline, en SQLite — sin Neon)
   - _run_with_lock: graba fila "ok" con resultado_json en éxito; "error" con traceback
     en excepción; "omitido" cuando el lock está ocupado (inyecta try_lock_fn que devuelve
     False, como en los tests existentes). Y: si la escritura de telemetría lanza, el job
     igual corre y _run_with_lock retorna lo mismo que retornaría sin telemetría (mockea
     _registrar_corrida para que lance y verifícalo).
   - GET /api/salud/jobs: 200 cuando hay corridas "ok" frescas de todos los críticos; 503
     cuando un crítico está viejo o nunca corrió; un alias satisface a su canónico (una
     corrida "sync_activas" satisface "activas"); una corrida "error" reciente NO cuenta
     como OK.
   - retención borra job_runs viejas y conserva las recientes.

7. VERIFICACIÓN Y LÍMITES
   - Corre `ruff check .`, `python -m mypy app`, `python -m pytest` (como módulo: los .exe
     del venv dan "Acceso denegado"). Deja la suite verde.
   - NO cambies la lógica de negocio de ningún runner ni la cadencia del scheduler.
   - NO apliques la migración a prod ni toques el dashboard de Render.
   - Fuera de alcance de esta fase (NO lo toques acá): el 429 en cadena de `detalles`
     (docs/00-estado-actual.md §6), el modelo invertido (§3 de docs/11), y la rotación de
     secretos. Son fases aparte.
   - Un commit, mensaje en español con prefijo `F-observabilidad:`. Agrega una entrada a
     app/changelog.py (fecha, título, descripción simple) en el MISMO commit.
```

---

## Paso operativo (Boris, no es código)

Después del deploy, en cron-job.org crear un monitor nuevo apuntado a
`https://app-mercado-publico.onrender.com/api/salud/jobs`, con **notificación por correo
ante respuesta no-2xx**. Ese es el dead-man's switch: avisa el mismo día cuando la ingesta
se detiene, sea porque el servicio está dormido, porque Neon se agotó o porque el cron se
cayó.

- Frecuencia **cada 1–2 h, NO cada 10 min**: este endpoint consulta Postgres, y pegarle
  seguido despertaría a Neon 24/7 y quemaría CU-horas (justo lo que el modelo invertido
  busca evitar).
- User-Agent de navegador (mismo arreglo que levantó los crons: Cloudflare bloquea el UA
  bot de cron-job.org).
