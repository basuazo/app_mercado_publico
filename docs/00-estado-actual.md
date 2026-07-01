# Estado actual — mp-oportunidades (handoff entre sesiones)

> **Cómo retomar en una conversación nueva:** pídele al asistente que lea
> `docs/00-estado-actual.md` y `docs/03-roadmap.md`. Con eso queda al día.

## Qué es
App de búsqueda de oportunidades en compras públicas chilenas (API Mercado Público /
ChileCompra). Flujo: ingesta → Postgres (Neon) → perfiles por usuario → matching con
score → alertas email → dashboard con login. Costo objetivo: **$0** (Render free + Neon free).

## Desplegado en PRODUCCIÓN
- **Render free** (web) + **Neon free** (Postgres 0.5 GB). URL: https://app-mercado-publico.onrender.com
- Branches Neon: `production` (la usa Render) y `dev` (local + tests).
- Commit base de la sesión: `9a4928b` (F-deploy) + `d1369b0` (.gitattributes EOL).
- En vivo: F8 (resultados legibles, ficha enriquecida, link condicional), F9a/b/c
  (perfiles con regiones/montos/exclusiones, **rubros UNSPSC**, **organismos seguidos**,
  recall+score unificados en FTS con stemming), **F-rubros** (ítems desde datos abiertos
  sin gastar cuota), **F-seguir** (seguir/archivar + alertas de avance), **F-competencia**
  (análisis de competencia al adjudicarse), **F-plan** (consulta del Plan Anual de Compra,
  pestaña separada, on-demand), **F-datos** (clasificación de organismos por sector,
  datos abiertos sin ticket — alcance acotado, ver roadmap), **F10 (parcial)** (formulario
  de `/perfiles` rediseñado: rubros en acordeón, organismos en multi-select por sector;
  **dashboard rediseñado** con tarjetas escaneables, orden score/cierre, **descartar +
  registro de feedback** "me sirve"/"descarte" — señal para F11; **ficha de detalle
  rediseñada** con cabecera escaneable, competencia con oferentes que NO ganaron, rubro en
  ítems, y los mismos botones de feedback; **mail de match enlaza a la ficha de la app**
  (ya no a la URL no autorizada de MP) — **F10 COMPLETA**), Fix Compra Ágil 500 en frío,
  **F-feed-umbral** (umbral de relevancia en el dashboard: control Alta/Media/Todas +
  línea "N ocultas por baja relevancia — ver todas", ver detalle abajo), F-deploy.
- Suite: 499 tests verdes (1 skipped); persisten 1 falla + 4 errores preexistentes
  (`test_match_todos_procesa_todos_perfiles` no aislado, `pg_session` con
  `Session(connection=...)` — ver "Deudas conocidas", ninguno introducido en esta fase).
- **F-feed-umbral — umbral de relevancia del feed (este commit):** `get_oportunidades_usuario`
  (`app/api/query.py`) suma un parámetro `min_score` (default `0` = sin piso, para no romper
  a quien llama la función directo — p. ej. la API REST `/api/oportunidades`, fuera de
  alcance de esta fase) y retorna un tercer valor `total_sin_filtro_relevancia` para poder
  mostrar cuántos matches quedan ocultos. La ruta `GET /` (dashboard) sí aplica un piso por
  defecto — `settings.feed_min_score_default` (nuevo, **`40`**) — salvo que la request pase
  `?min_score=`. Control en `index.html`: presets "Alta relevancia" (`60`, fijo), "Media"
  (el default configurable) y "Todas" (`0`), más la línea "Mostrando N · M oculta(s) por baja
  relevancia — ver todas" (también cuando el filtro esconde absolutamente todo).
  **Confianza del default (regla 20/23):** la branch `dev` de Neon solo tenía **10** filas en
  `oportunidades_match` al momento de calibrar (rango de score 23–53) — muestra insuficiente
  para una distribución robusta; no se consultó `production` (fuera del alcance autorizado
  de esta sesión). El valor `40` es **INFERIDO** de la fórmula de scoring
  (`app/matching/engine.py`: `score_texto` 0–60 solo si hay keyword-hit real, más
  `score_urgencia`/`score_competencia`/`score_estructural` 0–35 sin necesidad de texto) más
  el patrón visto en los 10 matches de dev, no de una distribución grande verificada. Queda
  como env var ajustable sin re-deploy (`FEED_MIN_SCORE_DEFAULT`) — **recalibrar con datos
  reales de producción** en cuanto haya volumen para confirmarlo o corregirlo.
- F10/perfiles, F10/dashboard y F10/ficha: verificados server-side (TestClient con ciclo ASGI
  completo + servidor local real con login), **no** con un navegador real (sin herramienta de
  automatización disponible sin instalar dependencia nueva fuera del stack) — recomendado un
  vistazo manual a la interactividad JS/HTMX antes de dar el look final por cerrado.
- **Pendiente operativo:** la migración `e1f4a7c9b2d6` (tabla `match_feedback`, F10 parte 2)
  solo se verificó offline (`alembic ... --sql`); falta correr `alembic upgrade head` contra
  la branch `dev` y luego `production`. La branch `dev` ya estaba **3 migraciones detrás**
  del head antes de esta fase (deuda preexistente, no introducida aquí) — conviene revisar
  el historial completo de `alembic current` vs `alembic heads` antes del próximo upgrade.

