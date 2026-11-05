# 19 — Los anexos de Compra Ágil: explicación técnica del problema (para Gonzalo y Conejo)

*5-nov-2026 · Boris (Rastrillo). Escrito para compartir fuera del equipo de desarrollo. [V] = verificado en la
fuente o en el código; [I] = inferido. Compromiso de la reunión «Revisión CBS» del 5-nov.*

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

## 2. Qué entrega exactamente la API sobre los adjuntos [V]

El detalle de una CA (`GET /v2/compra-agil/{codigo}`) trae un campo `documentos` que es una lista de objetos
`{id, nombre}`. Ejemplo real de forma (datos ilustrativos):

```json
"documentos": [
  {"id": 123456, "nombre": "Anexo 1 Listado de productos.pdf"},
  {"id": 123457, "nombre": "Formulario de oferta.docx"}
]
```

- Sirve para saber **cuántos** adjuntos tiene la CA y **cómo se llaman**.
- **No trae el archivo ni una URL de descarga.** La guía oficial de la API (v3.0) documenta ese campo y
  **no documenta** ningún endpoint para bajar el documento [V en la guía]. Que el servidor no tenga uno es
  una inferencia [I]: probamos tres rutas plausibles en código (`/v2/compra-agil/{codigo}/documentos/{id}`,
  `/v2/compra-agil/{codigo}/documento/{id}`, `/v2/documentos/{id}`) y queda pendiente ejecutar la sonda con
  el ticket para anotar qué responde cada una (`scripts/smoke_test.py --documentos`).
- La ficha pública de cada CA (`buscador.mercadopublico.cl/compra-agil`, sin login) sí muestra los adjuntos
  con enlace de descarga. Esa ficha es una aplicación JavaScript servida por CloudFront que **responde 403 a
  cualquier cliente que no sea un navegador** [V, 4-nov]. Desde un navegador real (DevTools → Network al bajar
  un adjunto) se puede ver la URL verdadera del archivo; no la hemos capturado todavía.
- Términos de ChileCompra [V, leídos el 5-nov]: los documentos de los procesos «constituyen información pública»
  y su «acceso y uso por parte del público en general, no se encuentra restringido»; el bloqueo se reserva a
  usuarios «utilizados por BOT y que comprometan la disponibilidad de la plataforma». Nuestra regla interna:
  nada de scrapear listados ni fichas (eso viene de la API); bajar el adjunto público de una CA concreta por su
  URL directa es la **última alternativa**, uno a uno y a ritmo humano, si la API no lo entrega.

## 3. Los cuatro casos que la plataforma tiene que manejar (acordado el 5-nov)

| Caso | Dónde está el detalle | Frecuencia [I] | Qué hace la plataforma hoy |
|---|---|---|---|
| A | En la API, línea a línea, sin anexo | minoría en alimentos; común en otros rubros | Línea `api|ok` automática |
| B | Solo en la descripción de la ficha pública (la API trae un texto más pobre) | ocasional | Hoy no se distingue de A: queda la línea pobre |
| C | Solo en el anexo (a menudo **escaneado**, sin capa de texto: 15 de 32 en nuestra muestra [V]) | la norma en alimentos | Placeholder «sin detalle»; una persona sube el anexo; el LLM extrae; una persona confirma |
| D | En el anexo **y** en la ficha | ocasional | Igual que C |

**Regla acordada:** si hay anexo, manda el anexo; si no, la ficha; la API es la última fuente. (Está escrita
como fase `F-plat-anexos-2`.)

## 4. Lo que ya funciona (para no automatizar de más)

Una vez que el archivo **está** en la plataforma, el resto es automático y está probado:

1. Se valida por firma y tamaño (PDF, XLSX, DOCX, PNG, JPG; ≤ 10 MB) y nunca se ejecuta.
2. Se convierte a texto (pdfplumber / openpyxl / python-docx). Un PDF escaneado se renderiza a imagen (las
   primeras 5 páginas) y va a un modelo con visión.
3. Un modelo de lenguaje vía **OpenRouter** (elegimos el modelo por variable de entorno; estamos evaluando los
   más baratos que pasen ≥ 95 % de líneas correctas sobre 30 anexos reales) devuelve las líneas
   `{texto, cantidad, unidad, formato}`.
4. Cada línea se cruza con el catálogo canónico y queda «dudosa» hasta que una persona la confirma en una
   tabla editable. Cada confirmación enseña un alias: la próxima vez esa línea sale sola.
5. Presupuesto mensual duro en la base de datos, caché por documento (el mismo anexo nunca se paga dos veces),
   y si no hay modelo o presupuesto, el flujo manual sigue funcionando.

El cuello de botella es **solo el paso 0: conseguir el archivo**. Hoy es una persona que abre la ficha pública,
baja el adjunto y lo sube en la ficha de la CA dentro de Rastrillo (~2–5 minutos por CA).

## 5. Qué le pediríamos a una automatización externa (y preguntas para Conejo)

Si alguien arma algo que obtenga los adjuntos, Rastrillo necesita **recibirlos**, no que los procese. La
fase `F-plat-anexos-2` deja dos entradas:

- **Carga masiva**: varios archivos o un ZIP cuyos nombres empiecen con el código de la CA
  (`1234-56-COT26__Anexo 1.pdf`); la plataforma los asocia por código y los procesa como en §4.
- **Endpoint**: `POST /api/anexos/{codigo_ca}` con un token por empresa (cabecera), mismos validadores y
  topes. Idempotente: el mismo archivo (sha256) dos veces no se procesa dos veces.

Con eso, una automatización externa solo tiene que: (1) saber qué CA nos interesan (podemos exponer la lista
de códigos vigentes «sin detalle» en un `GET` con el mismo token), (2) obtener los archivos, (3) hacer el POST.

**Preguntas técnicas (las que salieron el 5-nov):**
1. ¿Existe una ruta de descarga en la API v2 que no esté en la guía? ¿Alguien la ha usado con ticket?
   (Nosotros corremos la sonda esta semana y anotamos el resultado.)
2. ¿Qué URL real baja el adjunto desde la ficha pública (la que se ve en Network al hacer clic)? ¿Está
   firmada o expira? ¿Depende de una sesión?
3. ¿La «API v3» que se licitó traerá los documentos? ¿Plazo? [Boris lo oyó; sin fuente verificada: [I]]
4. Para OCR de PDF escaneados a volumen (los 200 históricos de una vez): ¿un modelo con visión por página, o
   OCR tradicional (Tesseract/servicio) y después un modelo de texto barato? ¿Qué costo por página han visto?
5. Dependencia de un solo proveedor de modelos: ¿qué respaldo recomiendan (segundo modelo en OpenRouter,
   modelo abierto autoalojado) para el volumen de decenas de documentos al mes?
6. Para la actualización de precios de proveedores: el caso de los agentes conectados a WhatsApp que actualizan
   precios con más de 3 meses, ¿cómo resuelve la identidad del producto (el «tornillo autoperforante» exacto)?
   Nosotros resolvemos eso con el catálogo canónico y queremos saber si usan algo parecido.

*Fuente de los datos de dominio: Dirección ChileCompra.*
