# Auditoría integral — mp-oportunidades · 08-oct-2026

> Cowork, sobre `main` = `origin/main` en `e33897a` (F-ficha-modal en producción). Revisión de
> código completa (snapshot de `HEAD`), docs del repo y fuentes externas consultadas el 08-oct.
> Marcas: **[V]** verificado (código con archivo:línea, o fuente primaria consultada), **[I]**
> inferido / a confirmar con un Paso 0. Nada de esto se probó contra Neon (Cowork no llega):
> las cifras de producción vienen de los Paso 0 de sep–oct.
>
> Reemplaza como auditoría vigente a `archivo/13-auditoria-ux.md` (22-sep), cuyos hallazgos quedan
> resueltos o reabsorbidos aquí (§4.2).

---

## 0. Resumen ejecutivo

La app hace bien lo difícil (ingesta bajo cuota, lock, ventanas de CA, vigencia única, modal,
registro). Lo que más limita su valor hoy **no es la UI sino tres fallas de datos y una de
score**, todas baratas:

1. **El organismo de las licitaciones está siempre en NULL** [V] → "organismo seguido" no funciona
   en licitaciones, y en CA tampoco porque se compara `codigo_entidad` contra RUT [V]. Licitaciones
   tampoco tienen región [V] → todo perfil con regiones recibe licitaciones de todo el país.
2. **El score mezcla relevancia con urgencia y competencia** [V]: una CA que entra solo por rubro,
   sin ninguna palabra clave, puede sacar 60 ("Alta"); una licitación con 1 de 5 palabras y 45
   días de plazo saca 20 y queda oculta bajo el piso de 40.
3. **El tope de 500 candidatos se aplica antes de filtrar región y monto** [V] → en perfiles
   regionales se pierden oportunidades de la región.
4. **El correo-resumen no aplica umbral, no excluye descartadas y duplica** [V]; el feed también
   duplica cuando dos perfiles calzan con la misma oportunidad [V].
5. **Neon no tiene techo de crecimiento** [I]: la retención nunca borra filas de CA/licitaciones;
   ~3.100 CA nuevas por día. Con el PAC, el 70 % llega en 1–2 meses según la estimación.

Hay además fuentes nuevas que cambian el juego y no gastan cuota: **API OCDS pública sin
ticket** [V, probada hoy], **cotizaciones de Compra Ágil en Datos Abiertos (`COT_`)** [V existencia;
contenido I] y **Consultas al Mercado (RFI)** como señal temprana [V existen; canal I].

La propuesta de UX se ordena alrededor de una idea: **la unidad de trabajo es una decisión por
oportunidad antes de su cierre** → Bandeja de triage, Cierres (agenda), y `/perfiles` liviano con
vista previa en vivo. Y se agrega un **servidor MCP** para consultar perfiles y datos desde un
cliente LLM (§6).

Orden recomendado (detalle en §8): **F-datos-1 → F-match-1 → F-indices → F-perfiles-1/2 →
F-retencion-filas → F-bandeja → spikes de fuentes → F-mcp-1 → resto.**

---

## 1. Estado de partida [V]

- Producción: `e33897a`. Fases de sep–oct en producción: F-vigencia, F-ca-explorar, F-ca-vocab,
  F-acentos, F-guardar, F-ajustes, F-registro, F-ficha-modal. Sin prompts pendientes salvo
  `prompt-F-ca-rubro.md` (próxima versión) y `prompt-F-secretos.md` (rotación de credenciales sin
  evidencia de haberse hecho).
- Código: `pages.py` 2.784 líneas, `query.py` 1.260, `engine.py` 876, `orchestrator.py` 871;
  16 migraciones; ~1.150 tests.
- Operación: jobs en GitHub Actions disparados por cron-job.org; web en Render free; Neon free
  (0,5 GB; 118,5 MB al 27-sep + ~85 MB del PAC).

---

## 2. Obtención de datos: estado, fallas y nuevas estrategias

### 2.1 Mapa actual de ingesta

| Job | Cuándo (Chile) | Fuente | Requests/día (código) |
|---|---|---|---|
| `ca` | cada hora :05 | v2 listado por `cambio_desde/hasta` + estado | ≤150/corrida |
| `ciclo-activas` (`activas` + `detalles`) | 10:15 · 14:15 · 19:15 | v1 `estado=activas` + detalle | 3 + ≤600 |
| `ciclo-match` (`match`, `alerts`, `detalles-match`) | 08:50–20:50 c/2 h | v1/v2 detalle de lo que tiene match | ~280 (tope 20 min) |
| `nocturno` | 01:10 | ZIP `lic-da`, detalles, estados vencidos, backfill | ≤150 + ≤100 + ~240 |
| `retencion` | 05:40 | SQL | 0 |
| `catalogos` | lunes 06:35 | `BuscarComprador`, ZIP PAC | 1 |
| `resumen` | 08:30 | SQL + Brevo | 0 |

Máximo medido: 3.606 req/día (26-sep) de 9.000. **El cuello es el tiempo de lock, no la cuota.**

### 2.2 Datos que se pierden o se corrompen

