# Spike — Datos abiertos de ChileCompra como fuente de `licitacion_items.codigo_producto`

> Spike de investigación, NO ingiere datos ni toca la BD. Objetivo: confirmar si los
> archivos de datos abiertos sirven para poblar `licitacion_items.codigo_producto`
> (UNSPSC) sin gastar cuota de la API (regla 3: 9.000 req/día).
> Archivo inspeccionado: `lic-da/2026-5.zip` (licitaciones, mayo 2026), descargado y
> descomprimido en una carpeta temporal local, fuera del repo. No se imprimió ningún
> secreto (el portal de datos abiertos no requiere ticket ni autenticación).

---

## 0. Cómo se llegó a la URL real

El portal https://datos-abiertos.chilecompra.cl/descargas es una SPA (React) sin
contenido en el HTML estático: no hay enlaces de descarga visibles vía curl/WebFetch
directo. Se ubicó la URL real inspeccionando el bundle JS público
(`/static/js/main.*.js`), que define las variables de entorno del front:

```
REACT_APP_API_BASE_URL = https://mserv-datos-abiertos.chilecompra.cl   (API de la SPA, no usar)
REACT_APP_OCDS_BLOB_URL = https://ocds.blob.core.windows.net/ocds       (procesos OCDS)
```

y, en el texto de ayuda de la página, dos patrones de archivo servidos directo desde
Azure Blob Storage público (sin autenticación, sin headers especiales):

```
https://transparenciachc.blob.core.windows.net/lic-da/{yyyy}-{m}.zip   ← Licitaciones (incluye ofertas)
https://transparenciachc.blob.core.windows.net/oc-da/{yyyy}-{m}.zip    ← Órdenes de compra
https://ocds.blob.core.windows.net/ocds/{yyyymm}.zip                  ← Procesos de compra (OCDS)
```

`{m}` va **sin cero a la izquierda** (`2026-5.zip`, no `2026-05.zip`). Verificado con
`curl -I` (HEAD) contra varios meses: responde `200 OK` con `Content-Length` y
`Last-Modified` de Azure Blob (`Windows-Azure-Blob/1.0`), sin redirecciones.

