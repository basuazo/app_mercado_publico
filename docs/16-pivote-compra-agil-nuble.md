# 16 — Pivote: mesa de precios de Compra Ágil (alimentos y durables, piloto Ñuble)

*31-oct-2026 · Boris con Claude (Cowork, `/be-ambitious`). Propuesta: nada de esto está en
`decisiones.md` hasta que Boris lo apruebe. Insumos: `informe_1/2.md` y `modelo-de-negocio.md`
(30-oct), `04-datos-abiertos.md`, `05-competencia.md`, `14-auditoria-integral.md` §2.4 y una
medición sobre datos reales hecha hoy (§4). Convención: [V] verificado en la fuente · [I] inferido.*

## 0. En una página

- **Qué cambia.** La app deja de ser un buscador genérico de oportunidades y pasa a ser una
  **mesa de precios de Compra Ágil** (CA) para alimentos no preparados y bienes durables, sin
  medicamentos. El foco es solo CA (no licitaciones ni sus documentos; corrección de Boris del 31-oct).
- **Meta más allá de la meta.** Cada mañana saber qué CA de Ñuble ganar, a qué precio y con qué
  margen después de distribución, y comprobar con las adjudicaciones reales que el sistema acierta.
- **Hallazgo que ordena todo [V].** El histórico completo ya es público y gratis (`oc-da`, desde
  2014, línea por línea con precio neto, UNSPSC, comuna del comprador y del proveedor, flag CA).
  La API con la segunda key sirve **solo para el tiempo real**, y le sobra cuota.
- **El problema difícil no es bajar datos, es saber qué producto es [V].** Con el código UNSPSC
  como "producto", el precio del mes siguiente se predice con **55 % de error mediano** (litros 25 %,
  kg 33 %, "unidad" 61 %; corrida real del 31-oct). Un "pañal" puede ser uno o un paquete de 20. La inversión central es un
  **catálogo de productos canónicos** (producto + formato), no la ingesta.
- **Cómo se sabe si funciona.** Juez externo en cada peldaño: walk-forward contra precios reales
  ya ocurridos, luego modo sombra contra adjudicaciones reales, luego margen realizado.

## 1. Tipos de ambición usados

| Tipo | Por qué | |
|---|---|---|
| Altitud | El objetivo es plata y decisiones (qué cotizar), no un dashboard | Sí |
| Profundidad | El valor está en el precio correcto por producto; ahí falla lo obvio (§4) | Sí |
| Rango | Precios (estadística de índices), logística, mercados (subasta sellada), competencia | Sí, acotado a 4 oficios |

## 2. La escalera (Altitud)

| Peldaño | Qué | Juez externo | Pasa si |
|---|---|---|---|
| **1. Entender** (histórico) | Observatorio de CA de Ñuble: tamaño por categoría y comuna, quién gana, a qué precio, brechas contra el precio nacional | Walk-forward: el precio de referencia de los meses anteriores predice el precio ganador del mes siguiente | Error mediano ≤ 12 % y ≥ 60 % de las líneas dentro de ±20 % en la canasta foco (40–80 productos), con ≥ 70 % del monto de la canasta normalizado |
| **2. Decidir** (tiempo real, modo sombra) | Cada CA nueva de Ñuble en alcance sale con: precio ganador esperado, costo, logística a la comuna, margen y probabilidad de ganar. Se anota la predicción **antes** del cierre | Las adjudicaciones reales (CA v2 cerrada + OC) | En 4 semanas: el precio ganador real cae dentro del intervalo 80 % en ≥ 70 % de los casos, y las CA marcadas "cotizar" ganables con margen ≥ 8 % son ≥ 10 % del total (umbral del informe del 30-oct) |
| **3. Reemplazar el ritual** | Cotizar en serio la canasta foco con la herramienta (≤ 15 min por CA), juntar CA ganadas en compras consolidadas por ruta, y pedir precio firme por canasta a proveedores (cotización sellada) | Margen realizado (costo pagado vs. cotizado) y tasa de adjudicación | ≥ 20 % de adjudicación con margen realizado ≥ 10 %; colchón = p80 del error de costo medido en 20 compras |

