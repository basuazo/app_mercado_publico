# Roadmap mp-oportunidades

> Paso a paso de lo pendiente, ordenado por prioridad. Una fase por sesión/commit
> (regla de flujo de trabajo). Flujo de cada fase: Claude genera un prompt →
> se ejecuta en Claude Code → se audita la salida → commit.

## Reglas transversales (aplican a toda fase)
- Antes de cerrar: `ruff check .` ; `mypy app` ; `pytest` — todo verde.
- La suite necesita `DATABASE_URL` con driver psycopg v3 (`postgresql+psycopg://`).
- Sin nuevas dependencias fuera del stack sin justificar. Todo $0 (free tier).
- Commits en español con prefijo de fase.

---

## F8 — Resultados legibles + ficha enriquecida + link condicional
**Estado: implementado, pendiente de commit.**
- Razones del score traducidas a texto legible (`app/api/presentacion.py`).
- Ficha con organismo, región (nombre), fechas, montos, tabla de ítems/productos.
- Botón "Ver ficha oficial" solo en procesos abiertos (evita el error
  "No Pertenece a la unidad de la ficha"); link de Compra Ágil corregido.
- **Pendiente:** correr ruff/mypy/pytest y commitear
  (`F8: resultados legibles, ficha enriquecida y link condicional`).

## Deuda técnica
- **Driver psycopg v3: RESUELTO.** Confirmado en runtime real al correr
  `validar_unspsc.py` y la suite contra Neon.
- **Ingesta por lotes: RESUELTO (commit f4fe80d).** `commit_con_retry` + batching
  (`ingest_batch_size`) + `--limit`; un lote que falla se descarta sin abortar.
- **`test_jobs_run_job_ca`:** test preexistente que pega a la API real sin mock
  (viola "tests de red SIEMPRE mockeados"). Migrar a respx. No bloqueante.
- **`TestRefreshEstados` × 4: tests frágiles (no bug de lógica).** Hardcodean la
  fecha `2026-06-18` contra el reloj real → fallan al pasar esa ventana. Fix:
  congelar el tiempo con freezegun (ya en deps). No urgente.
- **Parseo de fecha del listado v1: RESUELTO.** `parse_fecha_v1` aceptaba solo
  ddmmaaaa; el listado `activas` trae ISO → fecha_cierre quedaba NULL y el recall
  descartaba todo. Ahora acepta ISO + ddmmaaaa, y upsert_basica no sobreescribe con
  nulos. (Confirmar si afectaba a producción.)

---

## F9a — Exponer filtros existentes + validar cobertura UNSPSC
**Estado: HECHO (commit 38a34ac).** Formulario expone regiones/montos, parseo
defensivo, fix `p.excluir`→`p.keywords_excluir`. Script `scripts/validar_unspsc.py`
confirmó cobertura UNSPSC en licitaciones ~99.5% válida; CA sin productos ingestados.

## F9b — Rubros UNSPSC + seguir organismos
**Estado: HECHO (commit cd479aa).** Migración `616613c3d7cf`; catálogo
`app/catalogos/unspsc.py` desde `data/unspsc_rubros.csv` (UNGM 22-jun-2026);
recall aditivo (FTS OR `codigo_producto LIKE 'prefijo%'` OR `organismo IN ...`),
`score_estructural` (+20 rubro / +15 organismo, tope 100), formulario con selector
de rubros + organismos, 36 tests. ruff/mypy verde.

## F9c — Consistencia recall/score (stemming)
**Estado: HECHO (commit 3436a55).** Detección de hits movida a Postgres FTS
(set-based, sin N+1) reusando la misma `websearch_to_tsquery('spanish', ...)` del
recall; `score_texto` sigue puro pero recibe hits stem-based. Eliminada la detección
por substring. Invariante recall/score documentada vía `keywords_validas()`.
- **Bonus:** corregido bug de precedencia `AND`/`OR` en los fragmentos FTS de
  exclusión (las exclusiones se "saltaban" en ciertos casos). Confirmado contra Neon.
- `@needs_postgres` corre verde por primera vez (95 passed), incluidos los 3 previos.

---

## Spike datos abiertos — HECHO
Ver `docs/04-datos-abiertos.md`. Fuente: `lic-da/{año}-{mes}.zip` (Azure Blob público,
sin ticket); `CodigoProductoONU` (UNSPSC 8 díg) a nivel de ítem, enlazado por
`CodigoExterno`. CSV ';' Latin-1 multilínea; dedup `(CodigoExterno, Codigoitem)`;
0,4% códigos de 9 díg ("CONSULTORIA") → manejo defensivo.

