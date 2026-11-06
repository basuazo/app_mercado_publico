# Prompt de implementación — F-plat-capas (catálogo en tres capas: Demanda MP, Catálogo Proveedores, Inventario) · 5-nov-2026

> Para Claude Code. **Modelo: Sonnet** (CRUD por empresa, navegación y una tabla nueva con ownership).
> Una fase = un commit, **sin push**. **Con migración**: `down_revision` = la cabeza que deje la fase anterior
> (`alembic heads`, una sola; tras F-plat-6-cruce-2 es `d9a4e2b7c1f6` [V]; si F-plat-anexos-2 se commitea antes, la de ella). Va sobre F-plat-1 y F-plat-3 commiteadas; no necesita F-plat-lote (pero
> F-plat-lote la necesita: define `stock` y `reservado`).
> Origen: decisión del equipo (Revisión CBS, 5-nov): «organizar el catálogo en tres capas: la oferta propia, los
> productos de proveedores y la demanda del mercado» (`docs/18-revision-cbs-y-flujos-operativos.md` §2.1).

## 0. Antes de escribir código
- `CLAUDE.md` (10, 11, 17, 18, 26). `docs/17-plan` §2 C2 y C4. `app/catalogos/canonico.py`, `app/models/tables.py`
  (`producto_canonico`, `producto_formato`, `proveedor_productos`, `empresas`), `app/api/routes/catalogo.py`,
  `app/api/routes/mesa.py` (proveedores), `app/mercado/proveedores.py` (`mejor_costo`), `base.html` (menú).
- **Principio:** las tres capas comparten la llave `producto_canonico × producto_formato`. No se crea otro
  catálogo: la capa (a) es una tabla **por empresa** que apunta al canónico; la capa (b) ya es
  `proveedor_productos` (por empresa vía proveedor); la capa (c) ya es `producto_canonico` + `ca_lineas`
  (común a todas las empresas). Un alias aprendido en (c) sirve a (a) y (b).
- **Hallazgo de la auditoría (5-nov) [V]:** `proveedor_productos.producto_id` / `formato_id` existen desde
  F-plat-1 pero **nadie los escribe**: ni `guardar_producto` (`app/mercado/proveedores.py:101`), ni el
  formulario (`mesa.py` `_guardar_producto_form`), ni `importar.py`; solo los tocan los tests y
  `fusionar_productos`. En producción la capa (b) **no está enganchada al canónico**, así que
  `mejor_costo(producto_id=…)`, los candidatos de «mi oferta» (F-plat-5, `oferta._candidatos`) y
  `CostoLinea.tiene_costos` (adjudicación) devuelven vacío con datos reales. Esta fase lo arregla (§1.3):
  sin eso, `proveedor_habitual`, «Stock propio» y `cobertura_de_ca` no tienen con qué cruzar.

## 1. Modelo de datos (solo `app/models` define esquema)
### 1.1 `empresa_productos` — capa «Inventario»
`id`, `empresa_id` (FK), `producto_id` (FK `producto_canonico`), `formato_id` (FK `producto_formato`),
`activo` (bool, default true), `stock` (numeric, default 0; en unidades del formato), `reservado` (numeric,
default 0; lo compromete F-plat-lote, acá solo existe y se muestra), `stock_minimo` (nullable),
`plazo_reposicion_dias` (int, nullable: lo que tarda el proveedor habitual en entregar; Mario: «este proveedor
no despacha de un día para otro»), `proveedor_habitual_id` (FK `proveedores`, nullable), `marca` (texto corto,
nullable: lo que ofrecemos cuando la CA pide «marca X o similar»), `notas`, `creado_en`, `actualizado_en`,
`actualizado_por_id`. `UNIQUE (empresa_id, producto_id, formato_id)`. Índice `(empresa_id, activo)`.
- `disponible = stock − reservado` es una propiedad, no una columna.
- Sin historial de stock en esta fase (no es inventario contable; es «qué tengo para responder una CA»).
  Cada cambio de stock escribe un evento `stock` en `ca_eventos`? **No**: `ca_eventos` es por CA. Escribir un
  log simple `empresa_producto_movimientos` (`id`, `empresa_producto_id`, `delta`, `motivo`
  (`ajuste|compra|entrega|reserva|liberacion`), `referencia` (texto: código de CA o de lote), `usuario_id`,
  `creado_en`) — append-only, barato, y lo que F-plat-seguimiento usa para descontar al entregar.

