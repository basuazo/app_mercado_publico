# 18 — Revisión CBS (5-nov-2026): brechas de la plataforma y flujos operativos que faltan

*5-nov-2026 · Boris con Claude (Cowork), a partir de las notas y la transcripción de la reunión «Revisión CBS»
(Boris, Mario Espinoza, Gonzalo Márquez). Convención: [V] verificado en el código o en la fuente · [I] inferido.
Nada de esto entra a `decisiones.md` hasta que Boris lo apruebe. Este documento manda sobre `17-plan` §7 donde
choque; el resto de `17-plan` sigue vigente.*

## 0. En una página

La reunión no cambió el propósito de la plataforma (`17-plan` §0: ver oportunidades producto a producto,
centralizar proveedores y costos, decidir con P(ganar), pronosticar demanda). Cambió **el orden y la forma de
operarla**. Lo que el equipo acordó, en una frase cada uno:

1. **Piloto chico y de calidad.** Chillán como foco logístico; una o pocas cotizaciones al día; primero las
   mejores oportunidades, no el volumen.
2. **Dos cuellos de botella mandan.** (a) Leer bien los anexos de las CA; (b) tener precios de proveedores al
   día. Todo lo demás «fluye» cuando eso está resuelto (Gonzalo).
3. **Catálogo en tres capas.** Lo que ofrecemos (con stock), lo que venden nuestros proveedores (con precio) y lo
   que pide el mercado. Hoy la pestaña «Catálogo» es solo la tercera capa y confunde.
4. **Flujo de trabajo por lotes.** Ir a la mesa, elegir 3–5 CA, apretar «Cotizar», que la plataforma agregue
   cantidades por producto, pida precio a los proveedores y devuelva una oferta por CA.
5. **Seguimiento administrativo.** Una CA no se gana solo con el precio: formularios, cotización formal adjunta,
   documentos que descalifican si faltan; y después de adjudicar, OC, entrega, factura y pago (caja).
6. **Modo sombra.** El modelo corre 4 semanas y `/mesa/resultados` mide cuánto acierta. **Decisión de Boris
   (5-nov, noche): los porcentajes se muestran desde el primer día, como ya está;** no se ocultan.
7. **IA barata y sin dependencia de un solo modelo.** OpenRouter con modelos económicos, validados en la
   tarea; análisis de costo antes de «optimizar al extremo».
8. **El rediseño visual va después** de los dos cuellos de botella.

**Qué ya existe [V]** (commits locales del 4–5 nov, sin push): catálogo canónico y líneas 1:1 (F-plat-1),
anexos por subida manual con extracción LLM y revisión (F-plat-2), empresas, proveedores, costos con historial
e importación por planilla o foto (F-plat-3), índice de precios y P(ganar) con juez (F-plat-4), mesa con
precio/margen/P(ganar), oferta por CA, exportación XLSX y modo sombra (F-plat-5), competencia (F-plat-comp),
estado compartido por CA con alarmas y comentarios (F-equipo), demanda y recomendaciones (F-plat-6).

**Qué falta para que la plataforma «cumpla su propósito» según la reunión** (detalle en §2; prompts en §3):

| Brecha | Fase nueva | Modelo |
|---|---|---|
| Catálogo en tres capas (Demanda MP · Catálogo Proveedores · Inventario); la capa Inventario no existe | **F-plat-capas** | Sonnet |
| Cotizar varias CA a la vez, agregar cantidades y pedir precio a proveedores; precios vencidos | **F-plat-lote** | Opus |
| Checklist administrativo, cotización formal, estados post-adjudicación, caja | **F-plat-seguimiento** | Sonnet |
| Anexos: carga masiva por código, prioridad anexo > ficha > API, cola por vigentes, modelo de respaldo | **F-plat-anexos-2** | Opus |

El flujo operativo completo que resulta (de la mesa a la caja) está en §1. La explicación técnica del problema de
los adjuntos para Gonzalo y Conejo está en `19-adjuntos-ca-explicacion-tecnica.md`.