| # | Hallazgo | Evidencia | Sev. |
|---|---|---|---|
| D1 | Organismo de licitación siempre NULL: el parser lee `CodigoOrganismo` en el primer nivel; viene bajo `Comprador`. Además `upsert` lo pisa con None en cada pasada del listado (también `tipo`). | `app/clients/mp_v1.py:47`; `app/ingest/licitaciones.py:46-47` [V] | ALTA |
| D2 | Licitaciones sin región (la columna no existe) → filtro de región no aplica a licitaciones. | `app/models/tables.py:98-125` [V] | ALTA |
| D3 | CA desierta/cancelada nunca se ingiere: el filtro de estado del listado solo pide `publicada, cerrada, proveedor_seleccionado`. Quedan en estado intermedio para siempre y la retención no las purga. | `app/ingest/compra_agil.py:30` [V] | ALTA |
| D4 | Fechas de CA pisadas con None por el listado → se pierde un cierre que trajo el detalle (contribuye al 58 % de CA sin cierre). | `app/ingest/compra_agil.py:78-79` [V] | MEDIA |
| D5 | Detalle de licitación pedido dos veces: `upsert_detalle` marca `detalle_obtenido` pero no `raw_json`; `detalles-match` busca `raw_json IS NULL`. | `licitaciones.py:70`; `orchestrator.py:249` [V] | MEDIA |
| D6 | Job `detalles` ordena por cierre ascendente sin filtrar vigencia → gasta hasta 600 req/día y ~44 min de lock en licitaciones ya cerradas. | `licitaciones.py:203-208` [V] | MEDIA |
| D7 | Ítems UNSPSC desde `lic-da` solo llegan a licitaciones que ya cerraron (`lic-da` no trae publicadas). | `datos_abiertos.py:159` + doc 04 §8 [I] | BAJA |
| D8 | Adjudicadas con fechas NULL: `stream_estados` no lee `FechaPublicacion/FechaCierre` de `lic-da`. | `datos_abiertos.py:192-195` [V] | BAJA |
| D9 | Catálogo `organismos` posiblemente vacío (forma de respuesta de `BuscarComprador` no verificada; el job mide "0,0 min"). | `mp_v1.py:221-226` [I] → Paso 0 | MEDIA |
| D10 | Tipos LQ y H2 eliminados por ChileCompra desde el 23-oct-2025 (vigentes L1, LE, LP, LR, LS); obras MOP/Minvu entran a Mercado Público desde el 12-dic-2025 con módulo propio. Revisar tipos desconocidos en logs. | chilecompra.cl (2025-10, 2025-12) [V] | BAJA |

### 2.3 Riesgos de robustez

- **R1. 429 diario no persistido** [V]: tras un 429 ≠ 10500 el `ca` de la hora siguiente vuelve a
  intentar (viola la regla 3). `retry_after_seconds` no se usa fuera de `app/clients/base.py`.
  → Persistir `api_bloqueada_hasta` en `sync_state` y revisarlo en `check_budget`. **Opus.**
- **R2. Cuota sin reserva por familia** [V] (`base.py:304`): de noche `detalles-match` podría
  consumir el día (ya pasó el 7-jul). → Reservas: CA 4.500 · detalles 3.500 · resto 1.000.
- **R3. Errores de transporte mal clasificados** [V]: `ConnectError`/`RemoteProtocolError` no se
  atrapan (`base.py:465`); en `detalles-match` suman fallo a la oportunidad, en `ca` cortan sin
  guardar el avance parcial (`compra_agil.py:479`).
- **R4. Crecimiento de Neon sin techo** [I]: retención borra `raw_json` e ítems, nunca filas
  (`app/core/retencion.py:60-112`). Ver F-retencion-filas (§8).
- **R5. Competencia O(n·m)** [V]: `stream_ofertas` recorre el CSV completo por licitación y mes
  (`datos_abiertos.py:514-524`). → Una pasada con `set` de códigos.
- **R6. Una descarga fallida aborta `sync_items_datos_abiertos`** [V] (`datos_abiertos.py:337`).
- **R7. SIGTERM de Actions no deja fila en `job_runs`** [V, backlog]: riesgo real en `nocturno`.

### 2.4 Nuevas fuentes (verificadas hoy)

| Fuente | Qué aporta | Estado | Costo |
|---|---|---|---|
| **API OCDS de ChileCompra** `api.mercadopublico.cl/APISOCDS/OCDS/listaOCDSAgnoMes/{a}/{m}/{desde}/{hasta}` + `/tender/{código}`, `/award/`, `/ocds/planning/`, `/ocds/contract/`; también `...TratoDirecto` y `...Convenio` | Licitaciones con todas sus etapas, ítems UNSPSC, oferentes con RUT, comprador `CL-MP-{codigoOrganismo}`, y en **planning los IDs de línea del PAC** (`1922-4-PC25`) → enlaza licitación ↔ Plan Anual | [V] Sin ticket, sin cuota MP. 2026/07: 8.004 registros. **2026/08 y 09 devuelven 404**: desfase de ≥1 mes [V], causa [I] | 0 cuota; requests a otro host |
| **Cotizaciones de Compra Ágil** `transparenciachc.blob.core.windows.net/trnspchc/COT_{aaaa}-{mm}.zip` | Quién cotiza CA, a qué precio, por organismo/rubro (mensual, desde 2020) | [V] existe; columnas [I] (¿descripción/productos?) | 0 cuota; ZIP mensual |
| **Consultas al Mercado (RFI)**, código con sufijo `RFIPRI26` | Señal temprana: el organismo sondea antes de licitar | [V] existen en producción (~10 publicadas may–ago 2026); [I] no hay canal en API/Datos Abiertos encontrado; probar `licitaciones.json?codigo=…RFIPRI26` | 1 request de prueba |
| **Convenio Marco** (productos y precios semanales; transacciones mensuales) | Precios de referencia; saber qué no se licitará porque va por CM | [V] descarga | 0 cuota |
| **BIP — Ministerio de Desarrollo Social** (CSV ~327 MB, 2019–2027, CC-BY) | Proyectos de inversión con estado: anticipa diseños/obras 6–18 meses | [V] descarga; columnas [I] | Pesado: procesar en Actions, guardar solo agregados |
| `oc-da` (OC tipo AG = Compra Ágil) | Quién gana CA del rubro y a qué precio | [V] existe; columnas a verificar | ~90 MB/mes comprimido |