### 1.2 `empresas`
Sin columnas nuevas. (Los nombres de las capas no se guardan: son etiquetas del menú.)

### 1.3 Vincular `proveedor_productos` al canónico (sin columnas nuevas)
- `guardar_producto` acepta `producto_id` / `formato_id` opcionales y los guarda; valida que el formato sea
  del producto (`catalogo._formato_valido`).
- Si no vienen, sugerir con `canonico.sugerir_producto` / `mejor_sugerencia` sobre `producto_texto` (+
  `formato_de_texto` para el formato): se guarda solo una sugerencia **única y con confianza alta**
  (`estado_por_confianza == "ok"`); si no, queda `NULL` y se cuenta como «sin vincular». Vale para el
  formulario, `importar.py` (hoja `precios`) y la lista de precios por foto (F-plat-2).
- Formulario de producto del proveedor (`mesa.py` ~l. 1000, plantilla `_form_producto`): campo «producto
  del catálogo» con el mismo selector de §3 (hoy `_catalogo_selector.html` exige `linea_id`: generalizar el
  parcial con un `destino` / prefijo de ids en vez de duplicarlo).
- `/proveedores/{id}`: columna «Catálogo» (nombre canónico o «sin vincular») y contador arriba; una acción
  «vincular pendientes» que aplica las sugerencias únicas al resto (idempotente). Migración de datos: **no**
  (lo hace esa acción, por empresa, cuando Boris la pulse).
- Test: `guardar_producto` con sugerencia única vincula; con sugerencia ambigua deja NULL; `mejor_costo`
  por `producto_id` encuentra el producto recién vinculado.

## 2. Lógica (`app/mercado/oferta_propia.py`, nuevo; sin httpx)
- `listar(session, empresa_id, *, solo_activos, texto)`; `guardar(...)` (crea o actualiza; valida que el
  formato pertenezca al producto; ownership por `empresa_id`); `ajustar_stock(session, empresa_id, ep_id,
  delta, motivo, referencia, usuario)` (escribe el movimiento en la misma transacción; stock nunca negativo,
  `reservado ≤ stock` se valida al reservar, no acá); `desactivar`.
- `cobertura_de_ca(session, empresa_id, codigo) -> dict[linea_id, Cobertura(disponible, en_stock: bool,
  plazo_dias, proveedor_habitual)]`: para cada línea `ok|dudosa` de la CA con `producto_id`, si la empresa tiene
  ese producto (cualquier formato convertible por `factor_base` a la unidad base): disponible en unidad base.
  La mesa y la ficha lo usan.
- `mejor_costo` (F-plat-3) no cambia: la capa (a) no reemplaza el costo del proveedor, lo complementa (tengo
  stock = no hay que comprar; el costo sigue siendo el de la última compra = `proveedor_habitual` si existe,
  si no `mejor_costo`).
- Importación por planilla (**es la forma principal de gestionar el inventario por ahora**, decisión de Boris): reutilizar `app/mercado/importar.py` con una hoja `inventario` (producto, formato,
  stock, stock mínimo, plazo, marca, proveedor habitual). Plantilla descargable. Vista previa + confirmación como en F-plat-3.
  **Ojo [V]:** `importar.parsear` hoy exige la hoja `precios` (`raise PlanillaInvalida("Falta la hoja 'precios'")`,
  l. 277) y `aplicar` solo escribe proveedores: hacer `precios` opcional cuando venga `inventario` (y al revés),
  con un `parsear_inventario` / `aplicar_inventario` separados que reutilicen `_filas_xlsx`, `_como_dicts`,
  `_txt/_num/_entero` y el mismo patrón «aplicar + rollback = vista previa». La columna `producto` se resuelve
  al canónico con las mismas reglas de §1.3 (sugerencia única → fila ok; ambigua o sin sugerencia → error de
  fila con las opciones, no rompe el lote). `proveedor habitual` se resuelve por nombre/RUT con `_Indice`
  (existente, de la empresa); si no existe, aviso y NULL. Mismo `MAX_BYTES`/`MAX_FILAS`. CSV: solo `inventario`
  si el nombre del archivo lo indica (`*inventario*.csv`) — o mejor, solo XLSX para inventario: decidirlo y
  anotarlo.