## 1. El flujo operativo objetivo (de la mesa a la caja)

Este es «el flujo» que Boris se comprometió a mandar hoy. Cada paso dice qué hace la plataforma, qué hace una
persona y qué fase lo cubre.

```
 ┌─────────────┐   ┌──────────────┐   ┌───────────────┐   ┌──────────────┐   ┌──────────────┐   ┌────────────┐
 │ 1. Mercado  │ → │ 2. Mesa      │ → │ 3. Lote       │ → │ 4. Ofertas   │ → │ 5. Portal    │ → │ 6. Venta   │
 │ (ingesta,   │   │ (elegir 3–5  │   │ (agregar,     │   │ (precio por  │   │ (subir a MP, │   │ (OC,       │
 │ líneas,     │   │ CA del día)  │   │ pedir precio) │   │ CA, margen,  │   │ formularios, │   │ entrega,   │
 │ anexos)     │   │              │   │               │   │ cotización   │   │ adjuntos)    │   │ factura,   │
 │             │   │              │   │               │   │ formal)      │   │              │   │ pago)      │
 └─────────────┘   └──────────────┘   └───────────────┘   └──────────────┘   └──────────────┘   └────────────┘
   existe + F-plat-   existe              F-plat-lote         existe (1 CA) +     F-plat-seguimiento   F-plat-seguimiento
   anexos-2                                                    F-plat-lote
```

**1. Mercado (cada 15 min, sin personas).** `mercado-ca` trae las CA vigentes de Ñuble con su detalle;
`mercado-catalogo` crea las líneas 1:1; las CA «según anexo» quedan `sin_detalle` y aparecen en «líneas por
revisar». *Persona:* para una CA vigente que interesa, bajar el anexo de la ficha pública y subirlo (hoy) o
soltar la carpeta de anexos del día (F-plat-anexos-2). El LLM extrae; una persona confirma. **Regla acordada:**
si hay anexo, el anexo manda sobre la línea de la API; si no hay anexo, manda la descripción de la ficha; la API
es la última fuente.

**2. Mesa (una vez al día, 10 minutos).** Filtrar por familia (p. ej. «jugo, agua y bebida»), comuna
prioritaria (Chillán), cierre y «con costo». Elegir 3–5 CA de calidad y **priorizarlas**; el equipo recibe la
alarma. La tarjeta muestra precio sugerido, costo, margen y P(ganar) (etiquetada «modo sombra» mientras se valida).

**3. Lote (botón «Cotizar seleccionadas», F-plat-lote).** La plataforma junta las líneas de esas CA, las agrega
por producto canónico × formato (150 bebidas de 350 ml, 40 kg de arroz grado 1…), propone el proveedor con mejor
costo vigente por línea y genera **una solicitud de precio por proveedor** (texto listo para WhatsApp o correo,
o planilla) con las cantidades totales y la fecha límite. *Persona:* la manda (o llama, como hoy). Cuando el
proveedor responde, se tipea o se sube la respuesta (foto/planilla, extractor de F-plat-2) y los costos quedan
registrados con fecha. Un precio con más de **15 días** (decisión de Boris, 5-nov; configurable por empresa) se marca
**vencido** y el lote avisa.

**4. Ofertas (una por CA, existe en F-plat-5).** Con los costos frescos, cada CA del lote tiene su oferta: precio
por línea (sugerido por el índice si hay), costo, logística por provincia, margen; se guarda (`cotizando` →
`lista`). F-plat-lote la pre-llena desde el lote; F-plat-seguimiento genera la **cotización formal** (PDF con
los datos de la empresa, líneas, precios, plazo y validez) que muchas CA exigen adjuntar.

**5. Portal (manual, Mercado Público).** No hay API de oferta [I, pendiente de confirmar en la guía]: la persona
ingresa precio por línea y sube los adjuntos en el portal. La plataforma le da un **checklist por CA**
(F-plat-seguimiento): cotización formal, formularios/anexos administrativos que pide la CA, documentos de la
empresa, y no deja marcar `enviada` si falta algo marcado como obligatorio. Alarmas de cierre 24 h / 4 h ya existen.