No fue necesario probar OCDS para items: el archivo de licitaciones ya trae el dato
a nivel de ítem (ver §2), así que se descartó esa rama del spike (requisito "si
licitaciones no trae ítems, probar OCDS" no aplicó).

---

## 1. Formato del archivo

| Propiedad | Valor |
|---|---|
| Contenedor | `.zip`, un solo archivo interno (`lic_2026-5.csv`) |
| Tamaño comprimido | 14.459.634 bytes (~13,8 MB) |
| Tamaño descomprimido | 250.315.061 bytes (~239 MB) |
| Encoding | **ISO-8859-1 / Windows-1252** (Latin-1) — NO UTF-8. Confirmado a nivel de bytes: `GASFITERÍA` viene como `GASFITER\xCDA` (0xCD = Í en Latin-1; decodificar como UTF-8 lanza `UnicodeDecodeError`) |
| Separador | `;` (punto y coma) |
| Quoting | comillas dobles `"..."` en todos los campos, incluidos numéricos |
| Fin de línea | mixto CRLF/LF; hay campos de texto libre (`Descripcion`, etc.) con saltos de línea **embebidos dentro de comillas** → un registro CSV puede ocupar varias líneas físicas. Hay que parsear con un lector CSV real (csv.reader/pandas), nunca `split("\n")` línea a línea |
| Filas físicas vs. registros | 369.828 líneas físicas → 126.514 registros CSV reales en el archivo de mayo 2026 |

**Gotcha para `app/clients` (capa anti-corrupción):** si se llega a ingerir esto,
debe vivir en un módulo nuevo de "datos abiertos" separado de `app/clients/mp_v1.py`
(formatos y reglas de parseo completamente distintos: CSV con `;`, Latin-1, ZIP) —
no mezclar con el cliente HTTP de la API con ticket.

---

## 2. Esquema — columnas relevantes

El CSV es **plano/desnormalizado**: una fila por combinación
`(licitación × ítem × oferta)`. 110 columnas en total; las relevantes para
`licitacion_items` y el enlace con `licitaciones`:

| Índice | Columna | Uso |
|---|---|---|
| 2 | `CodigoExterno` | **Código público de la licitación** — mismo valor que `Licitacion.codigo` en nuestra BD (ej. `1233623-31-LR25`, `2409-189-LE25`). Es la clave de enlace. |
| 12/13 | `CodigoEstado` / `Estado` | Igual semántica que `licitaciones_activas` (5=Publicada, 6=Cerrada, etc., texto en español) |
| 38–46 | `FechaCreacion`, `FechaCierre`, `FechaPublicacion`, etc. | En formato `YYYY-MM-DD` (ISO, sin hora) — distinto del `ddmmaaaa` de v1 y consistente con el bug que motivó la regla 6 reforzada en `parse_fecha_v1` |
| 83 | `Codigoitem` | Código de línea/ítem dentro de la licitación (≈ `LicitacionItem` por ítem) |
| **84** | **`CodigoProductoONU`** | **El código UNSPSC a nivel de ítem.** Nombre exacto de columna: `CodigoProductoONU` |
| 85–87 | `Rubro1`, `Rubro2`, `Rubro3` | Nombre legible de Segmento/Familia/Clase UNSPSC (jerarquía descriptiva, no códigos) |
| 88 | `Nombre producto genrico` | Nombre del commodity UNSPSC (sic, typo oficial del dataset — "genrico" sin é, sin la "é"; coherente con la regla 6 de "usar tal cual, no corregir") |
| 91/92 | `UnidadMedida`, `Cantidad` | Igual que `ItemLicitacion.unidad`/`.cantidad` del cliente v1 |
| 93–98 | `CodigoProveedor`, `RutProveedor`, `NombreProveedor`, ... | Datos de **oferta** (por eso el dataset se llama "incluye ofertas" y por eso hay filas duplicadas por ítem cuando hay múltiples oferentes) |
| 99–109 | `Monto Estimado Adjudicado`, `Estado Oferta`, `MontoUnitarioOferta`, ... | Resultado de la oferta/adjudicación — fuera de alcance de este spike |

### Largo de `CodigoProductoONU`
- **126.040 / 126.514 filas (99,6 %) → 8 dígitos** (estándar UNSPSC: Segmento+Familia+Clase+Commodity, 2 dígitos cada uno). Mismo formato que `LicitacionItem.codigo_producto` ya usado en `app/matching/engine.py` (matching `LIKE 'prefijo%'` contra `app/catalogos/unspsc.py`).
- **474 / 126.514 filas (0,4 %) → 9 dígitos**, ej. `102101001`, `104101001`. No es UNSPSC estándar: corresponden todas a `Rubro1/2/3 = "CONSULTORIA"` (categoría administrativa propia de Mercado Público para licitaciones de consultoría, sin commodity UNSPSC real). **Caso defensivo (regla 6): si el código no tiene exactamente 8 dígitos, no truncar ni inventar — guardar tal cual o descartar con log, igual que ya se hace con estados/tipologías desconocidas.**

---

## 3. Enlace ítem ↔ licitación

`CodigoExterno` (columna 2) es el mismo código externo que usamos como PK de
`Licitacion.codigo` (formato `NNNN(N)-N-LNN`/`LENN`/`LRNN`/`LPNN` según tipo y año).
Verificado con ejemplos reales del archivo: `1233623-31-LR25`, `2409-189-LE25`,
`1058043-16-LP25` — mismo patrón que se ve en los códigos que ya maneja
`app/ingest/licitaciones.py`. El enlace es directo, sin transformación.

Dentro de una licitación, `Codigoitem` agrupa las filas que son el mismo ítem
(repetidas una vez por cada oferta recibida) — para poblar `LicitacionItem` hay que
deduplicar por `(CodigoExterno, Codigoitem)`, no insertar una fila por cada oferta.

---

## 4. Volumen

Archivo de mayo 2026 (`lic-da/2026-5.zip`):

| Métrica | Valor |
|---|---|
| Filas totales (item × oferta) | 126.514 |
| Licitaciones distintas (`CodigoExterno`) | 7.159 |
| Ítems únicos por licitación | promedio **4,77**, mínimo 1, máximo 434 (outlier: licitación con cientos de líneas de insumos) |
| Filas (item×oferta) por licitación | promedio 17,7 (refleja ítems × Nº de oferentes) |
| Cobertura de `CodigoProductoONU` no vacío | 100 % de las filas (126.514/126.514) |

Para contexto de tamaño relativo: `oc-da/2026-5.zip` (órdenes de compra del mismo
mes) pesa 90 MB comprimidos — mucho más grande, porque cada OC adjudicada también
lleva sus líneas; no fue necesario inspeccionarlo a fondo para este spike porque el
dato que falta (`codigo_producto` de **licitaciones**, no de OC) ya está confirmado
en `lic-da`.

---

## 5. Periodicidad y patrón de URL

- **Patrón:** `https://transparenciachc.blob.core.windows.net/lic-da/{año}-{mes sin cero}.zip` (y `oc-da/` análogo para órdenes de compra). Histórico disponible desde **2014-12** (verificado `2014-12.zip` → 200 OK) hasta el **mes en curso**.
- **Archivos históricos** (años/meses ya cerrados hace tiempo): estáticos. Ej. `2015-1.zip` tiene `Last-Modified: 2021-11-03` — no se reescriben.
- **Mes recién cerrado y mes en curso:** se siguen actualizando. Observado el mismo día de la consulta (2026-06-27): `2026-5.zip` con `Last-Modified` de **hoy 15:05 UTC**, y `2026-6.zip` (mes en curso, archivo más chico — 8,3 MB, datos parciales) con `Last-Modified` de **hoy 12:37 UTC**. Esto indica una reprocesa/republicación periódica (al menos diaria) de los últimos 1–2 meses, mientras el resto del histórico queda fijo.
- **Sin endpoint de "última actualización" público conocido para este archivo** (el `/v1/fechaActualizacionTabla/getFechaActualizacion` del backend de la SPA es para otra sección del sitio, no para `lic-da`/`oc-da`). Para una futura ingesta incremental conviene comparar el header `Last-Modified` (HEAD request) contra un cursor persistido en Postgres (mismo patrón que `SyncState.cursor` que ya usamos para Compra Ágil) antes de descargar el ZIP completo.
- No hay autenticación ni rate limit aparente (es Azure Blob Storage público); de todos modos, conviene tratarlo igual de respetuosamente que la API (1 request de HEAD para chequear `Last-Modified`/`Content-Length` antes de bajar el ZIP completo, y no hacerlo más de 1 vez/día por mes).

---

## 6. Veredicto

**Sí, el archivo `lic-da/{año}-{mes}.zip` permite poblar
`licitacion_items.codigo_producto` para licitaciones activas sin gastar cuota de la
API**, y es la fuente recomendada por sobre OCDS:

- **`lic-da` (licitaciones) es el archivo correcto** — no se necesitó recurrir a
  OCDS: ya trae `CodigoProductoONU` (UNSPSC, 8 díg., 99,6 % de las filas) a nivel de
  ítem, con el mismo formato que usa hoy `app/matching/engine.py` para el recall
  aditivo por rubro (`LicitacionItem.codigo_producto LIKE 'prefijo%'`).
- El enlace con `Licitacion.codigo` es directo vía `CodigoExterno`, sin necesidad de
  transformación ni de un código adicional.
- **Implicancia de cuota:** hoy `LicitacionItem` solo se llena vía
  `licitacion_detalle()` (1 request de la API v1 por licitación,
  `fetch_detalles_pendientes`). Sustituir o complementar esa fuente con el ZIP mensual
  de datos abiertos evitaría gastar cuota en el detalle **solo para obtener ítems**
  — aunque el detalle de la API sigue siendo necesario para los demás campos que
  trae (`Descripcion`, `MontoEstimado`, `Moneda`, `Informada`, `Contrato`, `Obras`),
  que el archivo abierto no expone igual (`Descripcion` sí viene pero en formato de
  reporte, no validado en este spike).
- **No usar OCDS para esto:** no fue necesario evaluarlo a fondo porque `lic-da` ya
  resuelve el problema; además el patrón de URL de OCDS encontrado en el bundle
  (`ocds.blob.core.windows.net/ocds/{yyyymm}.zip`) no respondió 200 en las pruebas
  puntuales hechas (probable host/contenedor distinto al documentado en el texto de
  ayuda de la página) — si se quisiera explorar igual, sería un spike separado.
- **Antes de ingerir en serio (fuera de alcance de este spike):** definir si esto
  reemplaza o complementa `fetch_detalles_pendientes`; diseñar el parseo defensivo
  para los `CodigoProductoONU` de 9 dígitos (categoría "CONSULTORIA", no UNSPSC
  real) y para el CSV multilínea/Latin-1; y decidir el cursor de incrementalidad
  (`Last-Modified` del blob vs. reprocesar el mes vigente completo cada vez, dado
  que se reescribe sin avisar cuáles filas cambiaron).

---

*Fuente: Dirección ChileCompra — datos abiertos (https://datos-abiertos.chilecompra.cl/descargas).*
