# Estado actual — mp-oportunidades (app: Rastrillo)

*29-oct-2026. Documento corto y vivo: se reescribe, no se apila. La bitácora larga de jul–sep
quedó en `archivo/00-estado-actual.md` y los handoffs de sep en `archivo/`.*

**Al retomar:** leer este archivo → `14-auditoria-integral.md` (auditoría vigente y plan) →
`decisiones.md`. Reglas duras: `../CLAUDE.md`.

## En producción
- `main` = `origin/main`. Última fase: **F-perfiles-1** (`08cabc8`, 09-oct; `/perfiles` de 1,66 MB a 53 KB,
  pausar perfil, formulario único bajo demanda, `/cuenta`; probada en navegador por Boris).
- Fases de sep–oct: F-vigencia · F-ca-explorar · F-ca-vocab · F-acentos · F-guardar · F-ajustes ·
  F-registro · F-ficha-modal · F-datos-1 · F-indices · F-perfiles-1. Detalle de cada una en `app/changelog.py` y en
  `archivo/prompts/`.
- Lo que hace la app: perfiles (keywords, regiones, montos, exclusiones, rubros UNSPSC,
  organismos), matching con score, feed solo vigente con filtros y ficha en modal, Guardar /
  Descartar (solo o excluyendo términos) / Archivar con Deshacer, Mi registro, explorador de
  Compras Ágiles con rubros favoritos, Plan Anual con búsqueda inversa, competencia al adjudicar,
  resumen diario + alertas de guardadas, `/salud` y admin.

## F-equipo (5-nov, commit local sin push hasta auditar; migración `a8d4f2c6e9b1`, cuelga de `f6b2d9a4c1e8`)
Trabajo en equipo sobre las CA, alarmas dentro de la app y sin correo (`prompt-F-equipo.md`).
**Esquema:** `ca_trabajo` (estado compartido por empresa × CA: `nueva` = sin fila, `priorizada`, `cotizando`, `lista`,
`enviada`, `descartada` con motivo; responsable; `version` para concurrencia; `resultado`/`resultado_en`), `ca_eventos`
(solo se agrega; `usuario_id` NULL = sistema, único por empresa × CA × tipo con índice parcial; columna `destinatario_id`
para asignada/mención, así los no leídos no leen JSON), `ca_comentarios` (hilo por CA; eliminar es lógico),
`preferencias_alarma` (sin fila = activa), `usuarios.nombre` y `usuarios.actividad_leida_hasta`.
**Código:** `app/mercado/equipo.py` (transiciones con evento en la misma transacción; conflicto de versión con FOR UPDATE y
UNIQUE; menciones resueltas en servidor contra los activos de la empresa; `consulta_no_leidos` sin fan-out (sin
`actividad_leida_hasta`, solo cuenta lo de los últimos 7 días; también el punto de comentarios de la tarjeta);
`generar_alarmas` idempotente; resultado con `elegir_por_rut` y `rut_competidor`; `purgar_eventos`),
`app/api/routes/equipo.py`, `static/equipo.js` (409/422 se pintan, título con contador, Enter/Shift+Enter, autocompletar @).
**UI:** campana en las dos barras (un sondeo de 60 s a `/actividad/contador`, que de paso genera las alarmas a lo más una
vez por minuto por empresa con marca en `sync_state`), `/actividad` (registro paginado con filtros), bloque de equipo y
panel lateral del hilo en cada tarjeta de `/mesa` (filtro «Equipo», descartadas ocultas por defecto), bloque «Equipo» e hilo
en la ficha, bloque «Equipo» en «Hoy», interruptores en `/cuenta`, nombre visible en `/admin/usuarios`.
**Jobs:** `mercado-ca` y `mercado-backfill` (workflow `mercado-nocturno`) generan las alarmas de todas las empresas activas
al final (sin API; un fallo no rompe la ingesta). **Retención:** `ca_eventos` > 12 meses se purgan; una CA con `ca_trabajo`
no se purga. **Tamaño:** ~100 B por evento [I, a medir con `tests/test_equipo_pg.py` contra Postgres; el test exige < 200 B].
**Pendiente de Boris:** (1) `alembic upgrade head` en la base de tests local y `python -m pytest -rs` completo (el
clasificador no deja al asistente migrar ninguna base); (2) deploy con migración (pasos en el prompt §8); (3) nombres
visibles de Mario y Gonzalo en `/admin/usuarios` y que estén activos en la misma empresa; (4) prueba con dos navegadores.

