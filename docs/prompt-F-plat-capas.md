# Prompt de implementación — F-plat-capas (catálogo en tres capas: Demanda MP, Catálogo Proveedores, Inventario) · 5-nov-2026

> Para Claude Code. **Modelo: Sonnet** (CRUD por empresa, navegación y una tabla nueva con ownership).
> Una fase = un commit, **sin push**. **Con migración**: `down_revision` = la cabeza que deje la fase anterior
> (`alembic heads`, una sola). Va sobre F-plat-1 y F-plat-3 commiteadas; no necesita F-plat-lote (pero
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
  stock, stock mínimo, plazo, marca). Plantilla descargable. Vista previa + confirmación como en F-plat-3.

## 3. UI (solo usuarios con empresa; mutaciones con CSRF)
- **Menú** (nombres decididos por Boris, 5-nov): tres entradas bajo un encabezado «Catálogo» (en móvil tienen
  que caber): **«Demanda MP»** (la actual `/catalogo`, renombrada en título, migas y menú; la URL no cambia para no
  romper enlaces ni tests), **«Catálogo Proveedores»** (la actual `/proveedores`) e **«Inventario»** (nueva,
  `/inventario`). Constantes en un solo módulo `app/api/nav.py`.
- `/inventario`: tabla (producto, formato, marca, stock, reservado, disponible, mínimo, plazo, proveedor
  habitual, activo), buscador por texto (ILIKE escapado), «Agregar producto» (búsqueda del canónico con el
  mismo autocompletar que F-plat-1 usa en `/catalogo/ca/{codigo}` [V]: no duplicar), edición en línea (HTMX),
  «Ajustar stock» (delta + motivo), importar/plantilla, aviso rojo cuando `disponible < stock_minimo`.
- **Mesa** (`/mesa`, tarjeta): una línea «Tengo: 3 de 5 líneas en stock · falta: arroz 20 kg» y filtro
  «Con stock» (al menos una línea cubierta). Lo calcula `cobertura_de_ca` por lote de CA (una query por página,
  no por tarjeta).
- **Ficha de la CA**, sección «Precio y mi oferta» (F-plat-5): junto al proveedor por línea, «en stock: n»
  cuando aplica; el selector de proveedor ofrece primero «Stock propio» si cubre la línea (el costo que usa la
  simulación es el del `proveedor_habitual` o, si no hay, el último `costo_historial` de cualquier proveedor
  de la empresa para ese producto; anotar la regla en el código).
- **«Hoy»**: tarjeta «Inventario: n productos, m bajo mínimo».

## 4. Seguridad y datos
Ownership por `empresa_id` en cada query (regla 17); un `ep_id` de otra empresa → 404. Sin `raw_json`.
Tamaño: < 1 MB para 10 empresas × 500 productos [I]. Retención: no se purga.

## 5. Tests
CRUD con ownership; `ajustar_stock` nunca negativo y movimiento en la misma transacción; `cobertura_de_ca` con
conversión de formatos (pack 6 × 350 ml → L); importación (fila inválida no rompe el lote); render de las tres
entradas del menú y de la tarjeta de la mesa; plantilla descargable. `ruff`, `mypy app`, `pytest -rs` sin
saltados contra Postgres local.

## 6. Cierre
Changelog («Catálogo en tres capas: Demanda MP, Catálogo Proveedores e Inventario»). `00-estado.md`. Commit:
`F-plat-capas: catálogo en tres capas con inventario`.

## 7. Pasos de Boris
1. Deploy con migración. Cargar los productos propios con la planilla (Mario tiene 54 proveedores y las
   facturas históricas: de ahí salen los productos que ya se compran).
2. Fuera de alcance, anotado por Boris: integraciones con APIs de proveedores para actualizar inventario. Por ahora
   la planilla.