**Estrategias nuevas de obtención** (impacto/esfuerzo):

1. **Corregir D1–D6 + R1–R3** (F-datos-1). Cero requests nuevas; ahorra hasta ~600 req y ~2 h de
   lock al día (D6). **Lo primero.**
2. **OCDS como fuente complementaria** (spike, luego F-ocds): organismo y región verificados,
   oferentes y adjudicación sin cuota; enlace con PAC para "líneas del PAC aún no licitadas" →
   predicción de licitaciones. No reemplaza a v1 para lo vigente (desfase ≥ 1 mes).
3. **`q` del listado v2 por keyword de perfil** (spike): si `q` busca en descripción/productos [I],
   permite encontrar CA relevantes sin bajar su detalle — la limitación de fondo del match de CA.
4. **Priorizar la cola de `detalles-match` por score y cierre próximo**, saltando lo que cierra en
   < 2 h: más valor por minuto de lock.
5. **Cierres masivos de estado** con v1 `fecha=D&estado=adjudicada|desierta|revocada` de noche
   (~5 req/día en vez de ≤150 detalles) — spike para verificar qué fecha filtra (regla 20).
6. **Precios de referencia por UNSPSC** (agregados p25/p50/p75, n° oferentes, ganador) desde
   `lic-da` + `COT_` + OCDS award, guardando solo agregados (5–20 MB [I]).
7. **Postulaciones propias**: con `Usuario.rut_proveedor`, historial de ofertas del usuario desde
   `lic-da`/OCDS (gana/pierde, contra quién, a qué precio).

---

## 3. Optimización de resultados (matching, score, alertas)

### 3.1 Cómo funciona hoy [V]

Candidatos por fuente: `vigente AND (FTS OR rubro OR organismo) AND NOT exclusión ORDER BY
fecha_cierre ASC NULLS LAST LIMIT 500` (`engine.py:275-304`, `_MAX_CANDIDATOS=500` en :166).
Score = texto (`hits/keywords·60 + 5` si hay hit en el nombre, tope 60, :50-64) + urgencia (25 si
2–7 días, 10 si 8–30, :67-73) + competencia (CA 15/10/5 según ofertas; licitación 8 fijo, :76-84)
+ estructural (+20 rubro, +15 organismo, :87-96). Feed: piso 40 (Alta = 60). Digest: top 5 por
score de los matches nuevos.

### 3.2 Debilidades

| # | Problema | Evidencia | Efecto |
|---|---|---|---|
| M1 | Score mezcla relevancia y urgencia/competencia | `engine.py:50-96` [V] | Rubro-only + cierre en 5 días + 0 ofertas = 60 "Alta"; 1/5 keywords en 45 días = 20, oculta |
| M2 | Dilución por cantidad de keywords | `engine.py:61` [V] | Un perfil con 20 sinónimos y un hit saca 3 puntos: castiga a los mejores perfiles |
| M3 | Urgencia casi muerta en CA (58 % sin cierre → 0; mediana de plazo 47 h → <2 días → 0) | `engine.py:671-673` [V] + Paso 0 27-sep | El orden por "mejor match" no refleja urgencia real |
| M4 | Tope 500 antes de región/monto | `engine.py:281` vs `:772-784` [V] | Perfiles regionales pierden resultados |
| M5 | Organismo seguido no calza (lic NULL; CA compara `codigo_entidad` vs RUT) | `engine.py:226,253`; `pages.py:1757` [V] | Criterio inútil hoy |
| M6 | Exclusión evalúa la descripción completa ("no incluye arriendo" excluye) | `engine.py:130-137` [V] | Falsos negativos |
| M7 | Keywords: stopwords dan tsquery vacía en silencio; "-" inicial invierte; frase sin comillas = AND sobre todo el documento; sin sinónimos | `text.py:31-39` [V/I] | Perfiles que "no traen nada" sin aviso |
| M8 | Feed duplica una oportunidad por cada perfil que calza | `query.py:565-593` [V] | Ruido |
| M9 | Digest sin umbral, sin excluir descartadas/guardadas, sin dedup, con N+1 | `email.py:348-372` [V] | "Encontramos N" inflado |
| M10 | Filtro de texto del feed por substring sin unaccent | `query.py:238` [V] | "reparacion" no encuentra "reparación" |
| M11 | Sin aprendizaje: descartes y guardadas no influyen | `feedback.py:1-10` [V] | El usuario corrige lo mismo una y otra vez |