## F-rubros — Poblar licitacion_items desde datos abiertos — HECHO
Ingesta selectiva (solo activas sin ítems), streaming, nocturna, cursor por
`Last-Modified`. Validado en vivo: perfil solo-rubro → matches con razón de rubro, sin
gastar cuota para los ítems. La API (detalle) queda solo para enriquecer matches.
Nota producción: cobertura se construye con el `activas` completo + job nocturno; en
local quedó acotada por el `--limit 200` de prueba.

## F-datos — Compradores clasificados por sector (datos abiertos) — HECHO (alcance acotado)
Spike en `docs/08-datos-organismos.md`. Fuente: bulk
`GET https://mserv-datos-abiertos.chilecompra.cl/v1/elastic/organization/all` (datos
abiertos, sin ticket, sin cuota; envelope DISTINTO al resto: array JSON plano).
Cliente `listar_organismos_sector` (`app/clients/plan_compra.py`); normalización
`normalizar_sector` (`app/models/enums.py`, fallback "Sin clasificación"/`id_sector=8`
para `idSector` sin nombre o ausente del bulk). Columnas `sector`/`id_sector` en
`InstitucionPAC` (migración `c4a8e0f7b1d3`). Servicio `sync_sectores_organismos`
(`app/ingest/plan_compra.py`): upsert idempotente por `codigo_entidad`, TTL largo
(reutiliza `plan_compra_ttl_dias`), se invoca junto a `sync_instituciones_pac` en
`GET /plan-anual` y fuerza refresh si detecta filas sin clasificar (cubre el caso en
que `sync_instituciones_pac` reemplazó el catálogo y dejó `id_sector` en NULL).
Cobertura real ~85-87 % vía bulk, resto cae a "Sin clasificación" (ver spike §3-bis d).

**Diferido a F10 (rediseño de UI, fuera de este alcance):**
- **Agrupación del selector de organismos en el formulario de perfiles:** hoy
  `organismos_seguidos` (`app/api/templates/perfiles.html`) es un campo de texto libre
  separado por coma, **no un `<select>`** — no hay nada trivial que agrupar con
  `<optgroup>` sin rediseñar el campo. La capa de datos (sector/id_sector) ya está lista
  para cuando F10 construya el multi-select clasificado real.
- **Recomendación de organismos por rubro:** `getTreeMap/getSectors/{entCode}/{año}` solo
  trae nombres de segmento (top 10 por monto, sin código UNSPSC) — cruzar con los rubros
  del perfil exigiría *fuzzy matching* frágil y pierde lo que esté fuera del top 10
  (limitación real, ver spike §4). Queda pendiente de decidir si vale la pena igual.

## F-plan — Plan Anual de Compra (pestaña de consulta aparte) — HECHO
Spike en `docs/07-plan-anual.md`. Fuente: ZIP CSV en `pac-files.da.mercadopublico.cl`
filtrado por institución/año (datos abiertos, sin ticket, sin cuota). Cliente
`app/clients/plan_compra.py` (UTF-8 con BOM, sin quoting, reconstrucción de
descripciones multilínea sin comillas vía heurística de "cola plausible" de los 6
campos finales). Modelos `PlanCompraLinea`/`PlanCompraSync`/`InstitucionPAC`
(migración `b3f7c1d9e2a4`). Servicio on-demand `app/ingest/plan_compra.py`
(`get_plan`/`sync_instituciones_pac`) con TTL ~30 días, upsert idempotente
(borra+inserta el par institución/año) y caché de "sin_plan" (403). Ruta
`GET /plan-anual` (separada del feed, sin scoping de ownership — dato público) con
autocomplete de institución, selector de año y paginación. No incluye rubro/UNSPSC
ni mecanismo de compra (no vienen en esta fuente — limitación conocida, ver spike §6).

## F-seguir — Seguir/archivar oportunidades + alertas de avance — HECHO
Tabla `OportunidadSeguida`, migración `7c9d2a1f4b3e`; `Alerta` generalizada
(match_id|seguimiento_id). Rutas seguir/archivar/desarchivar/dejar-de-seguir + página
`/seguidas`; botones en ficha y nav. `detectar_cambio_estado_seguidas` (idempotente,
caso especial 'adjudicada') en `run_alerts`; lifecycle incluye seguidas no-matcheadas.
Mail de seguimiento enlaza a la ficha de la app vía `APP_BASE_URL` (degrada si no está).
Pendiente operativo: setear `APP_BASE_URL` en prod para links absolutos.