**6. Venta (F-plat-seguimiento).** Cuando llega el resultado (lo detecta `mercado-ca` al leer las ofertas): si
ganó, la CA pasa a **adjudicada** y empieza el seguimiento: OC recibida (número y fecha), compra a proveedores,
entrega (dirección, horario, quién recibe, guía), factura emitida, **pago esperado** (fecha = factura + plazo del
comprador, 30 días por defecto [I]) y pago recibido. La vista «Caja» muestra lo comprometido con proveedores y lo
por cobrar del Estado, que es la restricción real que nombró Mario.

**Lo que no entra todavía (fase 2, dicho en la reunión):** compra anticipada y bodegaje, scraping de precios
públicos de retail, automatización de WhatsApp con agentes, envío automático de ofertas, rediseño visual.

## 2. Brechas, una por una (lo acordado vs. lo que hay)

### 2.1 Catálogo en tres capas
- **Acordado:** (a) productos que ofrecemos, con stock; (b) catálogo de proveedores con precios; (c) productos
  que demanda el mercado. Mario: «catálogo» hoy confunde porque muestra la capa (c).
- **Hay [V]:** (b) = `/proveedores` (`proveedor_productos`, `costo_historial`); (c) = `/catalogo`
  (`producto_canonico`, `ca_lineas`, pendientes). (a) no existe: no hay stock, ni plazo de reposición, ni
  «productos que vendemos».
- **Brecha:** capa (a) completa y renombrar la navegación. El producto canónico sigue siendo la llave que une las
  tres capas (la demanda se expresa en canónicos, el proveedor vende canónicos, la empresa ofrece canónicos):
  no hay que crear otro catálogo, hay que crear la **vista por empresa** sobre el mismo catálogo.
- **Nombres (decisión de Boris, 5-nov):** **Demanda MP** (c: lo que el mercado demanda), **Catálogo Proveedores**
  (b: carga de nuestros proveedores para considerar en las ofertas) e **Inventario** (a: nuestras existencias).
  El inventario se gestiona desde el día 1 con una planilla simple (stock por producto × formato, movimientos);
  integraciones con APIs de proveedores quedan para después.

### 2.2 Cuello de botella 1: anexos
- **Acordado:** cuatro casos (detalle en la API sin anexo; detalle solo en la ficha; detalle solo en el anexo,
  a menudo escaneado; en ambos); si hay anexo, el anexo es «la posta». Dos caminos de automatización a
  consultar afuera (contacto de Gonzalo; Conejo); mientras tanto, Mario propone una «base secundaria» de
  anexos por código de cotización que se asocie a la plataforma. 738 CA históricas por revisar son demasiadas:
  revisar ~200 a mano para entrenar.
- **Hay [V]:** subida manual por CA con extracción LLM y tabla de confirmación (F-plat-2); la ficha de la CA y
  `/catalogo/ca/{codigo}` muestran `n_documentos` y el placeholder; descarga por API escrita pero apagada
  (`DOCUMENTOS_API_HABILITADO=false`) porque la sonda `smoke_test.py --documentos` **sigue sin correrse** (R8 en
  `01-analisis` §9). Enlace a la ficha pública: `query.py` devuelve la URL genérica del buscador, no la de la CA.
- **Brechas:** (1) no hay forma de subir muchos anexos de una vez ni de asociarlos por código (es exactamente la
  «base secundaria» de Mario, pero dentro de la plataforma: una carpeta o ZIP con `CODIGO__archivo.pdf`);
  (2) no hay un punto de entrada para una automatización externa (si Conejo o el contacto de Gonzalo arman algo
  que baje anexos, necesitan dónde dejarlos: un `POST` con token por empresa, mismos validadores de F-plat-2);
  (3) la cola «por revisar» mezcla históricas y vigentes: las vigentes van primero y las históricas se acotan a
  una muestra de 200 por monto; (4) la regla «anexo > ficha > API» no está escrita en código: hoy las líneas de
  anexo reemplazan placeholders y no tocan las `ok` de la API, lo que en el caso «en ambos» deja la línea pobre de
  la API como `ok`; (5) enlace directo a la ficha de **esa** CA en la tarjeta y en «líneas por revisar».
