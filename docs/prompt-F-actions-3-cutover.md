> **OBSOLETO (25-sep-2026).** Reemplazado por `docs/prompt-F-actions-3-disparo-externo.md` y
> `docs/operacion-disparos.md`. No correr.

# Prompt F-actions-3 — cutover, vigilancia y cierre documental

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> **Prerrequisito:** F-actions-2 en `main` y **un ciclo diario completo ya corrido solo**,
> con `activas`, `ca`, `datos-abiertos` y `resumen` frescos en `/api/salud/jobs` y el correo
> de resumen llegado a las 08:30 hora Chile. Si eso no ocurrió, no apagues nada todavía.
>
> Esta fase apaga el disparador viejo y resuelve el agujero que deja: **quién vigila ahora**.

---

```
Fase F-actions-3. Los jobs ya corren desde GitHub Actions. Queda cerrar el cutover: dar de
baja el camino viejo, tapar el hueco de vigilancia que abre el modelo nuevo y dejar la
documentación al día. Lee CLAUDE.md antes de empezar. No cambies la lógica de negocio de
ningún runner.

EL HUECO QUE HAY QUE TAPAR
Con los jobs en Actions, una falla de job manda correo de GitHub y se ve. Lo que NO se ve es
que los workflows dejen de existir: **GitHub deshabilita los workflows programados tras 60
días sin actividad en el repositorio**, y lo hace en silencio. Es exactamente la forma en que
murió el pinger el 17-jul-2026 — un disparador que se apaga solo y nadie se entera. Peor: un
workflow de vigilancia que viva en el mismo repo se apagaría junto con los demás, así que no
puede ser la única red.

1. SUBCOMANDO `check-jobs` EN EL CLI  (vigilancia sin pasar por HTTP)
   Hoy la lógica del dead-man's switch vive dentro de la ruta GET /api/salud/jobs en
   app/api/routes/api.py: el watchlist de jobs vigilados con sus alias y umbrales, y la
   consulta a job_runs. Extraé esa evaluación a una función pura y reutilizable (sugerencia:
   en app/ingest/orchestrator.py o un módulo nuevo app/core/vigilancia.py — elegí vos y
   justificá en el commit), que reciba la sesión/engine y devuelva el mismo resultado que hoy
   arma la ruta. La ruta pasa a llamarla. **Su respuesta JSON y sus códigos 200/503 no pueden
   cambiar**: hay un monitor externo colgado de ese contrato.
   Sobre eso, agregá `python -m app.ingest check-jobs` a app/ingest/__main__.py: imprime el
   estado de cada job vigilado y sale con código 1 si alguno está stale, 0 si todos frescos.
   Va contra Postgres directo, sin HTTP — inmune al 429 de Cloudflare que originó todo esto.

2. WORKFLOW `.github/workflows/vigilancia.yml`
   Usa `_job.yml`? No: este no ejecuta un job de ingesta. Escribilo aparte, con los mismos
   pasos de setup (checkout, setup-python 3.11 con cache, install) y un step final
   `python -m app.ingest check-jobs`. `schedule` cada 6 h (`0 2,8,14,20 * * *` UTC) más
   `workflow_dispatch`. Solo necesita el secret DATABASE_URL y las variables; reusá el mismo
   patrón de `env` y de SECRET_KEY/JOBS_TOKEN efímeros de `_job.yml`.
   Si sale 1, el workflow falla y GitHub manda el correo. Es la primera capa; la segunda
   —externa al repo— la monta Boris a mano (ver paso operativo).

3. `POST /api/jobs/run` SE QUEDA
   No lo borres ni lo deprecies. Queda como escotilla manual para desatascar a mano, y es lo
   que se usa hoy cuando Actions no está disponible. Sin cambios de código.

4. DOCUMENTACIÓN — commit aparte, prefijo `docs:`
   - `docs/00-estado-actual.md`: sesión nueva al principio. Registrar, marcando [V] lo
     verificado y [I] lo inferido: (a) el 429 del 21-sep venía de Cloudflare en el borde de
     Render contra las IPs del scheduler de cron-job.org, NO de la app ni de la cuota de
     ChileCompra — verificado con `grep` (cero `status_code=429` en app/), con el endpoint
     público `/api/salud/jobs` rebotando también, y con una ejecución de prueba desde
     cron-job.org que dio 200 OK con `Server: cloudflare`; (b) [I] sin confirmar: que sea
     limitación por IP del pool compartido de cron-job.org; (c) el modelo pasó a
     Actions-driven con el CLI contra Neon directo; (d) el riesgo nuevo de los 60 días.
   - `docs/03-roadmap.md`: `F-actions` cerrada. `F-cuota`, `F-429-concurrencia` y `F-ca-ventana`
     cerradas; `F-secretos` sigue abierta.
   - `docs/operacion.md` y `docs/despliegue.md`: el runbook cambia de raíz. Cómo disparar un
     job a mano (Actions → Run workflow, y el POST como alternativa), dónde viven los secrets
     y las variables, la trampa del branch dev/production de Neon, y qué mirar cuando algo no
     corre.
   - Commiteá también los `docs/prompt-F-*.md` que estén sin trackear.

5. VERIFICACIÓN Y LÍMITES
   - `ruff check .`, `python -m mypy app`, `python -m pytest`. Suite verde.
   - Tests: que `check-jobs` devuelva 0 con todo fresco y 1 con un job stale (offline,
     SQLite, como tests/test_observabilidad.py). Y que la ruta GET /api/salud/jobs siga
     devolviendo exactamente el mismo JSON y los mismos 200/503 que antes del refactor — ése
     es el test de regresión que importa.
   - NO toques la lógica de ningún runner, ni render.yaml, ni el arranque.
   - Dos commits: `F-actions-3:` para el código, `docs:` para la documentación. Entrada en
     app/changelog.py en el de código.
   - NO uses `git add -A` (`_to_delete/` tiene un `.env` con secretos de producción).
```