## F-plat-comp (5-nov, commit local sin push hasta auditar; migración `f6b2d9a4c1e8`, cuelga de `e4a7c2f9d1b3`)
Análisis de competencia (`prompt-F-plat-comp.md`, con su §6 «Revisión 5-nov»).
**Esquema:** `competencia_mes` (RUT × `clave_comprador` × familia × mes: CA, ofertas, ganadas, sin dato, log_ratio con
suma/n/p50, monto ganado, un oferente, razón social, EMT y ubicación precalculada de `oc_lineas_ca`) y
`competencia_comprador_mes` (comprador × familia × mes: CA, desiertas, oferentes, un oferente, «no ganó el más barato»).
Derivadas: el job borra y reinserta por mes. Sin índice `(organismo_rut, familia)`: ninguna consulta lo usa.
**Código:** `app/mercado/eleccion.py` (**`elegir_por_rut`: única regla F-merc-1c**; reemplaza las 3 copias de
`comprador`, `indice` y `adjudicacion`), `app/mercado/comprador.py` (`rut_competidor`: `normalizar_rut` con caída a
`indice._rut`; también en el top de proveedores de la mesa), `app/mercado/competencia.py` (adaptador genérico
`_filas_desde_ca` → `Proceso`/`Oferta`, `recalcular_competencia`, consultas de la UI, `variables_competencia` registrada
como grupo `competencia`, juez de competidores, aporte al modelo, job), `app/api/routes/competencia.py`.
**UI:** `/competencia` (buscador, ranking del territorio, competidores de un comprador × familia), `/competencia/{rut}`
(perfil con Wilson, tendencia en SVG inline, cara a cara por mercado), `/estrategia` (mapa filtrable y ordenable);
regiones de la empresa; enlace desde el historial de la mesa; menú «Competencia» y «Estrategia».
**Job `mercado-competencia`:** en `mercado-indices.yml` después de `mercado-indices` (timeout 60). Medido (sintético, Postgres
local, 1.200 CA/mes × 7): diario 5,3 s. **Tamaño:** 241 B/fila (tras VACUUM); 12 meses ≈ 24 MB con el sintético (peor caso,
sin repetición RUT × comprador) y ≤ ≈ 20 MB con el volumen real de §6.1 (cota sin agregación). El job avisa si pasa de 20 MB.
**Juez y modelo:** con los datos de hoy, «Brier con/sin: pendiente, 0 meses evaluables» (variables fuera del modelo); predicción de
competidores solo para noviembre (entrenada con octubre) y parcial.
**Pendiente de Boris:** (1) deploy con migración (pasos en el prompt §5); (2) `mercado-competencia --completo` desde el PC
contra producción y pegar: cuadratura por mes, top 1 / precisión / recall de noviembre, `ruts_no_validos` y
`ruts_forma_distinta`, `bytes`; (3) revisar `/estrategia` y 5 perfiles conocidos; (4) decidir trimestral si `bytes` > 20 MB.

## F-mesa-v0 (5-nov, commit local sin push hasta auditar; sin migración)
`/mesa` (`prompt-F-mesa-v0.md`): CA vigentes (`publicada` + cierre futuro) de `regiones_activas`, 50 por página,
orden por cierre y, si la empresa tiene `comunas_prioritarias`, primero las que nombran alguna en `direccion_entrega`
(sin tildes, por palabra completa; es [I]). Filtros GET con conteo HTMX (`/mesa/conteo`): familia, cierre
hoy/mañana/esta semana (hasta el domingo; días de Chile), detalle, anexos, monto mínimo, texto (ILIKE escapado; no hay
FTS para el nombre de la CA). Historial del comprador (`/mesa/ca/{codigo}/comprador`, HTMX al abrir, 12 meses: n.º de CA,
monto adjudicado, mediana de oferentes = RUT distintos, top 3 de proveedores). Sin raw_json en ninguna query.
**Código:** `app/api/routes/mesa_dia.py` (rutas), `app/mercado/mesa_dia.py` (consulta, filtros, tarjetas),
`app/mercado/comprador.py` (**`clave_unidad`: única definición**, movida desde `adjudicacion.py`, que la importa;
`historial_comprador`). «Hoy»: tarjeta «CA vigentes» (total y cierran hoy) con enlace a `/mesa`; menú «Mesa» tras «Inicio».
**Rendimiento:** medido solo en SQLite con datos sintéticos (250 vigentes, 3.000 CA en el año): `/mesa` ≈ 21 ms,
historial ≈ 9 ms. **Pendiente: medir en Postgres** (el historial filtra `organismo_rut` con `upper(replace(...))`,
sin índice; si pasa de 300 ms, proponer índice = migración).

## F-plat-4 (5-nov, commit local sin push hasta auditar; migración `e4a7c2f9d1b3`, cuelga de `c5f1a8d3e7b9`)
Precio esperado, P(ganar) y evaluación walk-forward (`prompt-F-plat-4.md`, con su Revisión 5-nov). Solo backend: la UI es F-plat-5.
**Esquema:** `precio_indice` (producto × `comuna:|provincia:|region:` × mes; el país se lee de `precio_referencia`),
`modelo_adjudicacion` (logístico y línea base, uno `activo`), `evaluacion_modelo` (métricas por mes; «ganables» por empresa),
`compras_agiles.comuna_inferida`/`comuna_fuente` (`oc|texto|NULL`; se escriben con `actualizado_en` fijo).
**Código:** `app/catalogos/territorio_nuble.py` (comunas → provincias, claves de `PROVINCIAS_NUBLE`; lista [I] a confirmar),
`app/mercado/indice.py` (unión ofertas × líneas `ok`, `referencia_precio` jerárquica con los 3 meses anteriores),
`app/mercado/adjudicacion.py` (logística L2 en Python puro, registro de variables para F-plat-comp, `p_ganar`,
`curva_p_ganar`, `evaluar_ca`), `app/mercado/evaluacion.py` (walk-forward y job), `query.py` (`evaluacion_ca`,
`resumen_evaluacion_modelo`), bloque en `/salud`.
**Job `mercado-indices`** (`mercado-indices.yml`, 07:45 Chile, sin crear en cron-job.org): índice de los últimos 3 meses,
evaluación del último mes cerrado, reentrena y activa. `--completo`: índice entero y evaluación de 6 meses.
**Medido (sintético, PC):** ajuste logístico de 168 mil ofertas y 62 columnas en 21 s (11 iteraciones), base en 4 s →
`--completo` ≈ 7 ajustes ≈ 2,5 min de entrenamiento; diario ≈ 2 ajustes. Sin `numpy`.
**Números reales de la evaluación:** pendientes de la corrida de Boris (`--completo` contra producción, paso 1 abajo).
Sin migrar no se puede correr en Neon y el asistente no aplica migraciones en bases reales.
**Pendiente de Boris:** (1) migración según `despliegue.md` y `python -m app.ingest run-once --job mercado-indices --completo`
desde el PC; (2) pegar por mes Brier (≥ 2 ofertas, ex ante), intervalo 80 %, ganables, cobertura y `descartadas_anexo`;
(3) crear `GH mercado-indices` a las 07:45 y medir la duración; (4) confirmar la lista de comunas por provincia;
(5) decidir con los números si F-plat-5 entra en sombra con el logístico o con la base.

