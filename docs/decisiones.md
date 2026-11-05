# Decisiones vigentes — mp-oportunidades

*Registro corto de decisiones que siguen mandando. Rescatadas de `archivo/12-free-vs-pago.md`,
`archivo/11-auditoria-ping-y-arquitectura.md`, los handoffs de sep y `archivo/00-estado-actual.md`.
Agregar al final con fecha; si una se revierte, tacharla y anotar por qué.*

## Infraestructura
- **Jobs en GitHub Actions, web en Render (Opción D, 22-sep).** Render free apaga el proceso 15 min
  después de la última request aunque haya un job corriendo, y Render puede suspender servicios
  free con mucho tráfico saliente. Actions da historial, logs y aviso de fallas gratis, y Neon
  despierta menos veces. [V] (`archivo/12-free-vs-pago.md` §2, §5)
- **Disparo externo con cron-job.org → `workflow_dispatch` (25-sep).** El `schedule` de GitHub
  llegó 2 a 5,5 h tarde y descartó 18 de 24 corridas horarias de `ca`. [V]
- **Render duerme a propósito (F-invertir-modelo).** No hay keepalive; `/api/salud/ping` no toca la
  base para no mantener Neon despierta.
- **Neon free mientras la base esté < 70 %** (27-sep). Launch: almacenamiento < US$1/mes; el costo
  real sería cómputo (~US$5–20/mes). Revisar al 70 % o si se agotan las CU-horas.
- **Correo por API REST de Brevo** (Render bloquea SMTP saliente).

## Datos y API
- **429 código 10500 = concurrencia, no cuota** (verificado 22-sep): 3 reintentos 30/60/120 s y
  cortar. ~~Otro 429 = tope diario.~~ (reemplazado el 29-oct por F-429-tasa, ver abajo). Tras
  504/timeout, 60 s de enfriamiento.
- **429 código 429 ("límite de tasa") = transitorio; ningún 429 agota el día** (29-oct, F-429-tasa;
  reemplaza a "otro 429 = tope diario"). Evidencia [V, logs de Actions 18–29 oct]: llegó con 8
  requests (23-oct) y 21 (24-oct) en la primera corrida del día y otro día tras ~1.700; tras el
  429 de la 01:05 del 29-oct y un `UPDATE` manual de `quota_log`, `ca` hizo 113 requests con 200
  el mismo día. El tope diario de 10.000 nunca se ha visto en un 429 real. Política: sin
  reintento, pausa persistida en Postgres de 20 min (`PAUSA_TASA_MIN`; 60 min para un código no
  visto, `PAUSA_429_DESCONOCIDO_MIN`, con log ERROR), cortar la corrida guardando el avance.
  `marcar_agotado` queda sin uso hasta verificar un código de tope diario: un falso positivo
  cuesta un día entero de ingesta (`15-diagnostico-ca-429.md`).
- **Detalle de CA inevitable:** el listado v2 no trae descripción ni productos [V]. Por eso
  `detalles-match` va aparte, con tope por tiempo (20 min día / 120 noche) y contador de fallos.
- **`ca` cada hora** (en punta hay ~2.000 cambios/h; la API entrega ~42 CA/min).
- **Plan Anual:** máximo una carga cada 28 días; búsqueda inversa por descripción (el
  `codigo_producto` del PAC no es UNSPSC), solo el año en curso.
- **FTS:** toda tsquery pasa por `inmutable_unaccent` (F-acentos).

## Producto
- **Dashboard solo vigente + "Mi registro"** (guardadas, cerradas, vencidas recientes con piso 30,
  descartadas, archivadas).
- **Vigente** = por fecha de cierre antes que por estado; **CA sin cierre = vigente solo si se
  publicó hace ≤ 7 días** (27-sep). Una sola definición en `app/core/vigencia.py` (F-ajustes).
- **Guardar** unifica "Me sirve" + "Activar alertas" (= `OportunidadSeguida`, F-guardar).
- **Match de CA por rubro no se automatiza:** explorador de CA + rubros favoritos; solo
  "confirmados" por defecto + palabras elegidas por la persona (F-ca-vocab). F-ca-rubro → próxima
  versión.
