# Prompt de implementación — F-plat-2 (anexos → líneas: sonda de documentos, carga manual y extracción con OpenRouter) · 3-nov-2026

> Para Claude Code. **Modelo: Opus** (dependencia externa nueva con secreto y presupuesto, subida de archivos,
> texto no confiable hacia un modelo, regla 20 sobre un negativo). Una fase = un commit, **sin push**.
> **Con migración.** Va después de F-plat-1. Origen: `docs/17-plan-pivote-plataforma-ca.md` §2 C3.
> **Revisado contra el código y contra la muestra real de anexos el 4-nov-2026 (HEAD `d2b13e8`).** Lo marcado
> [V] está verificado. Si algo no calza con el código (sobre todo con lo que dejen F-plat-3 y F-plat-1), gana
> el código: anotarlo en el commit y no inventar.
>
> **Hallazgos de la muestra [V, `data/mercado/anexos_eval/archivos/`, 32 archivos]:** 25 PDF, 6 DOCX, 1 XLSX;
> 2 pares son duplicados exactos (30 únicos). **17 de los 25 PDF no tienen capa de texto (escaneados)**: sin
> visión no hay extracción. 1 PDF está malformado (pdfplumber lee 0 páginas). Tamaño: mediana 294 KB, máx.
> 2,5 MB, total 13,9 MB.

## 0. Antes de escribir código

### 0.a Sonda humana primero (`scripts/smoke_test.py --documentos`) — regla 20
La guía v3.0 documenta `documentos[].{id, nombre}` en el detalle de CA y **no documenta** descarga [V en la
guía]. Que el servidor no la tenga es [I] hasta probarlo. `mp_v2.py` (~l. 305) ya lee `documentos` del
detalle, pero solo para contar (`CompraAgilDetalle.n_documentos`) y **no se persiste** [V]. `scripts/smoke_test.py`
**no tiene** `--documentos` [V]: esta fase lo escribe primero (función `sonda_documentos`, mismo patrón que
`sonda_claves_ca`). La sonda (solo la ejecuta Boris, con `MP_TICKET`):
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
`app/clients/base.py` (patrón de cliente), `app/core/logging.py` (máscara: tupla `_ENV_VARS` [V]; agregar
`OPENROUTER_API_KEY`), `app/catalogos/canonico.py` y el módulo donde F-plat-1 haya dejado la lógica de
`ca_lineas` (el prompt de F-plat-1 no lo fija: buscarlo), `app/models/tables.py` (`ca_lineas`,
`producto_alias`), `app/core/settings.py` (campos secretos con `repr=False` [V]), `app/clients/mp_v2.py`
(`documentos`), el importador de proveedores de F-plat-3 (`app/mercado/`).

### 0.c Dependencias nuevas (proponer y justificar en el commit; nada fuera de esto)
`pdfplumber` (texto y tablas de PDF; desde 0.11 trae `pypdfium2`, que permite **renderizar páginas a
imagen** con `page.to_image()` sin otra dependencia [V 0.11.10]), `openpyxl` (ya entró en F-plat-3),
`python-docx`. OpenRouter se consume con `httpx` (REST compatible con el formato de chat de OpenAI): **sin
SDK**. Imágenes: se envían al modelo con visión como `data:` base64; sin OCR local. `.xls` (formato viejo) no
se acepta: `openpyxl` no lo lee y `xlrd` sería otra dependencia; se pide guardarlo como `.xlsx`.

## 1. Cambios

### 1.1 Cliente OpenRouter (`app/clients/openrouter.py`) — capa anti-corrupción
- `OpenRouterClient(settings, engine)`: `extraer_lineas(texto: str | None, imagen: bytes | None, mime) ->
  ExtraccionAnexo` (dataclass en `clients/types.py`: `lineas: list[LineaExtraida(texto, cantidad, unidad,
  formato, observacion)]`, `modelo`, `tokens_in`, `tokens_out`, `costo_usd`, `desde_cache: bool`).
- Settings: `openrouter_api_key` (`repr=False`, enmascarada en el logger como `MP_TICKET`, con test),
  `llm_modelo_extraccion` (str, sin default fijo en código: se toma del `.env`; si falta, el cliente está
  deshabilitado; **debe aceptar imágenes**: la mayoría de los anexos son escaneados), `llm_presupuesto_mes_usd` (default 5.0), `llm_timeout_s` (60), `llm_max_tokens` (4.000).
- Prompt: sistema en español, pide **solo JSON** con el esquema; el contenido del anexo va entre
  delimitadores y se declara dato, no instrucción. `response_format` JSON si el modelo lo soporta; si la
  respuesta no parsea, un reintento con el error citado y luego `ExtraccionAnexo(lineas=[])` + log WARNING.
  Nunca romper (regla 6).
