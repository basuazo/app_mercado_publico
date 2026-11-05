# Prompt de implementación — F-plat-lote (cotizar varias CA a la vez: agregación, solicitud de precios a proveedores y precios vencidos) · 5-nov-2026

> Para Claude Code. **Modelo: Opus** (esquema nuevo que F-plat-seguimiento hereda, agregación con conversión de
> formatos, concurrencia entre usuarios y reserva de stock). Una fase = un commit, **sin push**. **Con migración**
> (`down_revision` = cabeza vigente, una sola). Va sobre F-plat-5, F-equipo y **F-plat-capas** commiteadas.
> Origen: Revisión CBS (5-nov). Boris: «seleccionar cinco compras ágiles y apretar un botón cotizar, que la
> plataforma calcule sola cuánto tiene que cotizar de cada cosa, qué proveedor tiene estos productos y devuelva la
> cotización»; Gonzalo: cuello de botella 2 = actualización de precios por parte del proveedor; Mario: ciclos de
> 3–5 CA, una semana entre ciclos, el precio depende del volumen, no quebrar stock
> (`docs/18-revision-cbs-y-flujos-operativos.md` §1 y §2.3–2.4).

## 0. Antes de escribir código
- `CLAUDE.md` (10, 11, 12, 14, 17, 18, 26). `docs/prompt-F-equipo.md` §1 (estados y `version`),
  `docs/prompt-F-plat-5.md` §§ oferta (`armar`, `simular`, `guardar`, `xlsx` en `app/mercado/oferta.py` [V]),
  `docs/prompt-F-plat-capas.md` (`empresa_productos.stock/reservado`, `ajustar_stock`, movimientos; capa «Inventario»),
  `app/mercado/proveedores.py` (`mejor_costo`, `guardar_producto`, `_registrar_costo`), `app/mercado/importar.py`
  (`planilla_de_lista`, `aplicar`: la respuesta del proveedor reutiliza este camino), `app/mercado/equipo.py`
  (`cambiar_estado`, eventos), `app/catalogos/canonico.py` (`factor_base`, `unidad_base`).
- [V] `proveedor_productos.precio_neto_actual` + `vigente_desde`; `mejor_costo` elige por precio y `disponible`
  y **no mira la antigüedad**. [V] `ofertas_propias.lineas` es JSON con proveedor por línea.
- Lo que NO hace esta fase: mandar WhatsApp o correo (se genera el texto y la planilla; la persona los envía),
  comprar, scraping de precios. Correo a proveedores podría ir después con Brevo (regla 14) si el equipo lo pide.

## 1. Modelo de datos (solo `app/models` define esquema)

### 1.1 `lotes` — un ciclo de cotización
`id`, `empresa_id`, `nombre` (default «Lote del {fecha}»), `estado` (`abierto|solicitado|con_precios|ofertado|
cerrado`), `creado_por_id`, `creado_en`, `actualizado_en`, `version`, `fecha_limite` (nullable: el cierre más
cercano de sus CA, se recalcula), `notas`. Índice `(empresa_id, estado)`.
- `lote_cas`: `lote_id`, `ca_codigo`, `agregada_por_id`, `agregada_en`; `UNIQUE (lote_id, ca_codigo)`. Una CA
  puede estar en **un solo lote abierto** de la empresa (índice único parcial sobre una vista? No: validar en
  servidor con `FOR UPDATE` del lote; test de carrera).
- Agregar una CA al lote la pasa a `cotizando` en `ca_trabajo` (vía `equipo.cambiar_estado`, con evento y
  `datos.lote_id`); quitarla la deja como estaba antes (guardado en `datos`) o `priorizada`.

### 1.2 `lote_lineas` — la agregación por producto × formato
Derivada; el servidor la recalcula al agregar/quitar CA o al cambiar líneas de una CA del lote (recalcular todo
el lote: son decenas de filas). `id`, `lote_id`, `producto_id`, `formato_id` (el formato **de compra**: el más
usado entre los proveedores de la empresa para ese producto, si no el base), `cantidad_base` (suma en
`unidad_base` de las líneas `ok|dudosa` con `producto_id` de todas las CA del lote), `cantidad_formato`
(= `cantidad_base / factor_base`, redondeada hacia arriba), `en_stock_base` (de `empresa_productos`),
`a_comprar_base` (= max(0, cantidad − en_stock)), `proveedor_sugerido_id` (mejor costo **vigente**, §2.3),
`costo_unitario_sugerido`, `detalle` (JSON: por CA, cantidad aportada y línea). `UNIQUE (lote_id, producto_id,
formato_id)`.
- Líneas sin `producto_id` (pendientes) no se agregan: el lote las lista aparte como «sin producto: n líneas en
  m CA» con enlace a corregirlas. **El lote no bloquea por eso**, avisa.