## F-plat-2 (4-nov, commit local sin push hasta auditar; migración `c5f1a8d3e7b9`)
Anexos de CA → líneas con extracción asistida (OpenRouter) y revisión humana (`17-plan-pivote-plataforma-ca.md` §2 C3).
**Esquema:** `ca_anexos` (metadatos + `texto_extraido` ≤ 50.000; **sin el binario**: se procesa en memoria), `llm_uso`
(gasto del mes, upsert) y `llm_cache` (clave = sha256 de modelo + versión del prompt + sha256 del texto e imágenes),
`ca_lineas.anexo_id`, `compras_agiles.anexo_resuelto_en`.
**Flujo:** ficha de CA (pestaña «Líneas», visible si hay líneas o `n_documentos > 0`) o `/catalogo/ca/{codigo}` → «Subir anexo»
(`POST /oportunidad/compras_agiles/{codigo}/anexo`; PDF/XLSX/DOCX/PNG/JPG ≤ 10 MB validados por firma) → texto con pdfplumber
(tablas con `|`), openpyxl o python-docx; PDF sin capa de texto → PNG de las primeras `LLM_MAX_PAGINAS_IMAGEN` (5) páginas;
si pdfplumber no abre el PDF, respaldo con pdfium → OpenRouter → líneas `origen=anexo`, `estado=dudosa` **siempre** (`n ≥ 1000`).
Reemplazan los placeholders y las líneas de anexo no confirmadas; no tocan `ok`, API ni manuales. Tabla editable (producto,
formato, cantidad), «Confirmar fila/todas», «Eliminar fila», «Agregar fila», **«Anexo resuelto»** (borra placeholders).
**Bandera:** solo el placeholder (`anexo` + `pendiente`) deja `sin_detalle`; las líneas leídas por confirmar → `incompleto`.
`mercado-catalogo` no recrea placeholders con `anexo_resuelto_en` ni toca líneas con `n ≥ 1000`.
**Sin modelo, sin key o sin presupuesto:** anexo `sin_presupuesto`, misma tabla vacía y flujo manual.
**Guardas de memoria (512 MB):** DOCX/XLSX que se expanden > 50 MB o con un miembro comprimido > 100:1 se rechazan (solo se lee
el directorio del ZIP); las páginas se renderizan con el lado mayor ≤ 2.000 px (máx. 150 dpi), lo que también baja tokens.
**Lista de precios por foto/PDF** (`/proveedores/{id}/lista-precios`): mismo extractor con esquema `precios`, vista previa
editable y confirmación = `importar.aplicar` de F-plat-3 sobre ese proveedor (`fuente=lista_llm`, con historial).
**§1.6 (descarga por API):** escrito y apagado (`DOCUMENTOS_API_HABILITADO=false`) hasta la sonda `--documentos`; la ruta es [I].
**Medido en la muestra (32 archivos, 4-nov):** 16 PDF van como imagen (15 sin capa de texto + 1 que pdfplumber no abre y pdfium sí),
9 PDF con texto, 6 DOCX, 1 XLSX, 1 vacío; `texto_extraido` mediana 0 B (escaneados), máx. 19 KB, total 56 KB.
**Pendiente de Boris:** sonda `--documentos`; key en `.env` y Render; `evaluar_extraccion.py` y fijar `LLM_MODELO_EXTRACCION`
(anotar en `decisiones.md`); revisar el bundle del buscador con navegador real (CloudFront bloquea `curl`); medir minutos por CA.

## F-plat-ui (4-nov, commit local sin push hasta auditar; sin migración)
- Diseño nuevo de la plataforma (estilo dashboard sobrio, tokens `--c-*` en `app.css`, Inter autoalojada, barra lateral con
  íconos y offcanvas en móvil, tablas/botones/badges/formularios restilizados sobre Bootstrap 5.3). Decisión en `decisiones.md`.
- Las pantallas de Archivo solo heredan el layout. El tutorial del feed antiguo queda solo para el admin en Archivo.
- Pendiente de Boris: revisar a 1366 px y en el celular (portada, catálogo, proveedores, login) y ajustar colores/densidad.

## F-ca-presente (4-nov, commit local sin push hasta auditar; sin migración)
- [V] El listado de CA (`_sync_en_ventanas`, usado por `mercado-ca` y `ca`) perseguía al reloj: al llegar al presente
  abría otra ventana de ~5 min cada vuelta, 100–150 requests por corrida (log del 4-nov, 14:50–15:00 Chile).