- **Ficha en modal** sobre el feed y Mi registro (URL con `ficha=`); la página `/oportunidad/...` se
  mantiene porque la enlazan los correos.
- **Exclusiones solo sobre el título** (09-oct, Boris): una palabra excluida saca la oportunidad
  solo si está en su nombre, no por la descripción ni por un ítem ("agua" en un ítem de una
  licitación de adulto mayor no la saca). Sin flag por perfil. Se implementa en F-match-1 §1.7.
- **Relevancia del match** (F-match-1, 09-oct, Boris tras simular contra producción): keyword en el título
  50; solo en ítem o descripción 35; cada keyword adicional distinta +10 (máx. +20); rubro +20;
  organismo +15; sin keyword, rubro u organismo 40 (55 ambos); tope 100. Urgencia y competencia no
  suman: ordenan. Piso del feed 40, "Alta" 60.
- **Descartar** tiene dos caminos: "Solo descartar" y "Descartar y excluir términos" (valida
  choques antes de descartar).
- Perfil 8 (30 rubros) es de prueba; los perfiles reales (5, 6, 7) son por keywords.

## Forma de trabajo
- Prompts de fase auditados en Cowork; Claude Code ejecuta; un commit por fase, sin push hasta
  auditar. Sonnet para fases acotadas; Opus para cuota/429/lock o borrado de datos.
- **Toda auditoría termina con un paso a paso de cómo seguir** (comando exacto, quién, qué esperar,
  qué hacer si falla).
- Investigación: no afirmar un negativo sin consultar la fuente primaria; marcar [V]/[I].
- **Plan Anual en pausa (09-oct):** el ZIP completo da 403 desde GitHub Actions (200 desde el PC de Boris).
  No se carga el PAC 2026; se retoma cuando se publique el PAC 2027 (en 1–2 meses), con carga
  mensual desde el PC de Boris. Mientras tanto `/plan-anual` muestra pocos datos (819 líneas).
- **Retención nunca borra lo que un usuario tocó (09-oct):** guardadas (también archivadas) y
  descartadas conservan fila, descripción, ítems y competencia para analizar perfiles. La purga de
  filas (F-retencion-filas) solo alcanza oportunidades terminales sin ninguna relación con usuarios.

## Pivote (31-oct)
- **Pausa de la ingesta actual (31-oct, Boris):** la app la usan Boris y Mario; el foco pasa al pivote de
  Compra Ágil en Ñuble (`16-pivote-compra-agil-nuble.md`). Se apagan en cron-job.org los disparos de `ca`,
  `ciclo-ca`, `ciclo-match`, `ciclo-activas`, `nocturno`, `resumen`, `catalogos` y `retencion` (retención
  también, para no purgar CA de Ñuble antes de F-merc-1 §1.7). La web en Render queda como está. Una sola
  key: `MP_TICKET` queda entera para el pivote.
- **Alimentos es el giro principal del pivote**; los demás rubros son expansión, no prioritarios.
- **Backfill diurno (2-nov-2026, Boris):** excepción a la regla 5 de `CLAUDE.md` mientras la ingesta de la
  app esté en pausa: `scripts/backfill_cot_nuble.py --diurno` puede correr fuera de 22:00–07:00 si no supera
  0,5 req/s (el ritmo de producción) ni 2.000 requests por corrida (el script baja con aviso un `--rps` o un
  `--max-requests` mayores). La espera tras un 429 y `--max-pausas` funcionan igual que de noche, y el modo se
  fija al arrancar: una corrida diurna que entra a la ventana nocturna sigue con los límites diurnos. Sin
  `--diurno` nada cambia (fuera de ventana no emite). Al retomar la ingesta de la app, la excepción deja de valer.