### 1.3 `solicitudes_precio` — lo que se le pide a cada proveedor
`id`, `lote_id`, `proveedor_id`, `estado` (`borrador|enviada|respondida|sin_respuesta`), `canal`
(`whatsapp|correo|llamada|planilla|otro`), `enviada_en`, `enviada_por_id`, `respondida_en`, `texto_generado`
(el mensaje listo para pegar), `notas`, `version`. `UNIQUE (lote_id, proveedor_id)`.
- `solicitud_lineas`: `solicitud_id`, `lote_linea_id`, `cantidad_formato`, `precio_respuesta` (nullable),
  `tramos` (JSON nullable: `[{desde, precio}]` cuando el proveedor da precio por volumen), `disponible`
  (bool nullable), `plazo_dias` (nullable), `respondida_en`.
- Al registrar una respuesta: `proveedores.guardar_producto`/`_registrar_costo` con `fuente=solicitud` y
  `vigente_desde = hoy` (historial de F-plat-3 [V]) → el precio vuelve a `proveedor_productos`. Un tramo se guarda
  en `proveedor_productos.tramos` (JSON, nueva columna nullable): `mejor_costo` usa el tramo que cubre la cantidad
  pedida cuando se le pasa `cantidad`.

### 1.4 Precios vencidos (cuello de botella 2)
- `empresas.dias_vigencia_precio` (int, **default 15**, decisión de Boris 5-nov; editable en `/admin/empresas/{id}` y en `/proveedores/logistica`?
  No: en la ficha de la empresa, es un parámetro comercial).
- `proveedores.vencido(pp, hoy, dias) -> bool` = `hoy − vigente_desde > dias`. `mejor_costo(..., permitir_vencidos=
  False)`: por defecto excluye vencidos; si no queda ninguno, devuelve el mejor vencido con `vencido=True` y la UI lo
  pinta en ámbar. **Toda** la plataforma que muestra costo (mesa, ficha, lote) muestra «precio de hace n días» si
  `n > dias`.
- `/proveedores`: filtro «Vencidos» y conteo en «Hoy» («12 precios por actualizar»). Alarma de sistema semanal
  `precios_vencidos` en `ca_eventos`? No es por CA. Escribirla como aviso en «Hoy» y en la campana con un evento
  **por lote** cuando un lote usa un precio vencido (`datos.lote_id`), idempotente por lote.

### 1.5 Reserva de stock
Al pasar el lote a `ofertado` (§2.5) se reserva en `empresa_productos.reservado` lo que cubre el stock
(movimiento `reserva` con `referencia = lote:{id}`); al cerrar el lote (`cerrado`, ganó o no) se libera
(`liberacion`) salvo lo que F-plat-seguimiento convierta en `entrega`. Reserva nunca > stock; si no alcanza,
avisa y reserva lo que hay.

## 2. Flujo (`app/mercado/lote.py`, nuevo; sin httpx)

### 2.1 Desde la mesa: «Cotizar seleccionadas»
- `/mesa`: casilla por tarjeta + barra inferior fija «n seleccionadas · Cotizar» (JS chico en `static/`, como
  `equipo.js`). `POST /lotes` con los códigos (CSRF) → crea el lote (o agrega al lote abierto del usuario si
  elige «agregar al lote actual») → redirige a `/lotes/{id}`.
- Solo CA vigentes de las regiones de la empresa; una CA ya en otro lote abierto → 409 con el nombre del lote.

### 2.2 `/lotes/{id}` — la pantalla de trabajo del lote
Tres bloques:
1. **CA del lote**: nombre, comprador, cierre, líneas (`completo|incompleto|sin_detalle` con enlace a
   corregir), estado de `ca_trabajo`, quitar.
2. **Agregado** (`lote_lineas`): producto, formato de compra, cantidad total, en stock, a comprar, proveedor
   sugerido (con «vencido» si aplica y los otros candidatos en un desplegable), costo unitario, subtotal. Cambiar
   el proveedor de una línea (HTMX). «Recalcular».
3. **Solicitudes**: una por proveedor con líneas asignadas: «Generar mensaje» → `texto_generado` (plantilla
   Jinja de texto plano: saludo, nombre de la empresa, lista «producto × formato × cantidad», fecha límite =
   `fecha_limite − 1 día`, pedir precio neto, disponibilidad y plazo; sin datos de la CA ni del comprador), botón
   «Copiar», «Descargar XLSX» (una hoja con las líneas y columnas vacías `precio`, `disponible`, `plazo`, que el
   proveedor devuelve llena), «Marcar enviada» (canal). «Registrar respuesta»: tabla editable (precio, tramos,
   disponible, plazo) **o** subir la planilla devuelta / foto de la lista (reutiliza el extractor de F-plat-2 con el
   esquema `precios` y `importar.aplicar` [V]) → guarda costos → recalcula el agregado.
