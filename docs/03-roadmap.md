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
- **`test_jobs_run_job_ca`:** test preexistente que pega a la API real sin mock
  (viola "tests de red SIEMPRE mockeados"). Migrar a respx. No bloqueante.

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

## F10 — UX/UI
**Estado: pendiente.** (Tarea original nº1.)
- Rediseño de dashboard, ficha de detalle y formulario de perfiles.
- Enfoque: prototipo HTML iterado en el chat → aprobado → portado a plantillas Jinja
  (stack actual Bootstrap). No se toca código hasta tener el diseño visado.
- Conviene hacerlo después de F9 (cuando los perfiles y resultados ya son más ricos).

## F11 — Matching con feedback (like/dislike)
**Estado: pendiente.** Enfoque elegido: **reponderación ligera**, sin LLM.
- Tabla de feedback (like/dislike por match).
- Modelo de ranking liviano (regresión logística) sobre las features que ya produce
  el score; reentrena en milisegundos, pesos persistidos en Postgres, re-ordena
  resultados. Corre server-side, $0.
- Necesita algo de UI (botones de feedback) — coordinar con F10.

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