- [I] Puede explicar parte de los 429 diurnos desde el 18-oct (ver `15-diagnostico-ca-429.md` §7).
- Fix: `limite` se fija una vez por corrida (va en el resumen) y no se abre una ventana que avance < 1 min.
  Esperado tras el push: 1–5 `requests_usadas` por corrida de `mercado`.

## F-429-tasa (29-oct, commit local sin push hasta auditar)
Desde ~18-oct la API manda un 429 nuevo, `codigo: "429"` ("límite de tasa"), que la app trataba
como tope diario: el día quedaba bloqueado y `ca` se atrasó ~2 días (`15-diagnostico-ca-429.md`).
F-429-tasa lo trata como transitorio: pausa de 20 min persistida en `sync_state` (`api_pausa`),
la corrida corta guardando el avance y Actions muestra `::warning::`. Ningún 429 agota el día.
Al desplegar el mismo día de un 429 ya marcado: `UPDATE quota_log SET requests_usadas = <consumo
real> WHERE fecha = '<hoy>';` en Neon (una vez).

**F-429-pausa-ca** (29-oct, commit local sin push hasta auditar, sobre F-429-tasa): un job con API
que arranca en pausa espera hasta 15 min (`ESPERA_PAUSA_MAX_MIN`) o sale **omitido** (0,
`::warning::`, fila `omitido`), en vez de quedar rojo con 0 requests. Ojo con los timeouts
(sin tocar): en el peor caso teórico `ciclo-match` (75 min) y `ciclo-activas` (150 min) ya no
cubrían la espera de lock de todos sus jobs; la pausa suma hasta 15 min a cada job con API.

## F-plat-nav (4-nov, commit local sin push hasta auditar; sin migración)
La plataforma es la app: `GET /` es la portada «Hoy» (conteos de CA abiertas de regiones activas, líneas `pendiente|dudosa` y
proveedores/productos de la empresa; sin empresa → aviso, admin sin empresa → «Hoy» con aviso). El feed antiguo se movió a
**`/archivo`** (mismos filtros; `_url_feed`, `action` del panel, `next` y redirects apuntan allí). Dependencia `requiere_admin_archivo`
(`deps.py`) en todas las rutas antiguas: GET de no admin → 303 a `/`, POST → 403; `/organismos/catalogo.json` → 403. Siguen abiertas
`/cuenta*`, `/perfiles/rut-proveedor` y la ficha `/oportunidad/compras_agiles/{codigo}` (solo con empresa activa y **sin** acciones
guardar/descartar para no admin); la ficha de licitación es solo admin. Menú: Inicio, Catálogo, Proveedores, Logística, Cuenta; admin
suma Usuarios, Empresas, Salud y el desplegable Archivo. No se borró nada. La API JSON `/api/oportunidades` y `/api/perfiles*` no se tocó
(sigue para cualquier usuario autenticado, propio dueño): decidir en F-retiro. Retiro definitivo y F-plat-ui: ver el prompt.

## F-plat-3 (4-nov, commit local sin push hasta auditar; migración `d3b7a9e1c5f8`)
Empresas con territorio priorizado, proveedores y costos con historial (pivote, `17-plan-pivote-plataforma-ca.md` §2 C4).
**Esquema:** `empresas`, `usuarios.empresa_id`, `proveedores`, `proveedor_productos` (precio neto por formato; `producto_id`
sin FK hasta F-plat-1), `costo_historial` (append-only), `tarifas_logistica` (3 provincias de Ñuble en 0 al crear la empresa).
**Territorio:** `app/mercado/territorio.regiones_activas` = unión de las regiones de las empresas activas (fallback
`MERCADO_REGIONES`); la usan `mercado-ca`, el backfill, `mercado-oc`, la purga y `reabrir_ofertas_faltantes.py`.
**Rutas:** `/admin/empresas` (admin: crear, editar, activar, asignar usuarios; el selector de empresa también está al crear
usuario) y `/proveedores` (lista, ficha con variación vs anterior y vs 90 días, formularios HTMX, importar XLSX/CSV con
vista previa, plantilla descargable, `/proveedores/logistica`). `mejor_costo` (backend, lo usa F-plat-4). Menú «Mesa».
**Permisos:** todo filtra por `empresa_id` de la sesión; un id de otra empresa responde 404; un usuario sin empresa activa
ve un aviso (403 en las demás rutas), también el admin. **Dependencia nueva:** `openpyxl` (lectura/escritura de planillas).
**Pasos de Boris:** ver el prompt §4 (migración fuera de 22:00–07:00 con `GH mercado` pausado; crear la empresa con región 16,
asignarse y asignar a Mario; cargar 3 proveedores y ~40 productos; medir el tiempo).

