# 19 — Los anexos de Compra Ágil: explicación técnica del problema (para Gonzalo y Conejo)

*5-nov-2026, actualizado el 6-nov-2026 · Boris (Rastrillo). Escrito para compartir fuera del equipo de desarrollo.
[V] = verificado en la fuente o en el código; [I] = inferido. Compromiso de la reunión «Revisión CBS» del 5-nov.*

> **Resumen del 6-nov.** Ya no hay dudas sobre la API: **no entrega los archivos** [V]. Se eligió que la descarga
> la haga el navegador de una persona (extensión con un botón) o Claude actuando en ese mismo navegador, a ritmo
> humano; el resto (extracción y revisión) es automático. Quedan abiertas 3 preguntas (§6).

## 1. Qué hace la plataforma y dónde se traba

Rastrillo consume la **API oficial de Mercado Público** (api2.mercadopublico.cl, v2, con un ticket que nos dio
ChileCompra) cada 15 minutos para traer las Compras Ágiles (CA) de Ñuble: las nuevas, las que cambian y, cuando
cierran, quién ofertó, cuánto y quién ganó. Con eso entrena modelos (precio esperado, probabilidad de adjudicar,
demanda por comprador) y arma la «mesa» del día.

El modelo necesita saber **qué producto exacto pide cada CA** (arroz grado 1, bebida 350 ml en lata, frasco de
vidrio de 1 L). El problema: en la mayoría de las CA de alimentos ese detalle **no viene en la API**. Viene en
un archivo adjunto («Anexo N°1», «Listado de abarrotes»), y la línea de la API dice literalmente
«ALIMENTOS SEGÚN ANEXO ADJUNTO, cantidad 1» [V: en agosto 2026, 2.577 CA de Ñuble con ofertas; 501 con más
de un producto; en alimentos el texto «según anexo» es la norma].

## 2. Qué entrega la API sobre los adjuntos y qué hace la ficha pública [V]

El detalle de una CA (`GET /v2/compra-agil/{codigo}`) trae un campo `documentos`, una lista de objetos
`{id, nombre}` donde `id` es un **UUID** (texto). Forma (datos ilustrativos):

```json
"documentos": [
  {"id": "3f2a…-…", "nombre": "Anexo 1 Listado de productos.pdf"},
  {"id": "9c1b…-…", "nombre": "Formulario de oferta.docx"}
]
```

- Sirve para saber **cuántos** adjuntos tiene la CA y **cómo se llaman** (Rastrillo los guarda en
  `compras_agiles.documentos`).
- **No trae el archivo ni una URL de descarga.** La Guía de Uso API Compra Ágil v2 (v3.0, mayo 2026) documenta solo
  dos endpoints (`GET /v2/compra-agil` y `GET /v2/compra-agil/{codigo}`) y ninguno de descarga [V, guía]. La sonda
  con ticket (5-nov) probó tres rutas plausibles (`/documentos/{id}`, `/documento/{id}`, `/v2/documentos/{id}`) y
  las tres responden **403** [V].
- La ficha pública (`buscador.mercadopublico.cl/ficha?code={codigo}`, sin login) es una aplicación JavaScript. Al
  abrirla en un navegador real (6-nov) se vio que **no** enlaza el archivo directo: lo pide a un servicio aparte,
  `adjunto.mercadopublico.cl/adjunto-compra-agil/v1/adjuntos-compra-agil/{listar|encrypt|descargar}/…`, con una
  credencial interna de la propia página; sin ella responde **403 «Authentication parameters missing»** [V].
  No es una URL fija que se pueda guardar, y usar esa credencial desde un servidor sería la vía de más riesgo
  (queda en reserva, no se usa).
- Términos de ChileCompra [V, leídos el 5-nov]: los documentos de los procesos «constituyen información pública»
  y su «acceso y uso por parte del público en general, no se encuentra restringido»; el bloqueo se reserva a
  usuarios «utilizados por BOT y que comprometan la disponibilidad de la plataforma».

## 3. Los cuatro casos que la plataforma tiene que manejar (acordado el 5-nov)

| Caso | Dónde está el detalle | Frecuencia [I] | Qué hace la plataforma hoy |
|---|---|---|---|
| A | En la API, línea a línea, sin anexo | minoría en alimentos; común en otros rubros | Línea `api|ok` automática |
| B | En la descripción de la CA (la ficha la muestra) | ocasional | [I] Por verificar si la descripción de la API (`compras_agiles.descripcion`) es la misma que muestra la ficha; si lo es, basta con leer ese campo |
| C | Solo en el anexo (a menudo **escaneado**, sin capa de texto: 15 de 32 en nuestra muestra [V]) | la norma en alimentos | Se consigue el archivo (§4), el modelo extrae, la revisión automática confirma lo seguro y una persona ve solo las excepciones |
| D | En el anexo **y** en la ficha o la API | ocasional | Igual que C; las líneas de la API quedan `reemplazada` |

