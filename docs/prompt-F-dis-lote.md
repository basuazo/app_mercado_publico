# Prompt — F-dis-lote (vestir En trabajo: lotes, modal de línea, abastecimiento y Enviar) · 7-nov-2026

> Para Claude Code. **Modelo: Opus**: es la pantalla más compleja (HTMX + `lote.js`, reservas, impuestos).
> Un commit, **sin push**, **sin migración**. Leer `docs/diseno/README.md`, `docs/diseno/decisiones-diseno.md`
> (En trabajo / Lote, Paso 3 · Enviar, Equipo) y las maquetas `Lote.dc.html`, `Enviar.dc.html` y `Cotizacion.dc.html`.
> Requiere en `main`: F-dis-1, F-dis-lote-datos y el cierre de F-plat-impuestos.
> Puede correr en paralelo con F-dis-ventas (worktree propio).

## 1. Encabezado y lotes (`trabajo.html`, `lotes.html`, `lote.html`)
- `/trabajo`: «En trabajo», N lotes abiertos · N CA recibidas sin lote; selector de lote (segmentado con nombre
  «Lote 14 · Limpieza»), «Nuevo lote». Franja **«Recibidas de la mesa»** con «Agregar al lote».
- Lote: nombre con lápiz para renombrar (Enter guarda, Esc cancela), nota, **presencia** («Gonzalo también está
  aquí · en …», `GET /presencia`), botón «Conversación del lote (N)» con «@» si te mencionaron y vista previa del
  último comentario; pasos **1 Agregar CA · 2 Armar ofertas · 3 Enviar**.
- Pestañas por CA (`m-tab`) con estado, cierre y total; menú de CA: «Mover a…» (lotes abiertos + «Nuevo lote»),
  «Quitar del lote», ambos con aviso y **Deshacer** (`HX-Trigger` `aviso` de F-dis-1). Lote vacío con su estado.

## 2. Oferta de una CA: filas compactas + modal por línea
- Tarjeta de oferta: total, % del disponible, margen (neto), P(ganar), plazo; toggle «N comentarios» con el hilo de
  la CA. Filas compactas: necesidad del comprador (texto original), producto elegido, cantidad, costo unitario,
  precio unitario, señales («Precio desactualizado · N días», «Sin precio», «No alcanza la bodega», «Impuesto por
  clasificar», «elegido por historial»).
- Clic en la fila abre el **modal** de la línea (`modal()` de `_r.html`; `Esc`/«Listo»; anterior/siguiente línea):
  - Arriba: texto original de la línea y su origen («del anexo, pág. N» si viene de `ca_anexos`).
  - **Piso 1 · Nuestro inventario**: productos propios que calzan, con stock libre (descontando reservas de **todos**
    los lotes abiertos), costo y «Usar este».
  - **Piso 2 · Proveedores**: opciones con precio **bruto** visible y neto en el explicativo, antigüedad, plazo.
  - **«Sin producto actual · buscar proveedores»**: crea la solicitud de precio por la línea a proveedores del rubro
    (función existente de solicitudes).
  - «Buscar en el inventario» + «Recordar para líneas parecidas» (F-dis-lote-datos) y «Olvidar».
  - Precio unitario editable; el margen se recalcula con `oferta`/`lote` existentes (sin lógica en la vista).
- **Abastecimiento del lote**: 1 · Desde nuestro inventario (reservas) · 2 · Solicitudes a proveedores, agrupadas por
  proveedor, por **correo o WhatsApp** según el proveedor (enlaces que ya genera `lote.py`); el mensaje pide precio
  **con impuestos** (F-plat-impuestos). Estado de cada solicitud y «Anotar respuesta».

## 3. Paso 3 · Enviar
Vista como `Enviar.dc.html`: lista de CA del lote con estado (Lista para enviar · Faltan N requisitos · Oferta en armado ·
Enviada HH:MM) y cierre; detalle con **«Lo que vas a escribir en el portal»** (precio unitario **bruto** por línea con
«Copiar» y «Copiar todos»), requisitos por CA (`_requisitos.html` vestido: cotización formal «Generar»/«Ver»,
declaración jurada, ficha técnica, resolución sanitaria, «Usar la guardada» si hay documento vigente), «Abrir CA» en el
portal y **«Marcar enviada»** bloqueado hasta cumplir los requisitos obligatorios, con Deshacer. Regla 9: el ingreso en
Mercado Público es a mano; Rastrillo no envía nada al portal.
Cotización formal: el DOCX de `cotizacion.py` sigue siendo la fuente. Ajustar su formato a `Cotizacion.dc.html` (logo
de la empresa si existe, sin firma, total bruto con neto/IVA/impuesto específico referencial, condiciones). Si generar
PDF exige una dependencia nueva, **no agregarla**: proponerla en el informe final.

## 4. Tests y cierre
Render de trabajo, lote (con 0, 1 y varias CA), modal de línea con cada piso y señal, Enviar con requisitos bloqueando;
mover/quitar con Deshacer; hilo del lote; presencia; DOCX con y sin logo. Teclado: `Esc` cierra el modal, flechas en
pestañas. Cierre según `docs/diseno/README.md` §13. Commit: `F-dis-lote: En trabajo con lotes con nombre, modal por
línea y paso Enviar`. Changelog visible.