## F-merc-3 (4-nov, commit local sin push hasta auditar; migración `b6e2d8a4f1c7`)
Detalle de las CA **vigentes** en `mercado-ca` (antes solo se pedía el de las cerradas; sin él una abierta no tenía `ca_productos`
ni `ca_lineas`). Columnas `compras_agiles.detalle_vigente_en` y `n_documentos` (esta última también la guarda la ruta de cerradas).
`cola_vigentes` (publicada, cierre futuro, sin detalle o con `detalle_vigente_en < fecha_ultimo_cambio`; primero las sin detalle,
luego cierre asc; espera por fallos) corre **antes** que la de cerradas, con tope propio `MERCADO_VIGENTES_MAX` (60) y
`MERCADO_VIGENTES_MINUTOS` (4) dentro de los 10 min totales. Solo `upsert_ca_detalle` + `n_documentos` + `detalle_vigente_en`: **no**
toca `ca_cotizaciones` ni `cotizaciones_obtenidas_en` (si lo hiciera, la cola de cerradas podría no volver a pedir la CA tras el
cierre y se perdería el ganador). Un 429 en vigentes corta la corrida sin pedir cerradas. Resumen: `vigentes_cola`,
`vigentes_detalles`, `vigentes_pendientes`. El cursor de `mercado-catalogo` suma `detalle_vigente_en`.
**Volumen medido (4-nov, Neon prod) [V]:** CA de región 16 publicadas por día (hora de Chile, últimos 30 días): mediana 142,
p90 196, máximo 239; abiertas al momento: 252. El tope (60 por corrida × 4 por hora = 240/h) cubre de sobra el ritmo diario
(~6–10/h en promedio). La primera puesta al día tras el deploy tarda ~5 corridas (~75 min). Las re-descargas por cambio
(`detalle_vigente_en < fecha_ultimo_cambio`) no están medidas [I]: mirar `vigentes_detalles` por día los primeros días.

## F-plat-1 (4-nov, commit local sin push hasta auditar; migración `a9d4c7e1f2b6`)
Catálogo canónico y líneas 1:1 por Compra Ágil (pivote, `17-plan-pivote-plataforma-ca.md` §2 C2). **Sin modelo de lenguaje:**
alias aprendidos + palabras clave; el LLM entra en F-plat-2.
**Esquema:** `producto_canonico`, `producto_formato` (todo producto nace con su formato base, factor 1), `producto_alias`
(`texto_normalizado` único), `ca_lineas` (`UNIQUE (ca_codigo, n)`, sin FK a `ca_productos` porque la ingesta los reinserta;
agrega `texto_norm` para re-aplicar un alias con un UPDATE), `compras_agiles.detalle_lineas` (la bandera «CA sin detalle») y FK de
`proveedor_productos` a producto y formato (los `producto_id` sueltos de F-plat-3 se ponen en NULL al migrar). Datos comunes, sin purga.
**F-plat-1-fix** (misma migración, aún no en Neon): `producto_canonico.clave_semilla` (único; slug del nombre ORIGINAL de la regla, estable
aunque se renombre) decide si la semilla ya sembró; un producto creado a mano con el mismo nombre se adopta; las reglas se resuelven por
clave. La bandera `detalle_lineas` es solo «la CA no trae el detalle»: `sin_detalle` = sin líneas o con línea `anexo` sin resolver;
`incompleto` = alguna línea `pendiente`/`dudosa`; `completo` = todas `ok` (F-plat-2 baja anexos solo de las `sin_detalle`).
**Observaciones:** cualquier usuario con empresa puede editar y desactivar un producto del catálogo común (fusionar es solo admin; ok con 2
usuarios, revisar al sumar empresas); las líneas son solo de alimentos (`G_ALIM`), F-plat-5 no tendrá líneas de otros rubros; cuando entre
F-merc-3, sumar `detalle_vigente_en` a la condición del cursor de `mercado-catalogo` (reemplazar `ca_productos` no siempre mueve `actualizado_en`).
**Reglas puras** (`app/catalogos/canonico.py`): `normalizar_texto`, `extraer_formato`, `detectar_sin_detalle`, `sugerir_producto`
(alias exacto 1,0 / contenido 0,8 / palabra clave de la canasta 0,5; ≥ 0,8 `ok`, 0,5–0,8 `dudosa`, resto `pendiente`).
Canasta en `app/catalogos/canasta_alimentos.py` (80 productos, generada con `scripts/mercado/generar_canasta_alimentos.py`
desde el Excel: si Boris llena `decision_boris`/`nombre_final_boris`, se regenera). **Con solo reglas todo sale `dudosa`:**
las ≥ 0,8 son alias que enseña la curación humana.
**Job `mercado-catalogo`** (sin API; `python -m app.ingest run-once --job mercado-catalogo [--inicial]`; corre tras `mercado-ca` en
`mercado.yml`): siembra lo que falta (nunca renombra ni desactiva) y crea/actualiza líneas de las CA de las regiones activas, por lotes
de 500 CA, incremental con el cursor `sync_state` `mercado_catalogo`. Solo líneas de alimentos (`G_ALIM`); el texto de las ofertas con el
mismo `codigo_producto` es segunda fuente. Una línea revisada por una persona nunca se pisa: si cambia su texto queda `dudosa`.
**`/catalogo`** (usuarios con empresa activa): Pendientes (por monto de la CA), Productos (crear, editar, formatos, fusionar solo admin),
Alias; `/catalogo/ca/{codigo}` (corregir y agregar líneas a mano); pestaña «Líneas» en la ficha de la CA (solo regiones activas).
**Juez:** `python scripts/mercado/walk_forward_canonico.py` (solo lectura; cobertura (a) y (b), error mediano, 10 peores productos).
**Pasos de Boris:** ver el prompt §4 (migración con `GH mercado` pausado; `--inicial` desde el PC; una hora en Pendientes; pegar el juez).