- Estado del lote: `abierto` → `solicitado` (≥ 1 solicitud enviada) → `con_precios` (todas respondidas o
  `sin_respuesta`) → `ofertado` → `cerrado`. Transiciones libres hacia atrás salvo desde `cerrado`.
  Concurrencia con `version` como F-equipo [V].

### 2.3 Mejor costo vigente
`mejor_costo(session, empresa_id, producto_id, cantidad_base, *, permitir_vencidos=False)` → `Candidato` con
`vencido`, `tramo_aplicado`, `dias`. Única definición; la mesa y la ficha de F-plat-5 la llaman con la cantidad de
la línea. Orden: no vencido antes que vencido; precio del tramo; `disponible`; proveedor habitual de
`empresa_productos` como desempate.

### 2.4 Pre-llenar las ofertas
«Armar ofertas» (desde `con_precios`): por cada CA del lote, `oferta.armar` con el proveedor y costo elegidos en el
lote por línea (nuevo parámetro `costos_por_producto`), guarda la oferta (`guardar`, estado `cotizando` ya está)
sin precio final si no hay predicción: el precio sugerido viene de F-plat-4 si existe, si no `costo × (1 + margen
objetivo)` con `empresas.margen_objetivo` (nueva columna, default 0,15 [I: Boris fija]). La persona ajusta precio
por CA en la ficha como hoy. El lote muestra el resumen: total a vender, total a comprar, margen simple del lote,
y «CA sin oferta armada: n» (líneas sin producto).

### 2.5 Ofertado y cierre
«Marcar ofertado» cuando todas las ofertas están `lista|enviada` en `ca_trabajo` → reserva (§1.5), `fecha_limite`
deja de moverse. «Cerrar lote»: cuando todas las CA tienen `resultado` (F-equipo lo llena [V]) o a mano; libera
reservas; resumen final (ganadas, monto, margen esperado vs. simple). F-plat-seguimiento sigue desde acá.

### 2.6 «En trabajo» (`/trabajo`)
La pantalla que Boris describió como «otro sector de trabajo»: lotes abiertos de la empresa (con sus CA y estado),
CA `priorizada|cotizando|lista` **sin** lote, y «cierran en 24 h». Menú: «Mesa» (lo abierto) · «En trabajo» (lo
mío) · … «Hoy» suma una tarjeta «En trabajo: n lotes, m CA, cierre más próximo».

## 3. Seguridad y datos
Ownership por `empresa_id` en lotes, solicitudes y respuestas; un id ajeno → 404; `ProveedorAjeno` como en
F-plat-5 [V]. `texto_generado` no incluye el código de la CA ni el comprador (no revelar al proveedor qué se está
cotizando). Tamaño: < 2 MB al año para 2 lotes/semana [I]. Retención: lotes `cerrado` > 24 meses se purgan con sus
hijas (anotar en F-plat-7 §1.4).

## 4. Tests
Fixtures: 3 CA de bebidas con líneas en formatos distintos (lata 350 ml, botella 1,5 L, pack 6) y 2 proveedores
(uno con precio vencido, uno con tramos). Casos: agregación en unidad base y redondeo a formato de compra;
líneas sin producto apartadas; una CA en un solo lote abierto (carrera con dos sesiones); proveedor sugerido no
vencido; tramo por cantidad; respuesta registra costo con historial y recalcula; texto generado sin datos del
comprador; XLSX ida y vuelta; armar ofertas con costos del lote; reserva ≤ stock y liberación al cerrar; estados y
`version`; ownership; «En trabajo». `ruff`, `mypy app`, `pytest -rs` sin saltados contra Postgres local.

## 5. Cierre
Changelog («Cotizar varias Compras Ágiles a la vez: lote, solicitud de precios a proveedores y precios
vencidos»). `00-estado.md`. `docs/prompt-F-plat-7.md` §1.4: sumar la purga de lotes. Commit:
`F-plat-lote: cotización por lote, solicitudes de precio a proveedores y precios vencidos`.

## 6. Pasos de Boris
1. Deploy con migración. Fijar `dias_vigencia_precio` y `margen_objetivo` de la empresa.
2. Primer ciclo real con Mario: 3 CA de una familia, un lote, 1–2 solicitudes por WhatsApp, respuesta tipeada,
   ofertas armadas. Medir el tiempo de «mesa → ofertas listas» y anotarlo en `decisiones.md`.
3. Decidir si el mensaje al proveedor sale por correo desde la plataforma (Brevo, regla 14) en una fase chica.
