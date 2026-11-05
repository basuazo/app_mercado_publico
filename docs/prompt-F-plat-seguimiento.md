# Prompt de implementación — F-plat-seguimiento (checklist administrativo, cotización formal, post-adjudicación y caja) · 5-nov-2026

> Para Claude Code. **Modelo: Sonnet** (estados y formularios sobre `ca_trabajo` y lotes; un generador de
> documento con una dependencia que ya está). Una fase = un commit, **sin push**. **Con migración** (`down_revision`
> = cabeza vigente, una sola). Va sobre F-equipo y **F-plat-lote** commiteadas (puede empezar en paralelo con
> F-plat-7; no tocan las mismas tablas, salvo que F-plat-7 §1.4 debe sumar la retención de esta fase).
> Origen: Revisión CBS (5-nov). Mario: «el proceso en Mercado Público implica ingreso manual de precios y
> documentos formales que pueden causar la descalificación»; «la plataforma debe emitir alertas para completar
> formularios, adjuntar archivos requeridos y dar seguimiento a las cotizaciones adjudicadas»; restricción de caja
> por plazos de pago del sector público (`docs/18-revision-cbs-y-flujos-operativos.md` §1 pasos 5–6 y §2.5).

## 0. Antes de escribir código
- `CLAUDE.md` (10, 11, 14, 17, 18, 26). `docs/prompt-F-equipo.md` §1 (`ca_trabajo` estados, `resultado`, eventos y
  alarmas), `docs/prompt-F-plat-lote.md` (lotes, reserva de stock, `cerrado`), `docs/prompt-F-plat-capas.md`
  (`ajustar_stock`, movimiento `entrega`), `app/mercado/equipo.py` (`cambiar_estado`, `generar_alarmas`,
  `resultado_de`), `app/mercado/oferta.py` (`xlsx`, `marcar_enviada`), `app/mercado/anexos.py` (nombres de
  documentos de la CA, si F-plat-anexos-2 los persistió), `app/models/tables.py` (`compras_agiles.direccion_entrega`,
  `plazo_entrega_dias`, `ofertas_propias`).
- **Sin dependencia nueva:** la cotización formal se genera como **DOCX con `python-docx`** (ya en el stack [V,
  `pyproject.toml`]) y como XLSX (ya existe). PDF no: exigiría `reportlab`/`weasyprint` (proponer aparte si el
  portal rechaza DOCX; anotar en `decisiones.md` qué formato aceptan las CA reales).

## 1. Modelo de datos

### 1.1 Checklist por CA (`ca_requisitos`)
`id`, `empresa_id`, `ca_codigo`, `texto` («Cotización formal adjunta», «Formulario de oferta firmado», «Declaración
jurada», «Precio ingresado línea a línea en el portal»…), `obligatorio` (bool), `origen` (`plantilla|documento_ca|
manual`), `cumplido` (bool), `cumplido_por_id`, `cumplido_en`, `orden`, `creado_en`. Índice `(empresa_id, ca_codigo)`.
- **Semilla automática** al priorizar una CA (hook en `equipo.cambiar_estado` → `priorizada` o al agregarla a un
  lote): (1) la plantilla de la empresa (§1.2); (2) un requisito por cada documento de la CA cuyo nombre parezca
  administrativo (`formulario|declaraci|anexo .*oferta|ficha|identificaci` [I: lista en una constante, ampliable])
  con `origen=documento_ca` y el nombre del archivo; los anexos de productos no generan requisito. Idempotente:
  no duplica por `(empresa_id, ca_codigo, texto)`.
- Regla dura de la fase: **no se puede marcar `enviada` con un requisito obligatorio sin cumplir** (`equipo.cambiar_estado`
  valida; 422 con la lista; el admin puede forzar con motivo, queda en el evento).