## F-merc-1c (4-nov, commit local sin push hasta auditar; migración `b8d4f1a6c2e9`)
`mercado-backfill` ya no termina al primer 429 de tasa: corta el tramo guardando el avance, lee el fin de la pausa
de Postgres, duerme hasta ese fin + 30–90 s y sigue en otro tramo, hasta `MERCADO_BACKFILL_MAX_PAUSAS` (12; 0 =
comportamiento anterior), las 06:45 o el tope de minutos (que cuenta el sueño). Retiene el lock mientras duerme. El
resumen suma `pausas`, `minutos_esperados` y `requests_por_tramo` (dato para la sonda H1). `ca_cotizaciones` pasa a
una fila por **oferta** (UNIQUE por `id_cotizacion`): un RUT puede mandar varias a una CA [V, sonda 4-nov] y antes se
perdía ~4 %. Contadores `ofertas_mismo_rut` y `ofertas_identicas`. Observatorio y bandas de precio: competidor =
su oferta más barata, ganador = la seleccionada, oferentes = RUT. `mercado-oc` deja de avisar por `tieneItems`.
**Pasos de Boris:** (1) push; variable de Actions `MERCADO_BACKFILL_MAX_PAUSAS` vacía (usa 12) o `0` para volver a
cortar al primer 429; (2) `alembic upgrade head` en Neon prod (Alembic no lee `.env`: exportar la URL) y después
`python scripts\mercado\reabrir_ofertas_faltantes.py` (dry-run: verifica si `total_ofertas` cuenta ofertas con las
CA de la sonda e imprime cuántas CA y requests costaría); si es razonable, repetir con `--aplicar`; (3) cron-job.org:
confirmar el disparo de las 22:30 y crear un segundo `GH mercado-nocturno` a las 03:40 Chile; (4) a la mañana
siguiente pegar el resumen de las dos corridas (`pausas`, `requests_por_tramo`, `meses_completos`) y `/salud`.

## F-merc-2 (4-nov, EN PRODUCCIÓN; migración `a7c3e9d1b4f2`)
**Hecho (4-nov):** carga inicial `--desde 2024-10` en producción: 26 meses, 108.051 líneas (50,5 MB) y 54.674
referencias (6,6 MB); juez exacto ($681.979.119, 1.516 OC, 105 compradores) y contadores defensivos en 0; disparo
diario a las 07:20 Chile en cron-job.org. Detalle en `decisiones.md`. Lo que sigue de los pasos de abajo es historia.
Órdenes de compra de CA de `MERCADO_REGIONES` desde datos abiertos (`oc-da`, sin ticket ni cuota) a las tablas
`oc_lineas_ca` (PK `(codigo_oc, id_item)`, `IDItem` [V]) y `precio_referencia` (p25/mediana/p75 nacional por `UNSPSC|base` y mes). Job
`mercado-oc` (lock de siempre; `--desde AAAA-MM` para la carga inicial; workflow `mercado-oc.yml`, disparo diario
07:20 Chile desde cron-job.org, creado). `resumen_mercado(region, desde, hasta)` en `app/api/query.py`
(lo consume F-plat-5). `/salud` muestra filas y tamaño de las 4 tablas de mercado. Sin purga (excepción regla 11).
**Pasos de Boris:** (1) `alembic upgrade head` (el asistente no lo aplica); (2) HEAD de `oc-da` desde Actions (si da
403, todo desde el PC: desde este PC el HEAD de `oc-da/2026-9.zip` dio 200, 96 MB); (3) carga inicial
`python -m app.ingest run-once --job mercado-oc --desde 2024-10`; (4) juez: `resumen_mercado(16, '2025-10',
'2026-09')` debe dar `total12` = $681.979.119 ±0,5 %, 1.516 OC y 105 compradores (`resumen.json` → `alimentos`);
(5) medir `pg_total_relation_size('oc_lineas_ca')` (/salud) y anotarlo en `decisiones.md`; (6) revisar en las notas del
mes (`sync_state` `mercado_oc:AAAA-MM`): `sin_id_item`, `movidas_de_otro_mes`, `moneda_distinta`.

## F-merc-1 + F-merc-1-fix (3-nov, commits locales sin push; F-merc-1 auditada; migración `e5b9c3a7d2f4`)
**F-merc-1-fix:** horizonte del backfill desde 2024-11 (barrido de la sonda [V]: 2025-01 = 362, 2024-10 = 0); un mes
`completo` cuenta como "con datos" para la protección "revisar filtros" (si no, desde la segunda noche tres meses
viejos vacíos cortaban y se re-listaban cada noche); `_job.yml` pasa también `MERCADO_BACKFILL_ESTADOS_DETALLE`,
`MERCADO_PRODUCTOS_SOLO_GRUPOS` y `MERCADO_BACKFILL_TOPE_BD_MB`. Pasos de Boris en `prompt-F-merc-1-fix.md` §4.

