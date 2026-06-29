# Spike — Plan Anual de Compra (PAC) como fuente para la pestaña de consulta (F-plan)

> Spike de investigación. NO ingiere datos ni toca la BD. Objetivo: confirmar la
> fuente, el formato y el costo del Plan Anual de Compra de los organismos, y decidir
> la arquitectura de la pestaña de consulta de F-plan (explorar qué planea comprar
> cada organismo en el año — dato de consulta, **no** alertas).
>
> **Estado: cerrado. Verificación en vivo completa (§5-bis): URL real del CSV, esquema de
> columnas, encoding, comportamiento sin datos y mapeo de institución, todo confirmado con
> curl contra los hosts reales (sin ticket). Veredicto final en §6.**

---

## 0. Corrección al supuesto inicial

Una primera lectura (basada solo en `docs/04-datos-abiertos.md`, que era un spike de
*licitaciones* y no enumeraba todas las secciones del portal) sugirió que el PAC NO estaba
en datos abiertos y que solo existía vía la API con ticket. **Eso era incorrecto.**

Verificado renderizando el portal (SPA, no visible por WebFetch): **el PAC SÍ está en
datos abiertos como CSV descargable**, y esa es la fuente recomendada (sin cuota). La API
con ticket existe como alternativa, pero es la vía cara (ver §4).

---

## 1. Fuente confirmada: portal de datos abiertos → sección "Plan Anual de Compra"

URL de la sección: `https://datos-abiertos.chilecompra.cl/descargas/plan-anual-compra`

| Aspecto | Valor confirmado |
|---|---|
| Tipo de archivo | **CSV** ("Archivo descargable en formato CSV que contiene los planes de compra para la institución y año seleccionados"). |
| Filtros | **AÑO** (selector; 2026 por defecto) + **INSTITUCIÓN** (autocomplete por nombre; devuelve organismos reales, ej. "MINISTERIO PUBLICO", "MINISTERIO DE LA MUJER Y LA EQUIDAD DE GÉNERO"). |
| Archivo completo | **"Si no seleccionas ningún filtro, se descargará el archivo completo con todos los registros disponibles del PAC."** → hay descarga masiva total, además de la filtrada por institución/año. |
| Autenticación | **Ninguna.** Es el portal de datos abiertos, igual que `lic-da`/`oc-da` → **NO gasta cuota** (regla 3 no aplica a esta fuente). |
| Mecanismo de descarga | La SPA llama al backend `https://mserv-datos-abiertos.chilecompra.cl/v1/descarga/validate-file` (preflight OPTIONS → 200; el POST/GET subsiguiente entrega/resuelve el archivo). El asset final (URL directa del CSV, probablemente S3/Azure como el resto del portal) **queda por capturar en §5**. |

Contenido del PAC (descripción oficial del portal): lista referencial de bienes/servicios
que el organismo espera contratar en el año, con **detalle y cantidad de productos/servicios,
valor estimado, fecha aproximada de compra y modalidad/mecanismo esperado** (Convenio Marco,
licitación pública, Compra Coordinada, Gran Compra, Compra Ágil, etc.).

---

## 2. Arquitectura recomendada: descarga on-demand del CSV filtrado por institución/año

**Recomendación: seguir el patrón `lic-da` (descargar CSV de datos abiertos, parsear
defensivo), NO la API con ticket. Y servir on-demand por institución/año, con caché corta
opcional en Postgres.**

Motivos:

- **$0 cuota:** la fuente es datos abiertos (sin ticket), igual que `lic-da` → no toca el
  presupuesto de 9.000 req/día (regla 3). Esto la hace muy preferible a la API (§4).
- **Neon 0.5 GB (regla 11):** la pestaña es de *consulta*. Descargar on-demand el CSV
  **filtrado por la institución/año que el usuario elige** trae un archivo chico; no hay
  que ingerir el universo. Si se cachea, guardar solo lo consultado, con TTL (el PAC se
  publica en enero y cambia poco → TTL largo, refrescar si el registro tiene > 7–30 días).
