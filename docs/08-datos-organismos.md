# Spike — Compradores clasificados por sector (F-datos)

> Spike de investigación. NO ingiere datos ni toca la BD. Objetivo: confirmar la fuente
> de la **clasificación/sector** de los organismos compradores (para el multi-select
> clasificado de organismos de F10) y de los **rubros que cada organismo compra** (para
> recomendar organismos a seguir según los rubros del perfil).
>
> **Estado: cerrado. Verificación en vivo completa (§3-bis): endpoint BULK de sector
> encontrado (`/v1/elastic/organization/all`, no hace falta iterar 1.333 organismos),
> taxonomía de 8 sectores, payload de `getTreeMap/getSectors` (solo nombres de segmento,
> sin códigos UNSPSC, top 10 por monto) y cobertura real (≈15 % sin clasificación) — todo
> verificado con curl contra los hosts reales (sin ticket). Veredicto final en §4.**

---

## 1. Fuentes confirmadas (datos abiertos, sin ticket)

Mismo backend que F-plan: `https://mserv-datos-abiertos.chilecompra.cl/v1/...`, sin auth,
no gasta cuota (regla 3 no aplica). Verificado renderizando `/ficha-organismo/{entCode}`
y leyendo los JSON directos.

### a) Clasificación/sector por organismo — CONFIRMADO

```
GET /v1/kpi/organismo/{entCode}
→ {"success":"OK","payload":[{"entCode":7002,"idEntity":7002,
   "name":"JUNTA NACIONAL DE AUXILIO ESCOLAR Y BECA",
   "sector":"Gobierno Central, Universidades"}],"errores":null}
```

- **`sector`** es la clasificación que buscábamos (ej. "Gobierno Central, Universidades").
  El portal la usa como agrupador en `/organismos-compradores` y en las descargas "por
  sector del Estado".
- **`entCode` == `codigoEntidad` (catálogo de F-plan) == `codigo_organismo` de nuestro
  modelo.** Mismo identificador ya verificado en `docs/07-plan-anual.md` §5-bis d. No se
  necesita tabla de mapeo.
- Es 1 organismo por request. Para clasificar el universo (~1.333 instituciones del catálogo
  de F-plan) haría falta un endpoint bulk (ver §3) o iterar (sin cuota, pero ~1.333 requests
  a 1 req/s ≈ 22 min — aceptable como job nocturno puntual, no en caliente).

### b) Rubros que el organismo compra — CONFIRMADO (para recomendaciones)

```
GET /v1/getTreeMap/getSectors/{entCode}/{año}
```

- Alimenta la sección "Rubros y productos más transados" de la ficha. En la UI se ven
  nombres de segmento UNSPSC con montos (ej. "Tecnologías de la información...",
  "Equipamiento y suministros médicos"). **Confirmar en §3 si trae los CÓDIGOS UNSPSC** (no
  solo nombres): si trae códigos, se puede cruzar directo con `app/catalogos/unspsc.py` y
  los rubros del perfil; si solo nombres, hay que mapear por nombre (más frágil).
- Esto habilita el segundo objetivo de F-datos: recomendar organismos a seguir cuyo gasto
  histórico cae en los rubros del perfil del usuario.

### c) Otros endpoints por-organismo observados (contexto, no necesarios para F-datos)

`/v1/modalidad/compra/{ent}/{año}`, `/v1/sankey/...`, `/v1/contract/...`,
`/v1/calculoMontoProveedor/...` — desgloses de la ficha (mecanismos, proveedores, contratos).
Fuera de alcance de F-datos.

---

## 2. Arquitectura recomendada (provisional, a confirmar con §3)

- **Enriquecer el catálogo que YA creó F-plan** (`InstitucionPAC`: `codigo_entidad`,
  `razon_social`, `rut`) con una columna `sector` (y opcionalmente nada más). Poblarla con
  el endpoint bulk si existe (§3), o con un job nocturno que itere `/v1/kpi/organismo/{ent}`
  para los códigos del catálogo (sin cuota; 1 req/s; idempotente). TTL largo (la
  clasificación cambia rarísimo). Costo en Neon: 1 string corto por organismo → trivial.
- **Recomendación por rubro** (segundo objetivo): tabla `organismo_rubro` poblada on-demand
  o por job desde `/v1/getTreeMap/getSectors/{ent}/{año}`, guardando (codigo_entidad, año,
  codigo/nombre de rubro, monto). Acotar a los organismos relevantes para no inflar Neon
  (regla 11) — p. ej. solo los que el usuario consulta o los top-N por rubro del perfil.
  Decidir alcance con Boris (ver pregunta abierta al final).