Más arriba (abrir la mesa a otras pymes, otras regiones) no tiene juez hasta pasar el 3.

## 3. Alcance (propuesta para decidir)

Por segmento UNSPSC, configurable en un solo lugar (`ALCANCE` en el script):

- **Núcleo:** 50 alimentos y bebidas, sin 5021 (tabaco). "No preparados" se cumple casi solo:
  la comida preparada se compra como servicio (segmento 90) y queda fuera [I: revisar 5019 con los
  200 productos más comprados].
- **Durables:** 56 muebles, 52 electrodomésticos y menaje, 44 oficina, 43 computación, 27
  herramientas, 46 seguridad, 49 deporte, 45 audiovisual, 39 iluminación.
- **Opcionales** (no son durables, pero son la canasta del informe del 30-oct y comparten canal):
  47 aseo, 53 vestuario y cuidado personal, 14 papel. **Recomendación: incluirlos en el piloto.**
- **Fuera:** 51 medicamentos, 42 equipamiento e insumos médicos (regulado; decisión pendiente), y todo
  servicio (70–95).

## 4. Diagnóstico real de Ñuble (corrida del 31-oct) [V]

Fuente: `oc-da` oct-2024 a sep-2026, solo CA en alcance (1,81 millones de líneas a nivel país) y
`COT_` de sep-2026 completo. Reproducir: `python data\mercado_observatorio.py informe`.

**Tamaño (últimos 12 meses, oct-25 a sep-26, CA en alcance, Ñuble):** $6.992 millones netos en
10.815 órdenes de compra, ticket medio **$646 mil** (calza con el supuesto de $500 mil del modelo).
Ñuble pesa 2,5–3,7 % del país según segmento. Alimentos son solo **$682 millones/año (10 %)**;
lo grande es computación, oficina, muebles, papel y electrodomésticos.

**Qué alimentos compra el Estado en Ñuble:** colaciones y eventos. Top 5 por monto: galletas
($70 M), cócteles sin alcohol ($58 M), empanadas ($45 M), pan fresco ($35 M), jugos ($30 M);
siguen bebidas, cecinas, lácteos, agua y café. Ojo: empanadas, sándwiches y cócteles son
**preparados** y quedarían fuera con la definición de §3.

**Comunas:** Chillán concentra el 27 % ($3.771 M en 24 meses; 28 % en los últimos 12), luego San Carlos, Quillón, Bulnes y
El Carmen; las 21 comunas compran. **Proveedor de Ñuble: 31 % del monto** en 24 meses (30 % en los últimos 12); el resto se lo
llevan proveedores de fuera de la región.

**Competencia:** muy atomizada. En las 15 familias más grandes hay 143 a 446 proveedores distintos
en 24 meses, el top 1 tiene 4–18 % y el HHI va de 110 a 549 (sin concentración). No hay un
incumbente que desplazar; se gana precio a precio.

**Lo que dice `COT_` (sep-2026, Ñuble) [V]:**
- 1.345 CA en el mes; **412 con todos sus productos en alcance** (~31 %).
- **64 % pide un solo producto** → el precio unitario de cada oferente es `MontoTotal / Cantidad`
  sin normalizar nada. Es la referencia de precio más limpia que hay.
- Oferentes por CA: 23 % recibe **una sola** oferta; 22 % recibe 8 o más.
- 78 % termina con proveedor seleccionado; **el ganador es el más barato en 78 %** de las que
  tienen ≥ 2 ofertas.