---

## Paso operativo (Boris)

**1. Apagar los 6 crons de cron-job.org.** Pausar, no borrar, por un par de semanas: si algo
de Actions sale mal, reactivarlos es un click. El monitor `GET Jobs` **sí conviene dejarlo
vivo** — es la segunda capa de vigilancia, la que está fuera del repo.

**2. Si el 429 sigue, mover el monitor externo.** El `GET Jobs` de cron-job.org no sirve
mientras Cloudflare le siga respondiendo 429. Alternativa: UptimeRobot free u otro monitor de
uptime apuntando a `https://app-mercado-publico.onrender.com/api/salud/jobs`, configurado
para alertar ante código ≠ 200, con timeout de 30–60 s y reintentos (el proceso duerme: la
primera request puede caer en un arranque en frío). Otras IPs, otro borde: hay buena chance
de que pase donde cron-job.org rebota.

**3. [23-sep] Ya hecho:** `_to_delete/` está vacío y `JOBS_TOKEN` se rotó el 22-sep. Si
pausaste los crons en F-actions-2, aquí solo confirmas que siguen pausados.

## Lo que queda abierto después de esto  [23-sep]

- Deuda de observabilidad: `run_match` registra `ok` aunque corte los detalles por un 429.
- Backlog de F-ca-ventana: `fecha_publicacion` NULL en CA (verificar), `organismo_*` con el texto
  'None', arranque en frío con páginas de 50, columna "Requests hoy" de `compra_agil` siempre en 0,
  espera de 2 s ante el 500 "Servicio no disponible" (debería enfriar como el 504), estado de
  licitación 15 sin mapear.
- **`F-secretos`** — rotar los 7 secretos (`docs/prompt-F-secretos.md`).
- Limpieza de argentinismos en UI, docs y código (estos prompts tienen voseo).