### 1.2 Plantilla de requisitos de la empresa (`empresa_requisitos`)
`id`, `empresa_id`, `texto`, `obligatorio`, `orden`, `activo`. Editable en `/admin/empresas/{id}` y en una página
`/requisitos` para la empresa. Semilla inicial (al crear la empresa o al migrar): «Cotización formal adjunta»,
«Precios ingresados línea a línea en el portal», «Plazo de entrega indicado», «Documentos de la empresa vigentes
(boleta de garantía / certificados si los pide)» — todos editables; nada se inventa como obligatorio del portal
sin verificar [I].

### 1.3 Cotización formal (`ofertas_propias`)
Columnas nuevas: `numero_cotizacion` (texto corto, correlativo por empresa `COT-{año}-{n}` generado en servidor con
`FOR UPDATE` sobre `empresas.ultimo_correlativo`, nueva columna), `validez_dias` (default 30), `condiciones`
(texto; default de la empresa en `empresas.condiciones_cotizacion`), `documento_generado_en`.
- `GET /mesa/ca/{codigo}/cotizacion.docx`: documento con membrete (nombre, RUT, dirección, contacto de la empresa
  — campos nuevos en `empresas`: `direccion`, `contacto`, `giro`), número, fecha, validez, destinatario (comprador
  y unidad de la CA), referencia (código y nombre de la CA), tabla de líneas (producto, formato, cantidad, precio
  neto unitario, total neto), totales neto / IVA 19 % / bruto, plazo de entrega, condiciones, pie «Fuente de los
  datos de la Compra Ágil: Dirección ChileCompra». Mismas líneas que el XLSX de F-plat-5 [V]; una sola función que
  arma las filas para ambos.
- Generar el documento marca cumplido el requisito «Cotización formal adjunta»? **No**: adjuntarla en el portal lo
  hace la persona; el documento solo se genera. El requisito lo marca la persona (un clic).

### 1.4 Post-adjudicación (`ca_trabajo` + `ventas`)
- `ca_trabajo.estado` suma `adjudicada` (la fija el sistema cuando `resultado = ganada` [V, F-equipo §1.3];
  también a mano), y después vive en `ventas`.
- `ventas`: `id`, `empresa_id`, `ca_codigo` (UNIQUE con empresa), `lote_id` (nullable), `oferta_id`, `estado`
  (`adjudicada|oc_recibida|comprando|entregada|facturada|pagada|anulada`), `oc_numero`, `oc_fecha`, `oc_monto`,
  `entrega_fecha_comprometida` (= `oc_fecha + plazo_entrega_dias` de la oferta), `entrega_fecha_real`,
  `entrega_direccion` (prellenada con `compras_agiles.direccion_entrega` [V]), `entrega_horario`, `entrega_recibe`
  (nombre/teléfono), `guia_numero`, `factura_numero`, `factura_fecha`, `factura_monto`, `pago_esperado_en`
  (= `factura_fecha + plazo_pago_dias`), `pago_recibido_en`, `plazo_pago_dias` (default `empresas.plazo_pago_comprador_dias`,
  nueva columna, default 30 [I: Boris fija; puede ajustarse por comprador en una fase posterior]), `costo_real`
  (suma de compras registradas), `notas`, `version`, `actualizado_en`, `actualizado_por_id`.
- `venta_compras`: `venta_id`, `proveedor_id`, `producto_id`, `formato_id`, `cantidad`, `precio_neto`, `fecha`,
  `pagada` (bool), `pagar_en` (nullable). Registrar una compra escribe `costo_historial` (`fuente=compra`) y, si el
  producto está en `empresa_productos`, un movimiento `compra` (+stock). Marcar `entregada` descuenta stock con
  movimiento `entrega` (referencia `venta:{id}`) y libera la reserva del lote por esa CA.
- `margen_realizado` de `ofertas_propias` [V] pasa a calcularse con `costo_real` cuando existe (si no, como hoy).
- Cada cambio de estado de la venta escribe un evento en `ca_eventos` (tipo `venta`, `datos.estado`), en la misma
  transacción, para la campana y `/actividad`.