## 3. UI (solo usuarios con empresa; mutaciones con CSRF)
- **Menú** (nombres decididos por Boris, 5-nov): tres entradas bajo un encabezado «Catálogo» (en móvil tienen
  que caber): **«Demanda MP»** (la actual `/catalogo`, renombrada en título, migas y menú; la URL no cambia para no
  romper enlaces ni tests), **«Catálogo Proveedores»** (la actual `/proveedores`) e **«Inventario»** (nueva,
  `/inventario`). Constantes en un solo módulo `app/api/nav.py` (no existe: crearlo).
  **Estado real [V]:** `base.html` l. 50–62 tiene entradas planas con la macro `entrada(href, texto, icono,
  activa)`: Inicio, Mesa, Actividad, Competencia, Estrategia, Abastecimiento, **Catálogo** (`/catalogo`),
  **Proveedores** (`/proveedores`, activa si no es logística) y **Logística** (`/proveedores/logistica`). La nav se
  renderiza dos veces (escritorio y móvil, con `sufijo`). Cambios: encabezado «Catálogo» como
  `nav-lateral__seccion` (igual que «Administración») y debajo Demanda MP · Catálogo Proveedores · Inventario;
  Logística queda como está, después del grupo. Ícono nuevo `inventario` en la macro `icono`. Actualizar
  `tests/test_plat_nav.py` (los textos «Catálogo»/«Proveedores» cambian) y el título/migas de `catalogo.html`
  y `proveedores.html`.
