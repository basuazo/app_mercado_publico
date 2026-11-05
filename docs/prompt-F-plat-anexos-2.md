# Prompt de implementación — F-plat-anexos-2 (anexos: carga masiva por código, prioridad de fuentes, cola y respaldo) · 5-nov-2026

> Para Claude Code. **Modelo: Opus** (toca la regla de fuentes de las líneas, un endpoint con token y el cliente
> LLM: reglas 1, 6, 9, 11, 12, 18). Una fase = un commit, **sin push**. **Con migración** (`down_revision` =
> cabeza vigente, una sola). Va sobre F-plat-2 y F-merc-3 commiteadas.
> Origen: cuello de botella 1 de la Revisión CBS (5-nov) y el acuerdo «la información de los archivos adjuntos tiene
> prioridad sobre los datos de la API» (`docs/18-revision-cbs-y-flujos-operativos.md` §2.2;
> `docs/19-adjuntos-ca-explicacion-tecnica.md`).

## 0. Antes de escribir código
- `CLAUDE.md` (1, 6, 9, 10, 11, 12, 17, 18, 20, 26). `docs/prompt-F-plat-2.md` entero (incluida la revisión
  de F-plat-1-fix sobre placeholders y `sin_detalle`). `app/mercado/anexos.py` (`MAX_BYTES`, validación por
  firma, `extraer_texto`, guardas de ZIP), `app/clients/openrouter.py`, `app/api/routes/catalogo.py`
  (`POST /oportunidad/{fuente}/{codigo}/anexo`, `anexo-resuelto`), `app/mercado/catalogo.py`
  (`recalcular_detalle_lineas`), `app/clients/mp_v2.py` (`documentos`, rutas candidatas, `descargar_documento`),
  `app/core/settings.py` (`documentos_api_habilitado`, `llm_*`), `app/api/query.py` (URL del buscador).
- **Sonda hecha (5-nov, Boris) [V]:** las tres rutas candidatas devuelven 403 JSON (`01-analisis` R8: recurso
  inexistente en el gateway [I]). **§1.6 de F-plat-2 queda apagada** (`DOCUMENTOS_API_HABILITADO=false`); la carga
  masiva/subida es el camino principal de esta fase, no el respaldo. §2.7 sigue como última alternativa y sigue
  esperando la captura de la URL real en el navegador.
- **Regla 9 reescrita (5-nov):** la descarga directa del adjunto público es la **última alternativa**, después de la
  API (sonda) y de la subida manual/carga masiva. Esta fase la implementa en §2.7, apagada por defecto.

## 1. Modelo de datos
- `ca_anexos`: agregar `fuente` (`manual|masiva|api_externa|api_mp`, default `manual`) y `empresa_id`
  (nullable: quién lo subió; el texto extraído y las líneas siguen siendo comunes a todas las empresas, como en
  F-plat-2 [V]). `UNIQUE (ca_codigo, sha256)`: el mismo archivo dos veces no se procesa dos veces (idempotencia).
- `empresas.token_anexos_hash` (nullable): hash (sha256) del token de la API externa; el token en claro solo
  se muestra una vez al generarlo, como un secreto. Nunca en logs (regla 1: agregar a la máscara si alguna vez
  se loguea la cabecera).
- `ca_lineas`: sin columnas nuevas; la prioridad de fuentes se resuelve con `origen` + `estado` (§2.1).

## 2. Cambios

### 2.1 Prioridad de fuentes: anexo > ficha > API (acuerdo del 5-nov)
Hoy [V]: las líneas extraídas de un anexo reemplazan placeholders y líneas de anexo no confirmadas, y **no
tocan** las `ok` de origen `api`. En el caso D (detalle pobre en la API + anexo), la CA queda con la línea pobre
`api|ok` y las líneas buenas `anexo|dudosa` conviviendo.
- Cambio: al **confirmar** una línea de anexo (o «Confirmar todas»), las líneas `origen=api` de la misma CA
  pasan a `estado=reemplazada` (nuevo valor del enum de estado; no se borran: son la evidencia de lo que dijo la
  API y las usan el índice y la competencia? **Verificar**: `indice.py` y `competencia.py` cruzan ofertas con
  líneas `ok` [V en F-plat-4]; `reemplazada` no cuenta como `ok`, así que el índice usa las del anexo. Anotar en
  el commit si algo más lee `estado`).
- «Anexo resuelto» a mano (sin extracción) hace lo mismo con las `api`.
- Líneas `manual|ok` nunca se tocan (regla de F-plat-2).
- `recalcular_detalle_lineas`: `reemplazada` no cuenta para `completo|incompleto|sin_detalle`.
- Caso B (detalle solo en la ficha): no hay forma automática de leer la ficha (regla 9). La persona lo escribe en
  `/catalogo/ca/{codigo}` como línea `manual`; al guardar la primera línea manual, las `api` pasan a `reemplazada`
  **solo si la persona marca «la API no describe bien esta CA»** (checkbox; default desmarcado). Test.