### 1.5 Alarmas de sistema (en `generar_alarmas`, idempotentes como las de F-equipo [V])
`requisitos_pendientes` (CA `cotizando|lista` con obligatorios sin cumplir y cierre < 24 h), `oc_sin_registrar`
(`adjudicada` > 3 días sin OC), `entrega_vence` (comprometida en ≤ 2 días y no entregada), `factura_pendiente`
(`entregada` > 5 días sin factura), `pago_atrasado` (`pago_esperado_en` < hoy y sin pago). Constantes en un lugar.

## 2. UI (solo usuarios con empresa; CSRF en mutaciones)
- **Ficha de la CA**: bloque «Requisitos» (checklist con casillas, agregar requisito manual, quién y cuándo);
  botón «Cotización formal (DOCX)» junto a «Exportar XLSX»; al intentar «Marcar enviada» con obligatorios
  pendientes, el 422 se pinta con la lista (como `equipo.js` [V]).
- **`/ventas`** (menú «Ventas»): tabla por estado (pestañas), con fechas comprometidas en rojo si vencen;
  `/ventas/{id}`: formulario por etapa (OC → compras → entrega → factura → pago), movimientos de stock de la venta,
  margen (ofertado vs. realizado).
- **`/caja`** (puede ser una pestaña de `/ventas`): por cobrar al Estado (facturado no pagado, por `pago_esperado_en`),
  comprometido con proveedores (compras no pagadas), ventas adjudicadas aún sin facturar (monto de la OC) y un
  total neto a 30/60 días. Sin gráficos en esta fase; una tabla y tres números.
- **«Hoy»**: tarjeta «Ventas: n en curso · por cobrar $X · entregas esta semana m».
- **Lote** (`/lotes/{id}`): las CA ganadas muestran enlace a su venta.

## 3. Seguridad y datos
Ownership por `empresa_id` en requisitos, ventas y compras; ids ajenos → 404. El DOCX se genera en memoria y se
devuelve; no se guarda (regla 11); el número correlativo sí. Datos de contacto de la entrega son del comprador y
se tratan como dato de negocio (no se exportan a terceros). Retención: `ventas` no se purga; `ca_requisitos` de CA
`descartada` > 12 meses se purgan (F-plat-7 §1.4).

## 4. Tests
Semilla de requisitos (plantilla + documentos administrativos; idempotente); bloqueo de `enviada` con obligatorio
pendiente y forzado por admin con motivo; correlativo bajo concurrencia; DOCX con las mismas filas que el XLSX
(abrirlo con `python-docx` y comparar la tabla); ventas: transiciones, fechas derivadas, compra → costo_historial y
stock, entrega → stock y liberación de reserva, margen realizado con costo real; alarmas (cada una, idempotente);
caja (tres números con fixtures); ownership; «Hoy». `ruff`, `mypy app`, `pytest -rs` sin saltados contra Postgres
local.

## 5. Cierre
Changelog («Checklist por Compra Ágil, cotización formal, seguimiento de ventas y caja»). `00-estado.md`.
`docs/prompt-F-plat-7.md` §1.2: el informe mensual suma ventas (OC, entregas, facturado, cobrado) y §1.4 la
retención de requisitos. Commit: `F-plat-seguimiento: requisitos, cotización formal, ventas y caja`.

## 6. Pasos de Boris
1. Deploy con migración. Completar membrete, condiciones y plazos de la empresa en `/admin/empresas/{id}`.
2. Con Mario: revisar 3 CA reales en el portal y ajustar la plantilla de requisitos a lo que de verdad piden;
   confirmar si aceptan la cotización en DOCX o hay que pasarla a PDF (decide una fase chica con `reportlab`).
3. Registrar la primera venta real de punta a punta y comparar el margen realizado con el ofertado.