- **Regla 9 reescrita (5-nov, noche):** scrapear listados y fichas sigue prohibido; bajar el adjunto público de
  una CA en alcance por su URL directa queda permitido **como última alternativa** (API → subida/carga masiva →
  descarga directa), a ritmo humano, cortando ante 403/429 (`prompt-F-plat-anexos-2.md` §2.7). Base: los términos
  de ChileCompra declaran públicos los documentos de los procesos y bloquean solo a bots que comprometan la
  disponibilidad [V, leídos el 5-nov; `decisiones.md`]. La sonda `--documentos` sigue siendo el paso 1.

### 2.3 Cuello de botella 2: precios de proveedores
- **Acordado:** no hay lista de precios constante; hoy se cotiza uno a uno por llamada; el precio depende del
  volumen; los precios públicos (retail) difieren del precio «oculto» del vendedor. Camino: contactar
  proveedores grandes para envíos semanales de precios + (fase 2) scraping público como contraste + (fase 2)
  estimar «este proveedor vende ~15 % bajo mercado».
- **Hay [V]:** precio por proveedor × producto × formato con `vigente_desde`, historial append-only, variación
  vs. anterior y vs. 90 días, importación por planilla, lista de precios por foto/PDF, `mejor_costo`.
- **Brechas:** (1) nada **pide** precios: no hay solicitud de precio por proveedor con las cantidades que se van
  a cotizar; (2) nada **vence**: un precio de hace 4 meses se usa igual que uno de ayer (`mejor_costo` no mira
  la fecha [V]); (3) la respuesta del proveedor por volumen (precio por tramo) no tiene dónde guardarse;
  (4) no hay «precio de referencia público» como dato de contraste (fase 2: campo manual primero, nunca scraping
  sin decisión).

### 2.4 Flujo por lotes
- **Acordado:** seleccionar varias CA en la mesa → «Cotizar» → la plataforma calcula cuánto de cada cosa, qué
  proveedor lo tiene, y devuelve la cotización; las CA elegidas pasan a «otro espacio de trabajo» con seguimiento.
  Pocas por ciclo (3–5), con una semana entre ciclos.
- **Hay [V]:** oferta por **una** CA (`armar`, `simular`, `guardar`, `xlsx`); estados `priorizada` → `cotizando`
  → `lista` → `enviada` por CA; filtro «Equipo» en la mesa.
- **Brecha:** el **lote** como unidad de trabajo (varias CA, agregación por producto, una solicitud por proveedor,
  stock comprometido) y la pantalla «En trabajo» (lo que está en `priorizada`/`cotizando`/`lista`, agrupado por
  lote) separada de la mesa (lo abierto).

### 2.5 Seguimiento administrativo y caja
- **Acordado:** alertas para completar formularios, adjuntar lo que pide la CA, cotización formal; si se
  adjudica pasa a venta con seguimiento; restricción de capital de trabajo (pagan tarde, hay que comprar antes).
- **Hay [V]:** estados hasta `enviada` y `resultado` (`ganada|perdida|…`) en `ca_trabajo`; alarmas de cierre;
  `ofertas_propias` con `precio_ganador_real` y `margen_realizado`.
- **Brecha:** checklist por CA (qué documentos pide esta CA: hoy `documentos[].nombre` de la API dice cuáles
  anexos administrativos hay, p. ej. «Formulario de oferta», y se puede usar como semilla del checklist);
  cotización formal generada; estados post-adjudicación; caja (por pagar a proveedores, por cobrar al Estado).