- Distancia entre el ganador y el rival más barato: **mediana 4,8 %** (p25 0 %, p75 21 %).
- Columnas [V]: organismo, unidad, `CodigoCotizacion`, `Region`, `DireccionEntrega`, fechas,
  `PlazoEntrega`, `MontoTotalDisponble`, producto (código UNSPSC, texto, cantidad), proveedor (RUT,
  `Tamano`), `MontoTotal` de la oferta, `ProveedorSeleccionado`, `CodigoOC`, `MotivoCancelacion`.
  **No trae precio por producto en CA de varios productos** (solo total por oferta). Gotcha:
  montos a veces en notación científica (`7e+05`). Si `MontoTotal` incluye IVA: [I].

**Juez walk-forward (sep-2026, con 23 meses de historia):** error mediano **55 %** (L 25 %, kg 33 %,
unidad 61 %); 21 % de las líneas dentro de ±20 %. Mejoró desde 82 % con más datos, pero sigue
lejos de la meta de 12 %. La tabla de "brechas" del informe está dominada por errores de unidad
(un brazalete a $115 vs. $34.800) y **no sirve todavía para decidir**.

**Conclusiones para el plan:**
1. El histórico de líneas de OC sirve para tamaño, comunas y competencia; **no sirve aún para
   precios** sin el catálogo canónico.
2. Para precio, empezar por `COT_` en CA de un solo producto: distribución de ofertas por
   solicitud, quién gana y por cuánto. Eso no necesita normalizar.
3. Ganar por 1–5 % bajo el rival más barato es la jugada típica; el margen de proveedor tiene que
   aguantar eso más la logística.
4. Re-decidir el alcance de alimentos: si "no preparados" saca colaciones preparadas y eventos, el
   núcleo alimentario de Ñuble baja de $682 M/año. Durables y papelería son 9 veces más grandes.

### 4-bis. Alcance ampliado y proveedores locales (corrida v2, 31-oct) [V]

22 grupos (se suman material educativo, ferretería, gasfitería, neumáticos, cocina comercial, bodega,
mascotas y ortopedia estándar; alimentos preparados aparte). Informe completo:
`data/mercado/informe/informe-compra-agil-nuble.pdf` (generado con `scripts/mercado/mercado_informe_pdf.py`).

- Ñuble, 12 meses: **$9.292 M** en 13.458 OC; **proveedores de Ñuble 32 %**, unos **$6.300 M/año** se van a
  otras regiones (sobre todo la RM).
- Lo que más se va: computación $1.187 M, oficina $637 M, muebles $550 M, material educativo $491 M,
  papel $437 M. **Alimentos envasados: los locales ya ganan el 85 %** (solo $79 M van fuera).
- Cotizaciones (7 de 12 meses; mar, abr, jun, jul y ago-2026 dieron 0 filas de Ñuble por región, el script
  ya tiene cruce de respaldo por RUT del comprador): los locales ganan más seguido por oferta en casi todos
  los grupos (material educativo 41 % vs 17 %; oficina 21 % vs 12 %), pero en durables solo el 10–17 % de
  las ofertas son locales.
- Precio: el walk-forward con UNSPSC sigue en 58 % de error.
- ~~Propuesta de canasta foco: oficina y papel, material educativo y muebles~~ **Corregido por Boris (31-oct):
  alimentos sigue siendo el giro principal; el resto son rubros de expansión, no prioritarios.**
- Alimentos en Ñuble (12 meses): $682 M ($541 M envasados + $141 M preparados), 1.516 OC, 86 % local,
  municipalidades 77 %, 105 unidades compradoras (30 compran ≥ 6 meses al año), 200 proveedores (top 5 = 39 %).
  Cotizaciones: 3 ofertas (mediana), 26 % con una sola, gana el más barato 81 %, segundo a 5,5 %, ganador ofrece
  el 92 % del presupuesto; ofertas locales ganan 39 % vs 6 % las de afuera. Walk-forward alimentos: 33 %.