- **Multi-select clasificado de organismos (F10):** una vez que `InstitucionPAC.sector`
  está poblado, el selector de organismos del formulario de perfiles se agrupa por sector
  (acordeón / optgroup). Esto es UI → cae en F10, pero F-datos es su prerequisito de datos.
- Cliente: reusar/extender el de F-plan (`app/clients/plan_compra.py` ya habla con
  `mserv-datos-abiertos`) o un módulo hermano; capa anti-corrupción, sin mezclar con la API
  con ticket.

---

## 3. Pendiente de cerrar en Claude Code (red sin restricciones)

Sin tocar BD ni código; solo investigar y documentar:

1. **¿Existe endpoint BULK con sector?** Capturar el request que alimenta la página
   `/organismos-compradores` (lista completa de compradores) — ahí debería venir la lista
   con su sector en una sola llamada, evitando 1.333 requests. Candidatos a probar:
   `/v1/kpi/organismos`, `/v1/organismo/list`, lo que use esa página. Documentar URL +
   payload. Si NO existe bulk, confirmar que iterar `/v1/kpi/organismo/{ent}` es la vía
   (job nocturno, 1 req/s).
2. **Taxonomía de sectores:** enumerar los valores distintos de `sector` (cuántas categorías
   y cuáles) — define los grupos del multi-select de F10.
3. **`getTreeMap/getSectors` payload:** ¿trae códigos UNSPSC o solo nombres de segmento?
   ¿qué granularidad (segmento/familia)? ¿montos? Esto decide si la recomendación por rubro
   cruza por código (robusto) o por nombre (frágil).
4. **Cobertura:** ¿todas las ~1.333 instituciones del catálogo de F-plan tienen `sector`, o
   hay nulos/"sin clasificación"? Manejo defensivo (regla 6) para los sin sector.

---

## 3-bis. Verificación en vivo

Todo lo siguiente se reprodujo con `curl` directo contra `mserv-datos-abiertos.chilecompra.cl`
(mismo backend sin ticket de F-plan, ver `docs/07-plan-anual.md` §5-bis) y, para encontrar el
endpoint bulk, descargando el bundle público de la SPA
(`https://datos-abiertos.chilecompra.cl/static/js/main.*.js`) y buscando literales `/v1/...` —
mismo método que F-plan y `lic-da`/`oc-da`. No se usó `MP_TICKET` en ningún paso (regla 1 no
aplica: esta fuente no pide ticket).

### a) Endpoint BULK con sector — CONFIRMADO

```
GET https://mserv-datos-abiertos.chilecompra.cl/v1/elastic/organization/all
```

- **Sin parámetros, sin auth, sin paginación** (dos llamadas consecutivas devolvieron el
  mismo array byte a byte). Encontrado en el bundle de la SPA junto a otras llamadas de la
  página de comparación/listado de compradores (`year, buyers, sectorId, dni, idProcess` en
  la función vecina) — es la fuente del listado bulk, no un endpoint por-organismo.
- **Envelope DISTINTO al resto de la API de datos abiertos** (regla 6, anotar la errata):
  no viene envuelto en `{success,trace,payload,errores}` como `/v1/kpi/instituciones` o
  `/v1/kpi/organismo/{ent}` — es un **array JSON plano** directamente en el body.
- **1.179 organismos**, los 1.179 con `idType:2, type:"comprador"` (no se mezclan
  proveedores). Forma de cada elemento:
  ```json
  {"idType":2,"type":"comprador","entcode":7002,"rut":"","name":"...",
   "hasProfile":1,"idSector":2,"sector":"Gobierno Central, Universidades",
   "synonyms":"...","comparables":"7253;1968268;7495;7063","id":"..."}
  ```
  - `entcode` == `codigoEntidad` del catálogo de F-plan / `codigo_organismo` del modelo
    (mismo identificador ya verificado independientemente en `docs/07-plan-anual.md` §5-bis d).
  - `rut` viene **siempre vacío** en esta fuente (no usarla para RUT; ya tenemos el RUT real
    vía `InstitucionPAC` de F-plan).
  - `hasProfile` siempre `1` y sin `entcode` duplicados en las 1.179 filas — set limpio.
  - `synonyms`/`comparables` no se necesitan para F-datos (alias de búsqueda y organismos
    comparables de la ficha; fuera de alcance).
- **Esto reemplaza la iteración planteada en §2** (1.333 × `/v1/kpi/organismo/{ent}` a
  1 req/s ≈ 22 min): una sola llamada bulk basta para poblar `sector` de ~87 % del catálogo
  (ver cobertura en §d). Iterar por-organismo queda solo como fallback para los faltantes,
  si se decide cerrarlos 1 a 1 (171 organismos × 1 req/s ≈ 3 min, opcional).

### b) Taxonomía de sectores — CONFIRMADA

