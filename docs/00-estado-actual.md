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
  línea "N ocultas por baja relevancia — ver todas"), **F-feed-agrupado** (el dashboard
  reemplaza la lista plana por una vista agrupada por categorías: motivo/región/fuente),
  **F-automatch** (crear/editar un perfil dispara matching inmediato de ese perfil en
  background, leyendo solo oportunidades ya en BD y sin consumir cuota API),
  **F-passwords** (cambio de contraseña propio y reseteo admin con CSRF),
  **F-notificaciones** (resumen consolidado de descubrimiento por usuario + inmediatas solo
  para oportunidades con alertas activas),
  **deuda técnica — suite 100% verde**, **fix enlace ficha oficial** (el botón "Ver ficha
  oficial en MP" de licitaciones ahora abre — ver detalle abajo), F-deploy.
- **Suite: 499 tests verdes, 20 skipped, 0 failed, 0 errors** (incluye `@needs_postgres`
  contra la branch `dev` de Neon). Ya NO hay "1 failed + 4 errors" — ver detalle abajo.
- **Deuda técnica — suite 100% verde (este commit):**
  - `tests/test_models.py::pg_session`: `Session(connection=conn)` no es un kwarg válido en
    esta versión de SQLAlchemy 2 (daba 4 errors bajo `@needs_postgres`) → `Session(bind=conn)`
    (participa igual en la transacción ya abierta sobre `conn`; se pudo quitar el
    `type: ignore[call-arg]`).
  - Ese fix desenmascaró un bug de aislamiento que antes quedaba oculto porque el fixture
    fallaba ANTES de llegar a la aserción: `test_fts_encuentra_sin_tilde`/`test_fts_compra_agil`
    hacían `SELECT codigo FROM licitaciones WHERE tsv @@ ...` **sin filtrar por el código que
    el propio test insertó** — con datos preexistentes en `dev` (real o de otras pruebas),
    Postgres podía devolver CUALQUIER fila que matcheara el FTS, no necesariamente la del
    test. Fix: agregar `AND codigo = '<código del test>'` a ambas queries — sigue verificando
    que el FTS encuentra esa fila (si la condición FTS fuera falsa para ella, el `AND` no
    devuelve nada y el test igual falla), pero ya no depende de qué más haya en la tabla.
  - `test_match_todos_procesa_todos_perfiles` (`tests/test_matching.py`) asumía un total
    absoluto de 4 perfiles activos en toda la BD → se rompía si `dev` traía otros (p. ej. los
    ids ~49–52 de pruebas anteriores). Fix: cuenta los perfiles activos AJENOS al dataset
    ANTES de correr `match_todos` y afirma `perfiles_procesados == ajenos_antes + 4` (delta,
    no total absoluto) — ya no asume una BD exclusiva/vacía.
  - `test_jobs_run_job_ca` (`tests/test_api.py`) estaba con `@pytest.mark.skip` porque
    `BackgroundTasks` corre el job dentro del mismo ciclo del `TestClient` y `run_sync_ca`
    pegaba a la API real de Compra Ágil. Se migró a `respx` (mock de
    `GET https://api2.mercadopublico.cl/v2/compra-agil` con un listado vacío) y se quitó el
    skip — corre determinístico, sin red real.
  - **Hallazgo adicional (mismo criterio, no estaba en el pedido explícito):**
    `test_jobs_run_token_correcto` (`job="all"`, el default) también pegaba a la red real —
    confirmado con `--log-cli-level=DEBUG`: con la BD de test vacía, el ciclo completo
    alcanza a golpear `api.mercadopublico.cl/servicios/v1/publico/licitaciones.json` (activas)
    y hace `HEAD` al blob de datos abiertos (`transparenciachc.blob.core.windows.net/lic-da/`)
    antes de quedarse sin más trabajo (0 licitaciones → el resto de los jobs no-opean). Se
    mockearon ambos con `respx` (el del blob con `url__regex` porque la URL depende del
    mes vigente, no es fija).
  - **Verificado con Postgres real** (branch `dev`, `alembic current=9a1e6b2c5d7f` — SIGUE
    detrás del head `e1f4a7c9b2d6`; la migración pendiente de `MatchFeedback` no afecta a
    ninguno de los tests `@needs_postgres` tocados aquí, así que no fue necesario aplicarla
    para verificar esta fase). **No se corrió `alembic upgrade head`** — sigue siendo tarea
    del humano (ver "Migración workflow" — el asistente no aplica migraciones).
  - **Fuera de alcance, anotado para después (no tocado aquí):** limpiar la branch `dev` de
    Neon (perfiles/seguimientos/matches de prueba ids ~49–52) es una tarea de DATOS (SQL en
    `dev`), la hace Boris; investigar por qué las adjudicadas quedan con
    `fecha_publicacion`/`fecha_cierre` NULL es una investigación aparte del refresh de
    estados terminales, no mezclar con esta limpieza de tests.
- **F-automatch — crear/editar perfil dispara matching on-demand (este commit):** las rutas
  HTML `POST /perfiles/nuevo` y `POST /perfiles/{id}/editar` encolan, tras el `commit`
  exitoso, una `BackgroundTask` que ejecuta `match_perfil` solo para ese perfil. La tarea
  abre una `Session(engine)` nueva usando `request.app.state.engine`, recarga el perfil por
  id y hace no-op si no existe o quedó inactivo. No reusa la sesión de la request ni objetos
  ORM atados a ella. `match_perfil` sigue siendo puro: no llama clientes HTTP, no busca
  `raw_json` ni detalles, y por tanto no consume cuota API; solo lee oportunidades ya
  presentes en la BD y hace upsert en `oportunidades_match`.
  - Idempotencia: el upsert por `(perfil_id, fuente, codigo_oportunidad)` evita duplicados
    aunque se solape con el cron nocturno o con ediciones repetidas.
  - Aislamiento: cualquier excepción del matching queda logueada y no afecta la respuesta ya
    enviada al usuario.
  - Deuda conocida, preexistente: si una edición vuelve el perfil más restrictivo, los matches
    antiguos que dejaron de aplicar no se borran en esta fase; el filtro de relevancia del feed
    mitiga el impacto, pero queda como limpieza futura de `match_perfil`/`match_todos`.
- **F-passwords — cambio de contraseña y reseteo admin (este commit):** `/perfiles` suma en
  "Ajustes de tu cuenta" un formulario para cambiar la contraseña propia vía
  `POST /cuenta/password`: exige CSRF, contraseña actual correcta (`verify_password`), nueva
  contraseña igual a confirmación y mínimo 8 caracteres; actualiza solo el `password_hash` del
  usuario autenticado con `hash_password`. `/admin/usuarios` suma, por fila, reset de contraseña
  vía `POST /admin/usuarios/{uid}/password`, protegido por `html_require_admin` + CSRF y mínimo
  8 caracteres. El reset renderiza la nueva contraseña en el cuerpo de la respuesta una sola vez
  para que el admin la copie; no va en logs ni en query string. Sin migración.
- **F-notificaciones — resumen consolidado + inmediatas solo para seguidas (este commit):**
  se elimina el spam de “un correo por match”. Los matches nuevos ya no crean `Alerta`; en su
  lugar, `run_resumen`/`enviar_resumen` evalúa por usuario activo `dias_resumen` (3, 7 o 0 =
  nunca) y `ultimo_resumen_en`, cuenta los `OportunidadMatch.fecha_match` nuevos de perfiles
  activos (`fecha_match` es inmutable: primera vez que esa oportunidad matcheó ese perfil, no
  se re-toca por re-score), y solo si hay >0 envía un correo consolidado con top 5 por score + link a la app.
  Si no hay nuevos no envía y no mueve `ultimo_resumen_en`, para acumular ventana. Las
  inmediatas quedan limitadas a oportunidades con alertas activas (`OportunidadSeguida`):
  cambio de estado (`seguimiento_estado:*`) y cierre ≤48h (`seguimiento_cierre`). Job diario
  `digest` reemplazado por `resumen`; plantillas `digest.*`/`alerta_inmediata.*` eliminadas y
  nuevas `resumen.html`/`resumen.txt` con "Fuente: Dirección ChileCompra". Migración
  `d2f8a6c1b9e0`: agrega `usuarios.dias_resumen`, `usuarios.ultimo_resumen_en` y elimina
  `perfiles_busqueda.frecuencia_alerta`. **Operativo:** aplicar `alembic upgrade head` en
  Neon dev y luego prod lo hace Boris.
- **Fix — enlace "Ver ficha oficial en MP" no abría (este commit):** spike previo en
  `docs/10-enlace-ficha.md` (veredicto cerrado). Causa: el parámetro `qs` de
  `DetailsAcquisition.aspx` espera un token interno ENCRIPTADO, no el `CodigoExterno` en
  texto plano — ninguna API oficial (v1 ni v2) lo entrega. Fix: `_url_ficha`
  (`app/api/query.py`) para licitaciones ahora arma
  `.../DetailsAcquisition.aspx?idlicitacion={codigo}` en vez de `?qs={codigo}` —
  Mercado Público resuelve `idlicitacion=<CodigoExterno>` y redirige al `qs` correcto
  (verificado: reproduce byte a byte el token real para la licitación de prueba
  `1300-31-LE26`). `mostrar_ficha_oficial` (gate a solo procesos `PUBLICADA`) **sin
  cambios** — es un problema distinto (MP igual bloquea la ficha a no-dueños en procesos
  cerrados). Compra Ágil sin cambios (sigue al buscador genérico). Tests nuevos:
  `tests/test_query.py` (`_url_ficha`/`mostrar_ficha_oficial` puros, sin DB) +
  `tests/test_ficha_routes.py` (render end-to-end: licitación publicada usa
  `idlicitacion=`, cerrada no muestra el enlace). Sin migración.
- **F-feed-agrupado — feed agrupado por categorías (este commit):** el dashboard (`GET /`)
  ya NO es una lista plana paginada — siempre agrupa. `app/api/query.py::agrupar_oportunidades`
  recibe el conjunto YA filtrado por relevancia y ordenado (score/cierre) de
  `get_oportunidades_usuario` y arma grupos según `agrupar_por` (query param, default
  `"motivo"`): "motivo" expande cada match en un grupo por rubro UNSPSC hit, uno por
  keyword hit y uno si "organismo seguido" (repetición **intencional**: una oportunidad con
  2 rubros + 1 keyword aparece en 3 grupos), "sin motivo" cae en "Otros"; "region" agrupa por
  `region_nombre` ("Sin región" incluye TODAS las licitaciones, que nunca traen región);
  "fuente" agrupa Licitaciones/Compra Ágil. **No se ofrece agrupar por organismo/sector**:
  `codigo_organismo` viene vacío en licitaciones (`docs/08-datos-organismos.md` §3-bis d),
  la mayoría de los grupos quedarían "sin organismo" — sin valor para el usuario.
  Encabezado "Mostrando N oportunidad(es) · M aparición(es)" (M ≥ N cuando hay repetición).
  Grupos ordenados por su mejor score (desc); orden de items dentro de cada grupo intacto
  (el que ya traía `get_oportunidades_usuario`). Cada grupo se capa a 10 items
  (`CAP_GRUPO_DEFAULT`) con "ver más en este grupo" (`?grupo_expandido=<key>`, sin reordenar
  el resto). La paginación global (`pagina`/`total_paginas`) del feed **se elimina** — la
  reemplaza el cap por grupo.
  UI: acordeón Bootstrap (colapsable, expandido por defecto — chevron nativo del componente,
  sin JS propio para eso) + control "Agrupar por: Motivo/Región/Fuente" junto a los controles
  existentes de orden y relevancia. Descartar/seguir/feedback son por oportunidad y ya se
  reflejan en **todas** sus apariciones en la próxima carga (la query excluye descartadas
  ANTES de agrupar, así que ninguna reaparece en ningún grupo); además, cada tarjeta lleva
  `data-oportunidad-key="fuente:codigo"` y un pequeño script (inline, sin librería nueva)
  escucha `htmx:afterRequest` sobre `.../descartar` y remueve del DOM **todas** las
  apariciones al instante (sin esperar un reload), para que no quede una copia obsoleta
  visible en otro grupo tras descartar desde uno.
  Gotcha de implementación: en Jinja, `dict.items` colisiona con el método builtin
  `dict.items()` cuando se accede por punto (`grupo.items` devuelve el método, no la lista) —
  la plantilla usa `grupo['items']` (bracket) en vez de `grupo.items` para ese campo.
  Sin migración (agrupar es lógica de query/plantilla; las razones ya vivían en
  `OportunidadMatch.razones`). Tests: `tests/test_feed_agrupado.py` (unit de
  `agrupar_oportunidades` sin DB + integración de la ruta).
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
enriquecida con razones legibles del match; recibir **resúmenes consolidados** de nuevas
oportunidades por usuario; **activar alertas/archivar** oportunidades puntuales y recibir
**alertas inmediatas** cuando cambian de estado o están por cerrar; y al adjudicarse, ver el
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
- ~~`tests/test_models.py`: el fixture `pg_session` usa `Session(connection=conn)` → debe
  ser `Session(bind=conn)`~~ — **resuelto** (deuda técnica — suite 100% verde, ver arriba).
- ~~`test_match_todos_procesa_todos_perfiles` no se aísla~~ — **resuelto**: ahora compara un
  delta (perfiles ajenos activos antes + los 4 del dataset), no un total absoluto.
- ~~`test_jobs_run_job_ca`: skip (pega a la API real sin mock)~~ — **resuelto**: migrado a
  `respx`, ya no hay skip; de paso se detectó y mockeó otro test (`test_jobs_run_token_correcto`,
  `job="all"`) que también pegaba a la red real.
- ~~`test_fts_encuentra_sin_tilde`/`test_fts_compra_agil` sin filtro por código propio~~ —
  **resuelto** (hallazgo de esta misma fase, quedaba oculto detrás del bug de `pg_session`).
- Branch `dev` tiene perfiles/seguimientos/matches de prueba (ids ~49–52) de la validación →
  limpiar (tarea de DATOS/SQL en `dev`, la hace Boris — no es cambio de código, fuera de
  alcance de la fase de deuda técnica de tests).
- ~~Análisis de competencia: badge "Adjudicatario" en todas las filas; resumen solo lista
  ganadores~~ — **resuelto en F10 parte 3**: `resumen_competencia` ahora incluye también a
  quienes ofertaron y no ganaron (`items_ofertados`/`items_ganados`/`total_adjudicado` por
  proveedor), y el badge "Ganó" solo aparece en la(s) fila(s) ganadora(s).
- Adjudicadas en BD con `fecha_publicacion`/`fecha_cierre` NULL (calidad de datos; revisar
  el refresh de estados terminales — investigación aparte, no mezclar con la limpieza de
  tests de esta fase).
- Migración `e1f4a7c9b2d6` (`MatchFeedback`) sigue sin aplicarse en `dev` (`alembic current`
  = `9a1e6b2c5d7f`, detrás del head) — pendiente que Boris corra `alembic upgrade head`; no
  bloqueó la verificación `@needs_postgres` de esta fase porque ninguno de esos tests toca
  `MatchFeedback`.

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