### 2.2 Carga masiva por código (`/catalogo/anexos/carga`)
- Formulario con varios archivos (input `multiple`) **o** un ZIP (≤ 50 MB; se abre con las mismas guardas de
  expansión que DOCX/XLSX en F-plat-2 [V]: solo se lee el directorio, miembro > 100:1 o total > 200 MB → rechazo).
- Asociación por nombre: el nombre empieza con el código de la CA seguido de `__` (`1234-56-COT26__Anexo 1.pdf`);
  si no hay `__`, se intenta el patrón de código de CA (`\d+-\d+-COT\d+`) al inicio del nombre. Sin código
  reconocible o CA inexistente → fila «no asociado» en la vista previa, nunca error del lote (regla 6).
- Vista previa (HTMX): por archivo: código, CA (nombre y comprador), estado (`nueva|duplicado|no asociado|CA ya
  resuelta`), tamaño. Botón «Procesar n». Cada archivo pasa por el mismo camino que la subida individual
  (validar → guardar `ca_anexos` con `fuente=masiva` → extraer → líneas `dudosa`), **en una tarea por archivo**
  con commit por archivo (regla 12: nunca el lote completo en memoria; el ZIP se lee miembro a miembro).
- Tope de LLM: respeta `llm_presupuesto_mes_usd` [V]; si se agota a mitad del lote, los restantes quedan
  `sin_presupuesto` y la vista lo dice.
- Tiempo: Render corta a los 30 s por request [I: verificar en `render.yaml`/docs]. Procesar en una ruta HTMX
  que toma **un** archivo por request y encadena (`hx-trigger="load"` al siguiente), así cada request es corta
  y el avance se ve.

### 2.3 Endpoint para automatización externa (`POST /api/anexos/{codigo}`)
- Cabecera `X-Anexos-Token`; se compara con `compare_digest` contra `token_anexos_hash` de la empresa (regla
  19 como modelo). Cuerpo `multipart/form-data` con `archivo`. Respuesta JSON: `{anexo_id, estado, duplicado}`.
  Mismos validadores y topes que la subida manual; `fuente=api_externa`.
- `GET /api/anexos/pendientes` (mismo token): códigos de CA vigentes de las regiones de la empresa con
  `detalle_lineas=sin_detalle` y `n_documentos > 0`, con `nombre`, `fecha_cierre`, `documentos[].nombre` [V,
  los guarda F-merc-3? **Verificar**: hoy se guarda `n_documentos`; si los nombres no se persisten, agregar
  `compras_agiles.documentos_nombres` (JSON) en la migración y llenarlo en `upsert_ca_detalle`]. Límite 200,
  orden por cierre. Sin datos personales.
- Admin: `/admin/empresas/{id}` → «Generar token de anexos» (muestra una vez) y «Revocar».
- Rate limit propio: 60 requests/min por token (contador en `sync_state`, regla 10); 429 propio con `Retry-After`.

### 2.4 Cola «líneas por revisar»: vigentes primero, muestra de históricas
- `/catalogo` pestaña Pendientes [V, orden por monto]: dividir en dos bloques con conteo: **Vigentes** (cierre
  futuro; orden por cierre) y **Históricas**. Las históricas se limitan a una **muestra** de 200 CA elegida por
  monto dentro de las familias con `empresa.rubros`? (no existe) → por monto dentro de `G_ALIM` [V, F-plat-1 solo
  crea líneas de alimentos]; el resto se cuenta («538 más») pero no se lista. Constante `MUESTRA_HISTORICAS = 200`.
- «Hoy»: «líneas por revisar» pasa a mostrar «vigentes: n · históricas (muestra): m».

### 2.5 Enlace directo a la ficha pública
`query.py` devuelve hoy la URL genérica del buscador [V]. Cambio: `url_ficha_ca(codigo)` con la URL de la CA.
**Verificar la forma real en un navegador** (la del buscador es una SPA; la URL con el código suele ir en la
ruta o en un query param [I]) y dejarla en una constante con comentario [V/I]. Mostrarla en la tarjeta de la
mesa, en la ficha y en «líneas por revisar» como «Abrir en Mercado Público» (`rel="noopener"`).

### 2.6 Modelo de respaldo
- `LLM_MODELO_RESPALDO` (setting, nullable). `OpenRouterClient.extraer_lineas`: si el modelo principal responde
  error 4xx/5xx, timeout o JSON inválido tras el reintento ya existente [V], reintenta **una vez** con el
  respaldo y anota `extraccion_modelo` = el que respondió. Si no hay respaldo, igual que hoy (`error`).
- `/salud` → LLM: llamadas por modelo y cuántas cayeron al respaldo.

### 2.7 Descarga directa del adjunto público (última alternativa; `ANEXOS_DESCARGA_DIRECTA=false` por defecto)
- La sonda `--documentos` no encontró ruta en la API (5-nov [V]): condición cumplida. Boris captura en el navegador (DevTools → Network al bajar
  un adjunto de la ficha pública) la URL real del archivo y su forma (¿lleva el `id` de `documentos[]`? ¿firmada?
  ¿expira?) y la deja en `01-analisis` R8 como [V]. **Sin esa captura, §2.7 no se enciende.**