8 valores de `idSector` (1.179 organismos), con su nombre (`sector`) tal cual viene:

| `idSector` | `sector` (texto) | organismos |
|---|---|---|
| 1 | Fuerzas Armadas | 8 |
| 2 | Gobierno Central, Universidades | 263 |
| 3 | Legislativo y Judicial | 13 |
| 4 | Municipalidades | 369 |
| 5 | Obras Públicas | 30 |
| 6 | Otros | 236 |
| 7 | Salud | 228 |
| 8 | *(sin nombre — `sector` viene `null`)* | 32 |

- **Errata/gotcha (regla 6):** `idSector` nunca es `null` (siempre viene un entero 1–8), pero
  para `idSector=8` el campo `sector` (texto) viene **`null`**, no `"Otros"` ni ningún string
  — son 32 organismos con clasificación numérica pero sin etiqueta legible. Tratar como
  "Sin clasificación" en vez de fallar o mostrar `None`/`null` en la UI.
- 7 categorías con nombre + 1 sin nombre → el multi-select de F10 son 8 grupos (7 nombrados
  + "Sin clasificación").
- Nota de encoding: los acentos (`Pública`, `Comisión`) llegan correctos en UTF-8
  (`content-type: application/json`, sin BOM); el mojibake visto en terminal durante esta
  investigación fue solo un artefacto del code page de la consola Windows, verificado
  decodificando los bytes (`'Obras Públicas'` ascii-escapado da `\xfa` = U+00FA `ú`, correcto).
  A diferencia del CSV del PAC (`docs/07-plan-anual.md` §5-bis b, UTF-8 con BOM) o `lic-da`
  (Latin-1), esta fuente no tiene ningún gotcha de encoding propio.

### c) `getTreeMap/getSectors/{entCode}/{agno}` — payload confirmado: SOLO NOMBRES, sin códigos UNSPSC

Probado con `7002/2026` (JUNAEB) y `7383/2026` (hospital, ver `docs/07-plan-anual.md` §5-bis d)
y comparado contra `app/catalogos/unspsc.py`:

```json
{"success":"OK","trace":null,"payload":[{"year":2026,"data":[
  {"idEntity":7002,"idSegment":1,"segment":"Organizaciones y consultorías políticas...",
   "amountSegment":648929242185,
   "idFamily":0,"family":"0","amountFamily":0,
   "idCategory":0,"category":"0","amountCategory":0,
   "idProduct":0,"product":"0","amountProduct":0},
  ... 9 filas más ...
]}],"errores":null}
```

- **Granularidad: solo segmento.** `idFamily`/`family`/`idCategory`/`category`/`idProduct`/
  `product` vienen **siempre** `0`/`"0"` en este endpoint — no resuelve a familia/categoría/
  producto (existen endpoints hermanos `getByFamily`/`getByCategory` en el mismo bundle, pero
  exigen un `idSegment` para drillear; no se exploraron, fuera de alcance de F-datos).
- **`idSegment` NO es el código UNSPSC — es un RANKING (1° a 10°) por monto, específico de
  cada organismo.** Confirmado cruzando dos organismos distintos: "Equipamiento y suministros
  médicos" sale `idSegment:6` para JUNAEB (7002) pero `idSegment:1` para el hospital (7383) —
  el mismo segmento real tiene un número distinto según cuánto gastó cada organismo en él.
  Tampoco coincide con el código UNSPSC real: "Tecnologías de la información,
  telecomunicaciones y radiodifusión" es el segmento UNSPSC **43** en
  `app/catalogos/unspsc.py` (`"Telecomunicaciones y radiodifusión de tecnología de la
  información"`, mismo segmento real con redacción ligeramente distinta), pero la API lo
  devuelve como `idSegment:2` para JUNAEB. **El único dato utilizable es el texto `segment`**;
  cruzarlo con nuestro catálogo UNSPSC requeriría *fuzzy matching* por nombre — frágil, tal
  como advertía §1 b. No hay ningún código UNSPSC en ningún campo de este payload.
- **Tope de 10 filas** (`data` trae exactamente 10 elementos en ambas pruebas) — son los
  **top 10 segmentos por monto**, no el universo completo de rubros del organismo. Para
  "recomendar organismos según rubros del perfil" esto es una limitación real: un organismo
  que compra *algo* de un rubro pero no está en su top 10 por monto, no aparecerá.
- **`amountSegment`**: monto acumulado (CLP, sin columna de moneda — igual que el PAC).
- **Sin datos (entCode inexistente, año futuro, organismo sin gasto ese año):** payload
  limpio `{"year": Y, "data": []}` — sin error ni 404. Probado con año futuro (`7002/2027`)
  y `entCode` inexistente (`999999999/2026`).
