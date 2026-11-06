# Prompt — F-dis-mercado (vestir Competencia, Estrategia, Abastecimiento y Actividad) · 7-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit, **sin push**, **sin migración**.
> Leer `docs/diseno/README.md` y las maquetas `Competencia.dc.html`, `Estrategia.dc.html`, `Abastecimiento.dc.html` y
> `Actividad.dc.html`. Requiere F-dis-1 en `main`. Puede correr en paralelo con F-dis-docs (worktree propio).
> Todas usan datos que **ya calculan** `competencia.py`, `abastecimiento.py`, `demanda.py` y `equipo.py`. Lo poco nuevo
> va como función pura con tests (§5). «Fuente: Dirección ChileCompra» al pie de cada pantalla con datos públicos.

## 1. Competencia (`competencia.html`, `competencia_perfil.html`, `_competencia_macros.html`)
- Cabecera con buscador por RUT o razón social (`data-buscar`), «Datos al …» (última corrida que alimenta
  `competencia_mes`), «Exportar planilla» (CSV del listado visible).
- Cifras: oferentes activos, oferentes por CA (mediana), con cuántos te cruzaste, tu tasa de adjudicación con delta.
- Segmentado **Ranking del territorio / Por comprador** (+ selector de comprador) y familia. Columnas: oferente (RUT ·
  comuna), CA ofertadas, % gana, precio vs mediana, monto ganado, **«Contigo»** (se cruzaron N · ganó M).
- Perfil en **panel lateral** (`GET /competencia/{rut}?panel=1` devuelve el fragmento; la página completa sigue
  existiendo): cifras, CA por mes (línea SVG inline, sin librería), **cara a cara contigo** con una lectura en lenguaje
  simple, dónde más oferta, familias, «Ver CA abiertas donde suele ofertar» (Mesa filtrada por comprador).
## 2. Estrategia (`estrategia.html`)
- Tres **oportunidades destacadas** arriba (`m-tab`): la de menos oferentes por CA donde aún no ofertas, la de más
  desiertas y la de más «no ganó el más barato», con las métricas que ya existen.
- Mapa comprador × familia con las 6 métricas de orden actuales, familia, «Solo donde aún no ofertas», señales «Poca
  competencia» (≥ 25 % con un oferente), «Quedan desiertas» (≥ 10 %), «Precio no decide» (≥ 25 %) con explicativo.
  Umbrales como constantes con nombre, en un solo lugar.
- Panel: métricas, cuotas (dominante / tú / resto), **lectura** en una o dos frases generadas por reglas (no IA),
  «Ver N CA abiertas en la Mesa» y «Ver competidores».
## 3. Abastecimiento (`abastecimiento.html`)
Cifras (demanda esperada 3 meses, cubierto por tu stock, **acierto del pronóstico vs línea base** con explicativo del
backtest, **«Peaks por venir»**); filtros por familia y «Solo donde el stock no alcanza»; tabla con barras de los 3
meses (SVG inline), unidades, stock, precio mediano, señales **«Peak en marzo»** / «Cae en verano» con explicativo;
panel con estacionalidad 12 meses (pronóstico en `accion`), frecuencia, rango de precio, quién compra, tus proveedores
y el primario «Pedir precios por volumen» (lleva a Proveedores con el producto filtrado). Las recomendaciones que ya
existen (`/abastecimiento/recomendaciones/{id}`) se conservan como acciones del panel.
## 4. Actividad (`actividad.html`)
Agrupada por día, filtros por tipo (Comentarios, Estados, Precios, Envíos y ventas, Detectado por Rastrillo), persona y
texto/código (querystring, recordados en `usuario_vista('actividad')`); cada fila con avatar, frase, CA, contexto, cita
del comentario y **«Ir a …»** con `equipo.destino_evento` (F-dis-paleta-campana; si no está en `main`, enlazar a la CA).
## 5. Funciones nuevas (puras, con tests)
`competencia.cara_a_cara(session, empresa_id, rut)`, `estrategia.destacadas(filas)`, `estrategia.lectura(fila)`,
`abastecimiento.peaks(series, umbral=0.4)`. Sin N+1.
## 6. Cierre
Render de las cuatro pantallas con y sin datos, panel por HTMX, filtros recordados, export CSV con control de empresa.
Cierre según `docs/diseno/README.md` §13. Commit: `F-dis-mercado: Competencia, Estrategia, Abastecimiento y Actividad
con el sistema Rastrillo`. Changelog visible.