### 3.3 Propuestas (orden por impacto/esfuerzo)

**Sin migración (F-match-1):**
1. Región y monto dentro del SQL de candidatos (reusar `_where_limpieza`); en CA ordenar por
   publicación descendente.
2. Organismo en CA: traducir `codigo_entidad` → RUT con `instituciones_pac` al construir el
   criterio (y en licitaciones, tras F-datos-1, comparar contra el código real).
3. **Separar relevancia de prioridad.** `relevancia` = texto saturado (1 hit 35 · 2 hits 45 ·
   3+ 55, +10 si es en nombre o ítem) + rubro + organismo; el umbral se aplica solo a relevancia.
   Urgencia y competencia pasan a orden secundario y a badges. CA sin cierre: estimar
   `publicación + 47 h` solo para ordenar.
4. Digest correcto: piso de relevancia, `NOT EXISTS` descartadas/guardadas, dedup por
   (fuente, código), carga en lote. Sección "Cierra en ≤ 48 h" dentro del mismo correo.
5. Feed deduplicado por oportunidad (score máximo + lista de perfiles).
6. Filtro de texto del feed vía FTS/unaccent.
7. Validación de keywords al guardar el perfil: avisar stopword (`numnode=0`), keyword que calza
   con > X % de lo vigente, limpiar "-" inicial, sugerir comillas para frases.

**Aprendizaje liviano (F-match-2, solo Postgres, sin dependencias):**
8. **Sugerir exclusiones desde las descartadas** y **keywords desde las guardadas** con `ts_stat`
   y lift contra la frecuencia global (mismo patrón que `rubro_vocabulario`). A pedido, en el
   asistente de perfil.
9. **Bonus "parecida a tus guardadas"** (0–15) con `ts_rank_cd` contra los top lexemas de las
   guardadas, aplicado solo a los ≤500 candidatos; penalización simétrica por descartadas.
10. **Bonus PAC/historial**: +10 "está en su Plan Anual" (GIN ya existente en
    `plan_compra_lineas`) y "este organismo compra esto habitualmente" (adjudicadas/competencia).
11. Exclusión opcional solo sobre nombre e ítems (flag por perfil; migración chica).
12. Sinónimos curados (tabla pequeña) expandidos en `build_tsquery`, mapeando el hit a la keyword
    original.

F11 (regresión logística) sigue pospuesto: hay muy poca señal (solo boris tiene >40 guardadas).

### 3.4 Rendimiento (F-indices)

- **Faltan índices de FK** [V]: `licitacion_items(licitacion_codigo)`, `ca_productos(ca_codigo)`,
  `alertas(match_id)`, `alertas(seguimiento_id)` (migración inicial + `tables.py:533-548`). Afecta
  `EXISTS`, `selectinload` y sobre todo los **borrados en cascada** de retención y limpieza.
- **FTS sobre ítems/productos sin índice** [V] (`engine.py:122-158`): índice GIN de expresión con la
  expresión exacta (patrón de `ix_plan_compra_lineas_desc_tsv`); validar con `EXPLAIN ANALYZE`.
- **N+1 en `_upsert_match`** [V] (`engine.py:582`) → `INSERT … ON CONFLICT … RETURNING (xmax=0)`.
- **Reescritura de todos los matches en cada ciclo** [I]: `razones.dias_al_cierre` cambia siempre →
  ~8.400 UPDATE por ciclo, tuplas muertas en 0,5 GB. Calcular días al mostrar.
- `lower(trim(estado))` no usa índice; CA sin índice en `fecha_cierre`/`fecha_publicacion` [V]
  (`vigencia.py:404,424`) → normalizar estado al escribir o índices de expresión.
- Limpieza repite el FTS del recall; cuando hay < 500 candidatos basta `NOT IN (candidatos)`.

---

## 4. UX / UI

### 4.1 Mapa actual [V]

`/` feed ("Dashboard"/"Oportunidades"/"tablero" según el lugar) · `/compras-agiles` explorador ·
`/registro` (Guardadas, Cerradas, Vencidas recientes, Descartadas, Archivadas) · ficha en modal
`?ficha=` y página `/oportunidad/...` · `/perfiles` (cuenta + nuevo perfil + rubros favoritos +
lista) · `/plan-anual` · admin/salud/login. El nav no tiene ítem para el feed (solo la marca).

### 4.2 Auditoría UX del 22-sep: estado

Resueltos: filtros unificados con chips, nav móvil, `<main>`/skip link/`aria-current`, anuncios
`aria-live` y foco, formato CLP y `tabular-nums`, una sola escala de score, ficha con acciones
arriba y pestañas, paginación, familias de estado, Descartadas en Mi registro.
Parciales: colores del nav (Admin/Salud), cierre sin huso, Deshacer con `location.reload()`
(pierde el scroll). **Abiertos:** `/perfiles` (ahora peor: 4 bloques), dos inputs de rubro con el
mismo `name` (`_rubros_widget.html:219,231`), organismos sin combobox y corte en 80 sin aviso,
tablas sin `thead` sticky.

### 4.3 Problemas nuevos

**ALTA**
- **U1. `/perfiles` pesa ~1,5 MB con 4 perfiles** [V]: el widget de rubros pinta 510 checkboxes
  (~277 KB) por instancia, una por perfil + la de nuevo; más ~1.333 organismos en JSON inline y un
  `<select>` de ~510 opciones (`perfiles.html:150,197-207,309,330`).