## F-competencia — Análisis de competitividad al adjudicar — HECHO
Spike en `docs/05-competencia.md`. Ganador = columna `Oferta seleccionada`;
reconstrucción por `Codigoitem` y totales por `RutProveedor` sumando `MontoLineaAdjudica`.
Modelo `OfertaCompetencia` (migración `9a1e6b2c5d7f`) + `Usuario.rut_proveedor` opcional;
cliente `stream_ofertas` (float defensivo: enteros, notación científica y coma-decimal);
`capturar_competencia` captura $0 desde `lic-da` para seguidas adjudicadas sin ofertas aún,
con fallback de escaneo de ~4 meses (ya que `fecha_publicacion` viene NULL en la práctica —
ver hallazgo del spike). Job nocturno tras `lifecycle` + CLI `run-once --job competencia`.
Vista "Análisis de competencia" (resumen por proveedor + detalle por ítem) en la ficha,
resalta el RUT propio si está configurado en `/perfiles`.
- Deuda aparte detectada (no corregida aquí): 100% de adjudicadas en BD con
  `fecha_publicacion`/`fecha_cierre` NULL — revisar el refresh de estados terminales.

## F10 — UX/UI
**Estado: parcial — formulario de perfiles y dashboard HECHOS; ficha y mail pendientes.**
(Tarea original nº1.) Enfoque: prototipo HTML iterado en el chat → aprobado → portado a
plantillas Jinja (stack actual Bootstrap). No se toca código hasta tener el diseño visado.

**Hecho — dashboard rediseñado + descartar + feedback (parte 2, este commit):**
- `index.html` rediseñado: tarjetas más escaneables vía macro `card_oportunidad`
  (`app/api/templates/_card_oportunidad.html`, reusada también para el partial HTMX) — bloque
  de score con color por tramo (≥80 verde, 50–79 ámbar, <50 gris), chip de urgencia "cierra en
  Xd" con color por tramo (≤3d rojo, ≤7d ámbar), monto a la derecha, meta
  "organismo · región · fuente", razones del match como chips.
- Orden configurable: toggle "Mejor match" (score desc) / "Cierran pronto" (días al cierre
  asc, nulos al final) vía `?orden=score|cierre`, preservando los filtros existentes
  (perfil, fuente, texto).
- Acciones por tarjeta vía HTMX (sin recargar), con fallback POST+redirect para clientes sin
  JS: **Ver ficha** (link existente), **Seguir** (reusa `POST .../seguir` de F-seguir, ahora
  HTMX-aware: responde con la tarjeta re-renderizada si `HX-Request`, redirect 303 si no),
  **Me sirve** (`POST .../me-sirve`, toggle) y **Descartar** (`POST .../descartar`, oculta el
  match del feed). HTMX "descartar" responde 200 con cuerpo vacío (no 204 — htmx no swapea
  ante un 204) para que la tarjeta desaparezca del DOM vía `hx-swap="outerHTML"`.
- "Descartar" es **distinto** de "archivar" (archivar sigue aplicando solo a seguidas).
  Reversible: banner "Ver descartadas (N)" → página `/descartadas` (mirror de `/seguidas`)
  con botón "Restaurar" (`POST .../deshacer-descarte`).
- Modelo nuevo `MatchFeedback` (`usuario_id`, `fuente`, `codigo_oportunidad`, `valor` enum
  {sirve, descarte}, `creado_en`, `actualizado_en`; unique por usuario+oportunidad — migración
  `e1f4a7c9b2d6`, down_revision `c4a8e0f7b1d3`) + servicio `app/matching/feedback.py` (ownership
  obligatorio, regla 17; alternar actualiza o borra, nunca duplica). Diseñada para que F11 la
  consuma directo como señal de entrenamiento (timestamp + valor + qué oportunidad bastan).
- Query del feed (`get_oportunidades_usuario`) excluye los matches con feedback "descarte" del
  usuario actual (salvo en `/descartadas`); expone `siguiendo` y `feedback` por ítem para que la
  tarjeta refleje el estado correcto sin N+1 (`listar_feedback_usuario`).
- **Esta fase solo REGISTRA la señal — no reordena ni reentrena nada.** F11 es quien consumirá
  `MatchFeedback` para reponderar el matching.
- Migración verificada solo con `alembic ... --sql` (offline, sin tocar ninguna BD real — ver
  nota operativa abajo); **no aplicada aún a la branch dev/prod de Neon**, pendiente de que el
  humano la corra (`alembic upgrade head`) — además la branch dev ya estaba 3 migraciones
  detrás de antes de esta fase (deuda preexistente, no introducida aquí).
- No verificado en navegador real (mismo motivo que `/perfiles` — sin herramienta de
  automatización en el entorno); verificado end-to-end con la app real vía `TestClient` (ciclo
  ASGI completo, plantillas Jinja reales) contra una BD sqlite descartable: render de
  tarjetas, orden score/cierre, toggle me-sirve, descartar+banner+`/descartadas`+restaurar,
  seguir vía HTMX con tarjeta parcial — los 8 pasos verificados manualmente, además de 19 tests
  nuevos (`tests/test_feedback_routes.py`) cubriendo lo mismo + CSRF + IDOR.

**Hecho — `/perfiles` (fase anterior):**
- Tarjeta de RUT de proveedor reetiquetada como "Ajustes de tu cuenta" (sin cambios de
  backend).