- **Capa anti-corrupción (regla de arquitectura):** nuevo módulo de cliente para datos
  abiertos del PAC (puede vivir junto a `app/clients/datos_abiertos.py`, que ya maneja
  ZIP/CSV/Latin-1 de `lic-da`, reutilizando su parseo defensivo). No mezclar con
  `mp_v1.py`/`mp_v2.py` (API con ticket).
- **Parseo defensivo (regla 6):** asumir, hasta confirmar en §5, mismas mañas que `lic-da`
  (separador `;`, encoding Latin-1/Windows-1252, posibles saltos de línea embebidos en
  comillas, erratas oficiales de nombres de columna). Confirmar en el spike.

Alternativa descartada por defecto: ingesta nocturna del **archivo completo** (todos los
organismos). Solo tendría sentido si quisiéramos *recomendar* organismos o cruzar PAC con
rubros del perfil de forma masiva (eso es territorio de F-datos/F11, no de esta pestaña de
consulta). Para F-plan, on-demand filtrado es más simple y más barato en Neon.

---

## 3. Mapeo con nuestro modelo

- El filtro de institución del portal es **por nombre** (autocomplete), no por código. Hoy
  tenemos `codigo_organismo` y el nombre del organismo en el modelo (`app/api/query.py`,
  `app/ingest/licitaciones.py`). Confirmar en §5 si la descarga filtrada admite código de
  organismo o solo nombre, y cómo se obtiene el listado de instituciones del autocomplete
  (probable endpoint del mismo backend `mserv-datos-abiertos`).
- Si la pestaña parte de un organismo que el usuario ya sigue/ve en la app, conviene mapear
  desde nuestro nombre/código de organismo al identificador que espera la descarga del PAC.

---

## 4. Alternativa secundaria (NO recomendada): API con ticket

Existe además un endpoint de API para el PAC:

```
https://apis.mercadopublico.cl/PlanDeCompra/Obtener/{CodOrg}/{Agno}/{Ticket}
```

- Host `apis.mercadopublico.cl` (tercer host, distinto de v1/v2), ticket **en el path** →
  **gasta cuota** (regla 3) y exige extender el enmascarado de ticket (regla 1) al
  ticket-en-path.
- Útil solo si se necesitara el PAC en JSON en tiempo real para un organismo puntual y el
  CSV de datos abiertos resultara insuficiente. **Para F-plan no se usa**: el CSV de datos
  abiertos cubre la necesidad sin gastar cuota.

---

## 5. Pendiente de cerrar en Claude Code (red sin restricciones)

Sin tocar BD ni código; solo investigar y documentar aquí:

1. **Capturar la URL final del CSV.** Con DevTools (pestaña Network) o curl, reproducir la
   descarga desde `/descargas/plan-anual-compra`: ver el request a
   `mserv-datos-abiertos.chilecompra.cl/v1/descarga/validate-file` (método, payload con
   año/institución) y la **URL directa del archivo** que devuelve/redirige. Documentar el
   patrón para (a) archivo completo y (b) filtrado por institución/año.
2. **Formato del CSV:** encoding (¿Latin-1 como `lic-da`?), separador, quoting, saltos de
   línea embebidos, fin de línea. Confirmar contra `docs/04-datos-abiertos.md` §1.
3. **Columnas exactas** (nombres tal cual, regla 6): descripción del bien/servicio,
   **rubro/UNSPSC si viene**, cantidad, unidad, valor estimado, mes/fecha aproximada de
   compra, modalidad/mecanismo, identificador y nombre del organismo. Anotar erratas.
4. **Tamaño:** bytes/filas del CSV filtrado por una institución típica y del archivo
   completo (para dimensionar Neon y decidir definitivamente on-demand vs ingesta).
5. **Listado de instituciones:** endpoint que alimenta el autocomplete y si acepta
   código además de nombre; mapeo con nuestro `codigo_organismo`.