- **U2. Crear/editar perfil no da retroalimentación**: "Perfil creado" y nada más; un error
  redirige con `?error=` y **borra lo escrito** (`pages.py:1909-1931,1990-2009`).
- **U3. Resumen de perfil ilegible**: montos crudos ("5000000 – — CLP"), regiones y organismos como
  códigos, sin conteo de resultados; `activo` existe sin UI (`perfiles.html:240-252`).
- **U4. Lo que se busca en `/perfiles` (la lista) está al fondo**; formularios nuevo/editar
  duplicados (60 líneas); montos `type=number` no aceptan "5.000.000".
- **U5. No hay noción de "ya revisé esto"**: cada visita obliga a re-escanear; el orden por defecto
  es "mejor match", no urgencia.

**MEDIA**
- U6. Onboarding: con 0 perfiles el feed no enlaza a crear uno; el tutorial es un glosario.
- U7. Rendimiento percibido: ningún `hx-indicator`; el modal se abre recién cuando llega el
  contenido; Archivar/Desarchivar/Restaurar recargan la página completa.
- U8. Inconsistencia de tarjetas: el explorador duplica la tarjeta; Descartadas usa otro formato
  (slug crudo, sin modal); el menú móvil "Descartar ▾" del prompt F-ficha-modal no existe (4
  botones a ancho completo en móvil).
- U9. Accesibilidad: ~37 labels sin `for`; selects con `onchange=location.href` (WCAG 3.2.2);
  textos de 11 px.
- U10. Ruido: badge "Guardada" + botón "✓ Guardada"; toda licitación dice "Sin región".
- U11. Plan Anual: tercer selector de organismos distinto; querystrings a mano.
- U12. Novedades con voseo ("recibís", "tocá"), fechas ISO y entradas de operación interna.

### 4.4 Propuesta de UX/UI

**Navegación:** **Bandeja** (`/`) · **Cierres** · **Explorar** (pestañas Compras Ágiles · Plan
Anual · Mercado) · **Mi registro** · menú ⚙ (Perfiles, Cuenta, Novedades, Admin, Salud, Salir).

1. **Perfiles livianos (F-perfiles-1, M).** Edición cargada por `hx-get` al abrir; buscador de
   rubros server-side (`/rubros/buscar?q=`); combobox único de organismos (reutilizado en Plan
   Anual); errores re-renderizan con 422 conservando lo escrito; resumen legible con conteo
   vigente por perfil e interruptor Activo; `/cuenta` separada. Meta: < 150 KB.
2. **Asistente de perfil con vista previa en vivo (F-perfiles-2, M-L).** Pasos: ¿Qué vendes?
   (keywords como chips, con validación M7 y sugerencias) → ¿Dónde y cuánto? → Afinar (rubros,
   organismos, exclusiones validadas) → Revisar. Columna sticky con **conteo vigente por fuente y
   5 títulos de muestra** (`hx-trigger="keyup delay:400ms"`, mismo SQL que `criterio_perfil`), y
   señales: 0 → "quita monto o agrega palabras"; > 300 → "agrega exclusiones"; "500+".
3. **Bandeja de triage (F-bandeja, L, migración chica).** Segmentos "Por revisar (N)" · "Guardadas
   que cierran pronto" · "Todo"; orden por cierre con relevancia ≥ media; decisiones Guardar /
   Descartar / Después; la tarjeta sale y el foco pasa a la siguiente; contador "te quedan 12";
   atajos j/k/g/d/o. Requiere marca "revisada" por usuario. Panel de filtros colapsado salvo lo
   activo; conteo en vivo como el explorador; "Descartar ▾" en móvil; esqueleto inmediato del
   modal.
4. **Cierres (F-cierres, M).** Agenda Hoy · Mañana · Esta semana · Próxima con guardadas y
   "por revisar" de alta relevancia; alternativa mensual; **export `.ics`** de guardadas (texto
   generado en servidor, sin librerías).
5. **Panel de inicio (S-M)** en la cabecera de la Bandeja: cierran en 48 h (guardadas), nuevas sin
   revisar, vencidas recientes, cambios de estado; "Datos al HH:MM" desde `SyncState`.
6. **Ficha para decidir (S).** Bloque destacado cierre/días/monto; **notas** (el campo
   `OportunidadSeguida.notas` ya existe sin UI); precio de referencia y "quién suele ganar" cuando
   existan (§2.4-6); `thead` sticky.
7. **Explorar → Mercado (L, después de las fuentes).** Por rubro/organismo: cuánto se compra, a
   quién, a qué precio, cuándo (estacionalidad), líneas del PAC aún no licitadas.
8. **Transversal (S).** Terminología única ("Bandeja"), Novedades sin voseo y en dd/mm, ≥ 12–14 px.

### 4.5 Deuda de front

- `pages.py` (2.784 líneas, un router) → partir en `routes/{feed,ficha,acciones,registro,perfiles,
  cuenta,admin,plan_anual,explorador}.py` + `web/{templating,forms,urls}.py`, sin tocar URLs.
- Macros `_ui.html` (chip, botón guardar, badge de cierre, icono) y una sola `tarjeta(item,
  variante)`; hoy el botón Guardar está escrito 3 veces y la tarjeta CA 2.