### 2.6 Modo sombra
- **Acordado en la reunión:** 4 semanas sin mostrar porcentajes.
- **Decisión posterior de Boris (5-nov, noche): se deja como está.** La mesa muestra P(ganar) con la etiqueta
  «Con predicción (modo sombra)» [V] y `/mesa/resultados` mide el acierto; cuando haya porcentaje, se muestra.
  No hay fase para ocultarlo (el prompt F-plat-5c quedó en `archivo/` descartado).

### 2.7 Costos de IA y dependencia de un modelo
- **Acordado:** modelos baratos vía OpenRouter validados en la tarea; consultar a Conejo si vale la pena
  optimizar; riesgo de depender de un solo proveedor (caso WhatsApp).
- **Hay [V]:** `LLM_MODELO_EXTRACCION` sin default (si falta, el flujo es manual), presupuesto mensual en
  Postgres, caché por texto, `scripts/mercado/evaluar_extraccion.py` con 30 anexos reales.
- **Aclaración para el equipo:** el LLM **no** corre cada 15 minutos. Corre solo cuando alguien sube un anexo o
  una lista de precios (y, si la sonda da descarga, para las CA vigentes `sin_detalle`, con tope por corrida). La
  ingesta de cada 15 minutos no usa IA. Con ~6 CA de alimentos por día hábil en Ñuble [V, `17-plan` §1] y un
  anexo por CA, el volumen es de **decenas de documentos al mes**, no miles. El costo que importa es el de la
  revisión histórica (200 CA de una vez) y el de las listas de precios.
- **Brecha:** `LLM_MODELO_RESPALDO` (segundo modelo si el primero falla o se encarece: OpenRouter permite
  cambiarlo sin tocar código, pero hoy un fallo deja el anexo en `error`), y el análisis de costo que Boris se
  comprometió a hacer: §4 da el método y deja los números para cuando corra `evaluar_extraccion.py`.

### 2.8 Lo que ya está bien y solo hay que operar
- Piloto Chillán: `empresas.comunas_prioritarias` ordena la mesa [V]; la ingesta sigue siendo Ñuble (regla 25).
- Alarmas de priorización a los tres usuarios: F-equipo [V]. Falta que Mario y Gonzalo tengan nombre visible y
  estén en la misma empresa (pendiente de Boris en `00-estado.md`).
- Logística por provincia desde Chillán: `tarifas_logistica` [V]; «se recibe acá y se despacha» es el modelo de
  costo que ya está. Lo que Mario pidió (hora y lugar de entrega, quién recibe) es dato de la CA
  (`direccion_entrega` [V]) + checklist de F-plat-seguimiento.
- Competencia y estrategia: existen (F-plat-comp). La reunión las vio y no pidió cambios.

## 3. Fases nuevas: orden, dependencias y qué deja cada una

| # | Fase | Prompt | Modelo | Qué deja | Juez / cómo se sabe que sirvió |
|---|---|---|---|---|---|
| 1 | **F-plat-capas** | `prompt-F-plat-capas.md` | Sonnet | Capa «Inventario» (existencias por producto × formato, movimientos, planilla, plazo de reposición, reserva por lote), menú Demanda MP · Catálogo Proveedores · Inventario | Boris carga 20 productos propios en < 15 min; la mesa marca «lo tengo en stock» |
| 2 | **F-plat-anexos-2** | `prompt-F-plat-anexos-2.md` | Opus | Carga masiva por código (ZIP/varios archivos), `POST` con token para automatización externa, regla anexo > ficha > API, cola vigentes-primero y muestra de 200, enlace a la ficha, modelo de respaldo | 10 anexos subidos de una vez quedan asociados y extraídos; una CA «en ambos» termina con las líneas del anexo |
| 3 | **F-plat-lote** | `prompt-F-plat-lote.md` | **Opus** | `lotes`, `solicitudes_precio` y respuestas por tramo; «Cotizar seleccionadas» en la mesa; agregación; precios vencidos; pantalla «En trabajo»; pre-llenado de las ofertas | Un lote de 3 CA de bebidas genera 1–2 solicitudes con cantidades correctas (test con fixtures); el tiempo de «mesa → ofertas listas» baja de horas a < 30 min de trabajo humano |
| 4 | **F-plat-seguimiento** | `prompt-F-plat-seguimiento.md` | Sonnet | Checklist por CA, cotización formal (PDF), estados post-adjudicación, caja | Una CA ganada se sigue hasta «pagada» sin planilla aparte; el primer informe mensual (F-plat-7) lee margen realizado real |
| 5 | F-plat-7 | ya escrito | Sonnet | Salud, informe mensual, retención (sumar lotes y seguimiento a la retención) | — |