- **Independiente del PAC:** `7055` (GOBERNACIÓN PROVINCIAL DE TALAGANTE, sin PAC publicado
  2026 según `docs/07-plan-anual.md` §5-bis f) sí tiene datos en `getTreeMap/getSectors` y sí
  tiene `sector` vía `/v1/kpi/organismo/7055` — la falta de PAC no implica falta de
  clasificación/histórico de gasto; son fuentes independientes.

### d) Cobertura — confirmada con datos reales (29-jun-2026)

Cruzando el bulk de §a (1.179 organismos) contra el catálogo `InstitucionPAC` de F-plan
(`/v1/kpi/instituciones`, 1.333 instituciones, mismo `entcode`/`codigoEntidad`):

| | cantidad |
|---|---|
| En ambos catálogos (intersección) | 1.162 |
| En `InstitucionPAC` (F-plan) pero SIN sector (no aparecen en el bulk) | 171 |
| En el bulk de sectores pero NO en `InstitucionPAC` | 17 |

- **171/1.333 (12,8 %) de las instituciones del catálogo de F-plan no tienen sector** vía
  esta fuente. Muestra real: congregaciones religiosas, ONG, empresas portuarias estatales
  (Talcahuano, Valparaíso, San Antonio, Arica), liceos — entidades más periféricas que no
  están en el índice "comprador" usado por la página de comparación de organismos.
- Sumando los 32 de §b (`idSector=8`, sin nombre) que SÍ están en el bulk pero sin etiqueta
  legible: **203/1.333 (≈15 %) del catálogo necesita el fallback "Sin clasificación"** al
  poblar `InstitucionPAC.sector` (regla 6 — nunca dejar `NULL` sin manejar explícitamente ni
  romper la ingesta por un organismo sin match).
- Los 17 que están en el bulk pero no en `InstitucionPAC`: asociaciones de municipalidades
  (agrupaciones, no organismos compradores individuales), reparticiones del MOP con sigla
  distinta a la del catálogo PAC, y un caso (`1999111`, comisión de remuneraciones) con
  encoding-display igual de "correcto pero mojibake en terminal" que el resto — no son casos
  que F-datos necesite resolver (no tienen PAC que mostrar; quedan simplemente sin fila en
  `InstitucionPAC` hasta que F-plan los vea en su propio catálogo).

---

## 4. Veredicto

- **La clasificación por sector existe en datos abiertos, sin ticket, y SÍ hay endpoint
  BULK**: `GET /v1/elastic/organization/all` trae los 1.179 organismos "comprador" con su
  `sector` en una sola llamada (envelope distinto al resto: array plano, no
  `{success,payload}`). **No hace falta iterar `/v1/kpi/organismo/{ent}` 1.333 veces** — esa
  vía queda solo como fallback opcional para completar los faltantes.
- **Taxonomía: 8 grupos** — 7 con nombre (Fuerzas Armadas, Gobierno Central/Universidades,
  Legislativo y Judicial, Municipalidades, Obras Públicas, Otros, Salud) + 1 sin nombre
  (`idSector=8`, tratar como "Sin clasificación").
- **Cobertura real: 87 % del catálogo de F-plan tiene sector vía el bulk; ≈15 % necesita el
  fallback "Sin clasificación"** (171 ausentes del bulk + 32 con `idSector` sin nombre) — el
  multi-select de F10 debe contemplar ese grupo, no asumir que todo organismo tiene sector.
- **Los rubros por organismo (`getTreeMap/getSectors/{entCode}/{año}`) traen SOLO nombres de
  segmento, top 10 por monto, sin ningún código UNSPSC** (ni siquiera `idSegment`, que es un
  ranking por organismo, no un código) — confirmado cruzando dos organismos y el catálogo
  `app/catalogos/unspsc.py`. Cruzar con rubros del perfil exigiría *fuzzy matching* por
  nombre (frágil) y además se pierde lo que esté fuera del top 10 — **limitación real para
  la recomendación por rubro**, a decidir con Boris si vale la pena igual (texto) o si ese
  segundo objetivo de F-datos queda fuera de alcance por ahora.
- **Arquitectura confirmada para el primer objetivo (sector):** poblar `InstitucionPAC.sector`
  (nueva columna) desde el bulk en una sola descarga, con fallback "Sin clasificación" para
  el ~15 % sin match; TTL largo (la clasificación cambia rarísimo). El segundo objetivo
  (recomendación por rubro) queda con alcance pendiente de decidir por la limitación de §c.
- Implementación (columna `sector`, cliente, poblar catálogo): prompt aparte, fuera de este
  spike.

---

*Fuente: Dirección ChileCompra — datos abiertos
(mserv-datos-abiertos.chilecompra.cl, secciones organismos-compradores / ficha-organismo).*