Cotizaciones de CA de `MERCADO_REGIONES` (Ñuble, 16) a Neon: `ca_cotizaciones` (oferta, `seleccionado`
nullable, `llamado`, `id_oc`) y `ca_cotizacion_productos` (precio unitario neto), más 8 columnas nullable en
`compras_agiles`. Una sola key (`MP_TICKET`, cuota, pausa y lock de siempre, sin reserva). Jobs: `mercado-ca`
(cursor `mercado_ca`, listado por región **sin `estado`**, cola de detalle con tope 120 / 10 min) y
`mercado-backfill` (22–07 Chile, ventanas de un día, cursor por mes `mercado_backfill:AAAA-MM`, para a las 06:45,
a los 300 min, con cuota − 300 o con la base en 350 MB; ~~un 429 corta guardando el avance~~: desde F-merc-1c espera la
pausa y sigue). Workflows `mercado.yml` / `mercado-nocturno.yml` (solo
dispatch; crons **sin crear**). Retención no purga las CA de la región. `productos_cotizados` solo de CA con algún
producto en los grupos del observatorio: el estimado pasa de 120 MB (ver `decisiones.md`, 3-nov; **decisión
pendiente de Boris**). Exportación: `scripts/mercado/exportar_cot_api.py` (no en `data/`, que está en
.gitignore). Sonda `scripts/smoke_test.py --mercado` (3-nov [V]): marzo 2026 = 600 CA en la primera semana, oct-2024
= 0; ahora trae además el barrido del horizonte 2025. Backfill solo de `proveedor_seleccionado` (desviación,
`decisiones.md`); horizonte `MERCADO_BACKFILL_PRIMER_MES`; mes terminado sin CA → cursor `vacio`, no se repite.
Migración ya aplicada en Neon dev.
**Pasos de Boris:** (1) `python scripts/smoke_test.py --mercado` otra vez, pegar el barrido y fijar la variable de
Actions `MERCADO_BACKFILL_PRIMER_MES` en el primer mes con total > 0; (2) —; (3) migración en Neon prod, push y crear en cron-job.org `GH mercado`
(cada 15 min) y `GH mercado-nocturno` (22:30); (4) decidir horizonte o alcance de productos (tamaño); (5) tras la
primera noche: `/salud` (tamaño), `python scripts/mercado/exportar_cot_api.py` y el observatorio `informe`.
El puente `backfill_cot_nuble.py` deja de hacer falta cuando corra `mercado-nocturno` (no correr ambos a la vez:
comparten cuota y pausa).

## Siguiente
**Plan confirmado (3-nov):** `17-plan-pivote-plataforma-ca.md` (plataforma de intermediación de CA: líneas 1:1,
anexos, proveedores y costos, P(ganar), mesa del día, demanda) con prompts `prompt-F-plat-1..7.md` y reglas 24–26
en `CLAUDE.md`. Orden: **F-merc-1 (Opus, ahora)** → F-merc-2 (Sonnet) → F-plat-3 (Sonnet) → F-plat-1 (Opus) →
F-plat-2 (Opus) → F-plat-4 (Opus) → F-plat-5 (Sonnet, 4 semanas en sombra) → F-plat-6 (Opus) → F-plat-7 (Sonnet).
Pendientes de Boris para las fases plat: 30 anexos reales con sus líneas, cuenta OpenRouter, 3 proveedores y
tarifa por provincia, canasta de 80 productos, confirmar en la guía v3.0 si hay API de oferta.

**Pivote (31-oct):** mesa de precios de Compra Ágil, piloto Ñuble, **alimentos como giro principal** y
otros rubros como expansión → `16-pivote-compra-agil-nuble.md` e informe en `data/mercado/informe/`.
Siguientes fases: ~~F-merc-1~~ (commit local, arriba; corregida a una sola key) y
~~F-merc-2~~ (`prompt-F-merc-2.md`: órdenes de compra de CA de Ñuble desde `oc-da` a la base; en producción, arriba).
Antes de habilitar el backfill: sonda humana `scripts/smoke_test.py --mercado`.
**Ingesta actual en pausa desde el 31-oct** (ver `decisiones.md`): apagar en cron-job.org los disparos de
`ca`, `ciclo-ca`, `ciclo-match`, `ciclo-activas`, `nocturno`, `resumen`, `catalogos` y `retencion`.
**Puente mientras no hay segunda key: F-merc-0b** (31-oct, commit local sin push hasta auditar): parser de
`proveedores_cotizando` / `productos_cotizados` en `mp_v2.py` (aditivo, se reutiliza en F-merc-1 §1.4) +
`scripts/backfill_cot_nuble.py` con `MP_TICKET`, desde el PC, solo 22–07 Chile, `--rps 0.5`, `reserva=0`
(la ingesta de la app está en pausa: disparos apagados en cron-job.org), persiste la pausa de 429 y de
noche espera su fin y sigue (F-merc-0b-fix: hasta 15 pausas y 6.000 requests por noche). Tras la sonda
del 31-oct (F-merc-0b-fix2, [V] en `01-analisis-api-mercado-publico.md` §10) lista cada día **sin `estado`**
(el filtro `estado=proveedor_seleccionado` devuelve 0) y filtra local; solo `--estados-detalle`
(`proveedor_seleccionado`) pide detalle. Salidas: `data/mercado/cot_api_{AAAA-MM}.csv.gz`, el listado completo de
CA de cualquier estado en `ca_api_listado_{AAAA-MM}.csv.gz` y el avance en `_backfill_estado.json`. El observatorio
(`scripts/mercado/mercado_observatorio.py`; sus salidas quedan en `data/mercado/`, fuera de git) prefiere `cot_api_*` a `COT_` por mes y agrega precio
unitario ganador/perdedores por producto de alimentos. **Backfill diurno (F-merc-0b-diurno, 2-nov):** `--diurno` permite correr de día mientras la ingesta de la
app esté en pausa, con tope de 0,5 req/s y 2.000 requests por corrida (regla 5, excepción en `CLAUDE.md`).
**Pasos de Boris:** (1) `--diurno --meses 2026-08 --max-requests 300` y pegar la salida (el resumen indica el
modo y trae el control de calidad de ganadores por CA); (2) lo mismo con `--meses 2026-03`; (3) corrida
completa de noche sin flags (hasta 6.000 requests) o de día con `--diurno` (2.000 por corrida), y revisar los
429 del resumen. Las claves de la oferta son [V] (sonda 31-oct); `monto_total` con o sin IVA sigue [I].