- **Plataforma de intermediación de CA (3-nov-2026, Boris confirma `17-plan-pivote-plataforma-ca.md`):**
  solo Compra Ágil y cuota solo para CA (regla 24); app actual congelada, el pivote vive al lado con el mismo
  login y base (`/mesa`, `/proveedores`, `/catalogo`, `/abastecimiento`); empresas con territorio priorizado y
  usuarios asignados por el admin (reglas 25 y 26); anexos: sonda a la API primero y, si no hay descarga, carga
  manual desde la ficha pública con bandera "CA sin detalle" y corrección en la plataforma; extracción de anexos y
  listas de precios con modelo de lenguaje vía OpenRouter (modelo elegido por set de evaluación de 30 anexos, el
  más barato con ≥ 95 % de líneas correctas; presupuesto mensual persistido en Postgres y caché); proveedores y
  costos se cargan desde cero. Orden de fases: F-merc-1 → F-merc-2 → F-plat-3 → F-plat-1 → F-plat-2 → F-plat-4 →
  F-plat-5 (4 semanas en sombra) → F-plat-6 → F-plat-7.
- **OC de Compra Ágil en la base (4-nov-2026, F-merc-2):** `oc_lineas_ca` y `precio_referencia` no se purgan
  (excepción del piloto a la regla 11, igual que las cotizaciones). Estimado 35–45 MB con índices para 24 meses de
  Ñuble (102.825 líneas, ~90 caracteres de especificación [V en `oc_ca2_*`]); **medir tras la carga y anotar acá**.
  PK `(codigo_oc, id_item)` con `IDItem` del `oc-da` [V, 2026-9.zip: 79 columnas, incluye `tieneItems`, `IDItem`,
  `monedaItem`]; sin IDItem la fila se descarta y se cuenta. Una OC puede venir en dos ZIP mensuales: el mes se borra
  y reinserta como upsert en una transacción y la fila queda en el mes más reciente. Moneda = `monedaItem` si viene,
  si no `TipoMonedaOC`. Textos largos (especificación, unidad, organismo, proveedor, producto) son Text. Región del comprador y del proveedor = texto en `oc-da`, convertido a código con
  `region_codigo` (`app/catalogos/regiones.py`). Solo CLP suma en `precio_base`, `precio_referencia` y
  `resumen_mercado` (el formato de `TipoMonedaOC` es [I] hasta la primera carga: vacío/NA = CLP). Datos públicos y
  comunes: no pertenecen a una empresa (regla 26 no aplica). ~~Cruce con F-merc-1: `ca_cotizaciones.id_oc` =
  `oc_lineas_ca.codigo_oc` vía `codigo_oc_normalizado` [I]~~ — **falso [V, 5-nov-2026, F-plat-6-cruce]**: `id_oc` (y
  `compras_agiles.id_orden_compra`) es el `ID` numérico de oc-da, no su `Codigo`; 0 de 1.366 calzan. El cruce va por
  `ID` (F-plat-6-cruce-2, con migración). No se infieren ganadores.
- **F-merc-2 en producción (4-nov-2026):** cargada con `--desde 2024-10`: 26 meses, **108.051 líneas** en
  `oc_lineas_ca` (**50,5 MB**) y **54.674 filas** en `precio_referencia` (**6,6 MB**). Total **57 MB**, sobre los
  35–45 MB estimados arriba. Juez exacto: alimentos de Ñuble 2025-10→2026-09 = **$681.979.119**, **1.516 OC** y
  **105 compradores** (desvío 0,000 %). Los contadores defensivos (`sin_id_item`, `repetidas_en_el_mes`,
  `movidas_de_otro_mes`, `moneda_distinta`) dan **0 en los 26 meses**. `mercado-oc` **no tiene tope de tamaño**: los
  57 MB se descuentan del margen de `MERCADO_BACKFILL_TOPE_BD_MB` (350), que sí frena al backfill. Disparo diario de
  `mercado-oc` a las **07:20 Chile** en cron-job.org.