- JS inline (~400 líneas en `base.html`, `index.html`, `perfiles.html`, con funciones copiadas de
  `rubros_widget.js`) → `static/{app,feed,organismos_widget,util}.js` con `defer`.
- CSS: `.punto-familia` muerto, colores fuera de tokens, 9 `style=""`, `login.html` sin `app.css`.

---

## 5. Otras mejoras

- **Producto:**
  - Alertas por **Telegram** (bot gratis), además del correo. WhatsApp tiene costo y es más complejo.
  - **Resumen de bases con LLM** a pedido desde la ficha (vía MCP, §6, sin infraestructura nueva).
  - **Compra Ágil "primer llamado EMT/local"** como filtro, si el campo existe en v2 [I].
  - **Notas y estado de postulación** en guardadas (preparando / enviada / ganada / perdida) → base para el historial propio.
- **Operación:**
  - Workflow de **CI** en Actions con `ruff`, `mypy` y `pytest` sin Postgres. Hoy no existe.
  - Rotación de secretos (`prompt-F-secretos.md`).
  - Limpieza de Render (crons viejos, endpoint y scheduler, `JOBS_TOKEN`).
  - Alerta del monitor si la base pasa el 70 %.
  - `alembic/env.py` que lea `.env`.
- **Seguridad:**
  - Revisar que `/oportunidad/...` de CA sin match en solo lectura no exponga `raw_json` de otro usuario. La ownership está verificada en las rutas [V según tests].
  - El MCP (§6) agrega superficie: tokens con hash, solo lectura primero y rate limit.
- **Docs:** reorganizadas en esta misma sesión (ver `docs/00-estado.md` y `docs/archivo/`).

---

## 6. Servidor MCP: consultar perfiles y datos desde un cliente LLM

**Objetivo.** Que Boris y el equipo puedan preguntarle a Claude (u otro cliente MCP) cosas como
"¿qué tengo que cierra esta semana y vale la pena?", "¿por qué mi perfil 6 trae tanto ruido?",
"¿quién gana las CA de aseo en la RM y a qué precio?", "resume la ficha 1234-56-LE26",
"sugiere exclusiones para el perfil 5", sobre los mismos datos de la app y respetando ownership.

**Principios (alineados con las reglas duras):**
- **El MCP nunca llama a la API de Mercado Público.** Solo lee Postgres, así que no gasta cuota ni compite por el lock.
- **Ownership en servidor:** cada token pertenece a un usuario, y las herramientas filtran por `owner_id` igual que las rutas.
- **Solo lectura en la versión 1.** Las escrituras (guardar, descartar, ajustar un perfil) quedan para la versión 2, con confirmación y la misma validación que la UI (`PerfilInvalido`, `exclusiones_que_chocan`).
- **Límites:**
  - máximo 50 filas por respuesta;
  - `statement_timeout` de 5 s por consulta;
  - rate limit por token.
- **Toda respuesta incluye "Fuente: Dirección ChileCompra".**
- **Capa propia** `app/mcp/`, que reutiliza `query.py`, `engine.py` (`criterio_perfil`) y `vigencia.py`. No agrega SQL nuevo fuera de esas funciones.

**Herramientas v1 (lectura):**

| Tool | Devuelve |
|---|---|
| `mis_perfiles()` | perfiles con criterio legible y conteo vigente |
| `bandeja(perfil_id?, dias_cierre?, min_relevancia?, fuente?)` | oportunidades vigentes deduplicadas, con razones |
| `ficha(fuente, codigo)` | detalle, ítems, competencia, estado y cierre en hora de Chile |
| `mi_registro(pestana)` | guardadas, cerradas, vencidas recientes, descartadas |
| `simular_perfil(keywords, exclusiones?, regiones?, monto?)` | conteo y muestra (lo mismo que la vista previa del asistente) |
| `diagnostico_perfil(perfil_id)` | keywords que no calzan, las que más ruido traen y exclusiones sugeridas (§3.3-8) |
| `buscar_plan_anual(texto, organismo?)` | líneas del PAC |
| `mercado(rubro_o_texto, region?, meses?)` | agregados de competencia y precios (cuando exista §2.4-6) |

**Dos caminos:**

- **A. MCP local por stdio, solo para Boris (F-mcp-1, S).**
  - **Cómo:** un script `scripts/mcp_local.py`, en el mismo patrón que los MCP de Caminatas que ya usas, que se conecta a Neon production con un **rol de Postgres de solo lectura** (`mcp_ro`, con `GRANT SELECT` sobre las tablas necesarias) y recibe `usuario` como parámetro de configuración.
  - **Ventajas:** no requiere hosting, no hay superficie pública y sirve para validar qué herramientas sirven de verdad.
  - **Contras:**
    - solo en tu PC;
    - la ownership depende de la configuración, no de un token;
    - consume CU-horas de Neon mientras se usa.
- **B. MCP remoto dentro de la app (F-mcp-2, M).**
  - **Cómo:**
    - endpoint `/mcp` (transporte HTTP "streamable") montado en FastAPI;
    - tokens por usuario creados en `/cuenta` y guardados con hash (tabla nueva `tokens_api`, migración);
    - header `Authorization: Bearer`.
  - **Contras:**
    - Render free duerme, así que la primera llamada tarda unos 30–60 s [I];
    - más CU-horas de Neon.
  - **Compatibilidad con clientes:**
    - Claude Desktop y Claude Code se conectan a un MCP remoto con un header [I, verificar en el spike];
    - los conectores personalizados de claude.ai piden OAuth o nada de autenticación [I]. Por eso el soporte OAuth queda como fase posterior.

