# Prompt — F-dis-datos (datos de «Hoy» y del panel lateral de la Mesa, sin tocar el diseño) · 6-nov-2026

> **EN ESPERA (6-nov, Boris):** depende de decisiones del rediseño en curso (cifras, «Te necesita», panel). No correr
> hasta que Hoy y Mesa estén cerrados en el canvas; entonces ajustar §1–§3 a la versión final.

> Para Claude Code. **Modelo: Opus**, porque hay que definir las cifras por período en hora de Chile y
> refactorizar las alarmas de seguimiento para que las comparta «Te necesita». Un commit, **sin push**.
> **Sin migración** salvo que `EXPLAIN` muestre que falta un índice; en ese caso, justificarlo.
> Va en paralelo con el rediseño de Boris (artifacts «Rastrillo» y «Rastrillo · Maquetas», handoff en el proyecto
> `claude/09-handoff-diseno.md`). **Prohibido tocar** `app/api/static/app.css`, `base.html`, `hoy.html`, `mesa_dia.html`
> y los parciales existentes. Solo se pueden **crear** templates nuevos, mínimos, sin estilo propio.

## Objetivo
Las maquetas cambian la forma de trabajar. «Hoy» deja de ser una grilla de conteos y pasa a ser una **bandeja**: cifras
del período, «Te necesita» ordenado por urgencia, embudo del equipo y actividad. La Mesa pasa a ser una **lista densa
con panel lateral**, donde el detalle de la CA se ve sin salir de la lista.
Esta fase deja lista y probada la **capa de datos** que esas pantallas necesitan. Cuando el sistema visual se congele,
F-dis-1 (tokens y átomos) y F-dis-hoy/F-dis-mesa solo tienen que vestirla.
Hoy esos datos están repartidos como conteos sueltos en `pages.portada` (`lt.resumen_hoy`, `vt.resumen_hoy`,
`eq.cierran_sin_enviar`, `conteo_por_revisar`, `filas_pendientes`, `vencidos_por_proveedor`).

## 1. `app/mercado/hoy.py` (datos puros: sin HTML, sin `Request`, todo filtrado por `empresa_id`, regla 26)

### 1.1 `te_necesita(session, empresa_id, usuario_id, regiones, ahora, limite=30) -> list[Pendiente]`
`Pendiente` es un dataclass congelado con estos campos: `tipo` (`cierres|precios|lineas|anexos|ventas`), `nivel`
(`urgente|atencion`), `senal` (frase corta: «Cierra hoy 15:00», «Precio vencido · 23 días», «Falta costo · 2 líneas»,
«12 líneas por revisar», «3 anexos sin leer», «OC sin registrar · 4 días»), `titulo`, `detalle`, `accion` (verbo:
«Terminar oferta», «Pedir precio», «Agregar costo», «Revisar líneas», «Ver anexos», «Registrar OC»…), `url`, `ca_codigo`
(si hay) y `vence` (datetime o None, para ordenar). Fuentes:
- **cierres**: CA con `CaTrabajo.estado` en `ACTIVOS` y cierre en menos de 24 h (mismo criterio que
  `eq.cierran_sin_enviar`). El `detalle` dice qué falta: lote activo y cuántos productos sin precio, requisitos
  obligatorios pendientes, o «oferta sin armar». Si cierra el mismo día calendario de Chile es `urgente`; si no,
  `atencion`.
- **precios**: (a) costos vencidos (`vencidos_por_proveedor`, vigencia de la empresa) **solo de productos que pide
  alguna CA de un lote activo o una CA en trabajo**: una fila por producto y proveedor, con los días de vencido, el
  último costo y «lo piden N CA del lote X». (b) Líneas sin costo en lotes activos: una fila agrupada por lote. Los
  vencidos que no pide ninguna CA no entran (quedan en /proveedores).
- **lineas**: `conteo_por_revisar(...)["vigentes"]` > 0 → **una** fila agrupada (cuántas líneas y de cuántos anexos),
  que apunta a `/catalogo/revision`.
- **anexos**: CA vigentes que esperan anexo (`anexos_pendientes.pendientes`, el mismo criterio de la lista) → **una**
  fila agrupada con los nombres de las 3 primeras. Mover `filas_pendientes` de `routes/anexos.py` a la capa
  `app/mercado` (hoy `pages.py` importa una función de otra ruta) y dejar la ruta llamándola.