- `/inventario`: tabla (producto, formato, marca, stock, reservado, disponible, mínimo, plazo, proveedor
  habitual, activo), buscador por texto (ILIKE escapado), «Agregar producto» (búsqueda del canónico con el
  mismo autocompletar que F-plat-1 usa en `/catalogo/ca/{codigo}` [V: `GET /catalogo/buscar-producto` y
  `GET /catalogo/formatos`, parciales `_catalogo_selector.html` / `_catalogo_formatos.html`; ambos exigen
  `linea_id` y lo usan en los ids del DOM → generalizar con un parámetro `destino` opcional, no duplicar), edición en línea (HTMX),
  «Ajustar stock» (delta + motivo), importar/plantilla, aviso rojo cuando `disponible < stock_minimo`.
- **Mesa** (`/mesa`, tarjeta): una línea «Tengo: 3 de 5 líneas en stock · falta: arroz 20 kg» y filtro
  «Con stock» (al menos una línea cubierta). Lo calcula `cobertura_de_ca` por lote de CA (una query por página,
  no por tarjeta).
- **Ficha de la CA**, sección «Precio y mi oferta» (F-plat-5): junto al proveedor por línea, «en stock: n»
  cuando aplica; el selector de proveedor ofrece primero «Stock propio» si cubre la línea (el costo que usa la
  simulación es el del `proveedor_habitual` o, si no hay, el último `costo_historial` de cualquier proveedor
  de la empresa para ese producto; anotar la regla en el código). **Representación [V]:** `Candidato`
  (`proveedores.py:234`) y `ofertas_propias.lineas[].proveedor_producto_id` identifican el origen por
  `proveedor_producto_id`; «Stock propio» no tiene uno. Guardar en el JSON `{"origen": "stock",
  "empresa_producto_id": …}` junto a `proveedor_producto_id = null`, y que `oferta.validar_*`, el simulador
  (`/mesa/ca/{codigo}/simular`) y la exportación lo acepten; F-plat-lote reserva a partir de ese campo.
- **«Hoy»** (`pages.portada`, ruta `/`, ya cuenta proveedores y productos [V]): tarjeta «Inventario: n
  productos, m bajo mínimo» con enlace a `/inventario`.

## 4. Seguridad y datos
Ownership por `empresa_id` en cada query (regla 17); un `ep_id` de otra empresa → 404. Sin `raw_json`.
Tamaño: < 1 MB para 10 empresas × 500 productos [I]. Retención: no se purga.

## 5. Tests
CRUD con ownership; `ajustar_stock` nunca negativo y movimiento en la misma transacción; `cobertura_de_ca` con
conversión de formatos (pack 6 × 350 ml → L); importación (fila inválida no rompe el lote); render de las tres
entradas del menú y de la tarjeta de la mesa; plantilla descargable; vinculación de §1.3. Convención del
repo: `tests/test_plat_capas.py` (SQLite/mocks) y `tests/test_plat_capas_pg.py` (lo que necesite Postgres:
`UNIQUE`, `numeric`, `ILIKE`). `ruff`, `mypy app`, `pytest -rs` sin saltados contra Postgres local.

## 6. Cierre
Changelog («Catálogo en tres capas: Demanda MP, Catálogo Proveedores e Inventario»). `00-estado.md`. Commit:
`F-plat-capas: catálogo en tres capas con inventario`.

## 7. Pasos de Boris
1. Deploy con migración. Cargar los productos propios con la planilla (Mario tiene 54 proveedores y las
   facturas históricas: de ahí salen los productos que ya se compran).
2. Fuera de alcance, anotado por Boris: integraciones con APIs de proveedores para actualizar inventario. Por ahora
   la planilla.

## 8. Auditoría del prompt contra el código (5-nov, Cowork) — resumen
[V] = leído en el código ese día. Cabeza de Alembic `c8f3a1e6d2b4`. Tablas y columnas que el prompt da por
existentes existen: `producto_canonico.unidad_base`, `producto_formato.factor_base`, `ca_lineas.estado`
(`ok|dudosa|pendiente`) y `cantidad_base`, `proveedores`, `empresas`, `costo_historial`, `ofertas_propias.lineas`
(JSONB). `check_csrf` y `html_require_empresa` existen. Desvíos corregidos arriba: (1) `proveedor_productos`
no está vinculado al canónico en producción → §1.3 nuevo; (2) `importar.parsear` exige la hoja `precios` →
§2; (3) el selector de canónicos exige `linea_id` → generalizar; (4) «Stock propio» no cabe en
`proveedor_producto_id` → campo `origen` en el JSON; (5) el menú real es plano y se renderiza dos veces;
`app/api/nav.py` no existe; (6) tests en pares `*.py` / `*_pg.py`. Alcance: sigue siendo una fase, pero §1.3
la agranda; si Claude Code la ve larga, §1.3 puede ir primero como commit propio (`F-plat-capas-0: vincular
productos de proveedores al catálogo canónico`) y el resto después, misma migración.

## 9. Cómo quedó (implementación, 5-nov)
- **Migración** `a3c8e5d1b7f2`, cuelga de `d9a4e2b7c1f6` (una sola cabeza). Solo tablas nuevas; `stock`/`reservado`/`stock_minimo`
  son `numeric(14,3)` (el modelo usa `Numeric(asdecimal=False)`: llegan como `float` también en SQLite).
- **Inventario solo XLSX** (decisión de §2): un CSV no dice de qué hoja viene y se confundiría con una lista de precios.
  `parsear` ya no exige `precios` si el libro trae `inventario` (avisa que se importa desde Inventario).
- **§1.3** sin migración de datos: `guardar_producto(catalogo=…)` vincula solo con sugerencia única y de confianza alta;
  las filas ya existentes se vinculan con «Vincular pendientes» (`POST /proveedores/vincular-pendientes`, de la empresa).
  `canonico.sugerir_vinculo` suma el **nombre canónico exacto** (con números: «Arroz grado 1» ≠ «Arroz grado 2») porque
  las reglas de palabra clave (0,5) no alcanzan «ok».
- **Desvíos:** `ajustar_stock` solo acepta `ajuste|compra|entrega` (reserva y liberación mueven `reservado`: F-plat-lote);
  el filtro `Con stock` y la tarjeta de la mesa usan `resumen_por_ca` (2 consultas por lote); filtro nuevo `cantidad`
  (`numero` redondea a enteros); `fusionar_productos` también junta el inventario (suma el stock si chocan en el `UNIQUE`).