6. **Cobertura temporal:** años disponibles en el selector (¿solo año en curso? ¿histórico?)
   y frecuencia de actualización del archivo.

---

## 5-bis. Verificación en vivo (datos abiertos, sin ticket)

Todo lo siguiente se reprodujo con `curl` directo contra los hosts reales (no fue necesario
DevTools: el bundle público de la SPA, `https://datos-abiertos.chilecompra.cl/static/js/main.*.js`,
expone los hosts y la construcción de URL en texto — mismo método que ya se usó en
`docs/04-datos-abiertos.md` §0 para `lic-da`/`oc-da`). No se usó `MP_TICKET` en ningún paso
de esta sección (regla 1 no aplica: esta fuente no pide ticket).

### a) URL real del archivo y mecanismo de descarga

- **Host del archivo (NO es el `REACT_APP_PURCHASE_PLAN_URL`/`gcompras-files...` que parecía
  el candidato obvio por posición en el bundle — ese host devuelve 403 en todos los años
  probados).** El host correcto es **`REACT_APP_PAC_URL`**:
  ```
  https://pac-files.da.mercadopublico.cl
  ```
- **Patrón de URL** (confirmado con 200 + tamaño real vía el preflight, ver abajo):
  - Archivo completo (todas las instituciones) de un año:
    `https://pac-files.da.mercadopublico.cl/{año}/pacorganismos_{año}.zip`
  - Filtrado por institución (`{codigoEntidad}` = el código de la institución, **no** su
    nombre, a pesar de que el front se navega por nombre vía autocomplete — ver §c):
    `https://pac-files.da.mercadopublico.cl/{año}/pacorganismos_{año}_{codigoEntidad}.zip`
  - Sin redirecciones; `Content-Type: application/zip`; servido por S3 + CloudFront
    (`Server: AmazonS3`, headers `X-Amz-Cf-*`), con `Last-Modified`/`ETag` por archivo.
- **Preflight real que hace la SPA antes de descargar:**
  `POST https://mserv-datos-abiertos.chilecompra.cl/v1/descarga/validate-file`
  body `{"url": "<la URL del zip de arriba>"}` → responde
  `{"success":"OK","payload":{"url":...,"fallo":bool,"codigo":"200"|"403"|...,
  "detalle":"...", "statusCode":int|null,"tamanio":int|null},"errores":null}`.
  Es solo un HEAD del lado del backend para decidir si mostrar el botón de descarga o un
  toast de error; el archivo real se descarga aparte, directo del host de arriba.
- Probado para **MINISTERIO PUBLICO** (`codigoEntidad=224060`, ver §d) y año 2026:
  `validate-file` devolvió `codigo:"200", tamanio:28131` y la descarga directa del zip dio
  exactamente esos 28.131 bytes — coincide byte a byte.

### b) Formato del CSV — distinto y más simple que `lic-da`

