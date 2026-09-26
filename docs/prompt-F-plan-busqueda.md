# Prompt F-plan-busqueda — Plan Anual: buscar por palabra o producto en todos los organismos

> **Destino en el repo:** `docs/prompt-F-plan-busqueda.md`
> **Fase:** F-plan-busqueda (una fase, un commit) · **Migración:** SÍ (índice FTS; columnas solo si
> hacen falta) · **Dependencias nuevas:** ninguna
> **Orden:** independiente de la serie vigencia → modal. Puede ir antes o después; no toca el feed.
> **Decisiones de Boris (24-sep):**
> - Búsqueda inversa: partir de **qué se compra** (palabras clave / productos), no de la institución.
> - En esa búsqueda **se puede elegir el o los organismos** que interesen (y acotar por sector).
> - Guardar completo **solo el año en curso**; los años anteriores siguen por organismo, on-demand.

Reglas del proyecto: CLAUDE.md completo, en especial 8 (atribución), 10 (estado en Postgres), 11
(Neon 0,5 GB: `/salud` muestra el tamaño), 12 (lotes, 512 MB), 13 (lock), queries 100 %
parametrizadas. Español de Chile; entrada en `app/changelog.py`; `ruff check .`,
`python -m mypy app`, `python -m pytest` verdes; commit "F-plan-busqueda: …"; **nunca `git add -A`**.

---

## Hechos que condicionan [V: `docs/07-plan-anual.md`, código 24-sep]
- Hoy `/plan-anual` solo funciona por institución: `get_plan` baja on-demand
  `pacorganismos_{año}_{codigoEntidad}.zip` y lo cachea en `plan_compra_lineas` con TTL
  (`PlanCompraSync`). No hay forma de buscar "quién planea comprar X".
- Existe el **archivo completo** del año: `https://pac-files.da.mercadopublico.cl/{año}/pacorganismos_{año}.zip`
  (sin ticket, cuota 0). 2026: ~7,2 MB comprimido, ~39 MB descomprimido, **303.540 filas**, 962
  instituciones (medido a mayo; hoy será más). Se regenera ~mensual (`Last-Modified`, §g).
- **`codigo_producto` NO es UNSPSC ni un catálogo**: es un id único por fila (0 duplicados). No hay
  columna de rubro. Por lo tanto "buscar por ítems" = **buscar en `descripcion_producto`** (texto
  libre); no se puede agrupar por producto ni cruzar con los rubros UNSPSC de los perfiles.
- Parseo: `parse_pac_csv` ya reconstruye registros partidos en varias líneas (9 separadores `;`) y
  maneja la errata `rut_institucion` (= `codigoEntidad`). Reusarlo.
- FTS del proyecto: `to_tsvector('spanish', inmutable_unaccent(...))` (ver `app/matching/engine.py`).
- Base: 88 MB de 500 la última vez anotada en `00-estado-actual.md` (fecha antigua).

## Paso 0 — medir antes de ingerir (pegar al final de este archivo)
1. Tamaño actual de la base en producción (`/api/salud` o `pg_database_size`, solo lectura).
2. Descargar el ZIP completo del año en curso **en local**, parsearlo con `parse_pac_csv` y medir
   filas, bytes de `descripcion_producto`, instituciones distintas.
3. En la base de **dev**, cargarlo y medir `pg_total_relation_size` de `plan_compra_lineas` con el
   índice FTS. **Si la proyección deja producción sobre 60 % de 500 MB, detenerse y reportar**
   (alternativas: índice solo sobre una columna recortada, o dejar fuera líneas sin monto).

## Qué construir

### 1. Ingesta del año completo (job, no request web)
- Job nuevo `plan-anual` en el CLI (`_JOBS`) con `pg_advisory_lock` (regla 13): HEAD al ZIP
  completo del año en curso; si `Last-Modified` no cambió respecto de lo guardado (`SyncState`),
  no hace nada. Si cambió: descarga, parsea en streaming por lotes y **reemplaza** las filas de ese
  año sin dejar la tabla a medias (cargar a filas nuevas con un `lote_id`/marca y borrar las viejas al
  final dentro de una transacción corta, o equivalente; documentar la elección). Idempotente.
- Workflow: agregarlo al workflow semanal `catalogos` (lunes) o uno propio semanal; corre en GitHub
  Actions (más memoria que Render), nunca dentro de una request.
- Con el año en curso cargado entero, `get_plan` de ese año **lee de la tabla** (sin descarga
  on-demand). Años anteriores: siguen con el flujo on-demand + TTL actual, sin cambios.
- La retención no toca estas filas; el reemplazo anual las mantiene acotadas a un año.

### 2. Índice
- Índice GIN de expresión `to_tsvector('spanish', inmutable_unaccent(descripcion_producto))` (sin
  columna tsvector almacenada, para ahorrar espacio, salvo que el Paso 0 muestre que conviene).
- Índices btree `(agno, codigo_entidad)` y el que haga falta para ordenar por monto.

