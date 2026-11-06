# Handoff — rediseño de Rastrillo (6-nov-2026)

## Dónde está todo
- **Sistema de diseño (artifact «Rastrillo», tipo Design System):** https://claude.ai/artifact/7XoUyHfaDEDerTg9bKop21 — ronda 2 (versión 1793978230-da31). Tokens claro/oscuro, README con principios, átomos: Botón, Estado, Segmentado (+filtros), Señal, Dato, Ícono, portada.
- **Canvas de maquetas (artifact «Rastrillo · Maquetas», tipo Design):** https://claude.ai/artifact/CQry1MV4XCCRMVqUjsuF6E — 18 artboards: flujo principal en la fila 1, componentes y cotización en la fila 2, pantallas de apoyo en las filas 3–4.
- Código de la app: `app/api/templates/` (Jinja + HTMX + Bootstrap 5.3), `app/api/static/app.css` (tokens viejos F-plat-ui: Inter, índigo #4F46E5, 3 sistemas de tokens superpuestos), menú en `base.html` (macro `menu`).

## Lo que pidió Boris
- «Operación profunda»: la app está diseñada desde la información, no desde el usuario; cajas con mucha información; no se distingue qué se acciona, qué es información y qué requiere atención; poco atractiva e «infantil».
- Es una **estación de trabajo**: cómoda, accesible, compleja pero en capas. El macro (menú y orden) está bien.
- Método: **diseño atómico**, iterar elemento por elemento, luego maquetas.
- Referencias que quiere incorporar: **Stripe** (dashboard y app móvil: marino, índigo, cifras grandes, segmentado en pastilla, íconos dúo, modo oscuro marino/lavanda, tarjetas sutiles) y **GradientDeck** (gradientdeck.com/ai-components/chat: Geist, oscuro zinc, paleta de comandos, atajos visibles, texto con brillo mientras la IA trabaja, menciones en pastilla).
- Modales: no como regla general, **sí para configurar una línea de oferta** (pedido explícito de Boris, 6-nov noche).

## Decisiones tomadas (ronda 2)
- Fuente Geist / Geist Mono (Google Fonts). Íconos Lucide (ISC), trazo 1,75, baldosa dúo índigo + cian.
- Color: marino (tinta), índigo `accion` SOLO para lo accionable, cian `acento-2` solo segundo tono/series; semánticos ok/atencion/urgente con fondo. Índigo propio, no el de Stripe; nada de su logo ni marca.
- Gramáticas: se aprieta (relleno/contorno, hover) · se lee (`r-estado` chico tenue con punto) · te necesita (`r-senal` ícono + frase).
- Resumen primero, detalle en panel lateral/modal; teclado primero (⌘K, atajos visibles); un primario por zona; movimiento con propósito.
- Regla de maquetado detectada: en estados, señales, botones y notas (`r-dato__nota`, todos inline-flex con gap) la frase va en UN solo nodo de texto (envolver en `<span>`), o el gap separa los pedazos.
- Vocabulario: **«Precio desactualizado · N días»** (>15 días) en todas las pantallas (reemplaza «Precio vencido»).
- **Montos: todo es bruto (IVA incluido)** — ofertas, OC, facturas y pagos (Boris, 6-nov). Rotular «bruto» donde se ingresa o copia un monto. Pendiente verificar en el código que costos de proveedores y precio sugerido usen la misma base al calcular margen.
- Patrón repetido **lista + panel lateral** (Mesa, Ventas): clic en la fila, ↑/↓, Esc, anterior/siguiente; con el panel abierto se ocultan columnas secundarias.

## Análisis de flujos (para las maquetas)
Flujo: Mercado → Mesa → Lote → Ofertas → Portal → Venta (`docs/18-revision-cbs-y-flujos-operativos.md` §1).
Pantallas:
1. **Hoy** = bandeja: cifras del día, «Te necesita» por urgencia (incluye avisos de Ventas), embudo completo (antes y después de enviar), actividad.
2. **Mesa** = lista densa + segmentado de cierre + filtros + selección múltiple («N CA · Cotizar en lote») + panel lateral con el detalle de la CA y la conversación del equipo.
3. **En trabajo** = lotes y ofertas; paso 3 «Enviar» en su propia vista.
4. **Ventas › Ofertas enviadas** = seguimiento por estado y gestión desde el envío hasta el pago (incluida la planificación de la entrega).
5. **Cotización formal** (PDF carta, a nombre de la empresa).
Hechos después: documentos de la empresa, cola de revisión de líneas (Demanda MP), paleta ⌘K y todas las pantallas de apoyo. Pendiente: revisión general en modo oscuro.

## Maquetas ronda 1 (6-nov) — en el canvas
- Sistema instalado como `project/ds/rastrillo/` (tokens.json + components/bundle.css copiados; `tokens.css` escrito a mano desde tokens.json porque el sistema no lo publica; oscuro con `data-tema="oscuro"`). Esqueleto compartido en `project/maquetas.css`.
- Artboards (Play, enlazados): `Main.dc.html` (Hoy), `Mesa.dc.html`, `Lote.dc.html` (En trabajo · lote y ofertas), `Enviar.dc.html` (En trabajo · enviar ofertas), `Ventas.dc.html` (Ventas · ofertas enviadas) — PAGE 1440, `expand: fill`, interactivos; `Cotizacion.dc.html` (carta 816×1056, fija); `Campana.dc.html` (componente del menú, `dc-import` en todas las pantallas). Tweak `tema`; en Lote, tweak `mostrarModal` (por defecto apagado).
- Menú: «Inicio» → «Hoy»; resto del macro igual. Contadores: Hoy (pendientes), En trabajo (CA recibidas sin lote), Ventas (con acción pendiente). Campana junto a «Rastrillo».
- Datos de ejemplo inventados (compradores reales de Ñuble; montos, productos, inventario, proveedores, transportistas, competidores y comentarios ficticios).

### Decisiones de Boris al iterar la Mesa (6-nov)
- **Panel lateral:** clic en cualquier parte de la fila; `↑`/`↓` (J/K) recorren; `Esc` cierra; anterior/siguiente y «n de N». Sin vista previa al pasar el mouse.
- **Se recuerda la última CA abierta** al volver (en la app: por usuario en servidor).
- **Cierre es la primera columna**; si cierra hoy, la celda es señal urgente.
- **Columnas ordenables** (Cierre, Monto, Margen, P(ganar)); por defecto cierre ascendente.
- **Explicativo al pasar el mouse sobre cada señal: informativo, sin acciones** (qué falta + dónde se resuelve). La acción se toma en En trabajo.

### Decisiones de Boris al iterar En trabajo / Lote (6-nov, noche)
- **«Pedir precios» es parte de «Armar ofertas».** Pasos: Agregar CA → Armar ofertas → Enviar (portal).
- **Línea de la CA = necesidad, no producto** («galletas» → varios productos). En cada línea se elige qué se oferta.
- **Fila compacta + modal para configurar la línea** (Esc / Listo; anterior/siguiente línea).
- **Modal en dos pisos:** (1) **Nuestro inventario** (se revisa primero); (2) **Proveedores**; más **«Sin producto actual · buscar proveedores»** (cotización por la línea a proveedores del rubro).
- **Alertas de precio desactualizado** en la fila y destacadas en el modal; también «Sin precio» y «No alcanza la bodega».
- **Inventario primero:** stock reservado entre todos los lotes abiertos; «Faltan N en bodega»; aviso si hay stock libre y se ofertó a proveedor.
- **Aprendizaje:** «Buscar en el inventario» + «Usar este» + «Recordar para líneas parecidas»; las líneas parecidas se llenan solas («elegido por historial»).
- **Abastecimiento del lote:** 1 · Desde nuestro inventario · 2 · Solicitudes a proveedores **por correo o WhatsApp según el proveedor**.
- **Lotes:** varios abiertos; **con nombre**; «Mover a…» con Deshacer; «Quitar del lote»; «Nuevo lote»; lote vacío.

### Paso 3 · Enviar (6-nov, noche; validado con ajustes)
- Lista de CA con estado (Lista para enviar · Faltan N requisitos · Oferta en armado · Enviada HH:MM) y cierre urgente.
- Detalle: total, % del disponible, margen, P(ganar), plazo de entrega, ruta; **«Lo que vas a escribir en el portal»** (precio unitario bruto) con copiar por precio y «Copiar todos».
- **Requisitos por CA, variables, leídos del anexo:** cotización formal, declaración jurada simple, ficha técnica, **resolución sanitaria**; visibles aunque la oferta esté en armado.
- **Cotización formal generada por Rastrillo a nombre de la empresa** («Generar PDF» / «Ver PDF» → `Cotizacion.dc.html`). **Sin firma** (Boris). Logo de la empresa desde Administración › Empresas; sin logo, el recuadro desaparece. Precios con IVA incluido y desglose neto/IVA referencial. Condiciones por defecto a confirmar (despacho incluido, validez 30 días, pago 30 días).
- Documentos de la empresa reutilizables («Usar la guardada», con vencimiento).
- Ingreso **a mano en Mercado Público** (regla 9): «Abrir CA» + **«Marcar enviada»** (bloqueado hasta cumplir requisitos) con Deshacer. **Sin comprobante**: queda en Ventas › Ofertas enviadas.

### Ventas › Ofertas enviadas (6-nov, noche)
- **Estados de seguimiento (decisión de Boris): cada oferta enviada tiene siempre un estado**, y se hace seguimiento filtrando por ellos. Estados y grupos:
  - **Por resolver:** Enviada (CA abierta) · En evaluación (cerró, falta adjudicación).
  - **Adjudicadas:** Pendiente de OC (la OC no llega o falta aceptarla) · Pendiente de despacho · Pendiente de factura · Pendiente de pago.
  - **Cerradas:** Completa (pagada) · No ganada.
- **Barra de estados** sobre la lista: cada estado es un filtro con conteo y monto; el nombre del grupo filtra el grupo; «Mostrando: X · Ver todas». Más el filtro «Con algo pendiente».
- El estado avanza solo con las acciones del panel; además **se puede cambiar a mano** (selector «Cambiar a mano» en el panel) para corregir o marcar «No ganada».
- **La OC puede no llegar y aun así hay que despachar (decisión de Boris):** desde «Pendiente de OC» → «Despachar sin OC»; el despacho sigue y la OC queda pendiente aparte (señal «Sin OC · ya despachada», paso de OC en naranjo, la tarjeta «Pendiente de OC» suma «+N ya despachadas sin OC» y el filtro las incluye). «Registrar OC» (con N.º) cuando llegue. **La factura referencia la OC: «Registrar factura» queda bloqueado mientras falte la OC** («Registra la OC primero»); el formulario muestra «Referencia: OC N.º».
- Cifras: ganado 30 días, esperando resultado, tasa de adjudicación, por cobrar.
- Fila: enviada (fecha, quién), CA, ofertado, margen, estado + barra de 7 pasos; responsable e indicador de comentarios. Señales con explicativo y notas de estado.
- Panel lateral: estado, ofertado, margen, P(ganar) al enviar; resultado si se perdió; **línea de tiempo**; **conversación del equipo**; documentos enviados; **un primario según el estado** + enlace al portal.
- **Entrega se planifica AQUÍ (decisión de Boris):** Logística solo guarda costos y proveedores logísticos. «Planificar entrega»: plazo de la OC, modo (ruta propia / transportista / retira el comprador, con costos de Logística), fecha (aviso si queda fuera de plazo), quién recibe, **margen tras el despacho**; «Programar entrega» → «Programada · fecha · modo» (Cambiar) → N.º de guía opcional → «Marcar entregada».
- **Factura a mano por ahora (decisión de Boris), integración SII después:** N.º, fecha de emisión, **monto bruto (IVA incluido)**, plazo de pago (30/45/60) → vencimiento; «Registrar factura» exige el N.º y la OC registrada. Luego «Registrar pago» (fecha, monto recibido bruto).

### Equipo: responsable, conversación y campana (6-nov, noche) — sobre lo que ya existe (F-equipo)
- Base en el código: `app/mercado/equipo.py` (estado compartido por CA con versión, `ca_comentarios` con `@nombre` → evento `mencion`, no leídos sin fan-out, eventos de sistema), `_equipo_hilo.html` (Enter envía, Shift+Enter salto, @ menciona), `_campana_lista.html`.
- **Conversación por CA, la misma en Mesa (panel), Lote (encabezado de la oferta, «N comentarios» / «Ocultar conversación», «@» si te mencionaron) y Ventas (panel).** Menciones como pastilla (`accion-suave`), responsable con selector, sugerencias al escribir `@`.
- **Conversación del lote completo (decisión de Boris):** además de la de cada CA. Botón «Conversación del lote (N)» junto al nombre del lote (con «@» si te mencionaron); cerrada muestra el último comentario en una línea; abierta, tarjeta «Conversación de Lote N» («para todo el lote; lo de cada CA va en su oferta»). **Requiere en el código un hilo por lote** (hoy `ca_comentarios` es solo por CA) y su evento de mención.
- Fila de Mesa y Ventas: iniciales del responsable + ícono con n.º de comentarios.
- **Campana en el menú de todas las pantallas (componente compartido).** **Decisión de Boris: cada aviso lleva a la acción correcta** — mención en un lote (de la CA o del lote) → al lote; comentario de una venta → Ventas; cierre próximo de oferta en armado → «Terminar oferta» (Lote); oferta lista → Enviar; asignación → Mesa. Cada aviso muestra su destino («Ir al lote →») y se marca leído al abrirlo. En la app, además, abre esa CA o ese lote con su conversación desplegada.

### Documentos y Paleta ⌘K (6-nov, noche)
- **Documentos de la empresa** (`Documentos.dc.html`, en Catálogo): resolución sanitaria, F30, vigencia, registro de proveedores, DJ (plantilla), e-RUT, fichas técnicas. Estados Vigente / Vence en N días (≤30) / Vencido / Sin vencimiento, con explicativo; panel con fecha de vencimiento, dónde se usa, versiones, «Reemplazar archivo». Tarjeta «Datos para la cotización» (se editan en Administración).
- **Paleta ⌘K** (`Paleta.dc.html`, componente bajo «Rastrillo» en todas las pantallas): Ir a · Acciones sugeridas · CA recientes; al escribir busca CA, lotes, productos y el resto de las pantallas. ↑↓ ↵ Esc.

### Pantallas de apoyo — propuesta 1 (6-nov, noche), segunda fila del canvas
Todas con el mismo patrón (cifras arriba → filtros → lista + panel lateral, un primario por zona, señales con explicativo, «Fuente: Dirección ChileCompra» donde hay datos públicos). Basadas en lo que hoy hacen las plantillas.
- **Actividad** (`Actividad.dc.html`): registro por día; filtros tipo (Comentarios, Estados, Precios, Envíos y ventas, Detectado por Rastrillo), persona y texto/código; cada línea lleva a donde se actúa. «Ver todo» de Hoy y de la campana apuntan aquí.
- **Competencia** (`Competencia.dc.html`): «Ranking del territorio» / «Por comprador» + familia + búsqueda RUT/razón social; columnas CA ofertadas, % gana, precio vs mediana, monto ganado, «Contigo» (cara a cara). Perfil en panel: cifras, CA por mes (línea), cara a cara, dónde más oferta, familias.
- **Estrategia** (`Estrategia.dc.html`): 3 oportunidades destacadas arriba; mapa comprador × familia (monto 12 m, CA, oferentes, domina, señales «Poca competencia» / «Quedan desiertas» / «Precio no decide»); orden por las 6 métricas del código; «Solo donde aún no ofertas». Panel: métricas, cuotas (dominante / tú / resto), lectura en lenguaje simple, «Ver N CA abiertas en la Mesa».
- **Abastecimiento** (`Abastecimiento.dc.html`): demanda esperada próximos 3 meses, cobertura de stock, **acierto del pronóstico vs línea base** (explicativo del backtest), picos. Tabla por producto con barras dic–feb, unidades, stock, precio mediano, señal «Pico en marzo» / «Cae en verano». Panel: estacionalidad 12 meses (pronóstico en índigo), frecuencia, rango de precio, quién compra, tus proveedores; primario «Pedir precios por volumen».
- **Demanda MP** (`Demanda.dc.html`): 4 pestañas-tarjeta: **Líneas por revisar** (texto del comprador → producto propuesto con confianza y por qué → unidad base; Confirmar / No es del catálogo / cambiar producto; «Confirmar las de confianza alta»; aviso «Rastrillo aprendió el alias»), **Anexos por leer** (se descargan a mano de la ficha y se suben; regla 9), **Productos** (unidad base, familia, UNSPSC, n.º de alias), **Reglas por familia** (unidad e impuesto; «Por clasificar»).
- **Proveedores** (`Proveedores.dc.html`): lista con canal (correo/WhatsApp), rubros, productos y antigüedad de la lista (>30 días = desactualizada); panel con contacto, plazo de pago, mínimo, despacho, precios **brutos** con variación, archivo de la lista; primario «Pedir lista por correo/WhatsApp» cuando está vieja (muestra el mensaje enviado), «Subir lista nueva».
- **Inventario** (`Inventario.dc.html`): stock, **reservado por ofertas en armado**, disponible, mínimo, costo; «Bajo mínimo» cuenta lo reservado; «Sin movimiento» (>90 días). Panel: reservas por lote (enlazan al Lote), movimientos, «Pedir reposición a <proveedor habitual>», «Ajustar stock». Mismos productos que el inventario del Lote.
- **Logística** (`Logistica.dc.html`): tarifas (transportista, cubre comuna/provincia, fijo, por kg, días); calculadora «¿Cuánto cuesta llevarlo?» (destino + kg → opciones ordenadas, «Más barato», aviso si nadie cubre); despachos de la semana (→ Ventas).
- **Administración** (`Admin.dc.html`, nuevo ítem del menú): Empresa (datos para la cotización, logo, **territorio por comunas de Ñuble**), Usuarios (roles Administrador / Operador / Solo lectura — propuesto), Salud del sistema (API, cuota, lectura de anexos IA, cruce de resultados/OC, envío de solicitudes, respaldo). Las claves nunca se muestran.
- Menú: generado igual en todas las pantallas (script local `gen_nav.py`); contadores nuevos Demanda MP (12) e Inventario (4). Paleta: productos → Inventario.
- Preguntas abiertas de esta ronda: ¿rol «Solo lectura» hace falta? ¿el territorio es por empresa? ¿«Pedir reposición» usa el mismo flujo de solicitud que el Lote? ¿Competencia muestra montos o solo conteos? ¿Estrategia sugiere priorizar automáticamente en la Mesa?

### Ronda de experiencia de uso (6-nov, noche)
- **Modo oscuro neutro (Boris: «muy azul»):** fondo #0B0B0C, superficie #151517, superficie-2 #1E1E21, borde #2A2A2E, tinta #EDEDEF / #A1A1AA (gris zinc); índigo de acción igual. Hecho en el `tokens.css` del canvas; **falta llevarlo a tokens.json del sistema «Rastrillo»**.
- **Vocabulario:** «Peak» en vez de «pico» (Boris) — «Peak en marzo», «Peaks por venir».
- **Proveedores › Por producto (nuevo, pedido de Boris):** matriz producto × proveedor con columna de bodega (stock y costo), precio bruto por celda, más barato en verde, desactualizado (>15 días, igual que el resto de la app) en naranjo, «—» si no lo vende, diferencia % entre el más caro y el más barato. Celda → detalle: lectura («hay $ X más barato en…», precio viejo, stock), historia de 4 meses, «Pedir precio actualizado por correo/WhatsApp», «Ver ficha». Nombres de proveedores alineados con En trabajo y la paleta (Comercial Itata, Distribuidora Ñuble Aseo, Librería Central Chillán, Insumos Clínicos, Aseo Industrial Biobío, Prisa Insumos).
- **Mis preferencias** (primera pestaña de Configuración; el pie del menú con el avatar lleva ahí): tema Claro/Oscuro/Según el sistema, densidad Cómoda/Compacta (tweak `densidad` en todas las pantallas), pantalla de inicio, atajos de teclado, y **qué avisos llegan por correo o WhatsApp** (la campana registra todo), resumen diario 8:00, silencio fuera de horario salvo cierres.
- **Hoy:** franja «Desde tu última visita» con enlaces a lo nuevo (CA nuevas, ganadas, menciones, envíos del equipo, precios actualizados) y «Marcar como visto».
- **Presencia del equipo** en el lote («Gonzalo también está aquí · en …») para no trabajar dos veces lo mismo. En la app: presencia por lote/CA.
- **Deshacer** en confirmaciones de Demanda MP, solicitudes a proveedores y reposición de inventario.
- **Frescura de datos y Exportar planilla** en Competencia, Estrategia, Abastecimiento, Proveedores e Inventario; **estados vacíos con «Limpiar filtros»**; **cada pantalla recuerda sus filtros** entre visitas (en la app: por usuario).
- Equipo de ejemplo unificado: Boris, Gonzalo, Mario.

- Ideas anotadas: sugerir el nombre del lote por rubro; repositorio «Documentos de la empresa» con vencimientos; vista de entregas de la semana agrupadas por ruta.
- Preguntas abiertas: ¿el mensaje al proveedor se ve/edita antes de enviar? ¿cómo entra el precio de respuesta? ¿equivalencias de unidad? ¿partir una línea entre bodega y proveedor? ¿qué es «línea parecida» y quién corrige lo aprendido? ¿hace falta un estado «Desierta/Revocada» aparte de «No ganada»?

## Código en paralelo al diseño (6-nov, noche)
- Plan del pivote completo en `main` (`17cb3c4`: seguimiento, auto-revisión, anexos-botón integrados).
- **`docs/prompt-F-dis-datos.md`** (Opus, sin migración, sin tocar app.css/base/hoy/mesa_dia): `app/mercado/hoy.py`, refactor `ventas.condiciones_seguimiento`, `app/mercado/panel_ca.py` + `GET /mesa/ca/{codigo}/panel`, vista previa admin `/hoy/previa`.
- Definiciones por defecto a validar por Boris en `/hoy/previa`: «en juego» = monto disponible de CA vigentes priorizadas/cotizando/listas; semana lunes–domingo; mes = adjudicado.
- Siguiente tras congelar átomos: F-dis-1 (tokens + macros Jinja por átomo), luego F-dis-hoy y F-dis-mesa.

## Pendientes operativos fuera del diseño (al 6-nov)
- Deploy con migración `d8f1b6c4a2e9` (auto-revisión) + fix `alembic/env.py` (disable_existing_loggers=False) + doc 19 actualizado.
- OpenRouter y `evaluar_extraccion.py` / `calibrar_auto_revision.py`; probar extensión de anexos y piloto del agente Claude.
- Corrida `mercado-indices completo`; carga de costos, tarifas, inventario; ticket a ChileCompra.