- **Presupuesto persistido** (regla 10): tabla `llm_uso` (`mes`, `requests`, `tokens_in`, `tokens_out`,
  `costo_usd`) con upsert; el costo sale de los campos `usage` de la respuesta (si OpenRouter no lo
  entrega, estimar con `llm_costo_por_mtoken_in/out` configurables y marcar `estimado`). Si el gasto del
  mes ≥ presupuesto → `LLMPresupuestoAgotado` y el flujo sigue en manual.
- **Caché** en tabla `llm_cache` (clave `sha256(modelo + version_prompt + sha256 del contenido enviado)`,
  donde el contenido es el texto o las imágenes; `respuesta` JSONB, `creado_en`): el mismo anexo nunca se paga
  dos veces, aunque llegue en otra CA (hay duplicados exactos en la muestra [V]). Con solo `modelo + texto`,
  todos los escaneados (texto vacío) compartirían la clave.
- Rate limit propio 1 req/s; 429 de OpenRouter → esperar `Retry-After` o 30 s, máx. 3 veces.

### 1.2 Carga del anexo (ficha de CA → "Subir anexo")
- `POST /oportunidad/{fuente}/{codigo}/anexo` (ruta de la ficha real [V], solo `fuente` de CA; multipart,
  CSRF, usuario con empresa). Acepta `pdf|xlsx|docx|png|jpg|jpeg`, ≤ 10 MB, validando **firma del archivo**
  (magic bytes), no la extensión. Nunca se ejecuta ni se sirve de vuelta como HTML. El archivo se procesa en
  memoria y **no se guarda** (regla 10: no al disco; regla 11: no a Neon).
- Tabla `ca_anexos`: `id`, `ca_codigo` FK, `nombre_original`, `mime`, `tamano`, `sha256`, `paginas`,
  `escaneado` (bool), `texto_extraido` (Text, ≤ 50.000 chars), `subido_por`, `subido_en`, `extraccion_estado`
  (`pendiente|hecha|sin_presupuesto|error`), `extraccion_modelo`, `extraccion_costo_usd`.
  **Sin columna con el binario**: con BYTEA de hasta 2 MB por anexo, unos cientos de anexos se comen la
  holgura de Neon, y `mercado-backfill` se detiene con la base en 350 MB [V, `mercado_backfill_tope_bd_mb`].
  El original siempre se puede volver a bajar desde la ficha pública. Regla 11: medir bytes por fila y
  anotarlo en el commit.
- `compras_agiles.n_documentos` (int, nullable): persistir lo que `mp_v2` ya cuenta [V]. Sin requests extra;
  sirve para la bandera de CA con anexos y para §1.6.
- Texto: `app/mercado/anexos.py: extraer_texto(bytes, mime) -> ResultadoTexto(texto, imagenes, paginas,
  escaneado)` (pdfplumber con tablas → filas con `|`; openpyxl todas las hojas; python-docx párrafos y tablas).
  **PDF sin capa de texto** (< 50 caracteres por página): renderizar con `page.to_image(resolution=150)` las
  primeras `llm_max_paginas_imagen` páginas (setting, default 5) → PNG → al modelo como imágenes; si hay más
  páginas, avisarlo en la vista. Imagen suelta → al modelo como imagen. PDF malformado o protegido → estado
  `error`, flujo manual (regla 6: nunca romper).

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
(producto, formato, precio) → confirmar = importación de F-plat-3 (con historial). Reutilizar la vista
previa y la confirmación que F-plat-3 haya dejado; no duplicar el importador. Es el mismo extractor con otro
esquema de salida (`precio` en vez de `cantidad`). Ownership de F-plat-3: solo proveedores de la empresa
del usuario (404 si no).