- `app/clients/mp_documentos.py` (capa anti-corrupción, el único que conoce esa URL): `descargar_adjunto(codigo, doc)
  -> bytes | None` con httpx, `User-Agent` propio identificable (nombre de la app y contacto genérico, no personal),
  sin cookies ni sesión, sin seguir más de 2 redirects, sin parsear HTML (si la respuesta es `text/html`, se descarta
  y se anota `no_archivo`), tope 10 MB por archivo (streaming con corte).
- Ritmo y cortes: 1 req/s con jitter (el mismo `RateLimiter` de `clients/base.py`); un 403 o 429 corta la corrida y
  persiste `sync_state` `anexos_pausa` (60 min) como la pausa de la API; tras 504/timeout, enfriar 60 s. Contador diario
  propio en `sync_state` (`anexos_descargas:AAAA-MM-DD`, regla 10), tope `MERCADO_ANEXOS_MAX` (20) por corrida y
  `MERCADO_ANEXOS_MAX_DIA` (60). **No consume ticket ni cuota de la API**; aun así corre dentro de `mercado-ca`
  después del detalle de vigentes, con el mismo lock, para que haya un solo proceso tocando mercadopublico.cl.
- Alcance: CA vigentes de las regiones activas con `detalle_lineas = sin_detalle`, `n_documentos > 0` y sin filas en
  `ca_anexos` (misma condición que §1.6 de F-plat-2 [V]); primero las que cierran antes. Cada archivo entra por el
  mismo camino que la carga masiva (`fuente=api_mp` si vino de la API, `fuente=descarga_directa` si de acá:
  agregar el valor al enum de §1), `subido_por = NULL`.
- `/salud` → Pivote: descargas del día, 403/429 recibidos, pausa vigente. Si en una semana hay más de 2 cortes por
  403/429, apagar y volver al flujo manual: anotar en `decisiones.md`.
- Tests: red mockeada con respx (200 archivo, 200 `text/html` → `no_archivo`, 403 → pausa persistida y corte, 429 ídem,
  504 → enfriamiento, > 10 MB → corte), tope diario, bandera apagada → nada corre, `User-Agent` presente, ninguna
  cookie.

## 3. Seguridad
Archivos nunca ejecutados; ZIP solo con miembros regulares (sin rutas con `..`, sin symlinks); nombre del
miembro solo para asociar el código (se guarda `nombre_original` recortado a 200). Token externo hasheado;
comparación en tiempo constante; sin cuerpo de error que revele si la CA existe para un token inválido.
Ownership: la carga masiva y el endpoint registran `empresa_id`; la lectura de líneas sigue común.

## 4. Tests
Prioridad de fuentes (caso D → `api` pasa a `reemplazada` al confirmar; `manual` intacta; `recalcular_detalle_lineas`
ignora `reemplazada`); caso B con y sin checkbox; carga masiva (asociación por `__` y por patrón; no asociado; duplicado
por sha256; ZIP con miembro malicioso rechazado; presupuesto agotado a mitad); endpoint (token válido/ inválido/
revocado, `compare_digest`, rate limit, duplicado idempotente); `pendientes` con ownership por regiones; muestra de
200; URL de la ficha; respaldo del modelo (principal falla → respaldo responde; sin respaldo → `error`). Red
mockeada con respx. `ruff`, `mypy app`, `pytest -rs` sin saltados contra Postgres local.

## 5. Cierre
Changelog («Anexos: carga masiva por código, el anexo manda sobre la API, enlace a la ficha»). `00-estado.md`.
`01-analisis-api-mercado-publico.md` R8: anotar que la plataforma recibe anexos por carga masiva y por endpoint;
la sonda sigue pendiente. Commit: `F-plat-anexos-2: carga masiva de anexos por código y prioridad del anexo`.

## 6. Pasos de Boris
1. ~~`smoke_test.py --documentos`~~ hecho el 5-nov (R8: sin ruta en la API). Falta: capturar la URL real del
   adjunto en el navegador (DevTools → Network al bajar un adjunto de la ficha pública de una CA con
   `n_documentos > 0`, p. ej. `3600-210-COT26`) y anotarla en R8. Recién con eso se puede encender
   `ANEXOS_DESCARGA_DIRECTA` (§2.7).
2. Deploy con migración. Generar el token de la empresa y guardarlo donde lo use quien automatice (Gonzalo/Conejo),
   junto con `19-adjuntos-ca-explicacion-tecnica.md` §5.
3. Bajar a mano los anexos de las CA vigentes de una mañana (5–10), subirlos de una vez con la carga masiva y medir
   minutos totales.
4. Fijar `LLM_MODELO_RESPALDO` cuando `evaluar_extraccion.py` dé el segundo mejor modelo barato.