| Propiedad | Valor |
|---|---|
| Contenedor | `.zip` con un único `.csv` interno (`pacorganismos_{año}[_{codigoEntidad}].csv`) |
| Encoding | **UTF-8 con BOM** (`\xef\xbb\xbf`) — **no** Latin-1 (a diferencia de `lic-da`/`oc-da`). Verificado a nivel de bytes: `Mantenci\xc3\xb3n` decodifica correctamente como UTF-8 ("Mantención"). |
| Separador | `;` |
| Quoting | **Ninguno** — ningún campo viene entre comillas, ni siquiera los que contienen texto libre. |
| Fin de línea | LF puro (`\n`), sin CRLF, en la inmensa mayoría de líneas. |
| **Gotcha (regla 6, parseo defensivo):** | Algunas filas de `descripcion_producto` contienen **saltos de línea reales embebidos sin comillas** (texto libre de formularios web). Sin comillas que delimiten el campo, un lector que asuma "una fila = una línea física" rompe: en el archivo completo 2026 se encontraron **775 registros lógicos partidos en 2 a 8 líneas físicas** (verificado reconstruyendo: total 9 `;` por registro lógico — acumular líneas hasta completar 9 separadores, no contar líneas). Ejemplo real (organismo/montos no sensibles): una fila de `MUNICIPALIDAD DE HUECHURABA` para impresión de un libro quedó partida en 5 líneas físicas porque la descripción tenía saltos de línea tipo "Tamaño: Media carta.\nPortada y contraportada...\ncouché.\nEncuadernación: tipo corchete...". **No usar `split("\n")` ni `csv.reader` línea a línea sin reconstrucción previa** — mismo principio que la regla ya aplicada a `lic-da`, pero el mecanismo de ruptura es distinto (ahí eran comillas con salto embebido; aquí es ausencia total de comillas). |
| Texto con doble espacio | Nombres de institución y descripciones vienen con espacios dobles entre palabras (ej. `"MINISTERIO  PUBLICO"`, `"MUNICIPALIDAD  DE  HUECHURABA"`) — cosmético, usar tal cual o normalizar espacios en blanco al mostrar, no es una errata que cambie significado. |

### c) Columnas exactas (CSV filtrado, header real)

```
institucion_nombre;rut_institucion;codigo_producto;descripcion_producto;cantidad_estimada;monto_unitario_clp;monto_estimado_clp;mes_estimado;trimestre_estimado;estado_planificacion
```

- **`institucion_nombre`**: nombre del organismo, tal cual (con dobles espacios, ver arriba).
- **`rut_institucion` — ERRATA OFICIAL (regla 6, usar tal cual):** el nombre de la columna
  dice "rut" pero el valor **no es un RUT** (no tiene puntos, guion ni dígito verificador).
  Es el **`codigoEntidad`/`codigo_organismo`** (confirmado: para MINISTERIO PUBLICO el valor
  es `224060`, idéntico al `codigoEntidad` del catálogo de instituciones — ver §d). El RUT
  real de esa institución en el catálogo es `61.935.400-1`, que no aparece en este CSV.
- **`codigo_producto` — NO es UNSPSC**, a diferencia de lo esperado en el §3 original de
  este spike y de lo que sí es `CodigoProductoONU` en `lic-da`. Es un **identificador
  secuencial interno** de la línea del PAC: 7 dígitos en el 99,99 % de los casos (con
  algunos de 5–6 dígitos), **globalmente único** (0 duplicados en 303.540 filas del archivo
  completo 2026) y sin relación jerárquica Segmento/Familia/Clase/Commodity. **No hay
  columna de rubro/UNSPSC en este dataset** — si F-plan necesita cruzar por rubro, no se
  puede hacer con este CSV; habría que volver a la API con ticket (§4) o aceptar que la
  pestaña de PAC no cruza por rubro.
- **`descripcion_producto`**: texto libre (ver gotcha de multilínea en §b).
- **`cantidad_estimada`**: entero (cantidad planeada).
- **`monto_unitario_clp`** / **`monto_estimado_clp`**: decimales en CLP (ej. `3360000.0`).
  No hay columna de moneda — asumir CLP siempre (consistente con la descripción oficial del
  portal, que no menciona otras monedas para el PAC).
- **`mes_estimado`**: entero 1–12, sin cero a la izquierda. **No hay fecha exacta**, solo mes.
- **`trimestre_estimado`**: entero 1–4, derivado del mes (no es información adicional).
- **`estado_planificacion`**: en los dos años disponibles (2025 completo y 2026 parcial),
  el **100 % de las filas vino como `"Publicado"`** (303.540/303.540 en 2026; no se observó
  ningún otro valor) — no se pudo confirmar en vivo qué otros estados existen (ej. anulado,
  modificado); tratar cualquier valor no visto como desconocido + log (regla 6), no asumir
  que `"Publicado"` es el único valor posible solo porque es el único visto hoy.