- COT_ de mar, abr, jun, jul y ago-2026: sin filas de Ñuble. **[V] COT_2026-03 trae solo 13 regiones (faltan Ñuble,
  Los Ríos y Arica y Parinacota) y ninguna municipalidad de Ñuble por nombre**: publicación incompleta de ChileCompra.
  Los otros 4 meses muestran el mismo síntoma [I]. El cruce por RUT del comprador no sirve (los RUT de servicios
  nacionales aparecen en todas las regiones). Reconstrucción propuesta: backfill nocturno con la API CA v2 y la
  segunda key (`region=16`, publicadas en esos meses, detalle de cada CA cerrada; ~1.300 CA/mes → ~7.000 requests,
  una noche de cuota).

## 5. Profundidad: cómo lo haría el mejor del oficio

1. **Lo que haría un profesional y no se pidió:** comparar precios **dentro de la misma CA**. Todos
   los que cotizan una solicitud ofertan el mismo producto, cantidad y comuna, así que la
   distribución de cotizaciones de esa solicitud es un precio de mercado sin problema de
   normalización (comparación pareada). La fuente es la API CA v2 (precio unitario por producto
   y proveedor desde "cerrada en segundo llamado") y, para el histórico, `COT_` [V existe; columnas I].
2. **Material más crudo:** líneas de OC (`oc-da`) y cotizaciones (`COT_`), no reportes ni
   promedios publicados. El texto libre de especificación es la materia prima del catálogo.
3. **Métodos e instrumentos:**
   - **Catálogo canónico** (como el INE arma el IPC): producto = categoría + atributos (formato,
     marca/genérico, envase). Extracción con reglas + un modelo de lenguaje en lote sobre textos
     únicos (no por línea), con diccionario curado por humanos para los 200 productos de mayor
     monto. Clave estable `producto_id`.
   - **Precio hedónico**: log(precio por unidad base) ~ producto + log(formato) + comuna + mes +
     cantidad; separa el "premio por formato chico" y el "premio por comuna lejana".
   - **Índice robusto** por producto × {comuna, provincia, región, país} × mes: mediana, p25, p75,
     n; con caída jerárquica cuando n < 5 (comuna → provincia → región → país).
   - **Competencia**: cuota, HHI, tasa de victoria y "distancia al ganador" por proveedor y
     familia; proveedor local vs. foráneo.
   - **Arbitraje con distribución**: margen esperado = precio ganador esperado − costo del
     proveedor − logística(comuna, kilos, consolidación) − costo financiero (30 días). Cotizar solo
     si margen > 2 × logística (regla del modelo de negocio).
   - Motor analítico: **DuckDB + Parquet** en el PC/Actions para el histórico nacional (columnar,
     cabe en disco local); Postgres (Neon) guarda solo agregados y el detalle de Ñuble. Es una
     dependencia nueva: se propone y justifica en la fase (regla del CLAUDE.md).
4. **Juez externo:** los precios reales que todavía no se usaron (walk-forward) y, después, las
   adjudicaciones reales. Nunca el propio modelo.

**Meta dura:** error mediano ≤ 12 % en la canasta foco (hoy 55 % con UNSPSC). Se trata como
alcanzable; si no se llega, se reporta el número real y qué producto falla.

## 6. Rango: el equipo

Verbos del problema: *identificar el mismo producto en textos distintos · estimar un precio que
aún no existe · llevar mercadería a 21 comunas sin perder el margen · ganar subastas selladas sin
caer en la maldición del ganador.*

| Oficio | Qué hace de verdad aquí | Método / herramienta |
|---|---|---|
| Estadístico de precios (INE/IPC) | Catálogo canónico y el índice por comuna-región-país | Clasificación de especificaciones, medianas robustas, regresión hedónica |
| Operador de mercado (*market maker*) | El colchón como *spread*; medir la maldición del ganador | Error de costo p80 sobre compras reales; precio de cotización = cuantil, no promedio |
| Logístico de distribución | Costo por comuna y consolidación de entregas | Tarifa por provincia (Diguillín, Itata, Punilla) desde Chillán, rutas semanales; VRP solo si el volumen lo pide |
| Economista de competencia | Dónde entrar y dónde no | Cuotas, HHI, rotación de ganadores, proveedor local vs. foráneo |