- **ventas**: las condiciones de `ventas.generar_alarmas_seguimiento` (requisitos pendientes, OC sin registrar,
  entrega que vence, factura pendiente, pago atrasado). **Refactor obligatorio:** extraer
  `ventas.condiciones_seguimiento(session, empresa_id, ahora) -> list[tuple[tipo, codigo, datos]]`, que pasan a usar
  tanto las alarmas como «Te necesita». Así hay una sola verdad. Un test de regresión verifica que las alarmas emiten
  exactamente lo mismo que antes.

«Te necesita» **se calcula en vivo desde el estado y no lee `ca_eventos`**. Los eventos de sistema son un registro de
notificaciones, únicos por (empresa, CA, tipo): no representan el estado actual y no se resuelven solos.
El orden es `urgente` primero, después por `vence` ascendente (los None al final) y después por tipo. También existe
`conteo_por_tipo(pendientes) -> dict[tipo, int]` para los chips de filtro.

### 1.2 `cifras(session, empresa_id, regiones, periodo, ahora) -> list[Cifra]` (4 cifras; `periodo` ∈ `hoy|semana|mes`)
`Cifra` tiene `clave`, `etiqueta`, `valor` (número crudo; el formato va en la vista), `unidad` (`clp|n|pct`), `nota`
y `delta` (fracción o None). Los períodos son en **hora de Chile** (`ZoneInfo("America/Santiago")`): hoy = día
calendario, semana = lunes a domingo, mes = mes calendario. Tests de borde: el cambio de horario de septiembre y abril,
y la medianoche de Chile frente a la de UTC.

| clave | hoy / semana | mes |
|---|---|---|
| `en_juego` | Suma de `monto_disponible_clp` de las CA vigentes en `PRIORIZADA/COTIZANDO/LISTA`. En «semana», solo las que cierran esta semana. Nota: «N CA priorizadas». | **Adjudicado en el mes**: suma de `ventas.monto_de` de las ventas creadas en el mes (sin anuladas). Nota: «N OC · margen X %» si hay margen. |
| `nuevas` | CA de `regiones` con `fecha_publicacion` en el período. `delta` contra el período anterior equivalente (ayer, semana pasada, mes pasado); None si el anterior es 0. | Igual |
| `cierran` | CA en trabajo activas que cierran en el período. Nota: «N sin oferta lista» (estado ≠ `LISTA`). | Igual |
| `enviadas` | Eventos `EV_ESTADO` → `enviada` en el período (contar CA distintas). Nota: «N adjudicadas» según `CaTrabajo.resultado`. | Nota: «tasa de adjudicación X %» (ganadas / con resultado), solo si hay ≥ 5 con resultado. |

Regla 6: verificar contra `condiciones_vigente`/`hoy_en_chile` cómo se guardan `fecha_publicacion` y `fecha_cierre`
(hora de Chile naive o UTC) y comparar con la misma convención. **No adivinar.** Dejar la convención citada en el
docstring.

### 1.3 `embudo(session, empresa_id, ahora) -> list[Etapa]`
Etapas: Priorizadas · Cotizando · Listas para enviar · Enviadas (esta semana). Cada una con `n`, `monto` y `nota`:
- Priorizadas: el monto en juego.
- Cotizando: los lotes activos y los precios por pedir.
- Listas: cuántas cierran mañana o antes.
- Enviadas: ganadas y perdidas según `resultado`.

Usar `ETIQUETA_ESTADO` de `equipo`; no inventar nombres.

### 1.4 Señales por CA (las comparten la fila de la Mesa y el panel)
`senales_por_ca(session, empresa_id, codigos, ahora) -> dict[codigo, list[Senal]]`, **en lote, con un número fijo de
consultas sin importar cuántos códigos** (regla 12; test que cuenta las consultas con un listener de SQLAlchemy).
`Senal` tiene `tipo` (`cierra_hoy|falta_costo|precio_vencido|anexo_sin_leer|lineas_por_revisar|requisitos`),
`nivel` y `texto` («Cierra hoy», «Falta costo · 1 línea», «Precio vencido · 23 días», «Anexo sin leer»).
Los días de vencido son los del costo más viejo que usa la CA. Aunque la Mesa actual no la use todavía, se deja
expuesta para F-dis-mesa.

## 2. Panel lateral: `app/mercado/panel_ca.py` + `GET /mesa/ca/{codigo}/panel`
`panel(session, empresa_id, codigo, ahora) -> PanelCA`, armado solo con datos que ya existen:
- Cabecera: código, nombre, comprador (organismo y unidad), comuna detectada, cierre (`cierre_chile`,
  `cuenta_regresiva`) y monto disponible.