- **No hay columna de modalidad/mecanismo de compra** (Licitación Pública, Convenio Marco,
  Compra Ágil, etc.) en este CSV, a pesar de que la descripción del portal (§1) la menciona
  como parte del contenido del PAC. El campo simplemente no vino en los archivos descargados.

### d) Mapeo institución ↔ `codigo_organismo` — confirmado idéntico

- Catálogo de instituciones (alimenta el autocomplete del front):
  `GET https://mserv-datos-abiertos.chilecompra.cl/v1/kpi/instituciones` (sin auth) →
  envelope `{success, trace, payload, errores}`, `payload` = lista de
  `{id, codigoEntidad, rut, razonSocial}` — **1.333 instituciones** al momento del spike.
  El autocomplete filtra client-side por `razonSocial`; el filtro real de descarga usa
  `codigoEntidad`, no el nombre (ver §a).
- **`codigoEntidad` de este catálogo == `CodigoOrganismo` de la API v1 con ticket == el
  `codigo_organismo` de nuestro modelo.** Verificado de forma independiente: se pidió el
  detalle de una licitación real (`1002772-130-LP25`) a la API v1 (`licitaciones.json`,
  con ticket, 1 request) y su campo `Comprador.CodigoOrganismo` vino `"7383"`, exactamente
  el mismo código y la misma razón social (`SERVICIO SALUD OCCIDENTE HOSPITAL DR FELIX
  BULNES CERDA`) que el `codigoEntidad=7383` del catálogo de datos abiertos. **No se
  necesita ningún mapeo ni tabla de traducción**: el código que ya tenemos en
  `Licitacion.codigo_organismo` es directamente el que espera la URL de descarga del PAC.
  (Nota aparte, fuera de este spike: hoy `codigo_organismo` está vacío para las 11.693
  licitaciones en la BD — el listado básico de la API no trae ese campo, solo el detalle
  por organismo/`Comprador`; es un gap de la ingesta actual, no de este spike.)

### e) Tamaños (para dimensionar Neon si se cachea)

| Archivo | Comprimido | Descomprimido | Filas |
|---|---|---|---|
| Completo 2026 (962 instituciones, año parcial a mayo) | 7.524.132 bytes (~7,2 MB) | 41.229.203 bytes (~39,3 MB) | 303.540 |
| Filtrado MINISTERIO PUBLICO 2026 (institución "grande") | 28.131 bytes | 138.914 bytes | 1.126 |
| Filtrado SERVICIO DE SALUD MAGALLANES 2026 (institución "chica") | — | 370 bytes | 1 |
| Completo 2025 (año cerrado) | 8.330.513 bytes (~8 MB) | (no descomprimido en este spike) | — |
| Filtrado MINISTERIO PUBLICO 2025 | 34.853 bytes | (no descomprimido en este spike) | — |

Confirma la recomendación del §2: el archivo **filtrado por institución pesa KB, no MB** —
cachear solo lo consultado en Postgres es completamente viable dentro de los 0,5 GB de Neon
(regla 11). El archivo completo (~7–8 MB comprimidos / ~40 MB descomprimidos) también sería
viable como ingesta puntual si en el futuro se quisiera, pero no es necesario para una
pestaña de consulta on-demand.

### f) Comportamiento sin datos — manejable, no rompe

- **Año sin archivo publicado:** 2024 y 2027 devuelven `403` (`AccessDenied` de S3) tanto en
  `validate-file` (`{"fallo":true,"codigo":"403","statusCode":403,"tamanio":null}`) como en
  la descarga directa. **Años con archivo real hoy: solo 2025 y 2026** (el bundle de la SPA
  trae el año de inicio hardcodeado en 2025 — el PAC no existe como dato abierto antes de
  esa fecha; el año tope es el año en curso).
