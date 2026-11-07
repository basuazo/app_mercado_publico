"""User-visible changelog, versioned with the code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ChangelogEntry:
    fecha: date
    titulo: str
    descripcion: str


CHANGELOG: list[ChangelogEntry] = [
    ChangelogEntry(
        fecha=date(2026, 11, 7),
        titulo="Los adjuntos de las Compras Ágiles se bajan y se leen solos",
        descripcion=(
            "La app baja sola el archivo adjunto de cada Compra Ágil vigente de tu territorio "
            "(cada hora, de 08:50 a 21:50) y lo lee con la lectura automática, igual que si lo "
            "hubieras subido tú: ya no hace falta abrir la ficha ni usar la extensión. Si alguien "
            "del equipo ya subió un anexo que se leyó bien, esa Compra Ágil no se vuelve a bajar. "
            "Si la lectura de un adjunto quedó «Manual» o falló la descarga, en la ficha de líneas "
            "aparece «Volver a bajar y leer»: lo deja en cola y se lee en la próxima corrida, en "
            "el mismo anexo. Para administradores: /salud muestra la última corrida, las descargas "
            "del día contra el tope y cuántos adjuntos faltan. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 7),
        titulo="Nuevo menú y modo oscuro",
        descripcion=(
            "Empieza el rediseño «Rastrillo». El menú lateral es nuevo: «Inicio» ahora se llama "
            "«Hoy» y muestra cuántas CA cierran en menos de 24 horas sin enviar; «En trabajo» "
            "cuenta las CA en curso que todavía no están en un lote; el grupo Catálogo suma "
            "Logística y Documentos, y Configuración lleva a tu cuenta. La campana de actividad "
            "queda junto al nombre de la app. Con ⌘K (Ctrl K en Windows) se abre una paleta para "
            "ir a cualquier pantalla escribiendo su nombre, y con G seguida de H, M, T, V o D "
            "vas directo a Hoy, Mesa, En trabajo, Ventas o Demanda MP. En Cuenta → Apariencia "
            "eliges tema claro, oscuro o según el sistema, densidad cómoda o compacta, y la "
            "pantalla con la que abre la app. Las pantallas se irán rediseñando de a una en las "
            "próximas semanas; mientras tanto conservan su aspecto actual."
                ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 7),
        titulo="Anexos: se pueden volver a leer y los escaneos con texto basura se leen como imagen",
        descripcion=(
            "Si la lectura automática de un anexo quedó «Manual (sin lectura automática)» o con "
            "error, volver a subir el mismo archivo lo lee de nuevo en el mismo anexo, sin "
            "duplicarlo (la ficha de la Compra Ágil lo recuerda junto al anexo). Los PDF escaneados "
            "que traen una capa de texto ilegible (letras sueltas, símbolos) ahora se detectan y "
            "se le entregan al modelo como imagen, en vez de leer esa basura. Para administradores: "
            "la evaluación de modelos usa un juez corregido que no castiga diferencias de formato "
            "(«1kg» vs «1 kg»), de envase (botella vs unidad) ni de conversión (750 g vs 0,75 kg). "
            "Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 7),
        titulo="Licitaciones archivadas y CA de otras regiones fuera de la base",
        descripcion=(
            "Para administradores. Para liberar espacio en la base (estaba al 75 % de su límite), "
            "las licitaciones de la app anterior se respaldaron en un archivo local y se borraron "
            "de la base, junto con sus matches, seguimientos y opiniones. También se borraron las "
            "Compras Ágiles de otras regiones que ya cerraron y que nadie trabajó, cotizó ni "
            "siguió. Ñuble y las regiones de las empresas activas no se tocan. Desde ahora la "
            "limpieza diaria borra sola las Compras Ágiles de fuera del territorio 90 días después "
            "de su cierre. En «Archivo», Oportunidades y Mi registro avisan que las licitaciones "
            "están archivadas. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 6),
        titulo="Impuestos específicos (ILA) en el catálogo: neto sin impuestos, bruto con todos",
        descripcion=(
            "El ILA de las bebidas se maneja como el IVA: va al precio bruto y no al neto. El "
            "margen, el precio sugerido y P(ganar) siguen calculándose en neto. El impuesto se "
            "asigna en el catálogo (Demanda MP → Impuestos, solo administradores): por familia, "
            "con el modo «clasificar» para las bebidas (tasa de 10 % o 18 % según el azúcar), y "
            "por producto, con una lista «Impuesto por clasificar» para asignarlo en bloque. La "
            "planilla de precios acepta la columna precio_bruto (precio final con IVA e impuestos) "
            "y la columna impuesto: el neto se calcula solo y la vista previa avisa si el neto y el "
            "bruto no cuadran. Catálogo Proveedores muestra el neto y el bruto, y tiene un filtro "
            "«Impuesto por clasificar». El mensaje y la planilla para pedir precios a un proveedor "
            "piden ahora el precio con impuestos, y al anotar la respuesta se guarda el neto. La "
            "cotización formal desglosa el neto, el IVA, cada impuesto específico y el total "
            "bruto. La caja muestra el neto y el bruto de cada movimiento, y los totales van en "
            "bruto. Tasas según el DL 825, art. 42 (Ley Chile y SII)."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 6),
        titulo="Adjuntos a Rastrillo con un clic desde la ficha pública",
        descripcion=(
            "Nueva lista «CA que necesitan anexo» (Catálogo): las Compras Ágiles vigentes con "
            "documentos adjuntos cuyo detalle no viene en la API, por cierre, con el botón «Abrir "
            "fichas» que abre las próximas fichas públicas en pestañas. En cada una, la extensión de "
            "Chrome (carpeta extension/) agrega «Enviar adjuntos a Rastrillo»: sube los archivos que tú "
            "abres y las líneas llegan por revisar solas. «Hoy» avisa cuántas esperan anexo. "
            "Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 6),
        titulo="Líneas de anexo confirmadas solas; el equipo revisa solo las excepciones",
        descripcion=(
            "Cuando se lee un anexo (subida, carga masiva, API externa o descarga automática), cada "
            "línea recibe un puntaje: calce con el catálogo, coherencia con lo que dice la API de la "
            "Compra Ágil, cantidad y unidad plausibles, acuerdo entre dos lecturas y tipo de "
            "documento. Las que superan el umbral se confirman solas (marca «auto»); si eso pasa con "
            "todas las líneas de un anexo, la Compra Ágil queda con el anexo resuelto. Lo demás "
            "va a la nueva «Cola de excepciones» (Catálogo → Cola de excepciones), por Compra Ágil y "
            "de la que cierra antes, con «Confirmar todas». Cada semana una muestra de control del "
            "5 % de lo confirmado solo entra a esa cola: lo que corrijas ahí cuenta como error y "
            "/salud muestra la precisión observada; si baja de 95 % dos semanas seguidas, el umbral "
            "sube solo. Mientras no se calibre el umbral, todo sigue como antes (revisión humana)."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 6),
        titulo="Checklist por Compra Ágil, cotización formal, seguimiento de ventas y caja",
        descripcion=(
            "Cada Compra Ágil que priorizas trae un checklist de requisitos administrativos (la "
            "plantilla de tu empresa, editable en «Requisitos», más los formularios y declaraciones "
            "que la propia CA lista): no se puede marcar «Enviada» con un requisito obligatorio "
            "sin cumplir, y un administrador puede forzarlo dejando un motivo. En la ficha, «Cotización "
            "formal (DOCX)» genera el documento con membrete de tu empresa, número correlativo, "
            "validez, líneas, neto, IVA y condiciones; adjuntarlo en el portal y marcar el requisito "
            "lo haces tú. Al ganar una CA nace su venta (o la registras a mano): orden de compra, "
            "compras a proveedores, entrega (descuenta el stock y libera la reserva del lote), factura "
            "y pago, con el margen realizado calculado con el costo real. Nueva sección «Ventas» y "
            "«Caja»: por cobrar al Estado, comprometido con proveedores, adjudicado sin facturar y el "
            "neto a 30 y 60 días. La campana avisa requisitos pendientes, OC sin registrar, entregas "
            "por vencer, facturas pendientes y pagos atrasados. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 6),
        titulo=(
            "Cotizar varias Compras Ágiles a la vez: lote, solicitud de precios a proveedores y "
            "precios vencidos"
        ),
        descripcion=(
            "En la mesa, marca varias Compras Ágiles y pulsa «Cotizar»: se arma un lote que suma lo "
            "que piden todas en la misma unidad (latas, botellas y packs se juntan), lo redondea al "
            "formato en que compras, descuenta lo que tienes en Inventario y sugiere el proveedor con "
            "mejor precio vigente, incluido el descuento por volumen si lo tiene. Para cada proveedor "
            "se arma un mensaje listo para copiar en WhatsApp o correo y una planilla para que la "
            "devuelva llena; el mensaje no dice qué Compra Ágil ni qué comprador estás cotizando. La "
            "plataforma no envía nada: tú lo mandas y después anotas la respuesta (a mano o subiendo "
            "la planilla) y el precio queda guardado con su historial. «Armar ofertas» pre-llena la "
            "oferta de cada Compra Ágil con esos costos; al marcar el lote como ofertado se reserva el "
            "stock que usa y al cerrarlo se libera. Un precio de proveedor con más de 15 días (se "
            "cambia en la ficha de la empresa) ahora es «vencido»: no gana el mejor costo si hay "
            "otro vigente, se ve como «precio de hace n días» en la mesa, la ficha y el lote, "
            "«Catálogo Proveedores» tiene el filtro «Vencidos» y «Hoy» cuenta los precios por "
            "actualizar. Nueva sección «En trabajo»: los lotes abiertos, lo que está en curso sin "
            "lote y lo que cierra en 24 horas. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 6),
        titulo="Anexos: carga masiva por código, el anexo manda sobre la API, enlace a la ficha",
        descripcion=(
            "Nueva «Carga masiva de anexos» (en Demanda MP): sube varios archivos o un ZIP de una "
            "vez; cada archivo se asocia a su Compra Ágil por el nombre (el código al inicio, por "
            "ejemplo «1234-56-COT26__Anexo 1.pdf»). Antes de procesar ves qué archivo va a qué "
            "Compra Ágil y cuáles ya estaban cargados; después, cada uno se lee de a uno y sus filas "
            "esperan tu revisión. El mismo archivo nunca se lee dos veces. Cuando confirmas una fila "
            "del anexo (o marcas «Anexo resuelto»), las líneas que traía la API para esa Compra Ágil "
            "pasan a «reemplazadas»: el anexo manda. Si el detalle solo está en la ficha, al "
            "agregar líneas a mano puedes marcar «la API no describe bien esta Compra Ágil». La "
            "pestaña Pendientes muestra primero las líneas de Compras Ágiles vigentes (de la que "
            "cierra antes) y aparte una muestra de las cerradas. Las tarjetas y la ficha enlazan a "
            "la Compra Ágil en Mercado Público. El administrador puede generar un token para que "
            "una automatización suba anexos. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 6),
        titulo="Salud e informe mensual del pivote",
        descripcion=(
            "Cada mes recibirás por correo (y verás en la mesa, en «/mesa/informe/AAAA-MM») un "
            "informe de tu empresa: cuántas Compras Ágiles hubo en tu territorio y por cuánto "
            "dinero, cuáles cotizaste, ganaste y perdiste, el margen realizado total y por familia, "
            "qué tan bien acertaron las predicciones (intervalo del 80 % y probabilidad de ganar), "
            "los cinco compradores con más compras, las cinco recomendaciones de abastecimiento "
            "vigentes y el estado del catálogo. En «Resultados» ahora se avisa de las ofertas que "
            "marcaste como enviadas y que cerraron hace más de 14 días sin que la plataforma pueda "
            "saber si ganaste, para que revises el resultado en el portal. Para el administrador, "
            "«Salud» suma la sección «Pivote»: requests por job, territorio, catálogo, gasto de "
            "lenguaje, pronóstico y modo sombra. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 5),
        titulo="Catálogo en tres capas: Demanda MP, Catálogo Proveedores e Inventario",
        descripcion=(
            "El catálogo se ordena en tres capas que comparten la misma llave producto × formato: "
            "«Demanda MP» (lo que piden los compradores; es el catálogo de siempre), «Catálogo "
            "Proveedores» (lo que te venden y a qué precio) e «Inventario» (lo que tú tienes), una "
            "sección nueva con stock, stock mínimo, plazo de reposición, marca y proveedor habitual; "
            "se carga con una planilla Excel o a mano, y cada cambio de stock queda anotado. Los "
            "productos de tus proveedores ahora se vinculan al catálogo (al guardarlos, si el nombre "
            "coincide, o con «Vincular pendientes» para los que ya tenías): sin ese vínculo no entran "
            "al mejor costo ni a «mi oferta». En la mesa, cada tarjeta dice «Tengo: 3 de 5 líneas en "
            "stock» y qué falta, hay un filtro «Con stock», y en «mi oferta» puedes elegir «Stock "
            "propio» como origen del costo. «Hoy» suma una tarjeta con los productos bajo el stock "
            "mínimo. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 5),
        titulo="Abastecimiento: qué van a comprar y cuándo",
        descripcion=(
            "Nueva sección «Abastecimiento» en la barra lateral. A partir de las órdenes de compra "
            "de Compra Ágil de tu territorio (más de dos años de historia), muestra qué compra "
            "cada comprador, cada cuánto, a quién y a qué precio, y qué se espera que compre en "
            "los próximos tres meses. «Recomendaciones» te propone pedir precio firme a tu "
            "proveedor más barato cuando la demanda esperada de un producto supera un umbral "
            "(con su costo, la variación de 90 días, el precio de referencia y el margen), "
            "buscar proveedor para lo que se va a comprar y no tienes, preparar ofertas para los "
            "compradores que suelen comprar el mes siguiente y mirar compradores con Compras "
            "Ágiles desiertas; tus comunas prioritarias van primero y cada una se marca como "
            "hecha o descartada sin que vuelva a aparecer. «Calendario» y «Ítem» muestran la "
            "demanda por mes, la estacionalidad y tus proveedores. Cada número trae su n y lo "
            "que tiene poca historia se marca. La plataforma mide cuánto acierta el pronóstico "
            "contra una regla simple (compra si compró el mismo mes del año anterior o el mes "
            "pasado) y, si no le gana, muestra esa regla y lo dice. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 5),
        titulo="Precio, margen y mi oferta en la mesa",
        descripcion=(
            "Cada tarjeta de la mesa muestra ahora el precio sugerido con el rango en que suele "
            "quedar el ganador, tu costo más la logística, el margen en pesos y en porcentaje, y "
            "la probabilidad de ganar a tres precios (agresivo, sugerido y cómodo). Si falta el "
            "costo de algún producto, la tarjeta lo dice y no inventa margen. Por defecto la mesa "
            "ordena por margen esperado (tus comunas primero) y suma los filtros «Con costo», "
            "«Margen mínimo» y «Con predicción». Al abrir una Compra Ágil, la sección «Precio y mi "
            "oferta» detalla cada línea con su referencia, su costo y el proveedor que elijas; "
            "ajustas los precios y ves el margen y la probabilidad al instante, guardas la oferta "
            "(la CA pasa a «Cotizando»), la exportas a Excel para subirla al portal y la marcas "
            "enviada. Durante cuatro semanas la plataforma mide en silencio cuánto acierta: lo ves "
            "en «Resultados del modo sombra». Mientras no haya un modelo mejor, la probabilidad "
            "usa una línea base y la tarjeta lo dice. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 5),
        titulo="Trabajo en equipo, comentarios y alarmas",
        descripcion=(
            "Cada Compra Ágil de la mesa tiene ahora un estado compartido por todo el equipo: "
            "Nueva, Priorizada, Cotizando, Lista para enviar, Enviada o Descartada (con motivo), "
            "y un responsable. Desde la tarjeta puedes priorizar, tomarla, asignarla a alguien o "
            "avanzarla; si otra persona la cambió justo antes, te avisamos quién y no pisamos su "
            "cambio. Cada CA tiene su conversación: comenta, edita o elimina tus mensajes y menciona "
            "a un compañero con @nombre. La campana de la barra te muestra lo que hizo el equipo, "
            "cuándo te asignan o mencionan, las CA en curso que cierran en 24 h o 4 h y el resultado "
            "de las enviadas (ganada, perdida u oferta no registrada). «Actividad» guarda el registro "
            "completo, «Hoy» suma tus CA y las que cierran sin enviar, y en «Cuenta» eliges qué "
            "alarmas recibir. La mesa tiene un filtro «Equipo» y oculta las descartadas. "
            "Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 5),
        titulo="Competencia y mapa de mercado",
        descripcion=(
            "Dos entradas nuevas en el menú. «Competencia»: busca a un oferente por RUT o razón "
            "social y ve el ranking de tu territorio; cada perfil muestra en cuántas Compras Ágiles "
            "ofertó, su tasa de adjudicación (con su margen de error cuando hay pocos casos), su "
            "precio típico frente a la mediana de cada compra, sus compradores y familias "
            "principales, su tendencia mes a mes y una comparación cara a cara con otro oferente. "
            "«Estrategia»: el mapa de mercado de tus regiones, comprador por familia, con monto, "
            "n.º de compras, oferentes, compras de un solo oferente, desiertas, el competidor "
            "dominante y cuántas veces no ganó el más barato. En la mesa, el historial del "
            "comprador enlaza a sus competidores. Si tu empresa tiene RUT, te marcamos como "
            "«Nosotros». Se actualiza cada mañana. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 5),
        titulo="Mesa: las Compras Ágiles vigentes de tu territorio",
        descripcion=(
            "Nueva entrada «Mesa» en el menú: todas las Compras Ágiles que siguen abiertas en las "
            "regiones de tu empresa, las que cierran antes primero y, si tu empresa tiene comunas "
            "prioritarias, primero las que nombran alguna en su dirección de entrega (es una "
            "inferencia por texto). Filtra por familia, cierre (hoy, mañana, esta semana), detalle "
            "de líneas, anexos, monto mínimo o texto, con el conteo en vivo. Cada tarjeta muestra "
            "comprador, cierre en hora de Chile con cuenta regresiva, monto, líneas, anexos y plazo "
            "de entrega, con enlaces a la ficha (líneas y anexo) y a la ficha pública, y un "
            "desplegable con el historial del comprador de los últimos 12 meses. «Hoy» suma la "
            "tarjeta «CA vigentes». Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 5),
        titulo="Precio esperado y probabilidad de adjudicación por Compra Ágil",
        descripcion=(
            "La plataforma ya calcula, para cada producto del catálogo, el precio al que se gana y "
            "el de las ofertas por comuna, provincia y región, y estima la probabilidad de ganar una "
            "Compra Ágil según el precio que ofrezcas. Con los costos y tarifas de tu empresa calcula "
            "el margen esperado (si falta algún costo, el margen queda sin calcular, nunca en cero). "
            "Cada día se mide contra las adjudicaciones reales del mes anterior y el administrador ve "
            "en Salud si se cumplen las metas. Las pantallas llegan en la próxima versión. "
            "Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 4),
        titulo="Anexos largos: se leen las primeras 40 páginas",
        descripcion=(
            "Al subir un anexo o una lista de precios en PDF con texto, se leen hasta 40 páginas. "
            "Si el archivo trae más, el aviso indica cuántas se leyeron de cuántas, para que agregues "
            "a mano las filas que falten. Así un consolidado de cientos de páginas ya no deja la "
            "plataforma lenta ni la reinicia. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 4),
        titulo="Anexos de Compra Ágil convertidos en líneas, con revisión",
        descripcion=(
            "Cuando una Compra Ágil dice «según anexo», ahora puedes subir ese anexo (PDF, Excel, "
            "Word o foto) en la pestaña «Líneas» de la ficha o en el catálogo. Las filas se leen "
            "solas, incluso de documentos escaneados, y se marcan «por revisar»: tú eliges el producto, "
            "corriges la cantidad y confirmas fila por fila o todas juntas. Si la lectura automática "
            "no está disponible, la misma tabla te deja agregar las filas a mano, y «Anexo resuelto» "
            "cierra el anexo cuando ya están todas. En la ficha de un proveedor también puedes subir "
            "una foto o PDF de su lista de precios y revisarla antes de guardarla. El archivo no se "
            "guarda: se lee y se descarta. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 4),
        titulo="La plataforma ahora se llama Rastrillo",
        descripcion=(
            "«MP Oportunidades» pasa a llamarse Rastrillo: cambia el nombre en el menú, el login, "
            "los títulos de las páginas y los correos. Tus datos y pantallas siguen igual. "
            "Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 4),
        titulo="Nuevo diseño de la plataforma",
        descripcion=(
            "La plataforma estrena un diseño más sobrio: menú lateral con íconos (en el celular se "
            "abre con el botón de arriba), fondo claro, tablas más legibles, un solo color de acento "
            "y tipografía Inter. Cambia solo la presentación: tus datos y pantallas siguen donde "
            "estaban. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 4),
        titulo="La plataforma de Compra Ágil pasa a ser la portada",
        descripcion=(
            "La portada ahora es «Hoy», con las Compras Ágiles abiertas de tus regiones, las líneas "
            "por revisar en el catálogo y tus proveedores. El menú trae Inicio, Catálogo, Proveedores "
            "y Logística. La app anterior (feed, perfiles, registro, explorador y Plan Anual) queda "
            "en «Archivo», solo para el administrador. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 4),
        titulo="Catálogo de productos y líneas por Compra Ágil",
        descripcion=(
            "Cada producto que pide una Compra Ágil de alimentos se asocia ahora a un producto "
            "del catálogo (con su formato: «caja 1 L», «saco 25 kg»), para poder comparar precios "
            "entre compras. El sistema propone el producto por palabras clave y por lo que ya "
            "aprendió; tú confirmas en «Mesa → Catálogo de productos»: cada confirmación enseña "
            "un alias y se aplica sola a las líneas con el mismo texto. Las compras que solo dicen "
            "«según anexo adjunto» se marcan como «sin detalle (anexo)», y puedes agregar sus "
            "líneas a mano; las que solo esperan tu revisión figuran «por revisar». Si renombras o "
            "desactivas un producto, el sistema lo respeta y no lo vuelve a crear. En la ficha de la compra aparece la pestaña «Líneas» con su estado. "
            "Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 4),
        titulo="Empresas, proveedores y costos con historial",
        descripcion=(
            "Ahora hay empresas con su territorio priorizado (las regiones que la plataforma "
            "ingesta) y usuarios asignados a cada una. Dentro de la empresa puedes cargar tus "
            "proveedores con sus productos y precios netos, importarlos desde la planilla "
            "(Excel o CSV, con vista previa), ver cuánto ha variado cada precio y anotar las "
            "tarifas de logística por provincia. Cada cambio de precio queda en el historial. "
            "Es el menú nuevo «Mesa → Proveedores»."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 4),
        titulo="Órdenes de compra de Compra Ágil de Ñuble en la plataforma",
        descripcion=(
            "La plataforma guarda ahora las órdenes de compra de Compra Ágil de Ñuble de los "
            "últimos dos años (líneas con producto, cantidad, precio y proveedor) y un precio de "
            "referencia nacional por producto y mes. Es la base de la mesa del día y de los "
            "precios de referencia que vienen en las próximas fases. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 11, 3),
        titulo="Cotizaciones de Compra Ágil de Ñuble con precio por producto",
        descripcion=(
            "La plataforma empieza a guardar, para cada Compra Ágil de Ñuble, quién cotizó, a "
            "qué precio por producto y quién ganó, en vivo y hacia atrás mes a mes. Es la base "
            "de los precios de referencia y de la probabilidad de ganar que vienen en las "
            "próximas fases. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 10, 9),
        titulo="El match mide qué tan bien calza, no qué tan urgente es",
        descripcion=(
            "El match ahora mide solo qué tan bien calza la oportunidad con tus palabras, "
            "rubros y organismos; lo urgente se ve en el orden y en la fecha. Las palabras "
            "excluidas solo sacan una oportunidad si están en su título, no por un ítem suelto. "
            "Una oportunidad que calza con dos perfiles aparece una sola vez, y el resumen "
            "diario trae una sección 'Cierra en 48 horas'. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 10, 9),
        titulo="Mis perfiles carga más rápido y se lee mejor",
        descripcion=(
            "Mis perfiles carga más rápido y se lee mejor: cada perfil muestra cuántas "
            "oportunidades vigentes trae, sus montos y organismos con nombre, y se puede "
            "pausar sin borrarlo. Si algo falla al guardar, no pierdes lo escrito. Los ajustes "
            "de tu cuenta están ahora en 'Cuenta'. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 10, 8),
        titulo="Las licitaciones muestran su organismo y su región",
        descripcion=(
            "Las licitaciones ahora muestran el nombre del organismo que compra (también en los "
            "correos) y su región; la ficha muestra también la fecha de publicación. "
            "Las que ya estaban abiertas se completan de a poco durante las próximas noches. "
            "Las Compras Ágiles que se declaran desiertas o se cancelan ahora cambian de estado en la "
            "app, y ya no pierden su fecha de publicación o de cierre cuando una actualización "
            "llega sin ellas. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 10, 7),
        titulo="La ficha se abre sobre el feed",
        descripcion=(
            "Al hacer clic en una oportunidad, la ficha se abre en una ventana sobre el feed o "
            "Mi registro, sin perder tus filtros ni tu lugar en la lista. Puedes pasar a la "
            "anterior o a la siguiente sin cerrar, guardar o descartar desde ahí y compartir el "
            "enlace. Al descartar eliges entre 'Solo descartar' y 'Descartar y excluir términos', "
            "que además saca de tus perfiles las palabras que elijas. Las que figuran como publicadas pero ya pasaron su fecha de cierre ahora "
            "dicen 'cierre vencido' en vez de 'Abierta'. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 10, 6),
        titulo="Mi registro: guardadas, cerradas y vencidas recientes",
        descripcion=(
            "Mi registro reúne tus guardadas (vigentes y cerradas), las vencidas recientes que "
            "tuvieron buen match y no guardaste (se ocultan a los 14 días), tus descartadas y "
            "tus archivadas. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 10, 6),
        titulo="Perfiles más precisos y exclusiones más seguras",
        descripcion=(
            "Tus perfiles buscan solo en lo que todavía se puede postular (las Compras Ágiles "
            "sin fecha de cierre cuentan una semana desde que se publican, igual que en Explorar "
            "CA). Ya no se puede excluir una palabra que borraría lo que el mismo perfil busca "
            "(por ejemplo, 'saludable' en un perfil de 'salud'). Deshacer una exclusión ya no "
            "hace esperar. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 10, 2),
        titulo="Guardar: un solo botón para lo que te interesa",
        descripcion=(
            "'Me sirve' y 'Activar alertas' ahora son un solo botón: Guardar. Lo que guardas "
            "queda en 'Mi registro' (antes 'Alertas activas') y te avisamos cuando cambia de "
            "estado o está por cerrar; lo que habías marcado con 'Me sirve' ya está ahí. "
            "También puedes guardar o descartar desde Explorar CA, aunque la Compra Ágil no "
            "calce con tus perfiles. El Texto del explorador ahora busca también en los "
            "productos de cada Compra Ágil. Al descartar puedes excluir la palabra que la "
            "trajo: lo que ya no calza con tu perfil sale del feed, y lo mismo pasa cuando "
            "editas un perfil. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 10, 1),
        titulo="Tus perfiles ahora encuentran bien las palabras con tilde",
        descripcion=(
            "Algunas palabras con tilde de los perfiles, como 'reparación', 'ferretería' o "
            "'evaluación', no encontraban las publicaciones que las usan. Ya está corregido, "
            "tanto en lo que buscas como en lo que excluyes, y también en la búsqueda del "
            "Plan Anual. Puede que veas oportunidades nuevas en el próximo resumen. Cada "
            "palabra busca su propia familia: 'economía' trae 'economía' pero no "
            "'económico'; si quieres las dos, agrega ambas a tu perfil. Fuente: Dirección "
            "ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 10, 1),
        titulo="Explorar Compras Ágiles: rubros más precisos y tus propias palabras",
        descripcion=(
            "Medimos qué tan bien acierta el 'posible' del explorador: de cada 10 Compras "
            "Ágiles que marcaba por parecerse al rubro, 1 o menos era del rubro. Por eso, "
            "ahora al elegir un rubro solo ves las confirmadas (las que traen productos de "
            "ese rubro), y los posibles son opcionales: se activan con 'Incluir posibles'. "
            "Además, el panel te sugiere las palabras típicas de cada rubro, como "
            "'construcción' o 'computador': tócalas para agregarlas, o escribe las tuyas. "
            "Salen también las Compras Ágiles cuyo nombre o descripción tenga alguna de "
            "esas palabras, marcadas como 'Por tus palabras'. Las palabras sugeridas se "
            "actualizan cada lunes. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 30),
        titulo="Explorar todas las Compras Ágiles vigentes, por rubro y más",
        descripcion=(
            "Hasta ahora solo veías las Compras Ágiles que calzaban con tus perfiles. La "
            "pantalla nueva 'Explorar CA' muestra todas las que siguen abiertas y las filtra "
            "por rubro, región, monto, plazo de cierre, texto y organismo. En el rubro, "
            "'Confirmado' quiere decir que la compra trae productos de ese rubro, y 'Posible' "
            "que su nombre se parece a lo que suele comprarse en él: como la mayoría de las "
            "Compras Ágiles aún no tiene detalle, verás algunas que no corresponden. Puedes "
            "marcar rubros favoritos (con la estrella, o en Perfiles) y vienen elegidos al "
            "entrar. Desde acá puedes abrir la ficha de cualquiera, aunque no coincida con tus "
            "perfiles; seguirla o descartarla sigue disponible solo para las que sí coinciden. "
            "Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 27),
        titulo="El tablero solo muestra lo que todavía se puede postular",
        descripcion=(
            "Hasta ahora la lista mezclaba oportunidades abiertas con otras ya cerradas, "
            "adjudicadas o vencidas que se habían quedado con un estado atrasado. Ahora "
            "el tablero muestra solo lo vigente; lo guardado, seguido o vencido se revisa "
            "en otra pantalla más adelante. De paso, el filtro por perfil de búsqueda ahora "
            "permite elegir varios a la vez (antes solo uno), se agregó un filtro nuevo por "
            "palabra clave de tus perfiles, y el filtro por estado del proceso se saca del "
            "panel porque dejó de aportar: todo lo que se ve ya está abierto."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 26),
        titulo="Buscar en el Plan Anual por lo que se compra, no solo por organismo",
        descripcion=(
            "Hasta ahora, para ver qué planeaba comprar un organismo en el año había que "
            "buscarlo uno por uno. Ahora se puede partir al revés: escribir qué se necesita "
            "(por ejemplo 'resmas de papel') y ver en qué organismos aparece, con el monto "
            "estimado y el mes. Al entrar sin escribir nada, se muestran directamente las "
            "coincidencias con las palabras de tus perfiles activos. Fuente: Dirección "
            "ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 25),
        titulo="Las descargas vuelven a correr a su hora",
        descripcion=(
            "El primer día con los horarios nuevos, el programador que usábamos "
            "arrancaba las descargas con dos a cinco horas de atraso y se saltaba "
            "la mayoría de las revisiones de Compra Ágil; el correo de resumen de "
            "la mañana no alcanzó a salir. Ahora las descargas las dispara un "
            "servicio externo, puntual y en hora de Chile, con los mismos horarios "
            "de antes."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 24),
        titulo="Las licitaciones ya cerradas dejan de figurar como abiertas",
        descripcion=(
            "Una licitación que había cerrado hace más de una semana, y que nadie "
            "estaba siguiendo, se quedaba con el último estado que conocíamos: podía "
            "aparecer como abierta aunque hubiera cerrado en julio o ya estuviera "
            "adjudicada. Ahora, cada noche, los estados se ponen al día con los datos "
            "abiertos que publica ChileCompra, y las que coinciden con algún perfil y "
            "no aparecen ahí se consultan una por una. Fuente: Dirección ChileCompra."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 24),
        titulo="Volver desde una ficha ya no borra los filtros",
        descripcion=(
            "Al abrir una oportunidad desde la lista y apretar Volver, o el enlace "
            "Dashboard, se regresaba a la lista sin filtros. Ahora se vuelve a la "
            "misma búsqueda que se tenía puesta."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 24),
        titulo="Las Compras Ágiles se revisan cada hora, también de noche",
        descripcion=(
            "Las descargas de Mercado Público ahora corren solas desde fuera del "
            "servidor web, con horarios propios. Las Compras Ágiles se revisan cada "
            "hora, las 24 horas: en las horas de más movimiento hay tantos cambios "
            "que revisarlas cada dos horas dejaba un atraso creciente, y de noche, "
            "con poco movimiento, se pone al día. La búsqueda de oportunidades y las "
            "alertas corren cada dos horas durante el día, y las licitaciones tres "
            "veces al día. Cuando una descarga encuentra otra en curso, ahora espera "
            "a que termine en vez de saltarse su turno. Por eso el correo de resumen "
            "de la mañana puede llegar entre las 08:30 y las 08:50."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 23),
        titulo="Primer paso para que las descargas no dependan del servidor web",
        descripcion=(
            "Hasta ahora las descargas de Mercado Público corrían dentro del mismo "
            "servidor que muestra la aplicación, y ese servidor se duerme cuando "
            "nadie la está usando: una descarga larga podía quedar cortada a la "
            "mitad. Se preparó una forma nueva de correrlas desde fuera, que no "
            "depende de que el servidor esté despierto. Por ahora solo se activa a "
            "mano, como prueba, para la descarga de Compras Ágiles; si todo anda "
            "bien, en los próximos días pasará a correr sola en los horarios de "
            "siempre. Además, cuando una descarga falle ahora queda marcada como "
            "fallida, en vez de parecer que terminó bien."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 22),
        titulo="Las Compras Ágiles vuelven a llegar",
        descripcion=(
            "Desde el 21 de septiembre la aplicación no estaba trayendo Compras Ágiles "
            "nuevas: cada vez que le pedía a Mercado Público los cambios pendientes, la "
            "consulta tardaba demasiado y se cortaba. Ahora las pide en tramos de dos "
            "horas y en páginas más cortas, que Mercado Público responde a tiempo, y "
            "avanza tramo a tramo: si una descarga se corta, la siguiente sigue desde el "
            "último tramo completo, sin perder ni repetir nada. El atraso acumulado se "
            "pone al día en unas pocas descargas. Además se corrigió cómo se lee la hora "
            "del último cambio de cada Compra Ágil, que se estaba tomando tres horas "
            "antes de lo real."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 22),
        titulo="La descarga de datos ya no se rinde ante un rechazo pasajero",
        descripcion=(
            "Mercado Público a veces rechaza una consulta porque le llegaron varias "
            "casi al mismo tiempo. Hasta ahora eso se trataba como si se hubiera "
            "agotado el cupo del día y la descarga se detenía. Ahora la aplicación "
            "reconoce ese aviso, espera entre medio minuto y dos minutos y vuelve a "
            "intentar hasta tres veces. Si el rechazo sigue, corta y el siguiente "
            "ciclo programado corre con normalidad. Además, cuando Mercado Público "
            "tarda demasiado en responder, la aplicación hace una pausa de un minuto "
            "antes de consultarle cualquier otra cosa, y todas las consultas pasan por "
            "un mismo regulador de ritmo, para no mandarlas de a dos."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 22),
        titulo="Panel de filtros en el tablero",
        descripcion=(
            "Ahora la lista de oportunidades tiene un panel al costado para acotarla "
            "por perfil, fuente, región, monto, fecha de cierre, estado del proceso y "
            "nivel de coincidencia. Cada opción muestra cuántas oportunidades hay "
            "antes de aplicarla, así se sabe de antemano si vale la pena. Los filtros "
            "puestos aparecen como etiquetas que se pueden quitar una por una, y la "
            "dirección de la página guarda la búsqueda completa: se puede compartir "
            "por correo o volver con el botón atrás del navegador. En el celular el "
            "panel se abre con el botón Filtros. La lista pasa a mostrarse sin "
            "agrupar —cada oportunidad una sola vez, con sus motivos de coincidencia "
            "en la propia tarjeta— y el selector de arriba permite volver a agrupar "
            "por motivo, región o fuente cuando convenga."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 22),
        titulo="La ficha y los correos dicen lo mismo que la lista sobre el cierre",
        descripcion=(
            "La ficha de una oportunidad y los correos mostraban una hora de cierre "
            "que Mercado Público nunca entregó: aparecía una medianoche inventada "
            "que además no calzaba con lo que decía la misma oportunidad en la lista. "
            "Ahora las tres pantallas usan el mismo texto, y la hora solo se muestra "
            "cuando la fuente la informa de verdad. Además, al descartar una "
            "oportunidad el teclado queda en la siguiente de la lista en vez de "
            "perderse al principio de la página."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 22),
        titulo="Preparamos los filtros nuevos de la lista de oportunidades",
        descripcion=(
            "Dejamos lista por dentro la maquinaria para acotar la lista por monto, "
            "por fecha de cierre y por la situación del proceso (abierta, en "
            "evaluación, adjudicada y las demás), para ordenarla de mayor a menor "
            "monto y para saber cuántas oportunidades llegaron hoy. Las "
            "oportunidades a las que Mercado Público no le informa el monto o la "
            "fecha de cierre siguen apareciendo: no se esconden por falta de dato. "
            "Los controles para usar todo esto llegan en la próxima actualización "
            "de la pantalla."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 21),
        titulo="Ya no se nos escapan las oportunidades en su último día",
        descripcion=(
            "Guardábamos la fecha de cierre sin la hora, y eso hacía que una "
            "licitación desapareciera de la lista y dejara de avisar durante todo su "
            "último día, justo cuando más importaba. Ahora se guarda la hora real "
            "de cierre que entrega Mercado Público, y cuando la fuente solo da el día "
            "la oportunidad sigue apareciendo hasta que ese día termina."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 21),
        titulo="Nueva vista de oportunidades",
        descripcion=(
            "Rediseñamos la lista de oportunidades: ahora cada una muestra su nivel de "
            "coincidencia en un círculo, el estado del proceso con su color e icono, y "
            "una franja de color a la izquierda que deja ver de un vistazo cuáles "
            "cierran primero. Al descartar una aparece un aviso con la opción de "
            "deshacer, por si fue sin querer."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 21),
        titulo="La búsqueda deja de insistir cuando la fuente la rechaza",
        descripcion=(
            "Cuando Mercado Público nos cerraba la puerta por el día, la app seguía "
            "pidiendo datos igual y gastaba el cupo diario sin traerse nada. Ahora se "
            "detiene apenas aparece el rechazo, guarda lo que alcanzó a bajar y el "
            "contador de consultas del día refleja el uso real."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 21),
        titulo="Mejoras de navegación, formato de montos y accesibilidad",
        descripcion=(
            "Arreglamos la navegación en celulares, los montos ahora se muestran en "
            "formato chileno y mejoramos el uso con lector de pantalla."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 20),
        titulo="La b\u00fasqueda de oportunidades ya no depende de que la app est\u00e9 despierta",
        descripcion=(
            "Hasta ahora, las cargas de datos solo ocurr\u00edan mientras la app segu\u00eda "
            "encendida, y bastaba que se apagara un rato para que dejaran de correr. "
            "Ahora se disparan desde afuera en su horario, funcione o no la app en ese "
            "momento. En la pr\u00e1ctica: menos cortes silenciosos y datos m\u00e1s al d\u00eda."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 20),
        titulo="Avisamos si la carga de datos se detiene",
        descripcion=(
            "Antes, si la b\u00fasqueda de nuevas oportunidades dejaba de funcionar, no se "
            "notaba: la pantalla se ve\u00eda igual, solo con menos resultados. Ahora el "
            "sistema guarda un registro de cada carga de datos y avisa solo cuando una "
            "lleva demasiado tiempo sin completarse, as\u00ed se arregla r\u00e1pido."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 7, 10),
        titulo="Mejor detecci\u00f3n por rubro en licitaciones",
        descripcion=(
            "La carga de \u00edtems UNSPSC de licitaciones ahora revisa el mes vigente y "
            "meses anteriores de datos abiertos. Esto mejora los resultados cuando una "
            "licitaci\u00f3n sigue publicada pero fue creada en un mes anterior."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 7, 7),
        titulo="Menos correos, m\u00e1s \u00fatiles",
        descripcion=(
            "Antes llegaba un correo por cada oportunidad que calzaba con tu perfil y se "
            "llenaba la bandeja. Ahora recib\u00eds un solo correo-resumen cada cierto tiempo "
            "('encontramos X oportunidades para tu perfil') que te invita a entrar y "
            "revisarlas ac\u00e1. Eleg\u00ed cada cu\u00e1ntos d\u00edas recibirlo \u2014o desactivalo\u2014 en "
            "Ajustes de tu cuenta. Y en las oportunidades que te interesan, toc\u00e1 "
            "'Activar alertas' para que te avisemos si cambian de estado (por ejemplo, "
            "al adjudicarse)."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 7, 7),
        titulo="Ahora ves las Compras \u00c1giles",
        descripcion=(
            "Corregimos un problema que imped\u00eda que las Compras \u00c1giles aparecieran en "
            "los resultados. Ahora se muestran junto a las licitaciones. Pod\u00e9s filtrar "
            "por fuente (Licitaciones / Compra \u00c1gil) y ajustar la relevancia del feed "
            "(Alta / Media / Todas)."
        ),
    ),
]


def entradas_changelog() -> list[ChangelogEntry]:
    """Return all entries, newest first."""
    return sorted(CHANGELOG, key=lambda e: e.fecha, reverse=True)


def fecha_ultima_novedad() -> date | None:
    """Return the newest changelog date, or None when there are no entries."""
    entradas = entradas_changelog()
    return entradas[0].fecha if entradas else None