## 7. Fuentes y uso de la segunda key

| Fuente | Aporta | Estado | Cuota |
|---|---|---|---|
| `oc-da/{a}-{m}.zip` | Histórico de líneas de OC de CA con precio, comuna, proveedor | [V] columnas; se republica a diario el mes en curso [V, doc 04 §8] | 0 |
| `trnspchc/COT_{aaaa}-{mm}.zip` | Cotizaciones de CA, incluidas las que pierden: monto total por oferta, proveedor, tamaño, seleccionado | [V] columnas (§4); sin precio por producto en CA multiproducto | 0 |
| API CA v2 (`api2`) con **segunda key** | Tiempo real: listado por región 16, detalle con productos solicitados y, al cierre, precio por producto y proveedor | [V] endpoints; precio por proveedor en respuesta real [I] | ver abajo |
| API OC v1 | Solo para cruzar `id_orden_compra` de una CA adjudicada el mismo día | [V] | mínima |
| Convenio Marco (precios semanales) | Referencia de costo para durables | [V] descarga; columnas [I] | 0 |

Presupuesto diario estimado de la segunda key (Ñuble, en alcance) [I: volumen real lo dirá el peldaño 1]:

| Uso | Requests/día |
|---|---|
| Listado `region=16` cada 15 min, páginas de 50 | ~100–200 |
| Detalle de CA nuevas en alcance | ~50–150 |
| Detalle al cierre (cotizaciones por producto) | ~50–150 |
| Relleno histórico nocturno (22:00–07:00, solo si `COT_` no trae precio por producto) | hasta 6.000 |
| **Total diurno** | **< 500** (5 % de la cuota) |

Reglas para la segunda key: variable `MP_TICKET_MERCADO`, enmascarada en el logger como
`MP_TICKET`; `quota_log` y la pausa de 429 **por ticket** (hoy son por fecha). Riesgo [I]: si el
429 "límite de tasa" es por IP (hipótesis H1 de `15-diagnostico-ca-429.md`), dos keys en los mismos
runners comparten límite; medirlo antes de subir la frecuencia.

## 8. Arquitectura (cambios mínimos sobre lo que ya hay)

- `app/clients/datos_abiertos.py` (ya existe) suma `oc-da` y `COT_` con el mismo patrón: HEAD +
  `Last-Modified` en `SyncState`, lectura en streaming Latin-1, parseo defensivo.
- Nuevo `app/mercado/`: `normalizar.py` (catálogo canónico), `indice.py` (precios),
  `competencia.py`, `arbitraje.py`. Sin httpx (capa anti-corrupción intacta).
- Tablas nuevas (solo `app/models`): `producto_canonico`, `oc_linea_ca` (solo Ñuble, en alcance),
  `cotizacion_ca` (Ñuble), `precio_indice` (producto × ámbito × mes), `costo_proveedor` (privado,
  por usuario/empresa), `tarifa_logistica` (comuna). Estimado < 100 MB/año para Ñuble [I].
- Histórico nacional: Parquet + DuckDB fuera de Neon (PC de Boris o artefacto de Actions); a
  Neon suben solo `precio_indice` y lo de Ñuble.
- UI: la ficha de CA muestra precio esperado, rango, competidores probables y margen; nueva vista
  "Mesa del día" (CA de Ñuble en alcance ordenadas por margen esperado × probabilidad).
- Lo que se apaga o congela: Plan Anual (ya en pausa), perfiles de licitaciones para estos
  usuarios. Se mantiene la ingesta de licitaciones solo si alguien la usa.
- Catálogo de costos con datos de negocios familiares: fuera de git y con autorización del dueño
  (decisión pendiente del informe del 30-oct).

## 9. Fases (una por commit, prompt `docs/prompt-F-*.md` cada una)