**Regla acordada:** si hay anexo, manda el anexo; si no, la descripción; la API es la última fuente
(implementada en `F-plat-anexos-2`).

## 4. Cómo se consigue el archivo (decidido el 6-nov)

Rastrillo calcula qué CA **necesitan** anexo: vigentes, de las familias de la empresa, con adjuntos, y cuyas líneas
de la API no bastan (vacías, pobres o «según anexo»). En alimentos son del orden de 6–10 nuevas por día [I]. Para
esas hay dos vías, las dos desde el navegador de una persona y a ritmo humano:

1. **Extensión de Chrome** (`extension/`, F-plat-anexos-boton): en la ficha aparece «Enviar adjuntos a Rastrillo».
   La descarga la hace la propia página, por el clic; la extensión toma el archivo y lo sube a Rastrillo. Desde
   Rastrillo, «Abrir fichas» abre las pendientes en pestañas. Meta: < 15 s por CA [por medir].
2. **Claude como agente en ese mismo navegador** (tarea programada, en piloto): abre las fichas pendientes, baja los
   adjuntos y los sube por la misma entrada. Cero minutos de una persona; más frágil y con costo de uso.

Las dos usan las entradas que ya existen: carga masiva (ZIP o varios archivos con el código de la CA al inicio del
nombre), `POST /api/anexos/{codigo_ca}` con token por empresa (idempotente por sha256) y
`GET /api/anexos/pendientes`. Si ChileCompra habilita una descarga oficial, un job reemplaza ambas.

**Histórico.** Para pronósticos, la fuente principal ya no son los anexos: desde el 6-nov cada CA que terminó en
orden de compra queda cruzada con sus líneas oficiales en datos abiertos (oc-da: producto, cantidad y precio de lo
comprado) [V: 81 % de las CA de alimentos con oferta seleccionada cruzan; las que faltan son de oct–nov y aparecen
cuando oc-da las publica]. Solo si las CA sin OC (desiertas) pesan en los pronósticos se completaría una muestra de
anexos, con el modelo más barato y sin revisión humana.

## 5. Qué pasa cuando el archivo está en la plataforma (automático)

1. Se valida por firma y tamaño (PDF, XLSX, DOCX, PNG, JPG; ≤ 10 MB; ZIP ≤ 50 MB con tope por archivo) y nunca se
   ejecuta.
2. Se convierte a texto (pdfplumber / openpyxl / python-docx). Un PDF escaneado se renderiza a imagen (las primeras
   5 páginas) y va a un modelo con visión.
3. Un modelo vía **OpenRouter** devuelve las líneas `{texto, cantidad, unidad, formato}`; si falla, un modelo de
   respaldo. Los modelos se eligen con 30 anexos reales (602 líneas revisadas a mano) [pendiente de correr].
4. **Revisión automática por excepción** (F-plat-auto-revision): cada línea recibe un puntaje con varias señales
   (calce con el catálogo canónico, coherencia con la API y con cantidades históricas, acuerdo entre dos modelos,
   calidad del documento). Sobre el umbral queda confirmada sola; el resto va a una cola corta. El umbral se
   calibra para ≥ 98 % de precisión y un 5 % semanal de lo automático se revisa como control. Mientras no se fija
   el umbral, todo queda «dudosa» como antes.
5. Presupuesto mensual duro, caché por documento (el mismo anexo nunca se paga dos veces), y si no hay modelo o
   presupuesto, el flujo manual sigue funcionando.

## 6. Preguntas abiertas (para Conejo y para ChileCompra)

Resueltas desde la versión del 5-nov: si existe ruta de descarga en la API (no, §2), qué URL baja el adjunto
(§2: servicio aparte con credencial interna), y qué respaldo usar para el modelo (segundo modelo en OpenRouter).

1. **«API v3» licitada:** ¿traerá los documentos? ¿Plazo? [Boris lo oyó; sin fuente verificada: [I]]. Se preguntará
   a ChileCompra junto con el pedido de un endpoint oficial de descarga de `documentos[].id`.
2. **OCR para una muestra histórica:** ¿modelo con visión por página, u OCR tradicional (Tesseract o un servicio) y
   después un modelo de texto barato? ¿Qué costo por página han visto? (Solo aplica si el histórico necesita anexos,
   §4.)
3. **Identidad del producto en los agentes de WhatsApp** que actualizan precios de proveedores: ¿cómo resuelven el
   producto exacto (el «tornillo autoperforante»)? Nosotros usamos un catálogo canónico con alias y formatos.

*Fuente de los datos de dominio: Dirección ChileCompra.*
