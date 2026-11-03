# Prompt de implementación — F-plat-2 (anexos → líneas: sonda de documentos, carga manual y extracción con OpenRouter) · 3-nov-2026

> Para Claude Code. **Modelo: Opus** (dependencia externa nueva con secreto y presupuesto, subida de archivos,
> texto no confiable hacia un modelo, regla 20 sobre un negativo). Una fase = un commit, **sin push**.
> **Con migración.** Va después de F-plat-1. Origen: `docs/17-plan-pivote-plataforma-ca.md` §2 C3.

## 0. Antes de escribir código

### 0.a Sonda humana primero (`scripts/smoke_test.py --documentos`) — regla 20
La guía v3.0 documenta `documentos[].{id, nombre}` en el detalle de CA y **no documenta** descarga [V en la
guía]. Que el servidor no la tenga es [I] hasta probarlo. La sonda (solo la ejecuta Boris, con `MP_TICKET`):
1. Detalle de una CA de agosto 2026 con `documentos` no vacío: imprime `len(documentos)` y las **claves** de
   `documentos[0]` (no los valores).
2. Prueba, con 1 request cada una y respetando el rate limit, `GET /v2/compra-agil/{codigo}/documentos/{id}`,
   `GET /v2/compra-agil/{codigo}/documento/{id}` y `GET /v2/documentos/{id}`; imprime status y `content-type`.
   Antes, revisar si en el bundle JS público del buscador (`buscador.mercadopublico.cl`, método de
   `docs/04-datos-abiertos.md` §0) aparece una ruta de descarga y agregarla a la lista. **No descargar HTML
   de fichas** (regla 9).
3. Anotar el resultado en `docs/01-analisis-api-mercado-publico.md` §8 con [V].
Si existe descarga: implementar además §1.6. Si no: solo flujo manual (§1.2–§1.5). **La fase se escribe
para ambos casos; el job de §1.6 queda detrás de `settings.documentos_api_habilitado=False` hasta la sonda.**

### 0.b Lectura
`CLAUDE.md` (1, 6, 9, 10, 11, 12, 18, 20), `docs/17-plan-pivote-plataforma-ca.md` §2 C3, §3 (seguridad),
`app/clients/base.py` (patrón de cliente, máscara de secretos en `app/core/logging.py`),
`app/catalogos/canonico.py` y `app/mercado/lineas.py` (F-plat-1), `app/models/tables.py` (`ca_lineas`,
`producto_alias`), `app/core/settings.py`.

### 0.c Dependencias nuevas (proponer y justificar en el commit; nada fuera de esto)
`pdfplumber` (texto y tablas de PDF), `openpyxl` (ya entró en F-plat-3), `python-docx`. OpenRouter se
consume con `httpx` (REST compatible con el formato de chat de OpenAI): **sin SDK**. Imágenes: se envían
al modelo con visión como `data:` base64; sin OCR local.

## 1. Cambios

### 1.1 Cliente OpenRouter (`app/clients/openrouter.py`) — capa anti-corrupción
- `OpenRouterClient(settings, engine)`: `extraer_lineas(texto: str | None, imagen: bytes | None, mime) ->
  ExtraccionAnexo` (dataclass en `clients/types.py`: `lineas: list[LineaExtraida(texto, cantidad, unidad,
  formato, observacion)]`, `modelo`, `tokens_in`, `tokens_out`, `costo_usd`, `desde_cache: bool`).
- Settings: `openrouter_api_key` (`repr=False`, enmascarada en el logger como `MP_TICKET`, con test),
  `llm_modelo_extraccion` (str, sin default fijo en código: se toma del `.env`; si falta, el cliente está
  deshabilitado), `llm_presupuesto_mes_usd` (default 5.0), `llm_timeout_s` (60), `llm_max_tokens` (4.000).
- Prompt: sistema en español, pide **solo JSON** con el esquema; el contenido del anexo va entre
  delimitadores y se declara dato, no instrucción. `response_format` JSON si el modelo lo soporta; si la
  respuesta no parsea, un reintento con el error citado y luego `ExtraccionAnexo(lineas=[])` + log WARNING.
  Nunca romper (regla 6).
- **Presupuesto persistido** (regla 10): tabla `llm_uso` (`mes`, `requests`, `tokens_in`, `tokens_out`,
  `costo_usd`) con upsert; el costo sale de los campos `usage` de la respuesta (si OpenRouter no lo
  entrega, estimar con `llm_costo_por_mtoken_in/out` configurables y marcar `estimado`). Si el gasto del
  mes ≥ presupuesto → `LLMPresupuestoAgotado` y el flujo sigue en manual.
- **Caché** en tabla `llm_cache` (`sha256(modelo + texto)`, `respuesta` JSONB, `creado_en`): el mismo anexo
  nunca se paga dos veces.
- Rate limit propio 1 req/s; 429 de OpenRouter → esperar `Retry-After` o 30 s, máx. 3 veces.

### 1.2 Carga del anexo (ficha de CA → "Subir anexo")
- `POST /oportunidad/ca/{codigo}/anexo` (multipart, CSRF, usuario con empresa). Acepta `pdf|xlsx|xls|docx|
  png|jpg|jpeg`, ≤ 10 MB, validando **firma del archivo** (magic bytes), no la extensión. Nunca se ejecuta ni
  se sirve de vuelta como HTML.
