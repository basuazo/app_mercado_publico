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

## Deuda técnica — driver psycopg v3
**Estado: pendiente de confirmar.**
- `make_engine` debe normalizar `postgresql://` → `postgresql+psycopg://` para que
  `pytest` colecte `test_api.py` (hoy falla por `psycopg2` no instalado).
- Prompt ya entregado; verificar que quedó aplicado y la suite colecta los 228 tests.

---

## F9a — Exponer filtros existentes + validar cobertura UNSPSC
**Estado: en curso.** Rutas `crear`/`editar` ya parsean y pasan regiones y montos.
- **Falta confirmar/terminar:**
  - Formulario `perfiles.html`: checkboxes de región (desde `seeds.REGIONES`),
    inputs de monto mín/máx, y mostrar estos filtros en la lista de perfiles.
  - Script `scripts/validar_unspsc.py` (read-only): % de ítems con `codigo_producto`
    poblado y con formato UNSPSC válido; top prefijos de 2 y 4 dígitos.
  - Tests del parseo de regiones/montos y de la persistencia.
  - ruff/mypy/pytest verde → commit `F9a`.
- **Salida a auditar:** el reporte de UNSPSC define cómo diseñamos F9b.

## F9b — Rubros UNSPSC + seguir organismos
**Estado: siguiente. Depende de F9a (números de cobertura UNSPSC).**
- Modelo: nuevas columnas en `PerfilBusqueda` — `categorias_unspsc` (list[str],
  prefijos de código) y `organismos_seguidos` (list[str], código/RUT de organismo).
  Migración Alembic.
- Matching (`app/matching/engine.py`): rubros y organismos como **recall aditivo**
  (suman candidatos aunque no haya keyword); región y monto siguen siendo filtros
  restrictivos. Nuevos componentes de score + razones (`categorias_hit`,
  `organismo_seguido`) y su traducción en `presentacion.py`.
- Formulario: campos para rubros (prefijos UNSPSC) y organismos a seguir.
- Tests de recall/score por rubro y por organismo. Commit `F9b`.

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
