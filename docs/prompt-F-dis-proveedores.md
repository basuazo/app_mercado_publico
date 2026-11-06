# Prompt — F-dis-proveedores (Proveedores con vista por producto, Inventario y Logística) · 7-nov-2026

> Para Claude Code. **Modelo: Opus**: la matriz producto × proveedor cruza precios netos y brutos con impuestos y
> reservas de inventario. Un commit, **sin push**, **sin migración**. Leer `docs/diseno/README.md` y las maquetas
> `Proveedores.dc.html`, `Inventario.dc.html` y `Logistica.dc.html`. Requiere en `main`: F-dis-1, el cierre de
> F-plat-impuestos y F-dis-ventas (usa `logistica.costo_despacho`). Puede correr en paralelo con F-dis-demanda.
> **Conservar** todo lo que agregó F-plat-impuestos en estas pantallas: neto y bruto, filtro «Impuesto por
> clasificar», aviso «el catálogo dice otra cosa», columnas nuevas de la planilla.

## 1. Proveedores (`proveedores.html`, `proveedor_ficha.html`, `proveedor_lista_precios.html`, `_proveedor_form.html`, `_producto_*.html`)
- Segmentado **Por producto / Por proveedor** (recordado en `usuario_vista('proveedores')`; por defecto Por producto).
- **Por proveedor:** lista (nombre, comuna, canal correo/WhatsApp, rubros, productos, estado de precios: «Al día» o
  «N desactualizados» con explicativo de cuáles), filtros (Todos, Con precios desactualizados, Impuesto por clasificar),
  búsqueda. Panel = ficha: contacto, canal, plazo de pago, mínimo, despacho; productos con precio **bruto**, neto en
  el explicativo, variación ▲▼ y antigüedad en días; lista de precios subida; primario **«Pedir lista por correo /
  WhatsApp»** cuando hay precios desactualizados (reusar el texto y enlaces de solicitudes de `lote.py`), «Subir lista
  nueva». Aviso con Deshacer al marcar la solicitud.
- **Por producto (nuevo):** función pura `proveedores.matriz_precios(session, empresa_id, familia=None, q=None) ->
  Matriz` en un número fijo de consultas: filas = productos canónicos (o propios sin vínculo) con al menos un precio;
  primera columna **Bodega** (stock libre descontando reservas y costo promedio, de `empresa_productos`); una columna
  por proveedor activo con precio **bruto por unidad base** (usar `impuestos.bruto`; si el formato difiere, normalizar
  con `producto_formato`), antigüedad y variación; **más barato** marcado; **desactualizado (> 15 días)** marcado; «—»
  si no lo vende; diferencia % entre el más caro y el más barato. Columnas ordenadas por cuántos productos cubre cada
  proveedor; tope de 12 columnas con «+N proveedores» (scroll horizontal solo en pantallas angostas).
  Celda → detalle debajo de la tabla: lectura («hay $ X más barato en …», precio viejo, stock en bodega, único
  proveedor), historia de 4 meses (de `costo_historial`), «Pedir precio actualizado por …», «Ver ficha». Leyenda.
  Filtros: familia y búsqueda. Export CSV.
## 2. Inventario (`inventario*.html`, `_inventario_*.html`)
Cifras (valor en bodega a costo, reservado en ofertas, bajo mínimo contando reservas, sin movimiento > 90 días);
filtros (Todos, Bajo mínimo, Con reservas); columnas stock, reservado, disponible, mínimo, costo, estado con señal;
panel con **reservas por lote** (enlazan al lote), movimientos (`_inventario_movimientos.html`), «Pedir reposición a
<proveedor habitual>» (crea solicitud de precio con el flujo existente; aviso con Deshacer) y «Ajustar stock»
(formulario actual). Importar y exportar planilla se conservan.
## 3. Logística (`proveedores_logistica.html`)
Tarifas (transportista, cubre comuna/provincia, fijo, por kg, días, editar); calculadora **«¿Cuánto cuesta
llevarlo?»** (destino comuna + kg → opciones ordenadas con `logistica.costo_despacho`, «Más barato», aviso si nadie
cubre); despachos de la semana desde ventas con entrega programada (enlace a la venta).
## 4. Tests y cierre
`matriz_precios`: más barato, desactualizado, sin precio, impuesto (bruto correcto con ILA), formatos distintos,
empresa ajena, número fijo de consultas; inventario bajo mínimo con reservas; calculadora sin cobertura.
Cierre según `docs/diseno/README.md` §13. Commit: `F-dis-proveedores: vista por producto, inventario con reservas y
calculadora de despacho`. Changelog visible.