- Tabla `ca_anexos`: `id`, `ca_codigo` FK, `nombre_original`, `mime`, `tamano`, `sha256`, `contenido`
  (BYTEA, solo si ≤ 2 MB; si no, NULL), `texto_extraido` (Text, ≤ 200.000 chars), `subido_por`, `subido_en`,
  `extraccion_estado` (`pendiente|hecha|sin_presupuesto|error`), `extraccion_modelo`, `extraccion_costo_usd`.
  Regla 11: medir bytes por anexo y anotarlo en el commit.
- Texto: `app/mercado/anexos.py: extraer_texto(bytes, mime) -> str` (pdfplumber con tablas → filas con `|`;
  openpyxl todas las hojas; python-docx párrafos y tablas). Imagen → sin texto, va al modelo como imagen.

### 1.3 Extracción → líneas dudosas
- Tras subir: si hay texto o imagen, cliente OpenRouter → `LineaExtraida[]` → por cada una,
  `sugerir_producto` (F-plat-1) → `ca_lineas` con `origen=anexo`, `estado=dudosa` (aunque la confianza sea
  alta: **toda línea de anexo la confirma un humano**), `texto_origen` = texto de la línea extraída.
  Reemplaza las líneas `pendiente` de esa CA; no toca líneas `ok` de origen `api` ni `manual`.
- Respuesta HTMX: tabla editable de líneas (producto con búsqueda, formato, cantidad), botones "Confirmar
  todas" / "Confirmar fila" / "Eliminar fila" / "Agregar fila". Confirmar → `ok`, alias aprendido, `detalle_lineas`
  recalculado (F-plat-1 §1.4).
- Sin presupuesto o sin `llm_modelo_extraccion`: la misma tabla vacía con "Agregar fila" y un aviso; el
  anexo queda guardado con `sin_presupuesto`. **El flujo manual nunca depende del modelo.**

### 1.4 Lista de precios de proveedor por foto/PDF (reutiliza 1.1–1.3)
`POST /proveedores/{id}/lista-precios` → mismas dependencias → vista previa de `proveedor_productos`
(producto, formato, precio) → confirmar = importación de F-plat-3 (con historial). Es el mismo extractor con
otro esquema de salida (`precio` en vez de `cantidad`).

### 1.5 Set de evaluación y elección del modelo (`scripts/mercado/evaluar_extraccion.py`, lo corre Boris)
- `tests/fixtures/anexos/` **no** lleva anexos reales (pueden traer datos personales). Los 30 anexos reales y
  su verdad (`lineas_esperadas.csv`: anexo, n, producto_texto, cantidad, unidad) viven en
  `data/mercado/anexos_eval/` (fuera de git, ya está en `.gitignore` por `data/mercado/`).
- El script corre cada modelo de `--modelos a,b,c` sobre los 30, usa el caché, y reporta por modelo:
  % de líneas correctas (producto normalizado + cantidad + unidad), % de anexos con 100 %, costo total,
  latencia mediana. **Criterio: el más barato con ≥ 95 % de líneas correctas.** Boris fija
  `LLM_MODELO_EXTRACCION` en `.env` con el ganador y anota el resultado en `docs/decisiones.md`.
- En tests (respx) se usan 3 anexos sintéticos (un XLSX generado con openpyxl en el propio test, un DOCX
  con python-docx y un PDF mínimo escrito a mano o generado con pdfplumber no aplica: usar un PDF de
  fixture de 1 página creado una vez y commiteado, sin datos reales) y respuestas del modelo mockeadas,
  incluida una inválida.

### 1.6 Descarga desde la API (solo si la sonda la confirma; `documentos_api_habilitado`)
`mp_v2.py: descargar_documento(codigo, id) -> bytes | None` con el mismo rate limit y cuota (consume
`MP_TICKET`: **cuenta como request de CA**, R-p1). `mercado-ca` descarga los anexos de CA vigentes en alcance
con `detalle_lineas = sin_detalle`, tope `MERCADO_ANEXOS_MAX` (20) por corrida, y los pasa por 1.2–1.3 con
`subido_por = NULL`. Si está deshabilitado, nada de esto corre y los tests lo cubren igual.

## 2. Tests
Máscara del secreto; presupuesto (agotado → manual, upsert mensual); caché (segunda llamada sin request);
respuesta inválida → reintento → vacío; firma de archivo rechazada (un `.exe` renombrado `.pdf`); tamaño;
extracción de texto por tipo; líneas `dudosa` que no pisan `ok`; ownership; `documentos_api_habilitado`
en ambos valores. `ruff`, `mypy app`, `pytest -rs` sin saltados.

## 3. Cierre
Changelog ("Anexos de Compra Ágil convertidos en líneas, con revisión"). `00-estado.md`. `CLAUDE.md` regla 1:
agregar `OPENROUTER_API_KEY` a los secretos (única edición de reglas duras). Commit:
`F-plat-2: anexos de Compra Ágil a líneas con extracción asistida y revisión humana`.

## 4. Pasos de Boris
1. `python scripts/smoke_test.py --documentos` → pegar salida (decide §1.6).
2. Cuenta en OpenRouter, `OPENROUTER_API_KEY` en `.env` (y en GitHub Secrets solo si §1.6 aplica).
3. Bajar 30 anexos de alimentos desde la ficha pública y llenar `lineas_esperadas.csv`.
4. `python scripts/mercado/evaluar_extraccion.py --modelos <3–4 candidatos>` → elegir y fijar el modelo.
5. Subir 5 anexos reales en la ficha y medir minutos por CA (meta ≤ 5 min con revisión).