### 3. UI de `/plan-anual`: dos modos en la misma página
- **"Por producto o palabra" (nuevo, pestaña por defecto; al entrar sin búsqueda muestra "Para mis perfiles", §3-bis)**:
  - Campo de búsqueda (misma semántica que los perfiles: palabras, frase entre comillas, `-excluir`
    si el motor lo soporta; si no, documentar qué soporta).
  - Chips "Usar palabras de mis perfiles" con las keywords de los perfiles activos del usuario.
  - **Filtro de organismo**: selector múltiple sobre las instituciones del PAC (reusar el catálogo
    `InstitucionPAC` y, si ya existe, el combobox de organismos de `/perfiles`), más filtro por
    **sector** (ya existe `sync_sectores_organismos`). Seleccionar organismos acota la búsqueda.
  - Filtros: mes/trimestre estimado, monto mínimo/máximo.
  - Resultado en dos vistas:
    - **Por organismo** (default): una fila por institución con n.º de líneas que coinciden, monto
      estimado total y meses; clic → despliega sus líneas o la agrega al filtro de organismo.
    - **Líneas**: tabla de líneas (descripción, institución, cantidad, monto unitario y total en
      `$12.500.000`, mes), orden por relevancia, monto o mes, paginada.
  - Exportar CSV de lo filtrado (máx. razonable, p. ej. 5.000 filas), con la leyenda de fuente.
- **"Por organismo" (el modo actual)** se mantiene tal cual.
- Estado vacío honesto: "Sin coincidencias en el Plan Anual {año}". Si la ingesta del año aún no
  corrió: "El Plan Anual {año} completo se está cargando; mientras tanto busca por organismo."
- Fecha de actualización visible ("Plan Anual publicado por ChileCompra, actualizado al
  {Last-Modified}") y "Fuente: Dirección ChileCompra" una vez.

### 3-bis. Encontrar sin tener que escribir (pedido de Boris: "más simple")
Buscar por palabra exige saber cómo lo escribió cada organismo ("resma", "papel fotocopia", "hojas
carta"). Para que lo normal sea no tener que pensar la búsqueda:
- **Vista "Para mis perfiles" como pantalla de entrada**: al abrir `/plan-anual` se corren, en la
  misma consulta FTS, las keywords y exclusiones de los perfiles activos del usuario (y, si el
  perfil tiene organismos elegidos, se ofrece acotarlo a ellos con un clic). Resultado agrupado por
  organismo. Cero tipeo. Se calcula al vuelo con el índice GIN; **sin tabla precalculada**.
- **"Desde este mes" activado por defecto**: `mes_estimado >= mes actual`. Es lo que todavía se va a
  licitar; lo pasado del año queda a un clic ("Todo el año").
- **Conteo en vivo mientras se escribe** (HTMX, `hx-trigger="keyup changed delay:400ms"`):
  "123 líneas en 45 organismos · $1.234.000.000". Permite probar sinónimos rápido sin cargar
  tablas.
- **Orden "Mayor monto primero"** disponible en ambas vistas: para detectar compras grandes.
- Enlace "Ver este organismo" en cada fila → abre el modo "Por organismo" actual ya
  seleccionado (así la búsqueda por organización no se pierde, se conecta).

Fuera de esta fase, pero anotado (verificar antes de prometer): cruzar líneas del PAC con
licitaciones ya publicadas del mismo organismo para marcar "ya salió a compra" / "aún no". [I] No hay
clave común entre PAC y licitaciones (no hay UNSPSC en el PAC): solo organismo + texto, así que sería
aproximado.

### 4. Salud
`/salud` muestra filas del PAC cargadas, año, `Last-Modified` de la fuente y tamaño de la tabla.

## Tests (mínimo)
- Ingesta: ZIP fixture chico (con un registro partido en varias líneas) → filas correctas;
  re-correr sin cambio de `Last-Modified` no descarga; con cambio, reemplaza sin duplicar y sin
  ventana vacía; lock tomado → no corre dos veces.
- Búsqueda: "resma" encuentra "RESMAS DE PAPEL" (unaccent/plural vía spanish); exclusión funciona;
  filtro de organismos y de sector acotan; parámetros siempre parametrizados (test con comillas y
  `%` en el texto).
- Vista por organismo: conteo y suma coinciden con las líneas.
- `get_plan` del año en curso no llama a la red; año anterior sigue on-demand (red mockeada).
- Export CSV respeta filtros y lleva la fuente.
- "Para mis perfiles": usa keywords y exclusiones del usuario y nunca las de otro usuario (regla 17);
  sin perfiles → estado vacío que invita a buscar o a crear un perfil.
- "Desde este mes" por defecto; "Todo el año" lo quita.
- Endpoint de conteo en vivo devuelve solo números (sin filas) y respeta todos los filtros.

## Fuera de alcance
Cruzar PAC con rubros UNSPSC (no hay columna de rubro); alertas sobre el PAC; guardar años
anteriores completos.

*Fuente de los datos de dominio: Dirección ChileCompra.*