**Actualización 31-oct:** F-merc-1 pasa a ser la segunda key + cotizaciones (`prompt-F-merc-1.md`) y
F-merc-2 la carga de `oc-da` de Ñuble a la base (`prompt-F-merc-2.md`). El catálogo canónico de la canasta
de alimentos queda como F-merc-3. La tabla de abajo es la versión original.

| Fase | Contenido | Juez |
|---|---|---|
| **F-merc-0** (hoy, sin commit de app) | `scripts/mercado/mercado_observatorio.py`: bajar 24 meses de `oc-da` (solo CA en alcance), sondear `COT_`, informe y walk-forward | Walk-forward con UNSPSC = línea base (55 %, hecho 31-oct) |
| F-merc-1 | Catálogo canónico v1 para la canasta foco (40–80 productos de mayor monto en Ñuble) + Parquet/DuckDB | Walk-forward por producto canónico: meta ≤ 12 % |
| F-merc-2 | Ingesta `oc-da` + `COT_` de Ñuble a Neon, `precio_indice`, vista de competencia | Totales por mes iguales a los del ZIP (conciliación) |
| F-merc-3 | Segunda key: CA v2 región 16 cada 15 min + detalle al cierre; cuota y pausa por ticket | Cuota usada < 500/día; 0 requests con la key 1 |
| F-merc-4 | "Mesa del día" + predicción guardada antes del cierre (modo sombra) | 4 semanas contra adjudicaciones reales (§2, peldaño 2) |
| F-merc-5 | Costos, logística por comuna, margen y cotización asistida | Margen realizado (§2, peldaño 3) |

## 10. Lo que falta

| Necesito | Estado | Desbloquea |
|---|---|---|
| Bajar `oc-da`/`COT_` | **Falta desde Cowork** (el proxy de la nube y de la VM bloquea `transparenciachc.blob.core.windows.net`). Desde PowerShell en el PC de Boris funciona [V, `export_longevidad.py`] | Peldaño 1 completo |
| Segunda key de la API | Boris la tiene / la pide | F-merc-3 |
| Decisión de alcance (§3: opcionales 47/53/14 y 42) | Pendiente | F-merc-1 |
| Catálogo de costos (40–80 productos, con fuente y fecha) y permiso de uso | Pendiente (informe 30-oct §7) | Peldaño 3 |
| Tarifa de despacho a cada provincia de Ñuble | Pendiente | Margen por comuna |
| Términos de uso de la API para uso comercial (si se abre a terceros) | Por verificar | Etapa plataforma |

## 11. Prompt del peldaño 1 (para Claude Code o Cowork con PowerShell)

```
Lee docs/16-pivote-compra-agil-nuble.md. Desde PowerShell en la raíz del repo corre
`python data\mercado_observatorio.py bajar --meses 24` (solo CA en alcance, ZIP borrados al
terminar) y luego `python data\mercado_observatorio.py informe`. Con la muestra de COT_
(data/mercado/cot_muestra_*.csv) verifica y anota en docs/04-datos-abiertos.md qué columnas trae
(¿precio unitario por producto y proveedor? ¿región? ¿código de CA?), marcando [V]/[I].
Después construye el catálogo canónico v1: toma los 80 productos UNSPSC de mayor monto en Ñuble,
extrae de EspecificacionComprador/Proveedor el producto y el formato (reglas + revisión humana de
los 200 textos más frecuentes) y repite el walk-forward con clave producto_canónico|base en vez de
UNSPSC|base. No toques la app ni Neon; todo en data/mercado/.
Juez: walk-forward del último mes cerrado contra precios reales no usados para entrenar.
Pasa si, en la canasta foco, el error mediano es ≤ 12 % y ≥ 60 % de las líneas cae dentro de ±20 %,
con ≥ 70 % del monto normalizado. Línea base de hoy: 55 % de error con UNSPSC. Si no pasa, reporta
el número real, los 10 productos que más fallan y por qué.
```

*Fuente de los datos: Dirección ChileCompra.*
