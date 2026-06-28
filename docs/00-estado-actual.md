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
  (análisis de competencia al adjudicarse), F-deploy.
- Suite: ~403 tests verdes en el run "plano" (sin DATABASE_URL).

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
- **Heal de datos tras deploy:** `POST /api/jobs/run?job=all` (o `activas`→`datos-abiertos`→`match`).
- **EOL:** `.gitattributes` fuerza LF (`* text=auto eol=lf`). En esta sesión hubo un
  incidente de CRLF + archivos truncados en el working tree; se recuperó con `git restore .`
  (HEAD estaba intacto). Si vuelve a pasar: `git restore .` recupera todo desde el commit.

## Deudas conocidas (pendientes de calidad; ninguna bloquea prod)
- `tests/test_models.py`: el fixture `pg_session` usa `Session(connection=conn)` → debe
  ser `Session(bind=conn)` (da 4 errors al correr `@needs_postgres`).
- `test_match_todos_procesa_todos_perfiles` no se aísla (cuenta perfiles globales de la BD).
- Branch `dev` tiene perfiles/seguimientos/matches de prueba (ids ~49–52) de la validación → limpiar.
- Análisis de competencia: el badge "Adjudicatario" sale en todas las filas del resumen
  (redundante; el resumen solo lista ganadores). Mejora futura: incluir también oferentes
  que NO ganaron, para el panorama competitivo completo.
- Adjudicadas en BD con `fecha_publicacion`/`fecha_cierre` NULL (calidad de datos; revisar
  el refresh de estados terminales).
- `test_jobs_run_job_ca`: skip (pega a la API real sin mock); migrar a respx.

## Roadmap pendiente (detalle en docs/03-roadmap.md)
- **F-datos:** catálogo de compradores clasificados → organismos buscables (multi-select).
- **F-plan:** pestaña Plan Anual de Compra (consulta; requiere mini-spike de formato).
- **F10 UX:** acordeón de rubros con súper-categorías seleccionables, multi-select de
  organismos, y fix del mail de match (enlazar a la ficha de la app vía `APP_BASE_URL`).
- **F11:** feedback like/dislike con reponderación ligera (regresión logística, sin LLM).
- **Backlog:** worker offline de anexos en Raspberry Pi (OCR + embeddings), condicionado.

## Mapa de documentos
- `00-estado-actual.md` (este) · `01-analisis-api-mercado-publico.md` (contrato/gotchas API)
- `02-plan-desarrollo-y-auditoria.md` · `03-roadmap.md` (historial de fases + pendientes)
- `04-datos-abiertos.md` (lic-da: ítems/UNSPSC) · `05-competencia.md` (ofertas/ganador)
- `arquitectura.md` · `despliegue.md` · `operacion.md`

*Reglas duras del proyecto (API, free tier, multiusuario, arquitectura): ver `CLAUDE.md`.*