- Cifras: oferta sugerida y su % del disponible, margen esperado y con cuántos de cuántos costos, P(ganar) con su nota,
  y cierre. Fuente: la **caché de evaluación** (`evaluaciones_de`, la misma que lee la Mesa). Si hay una
  `OfertaPropia` guardada, manda la oferta propia. Sin caché, los campos quedan en None con un motivo («sin
  evaluación», «faltan las líneas del anexo»); no se calcula nada en vivo en el GET.
- Líneas (`CaLinea` ≠ `reemplazada`): producto, cantidad y unidad, costo unitario elegido (el mismo criterio que
  `lote._elegir_costo` y el contexto de `mesa_precio`; reutilizarlo, no duplicarlo), precio unitario sugerido y la
  marca `sin_costo` o `vencido(días)`. Si no hay líneas y la CA espera anexo, indicarlo.
- Trabajo: estado del equipo y etiqueta, responsable, `version` (para mutaciones futuras con control de concurrencia),
  lote activo (id y nombre), venta (estado) y requisitos obligatorios pendientes.
- Señales: `senales_por_ca(..., [codigo])`.
- Enlaces: ficha interna `/oportunidad/compras_agiles/{codigo}`, ficha pública (`_url_ficha`) y las URL de los
  fragmentos que **ya existen** (`/mesa/ca/{c}/comprador`, `/competidores`, `/precio`). El panel los carga con
  `hx-get` diferido; no se duplica su lógica.

Ruta en `routes/mesa_dia.py` con `html_require_empresa`. Si la CA no existe o está fuera de las regiones de la
empresa, responde 404 (mismo `_ca_o_404`). Renderiza un template **nuevo** `_mesa_panel.html`: HTML semántico
mínimo (`<section>`, `<dl>`, `<table>`), con clases con prefijo `r-` según los nombres del sistema
(`r-senal`, `r-estado`, `r-dato`) y sin CSS nuevo. Cerrar con «Fuente: Dirección ChileCompra» (regla 8).
Navegar con ↑/↓ y cerrar con Esc es tarea del cliente en F-dis-mesa; aquí no.

## 3. Vista previa para validar con datos reales (solo admin)
`GET /hoy/previa` (`html_require_admin`; si no hay empresa activa, el mismo aviso que la portada) con un template
nuevo `hoy_previa.html` que extiende `base.html` **sin modificarlo**. Muestra las 4 cifras con un selector de período
por querystring, «Te necesita» con chips por tipo (querystring `tipo=`), el embudo y `ultimas_actividades`, todo como
tablas y listas planas. Sirve para que Boris compare los números con su realidad antes de vestirlos. No va en el menú.
`pages.portada` no cambia. Sin entrada en `app/changelog.py`, porque ningún usuario ve el cambio; anotarlo en
`docs/00-estado.md`.

## 4. Tests y cierre
- `te_necesita`: cada fuente por separado; orden (urgente → `vence`); los agrupados aparecen una sola vez; los vencidos
  que no pide ninguna CA no entran; **otra empresa no ve nada de la primera** (regla 17/26); sin datos → lista vacía.
- Regresión: `generar_alarmas_seguimiento` emite los mismos eventos con el refactor.
- `cifras`: los tres períodos, `delta` None con anterior 0, bordes de horario de Chile y la tasa solo con ≥ 5.
- `embudo` y `senales_por_ca` (incluido el conteo fijo de consultas).
- Panel: CA ajena o fuera de región → 404; sin caché; sin líneas que espera anexo; con oferta propia; render del
  template (smoke). `/hoy/previa`: un usuario no admin recibe 403 o redirección según el patrón de `/salud`.
- `ruff check .`, `python -m mypy app` y `python -m pytest -rs` contra Postgres (0 fallos, 0 errores, 0 saltados).
- Commit: `F-dis-datos: datos de Hoy (te necesita, cifras, embudo) y panel lateral de la CA`. `git add` solo de los
  archivos de la fase.

## 5. Pasos de Boris
1. Revisar el diff (sobre todo el refactor de `condiciones_seguimiento`), deploy y abrir `/hoy/previa` en producción.
2. Comparar las cifras y «Te necesita» con lo que sabes del día. Si una definición no calza (por ejemplo, «en juego» o
   semana de lunes a domingo), corregir la tabla de §1.2 antes de vestirla.
3. Cuando se congelen los átomos del sistema de diseño: prompt F-dis-1 (tokens y macros) y después F-dis-hoy y
   F-dis-mesa sobre estos datos.