- **Retención de las CA del piloto y tamaño de las cotizaciones (3-nov-2026, F-merc-1 §1.5 y §1.7):**
  `purgar_terminales` no toca las CA de `MERCADO_REGIONES` (hoy Ñuble, 16) ni, por lo tanto, sus
  `ca_cotizaciones` / `ca_cotizacion_productos`: excepción explícita a la regla 11 mientras dure el piloto.
  Sin `raw_json` para estas CA. Tamaño estimado con el volumen real de agosto 2026 (puente F-merc-0b, Ñuble:
  2.577 CA con detalle, 6,1 ofertas y 19,3 filas oferta × producto por CA; ~335 B por oferta y ~235 B por
  producto con índices): **todo, 24 meses ≈ 460 MB** (12 meses ≈ 230 MB), muy sobre los 120 MB del prompt. Se
  aplica el respaldo de §1.5 (`MERCADO_PRODUCTOS_SOLO_GRUPOS=true`, default): `productos_cotizados` solo de CA
  con algún producto en los grupos del observatorio (`app/catalogos/mercado.py`; 1.333 de 2.577 CA en agosto, el
  "~1.300 CA/mes" del prompt); las ofertas se guardan siempre → **≈ 15 MB/mes, 24 meses ≈ 360 MB**: sigue sin
  caber [I: estimado, falta medir en Neon]. Por eso `mercado-backfill` se detiene si la base llega a
  `MERCADO_BACKFILL_TOPE_BD_MB` (350; Neon free = 512) y recorre primero los meses que más importan.
  **Pendiente de Boris:** acotar el horizonte (12 meses ≈ 180 MB) o guardar productos solo de alimentos (giro
  principal: 182 CA en agosto; ≈ 7 MB/mes, 24 meses ≈ 170 MB). Medir el tamaño real tras la primera noche.
  **Ajuste F-merc-1-fix (3-nov):** el horizonte real es ≈ 24 meses (2024-11 → mes anterior; la API no trae
  Ñuble en oct-2024 [V]). Se mantienen `MERCADO_PRODUCTOS_SOLO_GRUPOS=true` y el tope de 350 MB; la decisión
  de alcance (solo alimentos o no) se toma tras medir en `/salud` la primera noche.
- **Estados que piden detalle en el backfill (3-nov-2026, F-merc-1, desviación del prompt):** `mercado-backfill`
  pide detalle solo de las CA `proveedor_seleccionado` (`MERCADO_BACKFILL_ESTADOS_DETALLE`, mismo default que el
  puente F-merc-0b); el prompt pedía todas las terminales (`cerrada`, `desierta`, `cancelada`). `mercado-ca` (en
  vivo) sí pide todas. Motivo: cuota (un detalle por CA; ~3.600 CA/mes en Ñuble, de las que ~2.600 tienen
  ganador). Costo: del histórico no quedan las ofertas de las CA desiertas o canceladas (sí su fila básica del
  listado). Se amplía con la variable si P(ganar) las necesita. Horizonte configurable con
  `MERCADO_BACKFILL_PRIMER_MES` (default 2024-11 desde F-merc-1-fix); un mes terminado y sin CA queda `vacio` y no se repite.
- **Backfill nocturno espera la pausa del 429 (4-nov-2026, F-merc-1c).** Evidencia [V, log de Actions del 4-nov,
  `mercado-nocturno` manual 00:45 Chile]: 137 requests en 16 min (0,14 req/s), todas 200; a las 01:01 un `429
  codigo=429`; el cliente persistió la pausa hasta las 01:21 y el job terminó, con ~5 h de ventana sin usar. 137 cae en
  el rango 110–190 de `15-diagnostico-ca-429.md`: el límite cuenta requests en una ventana de tiempo, no velocidad
  [I fuerte]. A ~137 requests por noche los ~24 meses no se terminan. **Regla 3 por tramo:** el job es una serie de
  tramos; un 429 de tasa corta el tramo sin reintento y guardando el avance (lo que la regla pide), el job lee el fin
  de la pausa **de Postgres** (otro proceso pudo alargarla), duerme hasta ese fin + 30–90 s y empieza otro tramo; el
  request del 429 no se repite antes de la pausa (la página cortada se vuelve a listar en el tramo siguiente, sus
  detalles ya guardados no se piden). Solo si el despertar cae antes de las 06:45 Chile y antes del tope de
  `MERCADO_BACKFILL_MINUTOS` − 5 (el reloj cuenta el sueño) y quedan pausas. 10500 agotado, cuota, cortes propios y
  401 terminan el job como antes. **Lock retenido mientras duerme** (regla 13): soltarlo abriría la puerta a dos
  backfills; efecto aceptado: un `mercado-ca` de noche espera el lock 10 min y sale omitido (0), y durante la pausa
  tampoco podría llamar a la API. **`MERCADO_BACKFILL_MAX_PAUSAS`** (12; 0 = cortar al primer 429, el comportamiento
  anterior) es el interruptor. **Segundo disparo** de `mercado-nocturno` en cron-job.org a las 03:40 Chile: el
  primero (22:30) para a los 300 min (~03:30); si sigue vivo, el `concurrency` encola el segundo. Estimado [I]:
  ~137 requests cada ~37 min → ~1.700–1.900 por noche; verificar con `requests_por_tramo` de la primera noche (es
  además el N_A de la sonda H1).