**Dependencia nueva.** El SDK oficial `mcp` para Python está fuera del stack. La regla dice "proponer y justificar". Aquí la justificación es que es el SDK de referencia del protocolo, que se puede montar como app ASGI dentro de FastAPI y que no requiere servicio adicional. Hay que medir la RAM que agrega (Render tiene 512 MB) antes de aprobarlo.

**Recomendación:** hacer A ahora, porque es barato y sirve para aprender, y B cuando A demuestre que se usa y el equipo lo quiera.

---

## 7. Paso 0 de verificación (solo lectura, producción) — lo corre Boris

Un script `data/paso0_auditoria.py` (lo prepara Cowork) con estas consultas:

1. `count(*)`, `count(codigo_organismo)` de `licitaciones` vigentes → confirma D1.
2. `count(*) FROM organismos` → D9.
3. CA por estado y antigüedad de `fecha_publicacion` (estados intermedios > 30 días) → D3.
4. `pg_database_size`, `pg_total_relation_size` por tabla, y CA nuevas por día de los últimos 14 días → R4.
5. Duplicados en el feed: oportunidades con ≥ 2 matches del mismo usuario → M8.
6. Distribución de score por componente (texto = 0 con total ≥ 40; relevancia vs urgencia) → M1.
7. Para cada perfil real: candidatos antes y después de región/monto con `LIMIT` 500 → M4.
8. `raw_json IS NULL AND detalle_obtenido` en licitaciones → D5.
9. `tipo` de licitación por frecuencia en 2026 (¿aparecen tipos nuevos o desconocidos?) → D10.

---

### 7-bis. Resultado del Paso 0 (08-oct, producción) [V]

Log: `data/logs/paso0_auditoria.txt`. Base 161 MB (31 % de 512 MB).

| Hallazgo | Resultado | Ajuste |
|---|---|---|
| D1 organismo NULL | **0 de 34.442** licitaciones con organismo (0 de 4.398 vigentes) | Confirmado. `raw_json` guarda el dataclass parseado, no la respuesta: `Comprador` no está en la base → los nombres de campo se verifican con una sonda (F-datos-1 Paso 0) |
| **D11 (nuevo)** fecha de publicación de licitaciones | **NULL en todas** las creadas en 2026: el parser lee `FechaPublicacion` en el primer nivel y en v1 viene bajo `Fechas` [I sobre la ruta] | → F-datos-1 |
| D1-bis `tipo` pisado | **18.120 de 34.442** con `tipo` NULL (el listado no trae `Tipo` y `upsert_basica` lo pisa) | → F-datos-1 |
| D9 catálogo de organismos | `organismos` = **0 filas**; `instituciones_pac` = 1.335, todas con RUT | Confirmado |
| **D12 (nuevo)** Plan Anual casi vacío | `plan_compra_lineas` = **819 filas** (se esperaban ~300.000 tras la carga completa) | La búsqueda inversa del Plan Anual no tiene datos en producción. Causa [I]: el job `catalogos` corre primero en el mismo workflow; si falla, `plan-anual` no corre. Revisar el log de Actions del lunes 05-oct |
| M5 organismo seguido | perfil 5 sigue 75 organismos con valores tipo `7032` (`codigo_entidad`); las CA guardan RUT `61.975.800-5`; **0 matches por organismo** | Confirmado |
| D3 CA estados | `publicada` 24.113 (7.201 con > 30 días); `desierta` 84, `cancelada` 125 (llegan solo por detalle) | Confirmado |
| **D4 subido a ALTA** | CA sin fecha de publicación = sin fecha de cierre **en todos los estados** (las dos faltan juntas): `publicada` **8.295**, `cerrada` 53.625. Una CA `publicada` sin fechas **nunca es vigente** → no aparece en feed ni matching | Causa [I]: ítems del listado sin el bloque `fechas` + `upsert_ca_basica` pisa con None. Sonda en F-datos-1 |
| R4 crecimiento | Días hábiles: **~4.300 CA y ~550 licitaciones nuevas/día** (no 3.100). `compras_agiles` 73 MB / 125.291 filas | ≈ 3 MB/día → 70 % en ~2 meses, 100 % en ~4 (sin PAC). F-retencion-filas sube de prioridad |
| M8 duplicados | boris 426 de 5.701 oportunidades; alejandra 73 de 4.267 | Confirmado |
| **M1/M2** score | Con keyword y score < 40 (ocultas por el piso): **licitaciones 2.625 de 4.716 (56 %)**, **CA 5.804 de 7.205 (81 %)**. Sin keyword y ≥ 40: 385 licitaciones (por rubro), 43 CA (17 ≥ 60). Perfiles con 6,8–7,8 keywords en promedio | Confirmado y es **el problema de resultados más grande**: la mayoría de lo que calza por palabra queda oculto |
| M4 tope 500 | Solo el perfil 7 usa región (60 candidatas): **sin pérdida hoy** | Baja a BAJA (preventivo) |
| D5 detalle doble | 22.981 licitaciones con detalle y sin `raw_json`, pero **solo 75 con match** | Baja a BAJA |
| ~~D6~~ job `detalles` | 31.789 con detalle; pendientes ~2.650, de ellas 9 cerradas | **Retirado**: el job trae la descripción que usa el FTS de licitaciones; la cola está al día |
| D10 tipos | LE, LP, L1, LR, O1, CO, B2, E2, I2, LS; **LQ 11** (viejas); sufijo nuevo `R1` (245) | Parseo defensivo ya cubre; anotar `R1` |