## Qué hace hoy
Descubrir oportunidades por keyword/región/**rubro UNSPSC**/organismo; ver ficha
enriquecida con razones legibles del match; **seguir/archivar** licitaciones y recibir
**alertas cuando cambian de estado** (sobre todo adjudicada); y al adjudicarse, ver el
**análisis de competencia** (proveedores, montos, quién ganó) reconstruido desde datos abiertos.

## Flujo de trabajo (IMPORTANTE — así trabajamos)
- **Cambios de código vía Claude Code:** el asistente genera un **prompt**, el usuario
  lo corre en Claude Code, y se **audita** el resultado. El asistente NO edita archivos
  de código del repo directamente (solo docs/planes).
- Un commit por fase; mensaje en español con prefijo de fase.
- Antes de cerrar una fase: `ruff check .` ; `mypy app` ; `pytest` (verde).
- Migraciones: **dry-run** en una branch Neon creada desde `production` antes de tocar prod.

## Gotchas operacionales (aprendidos; no repetir)
- `DATABASE_URL` siempre con prefijo **`postgresql+psycopg://`** (psycopg3).
  `app/core/db.py::normalizar_url_driver` lo normaliza para app y para alembic.
- **alembic lee `DATABASE_URL` de la VARIABLE DE ENTORNO, no del `.env`.** En local hay
  que exportarla en la ventana (`$env:DATABASE_URL = "..."`). Tras CADA migración nueva:
  `alembic upgrade head` contra la branch correspondiente.
- Cada ventana de PowerShell empieza limpia: activar venv + setear `$env:DATABASE_URL`.
- En prod las migraciones corren **solas** en el deploy (startCommand: `alembic upgrade head`).
- **Crons** (cron-job.org): pinger a `/api/salud/ping` (keep-alive) + `job=ca` horario +
  `job=all` nocturno (~02:00 Santiago). `job=all` ya incluye `datos-abiertos` y `competencia`.
  El endpoint `/api/jobs/run` es **POST** y exige header `X-Jobs-Token`.
- **`job=ca` arreglado (era 500 persistente, ver `docs/09-compra-agil-500.md` y
  `docs/03-roadmap.md`):** con cursor `NULL` la request a `/v2/compra-agil` salía sin
  filtro real → la API responde 500 → cursor nunca avanzaba → bucle infinito de ERROR.
  Fix: `sync_incremental` ahora manda siempre `estados` a la API. **Pendiente que Boris
  verifique post-deploy** que el cron `ca` pasa de ERROR a OK en los logs de Render y que
  aparecen Compras Ágiles en el dashboard (la primera corrida exitosa recién fija el cursor).
- **Heal de datos tras deploy:** `POST /api/jobs/run?job=all` (o `activas`→`datos-abiertos`→`match`).
- **EOL:** `.gitattributes` fuerza LF (`* text=auto eol=lf`). En esta sesión hubo un
  incidente de CRLF + archivos truncados en el working tree; se recuperó con `git restore .`
  (HEAD estaba intacto). Si vuelve a pasar: `git restore .` recupera todo desde el commit.

## Deudas conocidas (pendientes de calidad; ninguna bloquea prod)
- `tests/test_models.py`: el fixture `pg_session` usa `Session(connection=conn)` → debe
  ser `Session(bind=conn)` (da 4 errors al correr `@needs_postgres`).
- `test_match_todos_procesa_todos_perfiles` no se aísla (cuenta perfiles globales de la BD).
- Branch `dev` tiene perfiles/seguimientos/matches de prueba (ids ~49–52) de la validación → limpiar.
- ~~Análisis de competencia: badge "Adjudicatario" en todas las filas; resumen solo lista
  ganadores~~ — **resuelto en F10 parte 3**: `resumen_competencia` ahora incluye también a
  quienes ofertaron y no ganaron (`items_ofertados`/`items_ganados`/`total_adjudicado` por
  proveedor), y el badge "Ganó" solo aparece en la(s) fila(s) ganadora(s).
- Adjudicadas en BD con `fecha_publicacion`/`fecha_cierre` NULL (calidad de datos; revisar
  el refresh de estados terminales).
- `test_jobs_run_job_ca`: skip (pega a la API real sin mock); migrar a respx.

## Roadmap pendiente (detalle en docs/03-roadmap.md)
- **F10 UX:** COMPLETA (perfiles, dashboard, ficha y mail).
- **F11:** feedback like/dislike con reponderación ligera (regresión logística, sin LLM) —
  la señal ya se registra en `MatchFeedback` (F10 parte 2), falta el modelo que la consuma.
- **Backlog:** worker offline de anexos en Raspberry Pi (OCR + embeddings), condicionado.

## Mapa de documentos
- `00-estado-actual.md` (este) · `01-analisis-api-mercado-publico.md` (contrato/gotchas API)
- `02-plan-desarrollo-y-auditoria.md` · `03-roadmap.md` (historial de fases + pendientes)
- `04-datos-abiertos.md` (lic-da: ítems/UNSPSC) · `05-competencia.md` (ofertas/ganador)
- `07-plan-anual.md` (PAC: spike + veredicto) · `08-datos-organismos.md` (sector: spike + veredicto)
- `arquitectura.md` · `despliegue.md` · `operacion.md`

*Reglas duras del proyecto (API, free tier, multiusuario, arquitectura): ver `CLAUDE.md`.*