- **Ofertas repetidas del mismo RUT se guardan todas (4-nov-2026, F-merc-1c).** Un RUT manda varias ofertas
  distintas a una CA [V, sonda del 4-nov; `01-analisis` §10]; el UNIQUE `(ca, llamado, rut)` descartaba ~4 % de las
  ofertas (45 de ~1.090 la primera noche). Pasa a `(ca, llamado, id_cotizacion)` (migración `b8d4f1a6c2e9`); sin
  `id_cotizacion`, clave sustituta `"rut:<rut>"` (se comporta como antes). Un `id_cotizacion` repetido en la misma
  respuesta se guarda una vez con un WARNING por CA; un duplicado exacto con distinto id se guarda (para la API son
  dos ofertas) y se cuenta en `ofertas_identicas`. **Regla de elección para los consumidores:** precio de un
  competidor = su oferta de menor `valor_neto` (por producto, el menor precio unitario); precio del ganador = la
  oferta seleccionada; n.º de oferentes = RUT distintos, no filas. Aplicada en `mercado_observatorio.py` (antes
  quedaba la última fila de cada RUT) y `bandas_precio.py` (antes contaba cada oferta como un oferente y sumaba las
  ofertas del mismo RUT en la verificación de `ValorNeto`). [V, leyendo el código] `exportar_cot_api.py` no
  deduplicaba (escribe todas las filas de la base) y el puente `backfill_cot_nuble.py` escribía al CSV todas las
  ofertas de la respuesta: los CSV del puente ya traen las ofertas repetidas. Las CA ya guardadas con ofertas de menos
  se reabren con `scripts/mercado/reabrir_ofertas_faltantes.py` (lo corre Boris).

## Plataforma (4-nov-2026)
- **La plataforma de Compra Ágil es la interfaz principal; la app antigua queda en «Archivo», solo admin** (Boris, 4-nov).
  Control en el servidor (`requiere_admin_archivo`), no solo menú (regla 17). No se borra ruta, plantilla, job ni tabla; el retiro
  (F-retiro, con export previo de tablas) lo decide Boris aparte y no antes de que la sombra de F-plat-5 esté andando.
- **Diseño de la plataforma: estilo de dashboard sobrio, «referencia de estilo, no de marca»** (Boris, 4-nov; F-plat-ui).
  Se toma el lenguaje visual (fondo claro, superficies blancas con borde fino, un solo acento, tablas densas, badges suaves);
  no se copia logo, textos, degradado ni el color exacto de ninguna marca. Tokens `--c-*` en `app/api/static/app.css`
  (fondo `#F6F8FA`, borde `#E3E8EE`, texto `#1A2333`/`#5E6B7A`, acento `#4F46E5`, ok/aviso/error con su `-suave`);
  los contrastes AA los verifica `tests/test_ui_accesibilidad.py`. Sigue Bootstrap 5.3 (variables pisadas, sin framework
  nuevo). Tipografía Inter autoalojada (`static/fonts/`, OFL; sin CDN de fuentes). Barra lateral de 232 px (offcanvas < 992 px);
  el admin ve «Administración» (Empresas, Usuarios, Salud, Archivo con sus subpáginas). El modal de onboarding del feed
  antiguo solo lo ve el admin en las pantallas de Archivo. Sin modo oscuro. Colores y densidad: a ajustar tras la revisión de Boris.
- **Foco de la plataforma: pronosticar y diseñar estrategias para adjudicar** (Boris, 4-nov noche; `17-plan` §7). La
  corrección de líneas es el cimiento (estandarizar productos), no el fin. Se entrena con Compra Ágil y, cuando el método
  funcione, se lleva a licitaciones; reabrir licitaciones exige cambiar la regla 24 y es otra decisión. Consecuencias:
  fase nueva **F-plat-comp** (competencia: ficha de competidor, mapa comprador × competidor, variables para el modelo),
  en paralelo con F-plat-4; **vista de estrategia** (compradores × familia) dentro de F-plat-5; módulos de `app/mercado/`
  con "proceso/oferta/comprador" genéricos para portarlos a licitaciones.