- Rubros UNSPSC: acordeón Bootstrap por segmento (macro `rubros_widget` en
  `perfiles.html`), checkbox "seleccionar todo el segmento" con estado indeterminado,
  buscador en cliente, chips removibles. `name="categorias_unspsc"` sin cambios — el
  backend ya aceptaba múltiples valores bajo ese nombre.
- Organismos: multi-select buscable agrupado por sector (macro `organismos_widget`),
  alimentado por `InstitucionPAC` (F-plan) + `sector`/`id_sector` (F-datos) vía
  `listar_organismos_catalogo` (`app/api/query.py`). El catálogo (~1.333 organismos) se
  emite **una sola vez** como `const MP_ORGANISMOS_CATALOGO` en JS y lo reusan todos los
  widgets (nuevo + cada edición) — no se duplica en el DOM por formulario. Submit sigue
  enviando `organismos_seguidos` como CSV de `codigo_entidad` por un input oculto; códigos
  preexistentes que no están en el catálogo (legado) se muestran igual como chip con el
  código crudo, sin perder datos.
- `GET /perfiles` ahora invoca `sync_instituciones_pac` + `sync_sectores_organismos` (igual
  que `/plan-anual`); sin red disponible, degrada al input de texto libre de organismos en
  vez de romper la página (regla 6) — verificado con respx simulando `ConnectError` y,
  en vivo, contra los endpoints reales (1.333 organismos, 8 sectores, ver
  `docs/08-datos-organismos.md`).
- Fuentes y regiones pasaron de checkboxes simples a toggles tipo pill (`btn-check`).
- Sin migración (no hay cambios de esquema en este commit; `sector`/`id_sector` ya
  existían desde F-datos).
- **No verificado con un navegador real** (sin herramienta de automatización de browser
  disponible en el entorno sin instalar una dependencia nueva, fuera del stack declarado):
  la interactividad JS (acordeón, chips, buscador) se validó con `node --check` + revisión
  manual de lógica + un end-to-end real contra el servidor local (login, GET/POST
  `/perfiles` reales, catálogo real de 1.333 organismos sincronizado en vivo) — pero no se
  hizo clic-a-clic en un navegador. Recomendado probarlo a mano antes de dar por cerrado
  el look final.

**Pendiente (fuera de este commit):**
- Rediseño de la ficha de detalle de oportunidad.
- Fix del mail de match (enlazar a la ficha de la app vía `APP_BASE_URL`, no a la URL no
  autorizada de MP).
- Conviene seguir en sesiones separadas (una fase/commit por parte, regla de flujo).

## F11 — Matching con feedback (like/dislike)
**Estado: pendiente — la señal ya se registra (F10 parte 2: tabla `MatchFeedback` +
`app/matching/feedback.py`), falta el modelo que la consuma.** Enfoque elegido:
**reponderación ligera**, sin LLM.
- Modelo de ranking liviano (regresión logística) sobre las features que ya produce
  el score; reentrena en milisegundos, pesos persistidos en Postgres, re-ordena
  resultados. Corre server-side, $0.
- Fuente de entrenamiento: `MatchFeedback` (timestamp + valor + qué oportunidad), joineado
  contra `OportunidadMatch` para las features (score, razones, perfil) en el momento de
  entrenar — no se duplica nada en la tabla de feedback.

---

## Backlog (más adelante, condicionado)

### Worker offline de anexos en la Raspberry Pi
**Estado: diferido. Condicionado a medir, tras F9, cuánto se recupera solo con lo
estructurado; y a la decisión sobre la regla 9.**
- Idea: batch nocturno en la Pi (encendida 24/7, bajo consumo, ventana 22:00–07:00)
  que procesa **solo el subconjunto ya pre-filtrado** (no el universo completo):
  baja anexos → extrae texto / OCR (Tesseract) de los escaneados → calcula
  **similitud semántica con embeddings** (modelo chico tipo e5/bge-small) contra los
  temas del perfil → escribe un `score_anexo` (y resumen opcional) de vuelta en Neon.
- Embeddings (no LLM generativo) para puntuar: más robusto y liviano en la Pi.
  Vectores **solo del subconjunto filtrado** (cuidar el límite de 0.5 GB de Neon /
  pgvector). El servidor $0 nunca hace el trabajo pesado.
- **Bloqueos reales que esto NO resuelve:** la API de licitaciones no entrega anexos
  (solo viven en la ficha web → bajarlos es scraping, regla 9; zona gris de los
  términos de ChileCompra). Compra Ágil v2 sí lista adjuntos: ahí el acceso es
  legítimo y sería el punto de partida.

### Match semántico en el servidor
Versión liviana del semántico (embeddings precalculados) integrada al matching
normal, si la medición lo justifica.