Plan de fases en `14-auditoria-integral.md` §8 y paso a paso en §9. En corto:
1. ~~Paso 0 de la auditoría~~ hecho el 08-oct (resultado en `14-auditoria-integral.md` §7-bis).
   **D12:** Plan Anual en pausa hasta el PAC 2027 (403 desde Actions, 200 desde el PC de Boris;
   ver `decisiones.md`).
2. ~~F-datos-1~~ (`647900a`), ~~F-indices~~ (`e8e064a`), ~~F-perfiles-1~~ (`08cabc8`): en producción.
3. **F-match-1** (Sonnet, `prompt-F-match-1.md`; F-datos-1 ya está en producción): relevancia
   separada de urgencia, organismos seguidos, exclusión solo en el título (§1.7), feed y resumen
   sin duplicados. Prompt revisado contra el código el 09-oct; listo para Claude Code.
4. **Medición de precisión del match** (después de 2–3 semanas de F-match-1 en producción): script de
   solo lectura que use guardadas (acierto) y descartadas (error) como etiquetas, por usuario, fuente y
   banda de relevancia (35/40–59/60+), y por dónde calzó (título/ítem/descripción, rubro, organismo).
   Sirve para recalibrar las constantes de F-match-1 con datos y no a ojo. Línea base: `data/logs/paso0_score_nuevo.txt`.
5. F-perfiles-2 → F-retencion-filas → F-bandeja → spikes de fuentes (OCDS, `COT_`, RFI) → F-mcp-1.

## Prompts pendientes
- `prompt-F-indices.md` — en producción (`e8e064a`, migración `b8c2d5e9a1f7`; 9 índices ~7,7 MB, candidatos en 7–45 ms).
- `prompt-F-perfiles-1.md` — en producción (`08cabc8`). Desvíos: `_parse_monto` sigue (lo usa Plan Anual),
  catálogo JSON responde 401 sin sesión, rubros favoritos también bajo demanda.
- `prompt-F-datos-1.md` — en producción (`647900a`).
- `prompt-F-match-1.md` — siguiente (Sonnet, sin migración).
- `prompt-F-ca-rubro.md` — próxima versión (rediseñar el prefiltro; ver auditoría 14).
- `prompt-F-secretos.md` — rotación de credenciales; la parte de código (`repr=False`) ya está.

## Pendientes operativos (Boris)
1. Limpieza de Render: crons viejos hacia `/api/jobs/run`, decidir si se apagan endpoint y
   scheduler, retirar o rotar `JOBS_TOKEN`.
2. Vencimiento anual del PAT de cron-job.org → GitHub (`operacion-disparos.md` §2).
3. Revisar CU-horas de Neon del mes y tamaño de la base (umbral 70 %).
4. Pendiente de F-guardar: confirmar `borrados` del primer `ciclo-match` y `/salud` sin ráfaga.
5. **Decidir si se adelanta el cursor de CA** (F-429-tasa; ~2 días atrasado al 29-oct): adelantarlo
   a "ahora − 2 h" y dejar el hueco para un relleno nocturno, o dejar que se ponga al día solo
   (~110 requests/h con `CA_TAMANO_PAGINA=10`).
6. Probar H1 del 429 (límite por IP de los runners): cuando `ca` reciba un 429/429, correr
   `python scripts/smoke_test.py` desde el PC en ese minuto.
7. F-tasa-respuesta (5-nov-2026): tras el deploy, mirar si siguen los 429 diurnos de `mercado-ca`. Si
   desaparecen, la causa inferida (espera medida desde el inicio de la petición, no desde el fin de la
   respuesta) queda verificada; si siguen, es otra.

## Backlog chico
**`ca` pide `estado=proveedor_seleccionado` en el listado y ese filtro devuelve 0 [V 31-oct]; revisar al
retomar la ingesta** (en pausa; no se tocó en F-merc-0b-fix2).

F-429-tasa: mostrar "API en pausa hasta …" (hora Chile) en `/salud` (hoy se ve la fila `api_pausa`
con el cursor en UTC); en `nocturno` un paso que falla queda como `None` sin aviso;
`alembic/env.py` que lea `.env`; ~~tests PG leen `os.environ` y no normalizan `postgresql://`~~ (resuelto en F-tests-rapidos: `tests/pg_url.py`, `TEST_DATABASE_URL`); `_job.yml` exige `DIGEST_HOUR`/`TASA_*`; `_run_with_lock` sin
fila `cancelado` y CLI sin SIGTERM; retención escribe JSON `null` en `raw_json`; campos del detalle
de CA sin usar (`presupuesto.moneda`, `fecha_cierre_segundo_llamado`, `proveedores_cotizando`);
`ruff format` marca 48 archivos heredados (no formatear en masa); tasas UF/UTM/USD/EUR fijas;
docstring de `condicion_lic_vigente` cita `tests/test_vigencia.py` (está en
`test_ajustes_pg.py`); mensaje de choque de exclusión no nombra la keyword; conteos de pestañas
de Mi registro sin filtros; pestaña Descartadas carga todo en Python.

*Fuente de los datos de dominio: Dirección ChileCompra.*