## 8. Plan de fases propuesto

| # | Fase | Contenido | Modelo | Migración |
|---|---|---|---|---|
| 1 | **F-datos-1** | D1, D1-bis, D11, D3, D4, R1 (429 persistido), R2 (reservas), R3, R5, R6; D12 se diagnostica antes |  **Opus** (toca 429 y cuota) | posible (región de licitación) |
| 2 | **F-match-1** | §3.3 puntos 1–7 | Sonnet | no |
| 3 | **F-indices** | §3.4 (FK, GIN de expresión, ON CONFLICT, días calculados) | Sonnet | sí (solo índices) |
| 4 | **F-perfiles-1** | §4.4-1 y §4.2 abiertos de perfiles | Sonnet | no |
| 5 | **F-perfiles-2** | asistente + vista previa + sugerencias (§3.3-8) | Sonnet | no |
| 6 | **F-retencion-filas** | borrar CA/licitaciones terminales > 180 días sin match, guardado ni seguimiento; tratar como terminal la CA `publicada` sin fechas creada antes del 21-sep (8.295, histórico según la sonda de F-datos-1); alerta 70 % | **Opus** (borra datos) | no |
| 7 | **F-bandeja** | §4.4-3 y 5 | Sonnet | sí (marca de revisada) |
| 8 | Spikes de fuentes | OCDS (desfase, `tender` de activas, región/organismo), `COT_`, RFI, `q` v2, cierres masivos v1 | Cowork + `smoke_test` por Boris | — |
| 9 | **F-mcp-1** | MCP local de solo lectura | Sonnet | no (rol de BD a mano) |
| 10 | F-cierres · F-ficha-decidir · F-front-deuda | §4.4-4 y 6, §4.5 | Sonnet | no |
| 11 | F-ocds / F-mercado / F-match-2 | según los spikes | Opus/Sonnet | según el caso |
| 12 | F-mcp-2 | MCP remoto con tokens | Opus | sí |
| — | Pendientes | F-secretos, limpieza de Render, CI | — | — |

---

## 9. Paso a paso de cómo seguir

1. **Boris — revisar y subir la limpieza de docs.**
   - Cowork dejó un commit local `docs: auditoría integral 08-oct y reorganización de docs`, sin push.
   - Revisarlo con `git --no-pager show --stat HEAD` y luego hacer `git push`. Solo toca docs y comentarios, sin migración, así que se puede subir a cualquier hora.
   - **Si algo no te gusta:** `git reset --soft HEAD~1` y me dices qué cambiar.
2. **Cowork — escribir `data/paso0_auditoria.py`** con las consultas de §7, de solo lectura.
3. **Boris — correr el Paso 0 contra producción:**
   ```powershell
   $env:DATABASE_URL = ((Get-Content .env | Where-Object { $_ -match '^DATABASE_URL_PROD=' }) -replace '^DATABASE_URL_PROD=','' -replace '^["'']|["'']$','' -replace '^postgres(ql)?://','postgresql+psycopg://')
   $env:PYTHONIOENCODING = "utf-8"
   python data\paso0_auditoria.py 2>&1 | Tee-Object data\logs\paso0_auditoria.txt
   Remove-Item Env:DATABASE_URL
   ```
   - **Esperado:** 9 bloques con cifras.
   - **Si falla la conexión:** correr primero `python data\diag_conexion.py`.
4. **Cowork — con el Paso 0:**
   - confirmar o corregir D1, D3, D9, R4, M1, M4 y M8 en este documento;
   - redactar `docs/prompt-F-datos-1.md` (Opus) y `docs/prompt-F-match-1.md` (Sonnet).
5. **Boris + Claude Code — ejecutar las fases** en el orden de §8:
   - **instrucción:** "Lee docs/prompt-F-xxx.md y ejecútalo; `pytest -rs` con 0 saltados; commit sin push";
   - después, auditoría en Cowork y push. Si la fase tiene migración, el push va justo después de un `ca` de los :05.
6. **Boris — spike mínimo de fuentes**, en paralelo y sin cuota MP:
   - bajar `COT_2026-08.zip` del portal de Datos Abiertos;
   - decirme dónde quedó, para que yo lea sus columnas.
   - **Cowork en el mismo spike:** prueba el desfase de OCDS mes a mes. **Boris:** prueba un código `RFIPRI26` con `scripts/smoke_test.py`.
7. **Cowork — F-mcp-1:**
   - prompt con la lista de herramientas de §6;
   - SQL para crear el rol `mcp_ro`, que Boris corre una vez en Neon;
   - configuración para Claude Desktop.
8. **Decisión de Boris** antes de F-perfiles-2 y F-bandeja: aprobar o ajustar la navegación de §4.4. Puedo maquetarla en un mockup si ayuda.

*Fuente de los datos de dominio: Dirección ChileCompra.*
