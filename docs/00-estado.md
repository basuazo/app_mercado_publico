# Estado actual — mp-oportunidades

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

## F-plat-1 (4-nov, commit local sin push hasta auditar; migración `a9d4c7e1f2b6`)
Catálogo canónico y líneas 1:1 por Compra Ágil (pivote, `17-plan-pivote-plataforma-ca.md` §2 C2). **Sin modelo de lenguaje:**
alias aprendidos + palabras clave; el LLM entra en F-plat-2.
**Esquema:** `producto_canonico`, `producto_formato` (todo producto nace con su formato base, factor 1), `producto_alias`
(`texto_normalizado` único), `ca_lineas` (`UNIQUE (ca_codigo, n)`, sin FK a `ca_productos` porque la ingesta los reinserta;
agrega `texto_norm` para re-aplicar un alias con un UPDATE), `compras_agiles.detalle_lineas` (la bandera «CA sin detalle») y FK de
`proveedor_productos` a producto y formato (los `producto_id` sueltos de F-plat-3 se ponen en NULL al migrar). Datos comunes, sin purga.
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