- **Mesa v0 sí** (Boris, 4-nov noche): CA vigentes de Ñuble sin predicción, antes de F-plat-4, para que el equipo use la
  plataforma con CA vivas. Orden: F-plat-2 → mesa v0 → F-plat-4 ∥ F-plat-comp → F-plat-5 ampliada ∥ F-plat-6 → F-plat-7.
- **Extracción de anexos con LLM: en pausa, sin modelo fijado** (Boris, 5-nov). Evaluación de 9 modelos baratos de
  OpenRouter sobre los 30 anexos (`data/mercado/anexos_eval/resultados/informe_2026-11-05_0140.txt`, US$0,69 en total):
  ninguno cumple el criterio (≥ 95 % total, ≥ 90 % escaneados). El mejor fue `google/gemma-4-26b-a4b-it` (67,6 % total,
  68,6 % escaneados, US$0,0075 los 30); `z-ai/glm-5.3-flash` 58,5 %; `anthropic/claude-haiku-5.5` 55,2 %.
  `LLM_MODELO_EXTRACCION` queda vacío: la carga de anexos funciona en modo manual (F-plat-2 ya lo soporta). La fase de IA
  se retoma al final del plan. Antes de probar modelos más caros, diagnosticar si el error está en el modelo o en el
  juez/prompt: varios modelos leen **peor los PDF con texto que los escaneados** (Qwen3.6 y Qwen3.7: 0 % en texto), lo
  que apunta [I] al formato del texto que se le pasa (tablas con `|`) o a convenciones de la verdad (multiplicadores,
  consolidado) que el prompt no explica.
- **La plataforma es un espacio de trabajo colaborativo; estados del flujo de una CA** (Boris, 5-nov; F-equipo). Por
  empresa: `nueva` (sin fila), `priorizada`, `cotizando`, `lista` (para enviar), `enviada`, `descartada` (con motivo
  obligatorio). Transiciones libres (equipo chico, no se bloquea); reabrir una descartada se permite y queda en el registro.
  El resultado (ganada, perdida, no registrada, sin dato, desierta) va aparte, no como estado. Concurrencia: cada cambio
  manda la versión que vio el usuario; si no calza no se aplica y se muestra quién la cambió. Tomar o asignar una CA
  `nueva` la deja en `priorizada`.
- **Alarmas dentro de la app, sin correo por ahora** (Boris, 5-nov; F-equipo). Campana con sondeo de 60 s; cuatro
  preferencias (actividad del equipo, asignada a mí, cierre cercano, resultado), todas prendidas por defecto; la mención
  siempre llega. El cupo de Brevo queda para los resúmenes (regla 14); `generar_alarmas` y `consulta_no_leidos` quedan listas
  para un resumen por correo futuro. `ca_descartes_mesa` de F-plat-5 no se crea: el descarte es `ca_trabajo`.
- **Clave del comprador en la demanda: la decide la medición, no el código** (F-plat-6, 5-nov). Regla del prompt: si ≥ 80 %
  de las CA con OC tienen `clave_unidad(organismo_rut, unidad de la CA)` = `clave_unidad(rut_unidad, unidad de la OC)` →
  `clave_unidad`; si no, el RUT (`indice._rut`), con las unidades como detalle. Nunca un cruce por nombre libre. Con menos
  de 30 CA medibles no hay evidencia y gana el RUT. `mercado-demanda` mide (c) en cada corrida y, si la clave guardada no
  es la que toca, recalcula TODAS las series (nunca mezcla claves). **Hoy [V dev, 5-nov]:** dev no tiene CA, así que (c)
  no se puede medir y gana el RUT; (b) en Ñuble: 38 de 112 RUT compran con más de una unidad (máx. 25), así que con el RUT
  se juntan DAEM, salud, etc. de una municipalidad. El enlace a `/competencia?comprador=…` solo aparece con `clave_unidad`.
  La decisión definitiva sale de la primera corrida `--completo` en producción (Boris pega `medicion.c_*`).