### 1.5 Set de evaluación y elección del modelo (`scripts/mercado/evaluar_extraccion.py`, lo corre Boris)
- `tests/fixtures/anexos/` **no** lleva anexos reales (pueden traer datos personales). Los anexos reales ya
  están bajados [V]: `data/mercado/anexos_eval/archivos/` (32 archivos) y `muestra_30_ca_alimentos.xlsx`
  (columnas `codigo`, `archivo`, `n_lineas`, entre otras; `archivo` aún vacía). La verdad va en
  `lineas_esperadas.csv` (separador `;`, BOM UTF-8; columnas `anexo;n;producto_texto;cantidad;unidad;revisado;nota`,
  donde `anexo` = nombre del archivo). **Prellenada por Claude el 4-nov (602 filas, 32 archivos) y pendiente de
  revisión de Boris**: el script **solo usa los anexos con todas sus filas `revisado = si`** e informa cuántos
  quedan fuera, para que una verdad escrita por un LLM no evalúe a otros LLM sin control humano.
  Convenciones de la verdad: toda línea del anexo cuenta, también las que no son alimento (nota "no alimento";
  el script reporta el acierto con y sin ellas); cantidad = total pedido (si el anexo dice "15 canastas, cada una
  con…", se multiplica; nota "MULTIPLICADOR"); en tablas con consolidado y desglose por actividad vale el
  consolidado; el formato va en `producto_texto` y `unidad` es la del pedido (unidad, kg, L, caja, bolsa…). Un
  archivo vacío (`abarrotes emergencia.docx`) espera extracción vacía. Todo fuera de git (`data/` en `.gitignore`).
- El script corre cada modelo de `--modelos a,b,c` sobre los 30, usa el caché, y reporta por modelo:
  % de líneas correctas (producto normalizado + cantidad + unidad), % de anexos con 100 %, costo total,
  latencia mediana, **separado por tipo** (texto / escaneado / docx-xlsx). **Criterio: el más barato con
  ≥ 95 % de líneas correctas en total y ≥ 90 % en escaneados.** (OK de Boris, 4-nov-2026.) Boris fija
  `LLM_MODELO_EXTRACCION` en `.env` con el ganador y anota el resultado en `docs/decisiones.md`.
- En tests (respx) se usan anexos sintéticos: un XLSX generado con openpyxl en el propio test, un DOCX
  generado con python-docx, y **dos PDF de fixture de 1 página commiteados, sin datos reales** (uno con texto
  y otro solo imagen, para el camino de escaneado; pdfplumber no escribe PDF). Respuestas del modelo
  mockeadas, incluida una inválida.

### 1.6 Descarga desde la API (solo si la sonda la confirma; `documentos_api_habilitado`)
`mp_v2.py: descargar_documento(codigo, id) -> bytes | None` con el mismo rate limit y cuota (consume
`MP_TICKET`: **cuenta como request de CA**, R-p1). `mercado-ca` descarga los anexos de CA vigentes en alcance (requiere F-merc-3: hoy no se baja el detalle de las vigentes ni se guarda `n_documentos` [V, 4-nov])
con `detalle_lineas = sin_detalle` y `n_documentos > 0`, tope `MERCADO_ANEXOS_MAX` (20) por corrida, y los pasa por 1.2–1.3 con
`subido_por = NULL`. Si está deshabilitado, nada de esto corre y los tests lo cubren igual.

## 2. Tests
Máscara del secreto; presupuesto (agotado → manual, upsert mensual); caché (segunda llamada sin request);
respuesta inválida → reintento → vacío; firma de archivo rechazada (un `.exe` renombrado `.pdf`); tamaño;
extracción de texto por tipo; PDF escaneado → imágenes (render mockeable); PDF malformado → `error` sin
excepción; clave de caché distinta para dos escaneados distintos; ninguna columna guarda el binario;
`n_documentos` persistido; líneas `dudosa` que no pisan `ok`; ownership; `documentos_api_habilitado` en ambos
valores. `ruff`, `mypy app`, `pytest -rs` sin saltados, contra Postgres local (`TEST_DATABASE_URL`).

## 3. Cierre
Changelog ("Anexos de Compra Ágil convertidos en líneas, con revisión"). `00-estado.md`. `CLAUDE.md` ya
declara `OPENROUTER_API_KEY` secreto en la regla 26 [V]; agregarla también a la lista de la regla 1 (única
edición de reglas duras) y a `_ENV_VARS` del logger. Commit:
`F-plat-2: anexos de Compra Ágil a líneas con extracción asistida y revisión humana`.

## 4. Pasos de Boris
1. Tras el commit: `python scripts/smoke_test.py --documentos` (de día, ~3–5 requests) → pegar salida
   (decide §1.6). Migración según `docs/despliegue.md` (nunca `alembic upgrade` manual contra prod).
2. Cuenta en OpenRouter, `OPENROUTER_API_KEY` en `.env` (y en GitHub Secrets solo si §1.6 aplica).
3. Los anexos ya están bajados [V]. Falta llenar la columna `archivo` de la muestra y `lineas_esperadas.csv`
   (borrar las filas EJEMPLO). Con 2 duplicados exactos, quedan 30 únicos.
4. `python scripts/mercado/evaluar_extraccion.py --modelos <3–4 candidatos>` → elegir y fijar el modelo.
5. Subir 5 anexos reales en la ficha y medir minutos por CA (meta ≤ 5 min con revisión).