- **Institución sin PAC publicado ese año:** mismo `403` limpio (probado con
  `codigoEntidad=7055`, GOBERNACIÓN PROVINCIAL DE TALAGANTE, una de las 384 instituciones
  del catálogo de 1.333 que no tienen archivo 2026). No hay error 500 ni payload corrupto:
  un simple "no existe" que el cliente debe traducir a "sin plan publicado este año".
- **Institución con muy pocas líneas:** funciona igual que una grande, solo más liviano
  (370 bytes para 1 fila, ver §e) — no hay tamaño mínimo ni caso especial.

### g) Periodicidad

- `Last-Modified` de **todos** los archivos probados (completos y filtrados, 2025 y 2026)
  cae el mismo día, **2026-05-28** — más de un mes antes de esta verificación (2026-06-28).
  Indica una **regeneración periódica (al menos mensual) de todo el lote de archivos**
  —incluido el año 2025 ya cerrado—, no una actualización diaria/en vivo como el mes en
  curso de `lic-da`/`oc-da` (`docs/04-datos-abiertos.md` §5). Para una futura cache con TTL,
  comparar `Last-Modified` (HEAD) es válido pero no esperar cambios más frecuentes que
  mensuales.

### Lo que quedó sin resolver

- No se confirmaron valores de `estado_planificacion` distintos de `"Publicado"` (no había
  ningún caso en los datos reales de 2025/2026 al momento del spike) — el parseo
  defensivo debe tratar cualquier valor futuro como desconocido, no solo aceptar
  `"Publicado"`.
- No se exploró si existe un parámetro de URL/preflight para pedir **varios años en una sola
  descarga** — el patrón observado siempre amarra año + (opcionalmente) institución; para
  histórico habría que pedir un archivo por año.

---

## 6. Veredicto

- **El PAC se obtiene como CSV desde datos abiertos, host `pac-files.da.mercadopublico.cl`**,
  con patrón de URL confirmado para archivo completo y filtrado por institución/año (§5-bis
  a). **Sin ticket, sin cuota** — confirmado: ningún paso de esta verificación usó `MP_TICKET`.
- **Arquitectura confirmada: descarga on-demand del CSV filtrado por institución/año**,
  parseo defensivo en un cliente nuevo (puede vivir junto a
  `app/clients/datos_abiertos.py`, pero con sus propias reglas: UTF-8 con BOM, no Latin-1;
  reconstrucción de registros multilínea sin comillas, no el gotcha de comillas de `lic-da`).
  Caché opcional en Postgres acotada a lo consultado (KB por institución, regla 11) con TTL
  largo (cambios solo ~mensuales, §5-bis g).
- **`codigo_organismo` que ya tenemos es directamente el `codigoEntidad`/`{CodOrg}` que
  espera la descarga** — confirmado cruzando API v1 (con ticket) contra el catálogo de datos
  abiertos (§5-bis d). No se necesita tabla de mapeo.
- **Corrección importante de alcance:** este CSV **no trae rubro/UNSPSC ni modalidad de
  compra** (mecanismo esperado), a pesar de que la descripción del portal los menciona. Si
  F-plan necesita esos dos campos, no están disponibles por esta vía — quedan como
  limitación conocida o como motivo para evaluar la API con ticket (§4) solo para esos casos
  puntuales, fuera del alcance on-demand normal.
- La **API con ticket** (`apis.mercadopublico.cl/PlanDeCompra/...`, §4) queda descartada como
  fuente primaria: gasta cuota y no se verificó en este spike (la fuente sin ticket resultó
  suficiente y más simple). Si en la implementación se topa con un caso donde el CSV no
  alcance (ej. necesitar rubro/UNSPSC), retomarla ahí sería el siguiente paso, no este spike.
- Implementación (cliente, modelo, migración): prompt aparte, fuera de este spike.

---

*Fuente: Dirección ChileCompra — datos abiertos
(datos-abiertos.chilecompra.cl/descargas/plan-anual-compra) y API Mercado Público
(servicio "Plan de Compras", desarrolladores.mercadopublico.cl).*