- **Pronóstico de demanda: el juez manda y la meta no se afloja** (F-plat-6, 5-nov). El v1 se usa en la UI y en las
  recomendaciones de un nivel solo si cumple la meta (precisión ≥ 70 % y recall ≥ 50 % de «compra en M» con p ≥ 0,6) Y le
  gana a la línea base en la misma población (la base no cumple la meta, o la cumple con menor F1); si no, se muestra la
  línea base («compró el mismo mes del año anterior o el último mes observado») y se dice. No se ajustó la fórmula del
  prompt mirando el backtest: una variante (recurrencia de 12 meses, K = 2) llegó a la meta en familia en los 6 meses
  evaluados, pero no en los 6 anteriores, así que se descartó por sobreajuste.
- **Umbrales de «pedir precio firme»: 200 kg, 300 L, 500 u en 3 meses** (F-plat-6, default del prompt; `DEMANDA_UMBRALES`
  en JSON). A revisar por Boris con las 20 primeras recomendaciones reales (prompt §5.3).
- **Cruce CA ↔ OC por el `ID` de oc-da, no por el código** (F-plat-6-cruce, 5-nov). La OC de una CA = `id_orden_compra` o,
  si es nulo, los `id_oc` de sus ofertas seleccionadas (`demanda.oc_de_cas`). Ambos son el `ID` numérico de oc-da [V: sonda
  en prod, 0 de 1.366 calzan con `codigo_oc`; 10 de 10 encontrados en `oc-da/2026-10.zip` calzan por `ID`]. Hasta guardar
  `ID` en `oc_lineas_ca` (F-plat-6-cruce-2) no hay cruce: (c) = `None`, clave = RUT, aviso en el job. No se cruza por
  nombre ni por monto.

## Revisión CBS (5-nov, Boris · Mario · Gonzalo; aprobado por Boris esa noche)
- **Flujo operativo objetivo y fases nuevas:** `18-revision-cbs-y-flujos-operativos.md` (manda sobre `17-plan` §7).
  Piloto chico y de calidad (Chillán, 1–5 CA/día); foco en dos cuellos de botella: anexos y precios de proveedores.
- **Catálogo en tres capas:** **Demanda MP** (`/catalogo`, lo que el mercado demanda), **Catálogo Proveedores**
  (`/proveedores`) e **Inventario** (`/inventario`, nuestras existencias; planilla simple ahora, integraciones con
  APIs de proveedores después). La llave común es `producto_canonico × producto_formato`.
- **Precio de proveedor vencido a los 15 días** (`empresas.dias_vigencia_precio`, default 15): `mejor_costo`
  excluye vencidos salvo que no quede otro, y toda la UI lo marca.
- **Plazo de pago del comprador para la caja:** 30 días por defecto, ajustable.
- **P(ganar) se muestra desde el primer día** (como ya está, etiquetada «modo sombra»); no se oculta. F-plat-5c
  descartada (`archivo/prompt-F-plat-5c-descartado.md`).
- **Anexos:** la plataforma los recibe (subida, carga masiva por código, endpoint con token); cómo se obtienen es
  decisión aparte con los términos del portal a la vista; regla 9 vigente. Sonda `--documentos` primero (R8).
- Prompts aprobados: `prompt-F-plat-capas.md`, `prompt-F-plat-anexos-2.md`, `prompt-F-plat-lote.md`,
  `prompt-F-plat-seguimiento.md`.
- **Regla 9 reescrita (5-nov, Boris):** scrapear listados/fichas sigue prohibido (la API lo cubre). Descargar el
  adjunto público de una CA en alcance por URL directa queda permitido **como última alternativa** (API → subida
  manual/carga masiva → descarga directa), a ritmo humano y cortando ante 403/429. Base [V, leída el 5-nov]: las
  Políticas y Condiciones de Uso de ChileCompra (09-02-2026) declaran pública la información y los documentos de los
  procesos y reservan el bloqueo a «BOT que comprometan la disponibilidad»; no prohíben el acceso automatizado como
  tal. Las condiciones de la API solo se leyeron en parte [I: revisar el texto completo en chilecompra.cl/api/].