**Dependencias.** 1 no depende de nada nuevo (sobre F-plat-1 y F-plat-3). 2 depende de F-plat-2. 3 depende de
1 (stock y reserva) y de F-plat-5 (oferta). 4 depende de F-equipo y de 3 (el lote es lo que se sigue).
**Paralelismo** (reglas de `17-plan` §4-bis): ventana D = F-plat-capas ∥ F-plat-anexos-2 (tablas distintas; ninguna
usa ticket); luego F-plat-lote; luego F-plat-seguimiento ∥ F-plat-7.

**Lo que Boris dijo en la reunión y manda sobre el orden:** «de aquí a tres semanas sí o sí esto va a estar
funcionando». Con la ventana D y F-plat-lote el flujo de §1 queda operable de punta a punta en 2 semanas de
implementación [I]; F-plat-seguimiento puede entrar cuando haya la primera CA ganada.

## 4. Costo de IA: método y números pendientes

Para cerrar el pendiente «Analizar costos IA» sin inventar precios (cambian; se verifican en
openrouter.ai/models el día que se decida [I]):

1. **Volumen real por mes** (medir en `/salud` → LLM tras 2 semanas): anexos subidos, páginas renderizadas como
   imagen (16 de los 32 anexos de la muestra van como imagen [V, F-plat-2]), listas de precios, % desde caché.
2. **Tokens por documento**: `llm_uso` ya guarda `tokens_in`/`tokens_out` por llamada [V]; promedio por tipo
   (PDF con texto, PDF escaneado, foto de lista).
3. **Costo por modelo** = tokens × precio del día, para 3–4 candidatos, con `evaluar_extraccion.py` dando la
   exactitud de cada uno sobre los 30 anexos.
4. **Decisión**: el más barato con ≥ 95 % de líneas correctas (regla de `17-plan` C3). Si la diferencia entre el
   más barato que pasa y el mejor es menor que ~US$5/mes al volumen de (1), no vale la pena optimizar más: ese es
   el criterio que pidió Mario para la consulta a Conejo.
5. **Riesgo de dependencia**: `LLM_MODELO_RESPALDO` (F-plat-anexos-2) + presupuesto duro en Postgres (ya existe)
   + el flujo manual que nunca depende del modelo (ya existe). Si un proveedor sube precios, se cambia una
   variable de entorno.

## 5. Decisiones tomadas por Boris (5-nov, noche)

1. Nombres de las tres capas: **Demanda MP**, **Catálogo Proveedores**, **Inventario** (planilla simple ahora;
   integraciones con APIs de proveedores después).
2. Vigencia de un precio de proveedor: **15 días**.
3. Plazo de pago del comprador para la caja: 30 días por defecto, ajustable.
4. Anexos: sonda `--documentos` primero; regla 9 vigente.
5. Modo sombra: **los porcentajes se muestran** (F-plat-5c descartada).
6. Este documento y los prompts de §3 quedan aprobados para `decisiones.md` y `00-estado.md`.

## 6. Preguntas técnicas para Gonzalo y Conejo
Están al final de `19-adjuntos-ca-explicacion-tecnica.md` §5, con el contexto que necesitan para responderlas.

*Fuente de los datos de dominio: Dirección ChileCompra.*
