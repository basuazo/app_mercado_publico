# Estado actual — mp-oportunidades (handoff entre sesiones)

> **Cómo retomar en una conversación nueva:** pídele al asistente que lea
> `docs/00-estado-actual.md` y `docs/03-roadmap.md`. Con eso queda al día.

---

## Última sesión — 21-sep-2026 (auditoría UX y plan de rediseño del feed)

**Trabajo de diseño y planificación, sin cambios de código.** Auditoría UX/accesibilidad hecha
contra la FUENTE PRIMARIA (las plantillas, `presentacion.py` y `query.py`), no contra el sitio
renderizado: la URL de producción está bloqueada por la política de egreso del entorno del
asistente. Registro completo en `docs/13-auditoria-ux.md`.

### Verificado — cambia decisiones

**1. Dos escalas de score conviviendo en la misma pantalla.** Los presets del feed son Alta = 60 /
Media = `feed_min_score_default` (40) / Todas = 0, pero las bandas de color del badge son >=80
verde, >=50 ámbar. Un score 65 pasa "Alta relevancia" y sale ámbar. Se unifica hacia 60/40, no hacia
80/50, porque `feed_min_score_default` es ajustable por env y sigue pendiente de recalibrar con
datos de producción.

**2. El navbar está roto bajo 992px.** `base.html` declara `navbar-expand-lg` sin `navbar-toggler`
ni `.collapse.navbar-collapse`: los siete controles se desbordan en móvil. Bug de layout, en vivo.

**3. `index.html` no codifica `texto` en los enlaces de filtro.** `"texto=" ~ texto` sin
`|urlencode`: una búsqueda con `&` corrompe los filtros al primer clic en orden o agrupación.

**4. Los montos usan separador de miles estadounidense.** `'{:,.0f}'` produce `$12,500,000` en una
app chilena. Aparece en tarjeta, ficha, competencia y Plan Anual.

**5. No existe ninguna región `aria-live` en la app.** Tras los swaps de HTMX no se anuncia nada y
el foco vuelve a `<body>`. Es el único criterio WCAG 2.1 AA que la app incumple de forma inequívoca.
El contraste de color, en cambio, pasa AA en lo grueso porque Bootstrap viene calibrado: el problema
de accesibilidad es semántico y de estado, no cromático.

**6. El dashboard rediseñado necesita filtros que el backend NO tiene.** Existen fuente, perfil,
texto, relevancia y orden score/cierre. No existen monto, rango de cierre, familia de estado, orden
por monto, conteos por faceta, "nuevas hoy" ni paginación (eliminada a propósito en F-feed-agrupado
cuando el cap por grupo la reemplazó). `region` existe en la firma de `get_oportunidades_usuario` y
nunca se expuso — y solo aplica a Compra Ágil.

### Decisiones tomadas
- **Se queda en Bootstrap 5.3 + Jinja2/HTMX.** Sin migración a React. El rediseño es de plantillas
  sobre Bootstrap, no de stack.
- **Tres fases, en este orden:** F-ui-fixes (bugs y accesibilidad sobre lo que NO se rediseña, más
  los helpers puros de `presentacion.py` que las otras dos consumen) -> F-feed-filtros (backend
  puro, sin plantillas) -> F-feed-ui (reescritura de `index.html` y `_card_oportunidad.html` +
  `app/api/static/app.css` montado con `StaticFiles`).
- El prompt de F-feed-ui se escribe **cuando F-feed-filtros esté auditada**, para poder citar las
  firmas reales en vez de inventarlas.
- Los conteos por faceta se calculan **en Python sobre el conjunto ya cargado. Ninguna query SQL
  nueva**: seis agregados por carga contra Neon free es exactamente el patrón que ya agotó CU-horas.
- Paginación (20 por página) cuando `agrupar_por` es `"ninguno"` (valor nuevo); cap por grupo cuando
  el usuario agrupa. El default de la ruta sigue en `"motivo"` hasta F-feed-ui, para no dejar el
  dashboard actual mostrando una lista plana dentro de un acordeón de un solo ítem.
- Sin sistema de diseño: los dos que la cuenta tiene son de Caminatas y este es otro producto.
- Tipografía: el stack del sistema, **no Inter** — incompatible con mantener la referencia visual
  actual, y de las dos cosas la tipografía era la prescindible.

### Abierto al cierre
- ~~**Correr F-ui-fixes en Claude Code y auditarla.**~~ Corrida el 21-sep-2026 (ver abajo); falta la
  auditoría manual en navegador.
- **`fecha_cierre`: huso indeterminado — NO etiquetar la hora todavía.** ~~Ver "Hallazgo de zona
  horaria" más abajo.~~ **F-fecha-cierre (21-sep) lo arregló en datos y matching**; falta correr el
  Paso 0 (`python scripts/smoke_test.py --fechas`) para confirmar el huso de la fuente y escribir
  el resultado acá. La hora sigue sin mostrarse en licitaciones. F-feed-filtros ya puede construir
  el filtro por rango de cierre sobre este campo.
- **Confirmar `suspendida` en la familia `SIN_EFECTO`** — única asignación discutible del mapa de 6
  familias de `EstadoOportunidad`.
- Lienzo de diseño del dashboard: Artifact de tipo Design, un artboard a 1440x1640. Faltan el estado
  vacío del feed, la versión móvil del panel de filtros y la ficha de licitación.
- Siguen abiertos de la sesión anterior: rotar `JOBS_TOKEN`, borrar `_to_delete/_mp_snapshot.tar.gz`,
  commitear los docs sin trackear, y la limpieza de argentinismos.

---

## Hallazgo de zona horaria de `fecha_cierre` (F-ui-fixes §2.5, 21-sep-2026)

F-ui-fixes exigía verificar el almacenamiento **antes** de etiquetar la hora de cierre. Resultado:
la etiqueta **no se agregó** y la conversión **no se hizo**, porque el huso no quedó verificado.

**Verificado [V] — fuente primaria: el código de la app.**

1. La columna es naive. `app/models/tables.py:110` (Licitacion) y `:179` (CompraAgil) declaran
   `mapped_column(DateTime)` **sin** `timezone=True`: Postgres guarda `timestamp without time
   zone` y el objeto Python no lleva `tzinfo`. No hay offset almacenado en ninguna parte.
2. En licitaciones la hora es **fabricada**. `parse_fecha_v1` (`app/clients/types.py:29`) devuelve
   un `date` — v1 entrega `ddmmaaaa`, o ISO que el parser recorta a `[:10]` — y
   `_fecha_a_dt` (`app/ingest/licitaciones.py:25`) lo expande a `datetime(y, m, d)`. El
   `%H:%M` que la ficha muestra para una licitación es siempre **00:00**, y no es un dato de la
   fuente. Esto es peor que un problema de huso: no hay hora que convertir.
3. En Compra Ágil el offset se descarta. `parse_fecha_iso` (`app/clients/types.py:50`) hace
   `.rstrip("Z")` y prueba solo `%Y-%m-%dT%H:%M:%S[.%f]` y `%Y-%m-%d`. Un valor con offset
   explícito (`...-04:00`) no calza con ningún formato y devuelve `None`; uno con `Z` pierde la
   marca de UTC. El huso nunca llega a la BD.

**No verificado [I] — falta fuente primaria.** A qué huso se refieren los valores ISO que devuelve
`api2.mercadopublico.cl` en `fechas.fecha_cierre`. Los fixtures de `tests/test_clients.py` son
sintéticos (los escribimos nosotros), y `docs/01-analisis-api-mercado-publico.md` no lo declara:
su línea 163 dice "fechas a ISO/UTC" como **recomendación de normalización**, no como observación
de la respuesta real. Verificarlo exige una respuesta real de la API o su documentación oficial.

**Decisión:** por regla 20/23, no se escribe "hora de Chile" como hecho sin verificarlo, así que la
ficha quedó igual que antes (sin sufijo de huso). Pendiente para una fase propia, con tests:

- Capturar una respuesta real de v2 y comparar `fecha_cierre` contra la hora de cierre que muestra
  el portal para esa misma Compra Ágil. Eso decide UTC vs. local.
- Según el resultado: guardar `timezone=True` y convertir en la capa de presentación, o etiquetar.
- Aparte y anterior: decidir qué mostrar como hora de cierre de una **licitación**, dado que hoy
  el 00:00 es inventado por `_fecha_a_dt`. Ocultar la hora es lo honesto mientras v1 no la entregue.

> **Resuelto en el código por F-fecha-cierre (21-sep-2026).** Los tres puntos [V] de arriba
> describen el código **anterior** a esa fase: `_fecha_a_dt` ya no existe, `parse_fecha_iso` ya no
> hace `rstrip("Z")`, y el parseo de v1 conserva la hora. Lo que sigue [I] es el huso de la fuente.
> Ver la sección siguiente.

---

## F-fecha-cierre — la hora real de cierre (21-sep-2026)

Cierra el hallazgo de arriba en lo que era **bug de negocio**, no de presentación: una licitación
que cerraba hoy a las 15:00 quedaba guardada como `00:00` de ese día; leído como UTC, eso son las
21:00 del día ANTERIOR en Chile, así que dejaba de ser candidata —desaparecía del feed y no
generaba alertas— durante todo su último día.

**Qué cambió [V] (leído en el código de esta fase):**

- `app/core/tiempo.py` es nuevo y es el **único** lugar del proyecto que convierte husos y el
  **único** que define "ahora" (`ahora_utc()`). Las ~15 copias de
  `datetime.now(UTC).replace(tzinfo=None)` desaparecieron.
- `parse_fecha_v1_dt` (`app/clients/types.py`) conserva la hora del ISO que manda el listado de
  activas. `parse_fecha_v1` sigue existiendo, sin cambios, para los campos que sí son fechas.
- `parse_fecha_iso` usa `datetime.fromisoformat`, que en 3.11+ entiende `Z` y los offsets: el
  offset **se conserva** en vez de descartarse.
- Cuando la fuente solo da la fecha (`ddmmaaaa`), `fecha_cierre` se guarda como el **FIN** del día
  en hora de Chile, no el comienzo. Eso solo arregla el bug incluso para las filas sin hora.
- En la base se sigue guardando **naive en UTC**. No hubo migración: las columnas siguen sin
  `timezone=True`.
- `app/clients/mp_v2.py::_iso_para_la_api` deshace la conversión al mandarle `cambio_desde` a la
  API. Sin eso el cursor de Compra Ágil se habría corrido 3–4 horas hacia adelante y la ingesta
  incremental habría empezado a perder cambios.
- Las filas viejas se curan solas: `upsert_basica` sobrescribe `fecha_cierre` siempre que el item
  entrante traiga el dato (solo protege el caso `None`), así que la próxima corrida de `activas`
  reescribe todas las licitaciones activas. No se escribió ningún script de backfill. Las
  terminales viejas quedan con el dato viejo: no son candidatas y la retención las purga a los 90
  días.
- La UI **no cambió**: `texto_cierre` sigue sin mostrar la hora en licitaciones (ver su docstring).

**Lo que sigue sin verificar [I]:** a qué huso se refieren los valores que la API manda **sin**
offset. `app/core/tiempo.py` los interpreta como hora de **Chile continental**
(`America/Santiago`), porque es una API del Estado de Chile publicando plazos chilenos. Es una
suposición razonable, no un hecho.

### Paso 0 — RESULTADO, ejecutado el 21-sep-2026

`python scripts/smoke_test.py --fechas` contra la API real. Lo observado:

**[V] La v1 SÍ manda la hora de cierre, con hora real y SIN offset.** Tres códigos del listado de
activas:

```
1002584-12-LE26   FechaCierre = '2026-09-24T16:00:00'
1002588-96-LE26   FechaCierre = '2026-09-23T15:00:00'
1002588-97-LP26   FechaCierre = '2026-09-29T16:37:00'
```

Queda confirmado que el slice `s[:10]` de `parse_fecha_v1` estaba tirando un dato que la fuente sí
entrega, y que la medianoche que fabricaba `_fecha_a_dt` era invención del código. F-fecha-cierre
atacaba un problema real.

**[V] `FechaPublicacion` llega `None` en el listado de activas.** No viene en ese endpoint; si se
necesita, sale del detalle o de datos abiertos. No confundir con un fallo del parser.

**[I] El huso sigue sin marca explícita en la respuesta**, así que la suposición
"sin offset = hora de Chile continental" (`_TZ_SIN_OFFSET` en `app/core/tiempo.py`) se mantiene
como inferencia. Ahora tiene evidencia circunstancial fuerte: 16:00, 15:00 y 16:37 son horas de
cierre típicas del portal; leídos como UTC serían 13:00, 12:00 y 13:37 en Chile, menos plausibles
para un cierre de licitación. **Para pasarlo a [V] falta un paso manual:** abrir la ficha de
`1002588-97-LP26` en el portal y comparar la hora de cierre que muestra contra el `16:37`. Si
coincide, la suposición queda verificada; si muestra `13:37`, el único cambio es `_TZ_SIN_OFFSET`.

**[V] La paginación de v2 tiene MÍNIMO, no solo máximo.** `tamano_pagina` debe estar entre **10 y
50**; con un valor menor la API responde `success='NOK'` y
`400 — "tamano_pagina debe estar entre 10 y 50"`. `scripts/smoke_test.py` lo pedía más chico en dos
lugares (líneas 97 y 158) y por eso el bloque de Compra Ágil del Paso 0 abortó. Anotado también en
`docs/01-analisis-api-mercado-publico.md`.

**Pendiente de esta verificación:** cerrar el bloque v2 del Paso 0 una vez corregido el
`tamano_pagina`, y hacer la comparación manual contra la ficha del portal.

---

**Paso 0 — lo corre Boris, no los tests (regla 20/23):**

```
python scripts/smoke_test.py --fechas
```

Pide una página del listado de activas (v1) y una de Compra Ágil (v2) —2 requests— e imprime, sin
exponer el ticket, el valor crudo de `FechaCierre`/`fecha_cierre`, si trae hora distinta de
`00:00:00` y si trae offset explícito. **El resultado va escrito acá, marcado [V].** Según lo que
muestre:

- hora real y sin offset → confirmar contra la hora de cierre que el portal muestra para ese mismo
  código; si no coincide con Chile, el único cambio es `_TZ_SIN_OFFSET` en `app/core/tiempo.py`;
- con `Z` o con offset → no hay suposición que confirmar, el código ya lo respeta;
- sin hora → la mitad de la fase no aplicaba y hay que replantearla.

**Verificación manual pendiente:** tras un `POST /api/jobs/run?job=activas` en producción, mirar en
el feed que una licitación que cierra HOY siga apareciendo.

**Queda anotado para una fase de una línea:** devolver la hora al badge de cierre de licitaciones,
cuando el Paso 0 confirme que la API manda hora y haya pasado una corrida completa de `activas`.
Antes no, porque hasta entonces conviven filas con hora real y filas con el borde del día derivado,
sin manera de distinguirlas en la UI.

---

## Última sesión — 21-sep-2026 (leer esto primero)

**Estado al cierre:** ingesta **detenida desde ~00:30 del 21-sep**. La app está sana; el que
falla es el disparador. Decidido y documentado el paso a GitHub Actions; tres prompts listos
para correr en Claude Code.

### Verificado — cambia decisiones

**1. El 429 no viene de la app ni de la cuota de ChileCompra.** [V] Los 6 crons de
cron-job.org más el monitor pasaron a `429 Too Many Requests` entre el 20-sep 21:00 y el
21-sep 00:00 (hora Chile). Evidencia: (a) `grep` en `app/api/` y `app/core/` da **cero**
`status_code=429` — el único rate-limit propio es el de login; (b) también rebota
`GET /api/salud/jobs`, que es público, sin token y sin cuota; (c) ese mismo endpoint
responde **200** a fetches externos en el momento de los fallos, con el switch en verde;
(d) los rechazos tardan 226–614 ms, imposible para un arranque en frío de Render (30+ s);
(e) una **ejecución de prueba** disparada desde el propio cron-job.org dio **200 OK** con
`Server: cloudflare` y `x-render-origin-server: uvicorn`.
**Conclusión [V]:** limita Cloudflare, en el borde de Render, contra las requests del
*scheduler* de cron-job.org. Render documenta que su protección es Cloudflare y que no se
pueden aflojar sus reglas de plataforma.
**[I] sin confirmar:** que sea limitación por IP del pool compartido de cron-job.org. Encaja
con que la ejecución de prueba —que sale por otra infra— pase sin problema.
Esto **refuerza** la hipótesis que el 20-sep quedó marcada [I]: el HTML del 17-jul era
probablemente Cloudflare, no la página de arranque en frío de Render.

**2. El repo es PÚBLICO** (`github.com/basuazo/app_mercado_publico`). [V] Cambia dos cosas:
Actions es gratis e **ilimitado** (el techo de 2.000 min/mes no aplica), y los **logs de las
corridas son públicos** — de ahí que el `repr=False` de `Settings` (deuda A1) entre en la
fase 1 y no después.

**3. El CLI ya sirve tal como está.** [V] `app/ingest/__main__.py` envuelve cada job en
`_run_with_lock`, así que por el camino del CLI el advisory lock se sigue tomando, `job_runs`
se sigue escribiendo y `/api/salud/jobs` sigue funcionando **sin tocar una línea**. Lo único
que no tiene son los compuestos `ciclo-ca`/`ciclo-activas` (viven en `_secuencia` del
endpoint): se resuelven como secuencia de pasos en el workflow, sin código nuevo.

**4. Bug de configuración, aparte del 429.** [V] El cron `Activas + detalles + match +
alertas` tenía la URL literal `...?job=<ciclo-activas>`, con los signos `<>` adentro
(verificado en el campo URL del formulario). Habría fallado igual sin el 429. El test
`tests/test_workflows.py` de la fase 1 existe para que no se repita del lado de Actions.

**5. El CLI sale 0 aunque el job falle.** [V] Descubierto el 21-sep al disparar `ciclo-ca` a
mano: la API v2 devolvió `504 Endpoint request timed out` en la pág. 1 de CA y el job murió,
pero `_run_with_lock` atrapa la excepción, la registra en `job_runs` como "error" y
**devuelve None sin propagar**; `cmd_run_once` imprime ese None y termina en 0. Llevado a
Actions, todas las corridas saldrían verdes para siempre y el correo de fallo de GitHub
—la red de seguridad del modelo nuevo— nunca se dispararía. Ya estaba anotado como pendiente
("exit codes del CLI") en el prompt de F-invertir-modelo. **Corregido en
`docs/prompt-F-actions-1-canary.md` §3** (parámetro `propagar` en `_run_with_lock`, solo el
CLI lo usa; "omitido" por lock ocupado sigue saliendo 0).

**6. `_secuencia` NO corta la cadena ante un error.** [V] Mismo incidente: `ca` falló con 504
y `match` y `alerts` corrieron igual, porque `_run_with_lock` no propaga. Consecuencia para
Actions: el loop del workflow **no** debe usar `set -e` — tiene que seguir con los jobs
siguientes y salir 1 al final si alguno falló. Corregido en el mismo prompt, §2.f.

**7. La API de Mercado Público está devolviendo errores hoy (21-sep, ~11:00–11:30 Chile).**
[V] Dos síntomas simultáneos: la v2 devuelve `504 Endpoint request timed out` en la pág. 1
del listado de Compra Ágil (dos intentos separados por 7 min, ambos fallidos tras el
reintento interno), y la v1 devuelve **429 en todas** las peticiones de detalle.

**8. El 429 probablemente NO es el tope diario.** [I] fuerte, sin confirmar. `activas` (v1)
corrió bien y acto seguido **cero** detalles pasaron: si fuera el tope de 10.000 al menos
los primeros habrían funcionado antes de cruzarlo, y el volumen del día era mínimo (ingesta
parada desde las 00:30). Apunta a **limitación por tasa**, coherente con que la plataforma
esté además devolviendo 504. Si se confirma, **la regla 3 de CLAUDE.md está escrita sobre un
supuesto equivocado** y hay que corregirla. Cómo verificarlo: el punto 3 de
`docs/prompt-F-cuota.md` instrumenta los headers del 429 para buscar `Retry-After`.
Dato tranquilizador [V]: `QuotaTracker` no persiste ningún flag de "agotado" —solo un
contador en `quota_log`—, así que un 429 no deja la app bloqueada hasta medianoche; se
recupera sola cuando la API deja de rechazar.

**9. Bug nuevo: el advisory lock se suelta solo en los jobs largos.** [V] `_run_with_lock`
deja la conexión del lock **inactiva dentro de una transacción** mientras `fn()` trabaja con
otras sesiones; Neon la mata por `idle_in_transaction_session_timeout` y el lock se libera a
mitad de la corrida (dos veces en el log del 21-sep). La garantía de la regla 13 hoy no se
cumple en `detalles` ni en `nocturno`. Arreglo propuesto en `docs/prompt-F-cuota.md` §4
(conexión en AUTOCOMMIT: `pg_advisory_lock` es de sesión, no de transacción).

**10. El bug 1 de F-cuota es real y está activo.** [V] `licitaciones.py:246` atrapa
`except Exception` y siguió el loop: ~60 requests en 6 minutos contra una API que rechazaba
todas. Con los jobs desatendidos en Actions esto correría solo cada 2 h.

### Decisiones tomadas
- **F-cuota pasa ANTES de F-actions** (21-sep). Mover el disparador sin arreglar esto
  automatiza el problema en vez de resolverlo. Prompt: `docs/prompt-F-cuota.md`.
- **F-actions por la variante A: Actions ejecuta el CLI contra Neon directo**, no `curl` al
  endpoint. Motivo: no pasa por Render ni por Cloudflare, así que es inmune a lo que acaba de
  pasar. Descartada la variante B (curl desde Actions) porque las IPs de GitHub cruzan el
  mismo borde.
- **El repo queda público.** Los minutos ilimitados pesan más que el riesgo de log, que ya
  cubren dos capas: el enmascarado automático de GitHub sobre los valores registrados como
  secret, y el `_SecretFilter` propio (que ya incluye `DATABASE_URL` y `MP_TICKET`).
- **`POST /api/jobs/run` se queda** como escotilla manual. `JOBS_TOKEN` deja de bloquear
  F-actions (el CLI no lo usa), pero su rotación sigue pendiente.

### Prompts listos para Claude Code (sin correr)
- `docs/prompt-F-actions-1-canary.md` — `repr=False`, `_job.yml` reutilizable, canario
  `ciclo-ca` manual, `tests/test_workflows.py`.
- `docs/prompt-F-actions-2-horarios.md` — los 5 workflows restantes y los `schedule` en UTC,
  con guardia de hora Chile para `resumen` (el cron de GitHub es solo UTC y Chile alterna
  UTC−3/UTC−4).
- `docs/prompt-F-actions-3-cutover.md` — `check-jobs` en el CLI, workflow de vigilancia,
  apagado de cron-job.org y cierre documental.

### Abierto al cierre
- **Riesgo nuevo que hereda el modelo:** GitHub **deshabilita los workflows programados tras
  60 días sin actividad en el repo**, en silencio. Es la misma muerte del pinger el 17-jul.
  Por eso hace falta un monitor **fuera del repo** sobre `/api/salud/jobs`, no solo el
  workflow de vigilancia.
- **Trampa al cargar secrets:** el `.env` local tiene `DATABASE_URL` en el branch **dev** de
  Neon; en Actions va el de **production** (`DATABASE_URL_PROD`).
- **Sin verificar:** que `_make_engine` del CLI conecte a Neon desde fuera de la máquina de
  Boris (el `connect_args={"sslmode": "require"}` nunca corrió ahí). Es lo que prueba el
  canario de la fase 1.
- Siguen abiertas de antes: `F-cuota` (los tres bugs del 429 interno), `F-secretos`,
  borrar `_to_delete/_mp_snapshot.tar.gz` (agravado por ser repo público), y la limpieza de
  argentinismos.

---

## Última sesión — 20-sep-2026 (leer esto primero)

**Estado al cierre:** app arriba y **la ingesta volvió a correr y actualizarse** tras
arreglar el disparador. El código no estaba roto; lo roto era el cron externo.

### Verificado — cambia decisiones

**1. Causa raíz del corte del 17-jul — mecanismo confirmado, causa del HTML NO.** El
`/api/salud/ping` devuelve 15 bytes (`{"status":"ok"}`, verificado con fetch externo), así
que la "salida demasiado grande" que cron-job.org marcó el 17-jul no puede ser el JSON [V]:
recibió una página HTML. Cadena verificada [V]: cron-job.org **deshabilita** el pinger tras
esa falla (17-jul) → sin keep-alive → Render free duerme a los 15 min → APScheduler (dentro
del proceso web) muere con él → cero ingesta; el job-all nocturno pegaba contra la instancia
dormida → "error HTTP" → inactivo (12-ago). Lo que quedó **[I], sin confirmar**: si ese HTML
era un desafío de Cloudflare al UA-bot o la página de cold-start de Render.

**2. CORRECCIÓN — lo que revivió la ingesta NO fue cambiar el User-Agent.** Verificado el
20-sep: **ningún cron tiene User-Agent custom** y aun así job-all da 200 y el switch se puso
verde con corridas reales → con el UA por defecto de cron-job.org las requests pasan al
origen hoy. O sea, Cloudflare no está bloqueando ese UA ahora, y la teoría del UA-block
(punto 1) queda debilitada. Lo que revivió la ingesta fue **re-habilitar los crons**
deshabilitados, no un cambio de UA. El UA de navegador queda solo como recurso de reserva si
un cron vuelve a fallar con un 403/503 raro.

**3. El endpoint ya está listo para el modelo invertido.** `POST /api/jobs/run` expone los
12 jobs (`ca, activas, detalles, datos-abiertos, lifecycle, match, competencia, alerts,
resumen, retencion, catalogos, nocturno`), todos con advisory lock; `_full_cycle` incluye
`resumen`. Los 4 bloqueantes de código del §4 de `docs/11` se cerraron en `fd78ff0`. Falta
solo sacar el APScheduler del proceso web y mover la cadencia al cron (Capa 2, fase aparte).

### Decisiones tomadas
- **Capa 2 en Claude Code, orden acordado: observabilidad PRIMERO, invertir el modelo
  después.** La observabilidad es la red de seguridad y no toca el arranque (bajo riesgo);
  invertir el modelo toca el startCommand/lifespan (sensible — ahí se cayó prod con el
  `--factory` el 4-sep).

### F-observabilidad — IMPLEMENTADA Y AUDITADA (commit `3ee78fe`)
Verde: `ruff` limpio, `mypy app` limpio, `pytest` 569 passed / 20 skipped (los 20 son
`@needs_postgres`, credencial dev vencida — deuda preexistente, no regresión). 21 tests
nuevos offline (SQLite) en `tests/test_observabilidad.py`. Diseño tal cual el prompt:
tabla `job_runs` + `_run_with_lock` graba una fila por corrida (ok/error/omitido, sesión
propia, telemetría que no propaga) + `GET /api/salud/jobs` público 200/503 (watchlist con
alias y umbrales 30–36 h) + `purgar_job_runs` (>90 d) en retención. Migración
`b8d4e2a91c07` (`down_revision f3a9b8c7d6e5`, additiva) — NO aplicada aún a ninguna base.

**Hallazgo verificado durante la fase (bug preexistente, no introducido):** `run_retencion`
abría `Session(engine)` y retornaba **sin `commit()`**; como `purgar_terminales` solo hace
`session.execute(delete/update…)` sin commit propio, el cierre de la sesión hacía rollback
→ **la retención era un no-op silencioso en producción** (regla 11 nunca se aplicó). Fix: una
línea (`session.commit()`) + test. Auditado y correcto. Consecuencia operativa: la PRIMERA
corrida real de retención (03:00 vía scheduler, ahora que el keep-alive vive) purgará por
primera vez `raw_json`/ítems de terminales >90 d y productos de CA — es lo diseñado, solo
achica la base (hoy 88 MB/16,8%); no confundir el conteo alto ni la baja de tamaño con un bug.

### Desplegado y operativo (20-sep)
F-observabilidad **en producción**: push a `main`, migración `b8d4e2a91c07` aplicada sola en
el deploy, `/api/salud/jobs` responde. Tras disparar `job=all` + `job=ca`, el switch quedó en
**200 / `status:ok`** con los tres críticos frescos. **Monitor conectado** en cron-job.org a
`/api/salud/jobs` (GET, cada 1–2 h, sin headers — endpoint público). Dead-man's switch vivo.

### Abierto al cierre
- **ROTAR `JOBS_TOKEN`** (prioridad): el token de producción se pegó en el chat (riesgo A1
  [CRÍTICO-1], aún abierto). Cambiarlo en los TRES lados que deben coincidir: variable en
  Render, header `X-Jobs-Token` de los **6 crons que disparan jobs** (`ciclo-ca`, `ciclo-activas`, `nocturno`, `resumen`, `retencion`, `catalogos` — `job-all` quedó pausado), y `.env`.
- **Borrar `_to_delete/_mp_snapshot.tar.gz`**: NO está en `.gitignore` y contiene `.env` con
  secretos de producción. Nunca hacer `git add -A` mientras exista.
- **Commitear los docs** (fuera de los commits de código): `docs/00-estado-actual.md` y los
  `docs/prompt-F-*.md` sin trackear (observabilidad, invertir-modelo, y secretos si sigue sin
  commitear) — un commit `docs:` aparte. NO usar `git add -A` (arrastra `_to_delete/`).
- **Verificar la 1ª purga de retención** (~03:00, ahora que el commit-fix está vivo): mirar
  tamaño de BD en `/api/salud` antes/después; el conteo alto es esperado, no un bug.
- **Limpieza de argentinismos** (pedido de Boris, 20-sep): la app (textos de UI/plantillas) y
  docs/código tienen expresiones argentinas; Boris es chileno. Corregir a español chileno
  (sin voseo). Fase aparte, candidata a prompt de Claude Code.
### F-invertir-modelo — DESPLEGADA, CUTOVER EJECUTADO (commit `d671dab`, 20-sep)
Verde: ruff/mypy limpios, pytest 578 passed / 20 skip (9 tests nuevos). Flag
`scheduler_en_proceso` (env `SCHEDULER_EN_PROCESO`) default False → el lifespan no arranca el
BackgroundScheduler y `app.state.scheduler=None`; el `yield` queda fuera del `if` (la app
arranca igual), shutdown guardado, import de `build_scheduler` lazy. Jobs compuestos
`ciclo-ca` (ca→match→alerts) y `ciclo-activas` (activas→detalles→match→alerts) vía
`_secuencia`, reusando las entradas `_locked` → job_runs y el watchlist intactos.
`_make_app`/factory/startCommand/render.yaml sin tocar. Auditado, correcto.

**Cutover ejecutado (20-sep) — el modelo quedó cron-driven:**
- Desplegado `d671dab`. En cron-job.org quedaron **6 crons activos con horario en
  America/Santiago** (POST a `/api/jobs/run?job=<X>` con header `X-Jobs-Token`):
  `CA + match + alertas` (`ciclo-ca`, 08–20 c/2h), `Activas + detalles + match + alertas`
  (`ciclo-activas`, 08:10/13:10/18:10), `Ciclo nocturno` (`nocturno`, 01:00),
  `Resumen por correo` (`resumen`, 08:30), `Retención (purga)` (`retencion`, 03:00),
  `Catálogos` (`catalogos`, lunes 02:30).
- `mp-oportunidades job-all` y `mp-oportunidades pinger`: **pausados**. El scheduler interno ya
  no es el motor; el cron dispara y el proceso duerme entre disparos.
- `/api/salud/jobs` en verde con corridas reales de cron (`ca`/`resumen` refrescados por sus
  crons el 20-sep).
- **Falta confirmar/afinar:** (a) que `SCHEDULER_EN_PROCESO` NO quede en Render (si se puso en
  `true` para desplegar sin gap, quitarla → default False; con el pinger apagado el scheduler
  igual muere al dormirse, pero conviene dejar limpio); (b) el monitor `GET Jobs` a cada 1–2 h
  con timeout 30–60 s + reintentos (por el cold-start, ahora que el proceso duerme); (c) mañana
  al mediodía, ver `activas`/`datos-abiertos` con OK de sus crons (no del run manual) = modelo
  andando solo.

---

## Última sesión — 4-sep-2026 (leer esto primero)

**Estado al cierre:** app arriba y sirviendo. `fd78ff0` (F-jobs-endpoint) + `9b3017d` (docs)
pusheados y desplegados. Prod en alembic `f3a9b8c7d6e5` = head del repo. Base 88 MB de 500 (16,8%).
Contraseña de admin rotada a mano en `/perfiles`.

### Verificado — cambia decisiones

**1. El `startCommand` de Render NO vive en `render.yaml`.** El servicio se creó a mano, así que
manda el campo del dashboard y `render.yaml` es decorativo para ese valor. El dashboard corría
`uvicorn app.api.main:app` (sin `--factory`), mientras `render.yaml`, `README.md:93` y
`docs/despliegue.md:60` decían `--factory _make_app`. **Verificar siempre en el dashboard antes de
tocar cualquier cosa del arranque.**

**2. MEDIO-1 de `docs/11` estaba mal diagnosticado.** Decía que `_make_app()` corría dos veces y
que la instancia de módulo "nunca sirve tráfico". Era al revés: como `--factory` nunca estuvo en
el comando real, **esa instancia era la única que servía tráfico**. Borrarla (punto 5 de
F-jobs-endpoint) no ahorró memoria: dejó el servicio sin app y tiró el deploy con
`Error loading ASGI app. Attribute "app" not found`. Se arregló alineando el dashboard, no
revirtiendo el código.

**3. La ingesta estuvo muerta desde el 17-jul-2026**, no desde agosto. Al 4-sep,
`licitaciones_activas`, `compra_agil` y `datos_abiertos_lic` seguían con fecha del 17 de julio.

**4. La causa es que el pinger externo no está llegando.** Evidencia: el 4-sep a las 20:48 el
servicio se apagó solo por inactividad, diez minutos después de la última visita, y en todo el log
no hay una sola request a `/api/salud/ping` desde una IP externa. Con el pinger vivo ese apagado
no puede ocurrir. Consecuencia: proceso dormido → APScheduler muerto con él → ningún job corre.
**No hubo suspensión por cuota**, lo que además explica por qué los resets del 1-ago y el 1-sep no
arreglaron nada. Queda por confirmar en cron-job.org POR QUÉ dejó de llegar (pausado, URL vieja, o
el desafío de Cloudflare = hipótesis 3 de `docs/11` §2).

**5. El pipeline y el `MP_TICKET` están sanos.** `job=activas` disparado a mano el 4-sep a las
21:22 corrió y grabó `ultimo_ok`. Lo roto es el disparador, no la ingesta.

**6. `detalles` recibe 429 en cadena — tres bugs encadenados.**
- `app/ingest/licitaciones.py:246` atrapa `except Exception` y **sigue el loop**, así que se come
  hasta 400 rechazos seguidos. Contradice el contrato del docstring de `orchestrator.py:7`. El
  patrón correcto ya existe en `compra_agil.py:209`, que corta ante `MPRateLimitError`.
- `app/clients/base.py`: `self._quota.consume()` corre **después** de `_handle_response()`, así que
  un 429 nunca se contabiliza. Por eso `/api/salud` muestra `usadas_hoy: 10` de 9.000 mientras la
  API rechaza todo. `check_budget()` es ciego a la cuota real.
- `app/clients/base.py:189-193`: el 429 nunca lee el header `Retry-After`; siempre calcula
  `_seconds_until_next_day_chile()`. El "Reintentar en 23782 s (00:01 Chile)" es una suposición de
  la app impresa como si fuera dato de ChileCompra.

**7. `errores_recientes` de `/api/salud` es cry-wolf.** Lista como error cualquier fila con campo
`notas`, así que muestra ocho "errores" que dicen `"2026-7: sin cambios"`. Con la página gritando
falsos positivos, un error real pasa desapercibido — probablemente parte de por qué el corte de
julio no se notó.

**8. A1 [CRÍTICO-1] es más chico de lo que decía.** Verificado: `.env` **nunca** estuvo rastreado
en ninguna rama (`git log --all -- .env` vacío) y `audits/` está en `.gitignore:60`. Los siete
secretos viven solo en `.env` y `audits/AUDIT-FINAL-A1-seguridad.md`, ambos locales.
**No hay que reescribir historial de git.** La mitad del hallazgo ya estaba cerrada:
`_SecretFilter._ENV_VARS` en `app/core/logging.py` ya enmascara los ocho. Falta el `repr=False`
en `Settings` (cero ocurrencias hoy).

### Abierto al cierre

- **Sin verificar y decisivo:** si el 429 de `detalles` es tope diario o limitación por tasa.
  Indicio a favor de lo segundo: `activas` corrió OK a las 21:22 y `detalles` rebotó a las 21:25 —
  una cuota diaria no se apaga en tres minutos. **Test:** disparar `detalles` y ver si las primeras
  requests pasan antes de empezar a 429. No se alcanzó a correr.
- Historial de cron-job.org sin revisar (cierra el punto 4).
- `licitaciones_detalles.ultimo_ok` sigue en `null`: nunca registró un éxito, ni en julio. Si tras
  una corrida buena sigue nulo, es un bug de persistencia de estado de esa fuente.
- `docs/prompt-F-secretos.md` escrito y sin commitear.

### Gotchas nuevos

- **PowerShell:** una URL con querystring necesita `${u}?job=x`, no `$u?job=x` — el `?` es carácter
  válido en nombres de variable y PowerShell se come la URL entera.
- **`job=all` ahora manda el correo-resumen al final** (`run_resumen` entró a `_full_cycle`). El
  cron nocturno de las ~02:00 lo dispara a esa hora en vez de a `DIGEST_HOUR` (08:00), y como
  mueve `ultimo_resumen_en`, el job de las 08:05 ya no envía. Decidir en F-actions.
- El working tree en Windows y el mount del asistente comparten `.git`: un `git status` del
  asistente puede dejar un `index.lock` huérfano que Windows no borra solo. Usar
  `git --no-optional-locks` desde el asistente.

---

## Qué es
App de búsqueda de oportunidades en compras públicas chilenas (API Mercado Público /
ChileCompra). Flujo: ingesta → Postgres (Neon) → perfiles por usuario → matching con
score → alertas email → dashboard con login. Costo objetivo: **$0** (Render free + Neon free).

## Desplegado en PRODUCCIÓN
- **Render free** (web) + **Neon free** (Postgres 0.5 GB). URL: https://app-mercado-publico.onrender.com
- Branches Neon: `production` (la usa Render) y `dev` (local + tests).
- Commit base de la sesión: `9a4928b` (F-deploy) + `d1369b0` (.gitattributes EOL).
- En vivo: F8 (resultados legibles, ficha enriquecida, link condicional), F9a/b/c
  (perfiles con regiones/montos/exclusiones, **rubros UNSPSC**, **organismos seguidos**,
  recall+score unificados en FTS con stemming), **F-rubros** (ítems desde datos abiertos
  sin gastar cuota), **F-seguir** (seguir/archivar + alertas de avance), **F-competencia**
  (análisis de competencia al adjudicarse), **F-plan** (consulta del Plan Anual de Compra,
  pestaña separada, on-demand), **F-datos** (clasificación de organismos por sector,
  datos abiertos sin ticket — alcance acotado, ver roadmap), **F10 (parcial)** (formulario
  de `/perfiles` rediseñado: rubros en acordeón, organismos en multi-select por sector;
  **dashboard rediseñado** con tarjetas escaneables, orden score/cierre, **descartar +
  registro de feedback** "me sirve"/"descarte" — señal para F11; **ficha de detalle
  rediseñada** con cabecera escaneable, competencia con oferentes que NO ganaron, rubro en
  ítems, y los mismos botones de feedback; **mail de match enlaza a la ficha de la app**
  (ya no a la URL no autorizada de MP) — **F10 COMPLETA**), Fix Compra Ágil 500 en frío,
  **F-feed-umbral** (umbral de relevancia en el dashboard: control Alta/Media/Todas +
  línea "N ocultas por baja relevancia — ver todas"), **F-feed-agrupado** (el dashboard
  reemplaza la lista plana por una vista agrupada por categorías: motivo/región/fuente),
  **F-automatch** (crear/editar un perfil dispara matching inmediato de ese perfil en
  background, leyendo solo oportunidades ya en BD y sin consumir cuota API),
  **F-passwords** (cambio de contraseña propio y reseteo admin con CSRF),
  **F-notificaciones** (resumen consolidado de descubrimiento por usuario + inmediatas solo
  para oportunidades con alertas activas),
  **F-onboarding** (tutorial de primera vez + panel de novedades versionado por fecha,
  ambos con estado visto por usuario),
  **fix CA NULL cierre** (Compra Ágil `publicada` con `fecha_cierre` NULL vuelve a ser
  candidata para matching), **deuda técnica — suite 100% verde**, **fix enlace ficha oficial** (el botón "Ver ficha
  oficial en MP" de licitaciones ahora abre — ver detalle abajo), F-deploy.
- **Suite: 499 tests verdes, 20 skipped, 0 failed, 0 errors** (incluye `@needs_postgres`
  contra la branch `dev` de Neon). Ya NO hay "1 failed + 4 errors" — ver detalle abajo.
- **Deuda técnica — suite 100% verde (este commit):**
  - `tests/test_models.py::pg_session`: `Session(connection=conn)` no es un kwarg válido en
    esta versión de SQLAlchemy 2 (daba 4 errors bajo `@needs_postgres`) → `Session(bind=conn)`
    (participa igual en la transacción ya abierta sobre `conn`; se pudo quitar el
    `type: ignore[call-arg]`).
  - Ese fix desenmascaró un bug de aislamiento que antes quedaba oculto porque el fixture
    fallaba ANTES de llegar a la aserción: `test_fts_encuentra_sin_tilde`/`test_fts_compra_agil`
    hacían `SELECT codigo FROM licitaciones WHERE tsv @@ ...` **sin filtrar por el código que
    el propio test insertó** — con datos preexistentes en `dev` (real o de otras pruebas),
    Postgres podía devolver CUALQUIER fila que matcheara el FTS, no necesariamente la del
    test. Fix: agregar `AND codigo = '<código del test>'` a ambas queries — sigue verificando
    que el FTS encuentra esa fila (si la condición FTS fuera falsa para ella, el `AND` no
    devuelve nada y el test igual falla), pero ya no depende de qué más haya en la tabla.
  - `test_match_todos_procesa_todos_perfiles` (`tests/test_matching.py`) asumía un total
    absoluto de 4 perfiles activos en toda la BD → se rompía si `dev` traía otros (p. ej. los
    ids ~49–52 de pruebas anteriores). Fix: cuenta los perfiles activos AJENOS al dataset
    ANTES de correr `match_todos` y afirma `perfiles_procesados == ajenos_antes + 4` (delta,
    no total absoluto) — ya no asume una BD exclusiva/vacía.
  - `test_jobs_run_job_ca` (`tests/test_api.py`) estaba con `@pytest.mark.skip` porque
    `BackgroundTasks` corre el job dentro del mismo ciclo del `TestClient` y `run_sync_ca`
    pegaba a la API real de Compra Ágil. Se migró a `respx` (mock de
    `GET https://api2.mercadopublico.cl/v2/compra-agil` con un listado vacío) y se quitó el
    skip — corre determinístico, sin red real.
  - **Hallazgo adicional (mismo criterio, no estaba en el pedido explícito):**
    `test_jobs_run_token_correcto` (`job="all"`, el default) también pegaba a la red real —
    confirmado con `--log-cli-level=DEBUG`: con la BD de test vacía, el ciclo completo
    alcanza a golpear `api.mercadopublico.cl/servicios/v1/publico/licitaciones.json` (activas)
    y hace `HEAD` al blob de datos abiertos (`transparenciachc.blob.core.windows.net/lic-da/`)
    antes de quedarse sin más trabajo (0 licitaciones → el resto de los jobs no-opean). Se
    mockearon ambos con `respx` (el del blob con `url__regex` porque la URL depende del
    mes vigente, no es fija).
  - **Verificado con Postgres real** (branch `dev`, `alembic current=9a1e6b2c5d7f` — SIGUE
    detrás del head `e1f4a7c9b2d6`; la migración pendiente de `MatchFeedback` no afecta a
    ninguno de los tests `@needs_postgres` tocados aquí, así que no fue necesario aplicarla
    para verificar esta fase). **No se corrió `alembic upgrade head`** — sigue siendo tarea
    del humano (ver "Migración workflow" — el asistente no aplica migraciones).
  - **Fuera de alcance, anotado para después (no tocado aquí):** limpiar la branch `dev` de
    Neon (perfiles/seguimientos/matches de prueba ids ~49–52) es una tarea de DATOS (SQL en
    `dev`), la hace Boris; investigar por qué las adjudicadas quedan con
    `fecha_publicacion`/`fecha_cierre` NULL es una investigación aparte del refresh de
    estados terminales, no mezclar con esta limpieza de tests.
- **F-automatch — crear/editar perfil dispara matching on-demand (este commit):** las rutas
  HTML `POST /perfiles/nuevo` y `POST /perfiles/{id}/editar` encolan, tras el `commit`
  exitoso, una `BackgroundTask` que ejecuta `match_perfil` solo para ese perfil. La tarea
  abre una `Session(engine)` nueva usando `request.app.state.engine`, recarga el perfil por
  id y hace no-op si no existe o quedó inactivo. No reusa la sesión de la request ni objetos
  ORM atados a ella. `match_perfil` sigue siendo puro: no llama clientes HTTP, no busca
  `raw_json` ni detalles, y por tanto no consume cuota API; solo lee oportunidades ya
  presentes en la BD y hace upsert en `oportunidades_match`.
  - Idempotencia: el upsert por `(perfil_id, fuente, codigo_oportunidad)` evita duplicados
    aunque se solape con el cron nocturno o con ediciones repetidas.
  - Aislamiento: cualquier excepción del matching queda logueada y no afecta la respuesta ya
    enviada al usuario.
  - Deuda conocida, preexistente: si una edición vuelve el perfil más restrictivo, los matches
    antiguos que dejaron de aplicar no se borran en esta fase; el filtro de relevancia del feed
    mitiga el impacto, pero queda como limpieza futura de `match_perfil`/`match_todos`.
- **F-passwords — cambio de contraseña y reseteo admin (este commit):** `/perfiles` suma en
  "Ajustes de tu cuenta" un formulario para cambiar la contraseña propia vía
  `POST /cuenta/password`: exige CSRF, contraseña actual correcta (`verify_password`), nueva
  contraseña igual a confirmación y mínimo 8 caracteres; actualiza solo el `password_hash` del
  usuario autenticado con `hash_password`. `/admin/usuarios` suma, por fila, reset de contraseña
  vía `POST /admin/usuarios/{uid}/password`, protegido por `html_require_admin` + CSRF y mínimo
  8 caracteres. El reset renderiza la nueva contraseña en el cuerpo de la respuesta una sola vez
  para que el admin la copie; no va en logs ni en query string. Sin migración.
- **F-notificaciones — resumen consolidado + inmediatas solo para seguidas (este commit):**
  se elimina el spam de “un correo por match”. Los matches nuevos ya no crean `Alerta`; en su
  lugar, `run_resumen`/`enviar_resumen` evalúa por usuario activo `dias_resumen` (3, 7 o 0 =
  nunca) y `ultimo_resumen_en`, cuenta los `OportunidadMatch.fecha_match` nuevos de perfiles
  activos (`fecha_match` es inmutable: primera vez que esa oportunidad matcheó ese perfil, no
  se re-toca por re-score), y solo si hay >0 envía un correo consolidado con top 5 por score + link a la app.
  Si no hay nuevos no envía y no mueve `ultimo_resumen_en`, para acumular ventana. Las
  inmediatas quedan limitadas a oportunidades con alertas activas (`OportunidadSeguida`):
  cambio de estado (`seguimiento_estado:*`) y cierre ≤48h (`seguimiento_cierre`). Job diario
  `digest` reemplazado por `resumen`; plantillas `digest.*`/`alerta_inmediata.*` eliminadas y
  nuevas `resumen.html`/`resumen.txt` con "Fuente: Dirección ChileCompra". Migración
  `d2f8a6c1b9e0`: agrega `usuarios.dias_resumen`, `usuarios.ultimo_resumen_en` y elimina
  `perfiles_busqueda.frecuencia_alerta`. **Operativo:** aplicar `alembic upgrade head` en
  Neon dev y luego prod lo hace Boris.
- **F-onboarding — tutorial primera vez + novedades versionadas (este commit):**
  agrega `app/changelog.py` con entradas acumulativas ordenadas por fecha y helper de ultima
  novedad; `GET /` auto-abre el tutorial si `usuarios.tutorial_visto = false` y auto-abre
  Novedades si hay entradas con fecha mayor a `usuarios.novedades_visto_hasta` (o NULL).
  Si ambos aplican, el JS encola tutorial primero y Novedades despues. `/perfiles` suma
  "Revisar tutorial" para abrirlo manualmente sin tocar el flag, y la nav suma "Novedades"
  para ver el historial completo. Rutas nuevas con sesion + CSRF: `POST /cuenta/tutorial-visto`
  y `POST /cuenta/novedades-visto`, ambas solo actualizan al usuario autenticado. Migracion
  `f3a9b8c7d6e5`: agrega `usuarios.tutorial_visto` y `usuarios.novedades_visto_hasta`.
  **Operativo:** aplicar `alembic upgrade head` en Neon dev y luego prod lo hace Boris.
- **Fix — enlace "Ver ficha oficial en MP" no abría (este commit):** spike previo en
  `docs/10-enlace-ficha.md` (veredicto cerrado). Causa: el parámetro `qs` de
  `DetailsAcquisition.aspx` espera un token interno ENCRIPTADO, no el `CodigoExterno` en
  texto plano — ninguna API oficial (v1 ni v2) lo entrega. Fix: `_url_ficha`
  (`app/api/query.py`) para licitaciones ahora arma
  `.../DetailsAcquisition.aspx?idlicitacion={codigo}` en vez de `?qs={codigo}` —
  Mercado Público resuelve `idlicitacion=<CodigoExterno>` y redirige al `qs` correcto
  (verificado: reproduce byte a byte el token real para la licitación de prueba
  `1300-31-LE26`). `mostrar_ficha_oficial` (gate a solo procesos `PUBLICADA`) **sin
  cambios** — es un problema distinto (MP igual bloquea la ficha a no-dueños en procesos
  cerrados). Compra Ágil sin cambios (sigue al buscador genérico). Tests nuevos:
  `tests/test_query.py` (`_url_ficha`/`mostrar_ficha_oficial` puros, sin DB) +
  `tests/test_ficha_routes.py` (render end-to-end: licitación publicada usa
  `idlicitacion=`, cerrada no muestra el enlace). Sin migración.
- **F-feed-agrupado — feed agrupado por categorías (este commit):** el dashboard (`GET /`)
  ya NO es una lista plana paginada — siempre agrupa. `app/api/query.py::agrupar_oportunidades`
  recibe el conjunto YA filtrado por relevancia y ordenado (score/cierre) de
  `get_oportunidades_usuario` y arma grupos según `agrupar_por` (query param, default
  `"motivo"`): "motivo" expande cada match en un grupo por rubro UNSPSC hit, uno por
  keyword hit y uno si "organismo seguido" (repetición **intencional**: una oportunidad con
  2 rubros + 1 keyword aparece en 3 grupos), "sin motivo" cae en "Otros"; "region" agrupa por
  `region_nombre` ("Sin región" incluye TODAS las licitaciones, que nunca traen región);
  "fuente" agrupa Licitaciones/Compra Ágil. **No se ofrece agrupar por organismo/sector**:
  `codigo_organismo` viene vacío en licitaciones (`docs/08-datos-organismos.md` §3-bis d),
  la mayoría de los grupos quedarían "sin organismo" — sin valor para el usuario.
  Encabezado "Mostrando N oportunidad(es) · M aparición(es)" (M ≥ N cuando hay repetición).
  Grupos ordenados por su mejor score (desc); orden de items dentro de cada grupo intacto
  (el que ya traía `get_oportunidades_usuario`). Cada grupo se capa a 10 items
  (`CAP_GRUPO_DEFAULT`) con "ver más en este grupo" (`?grupo_expandido=<key>`, sin reordenar
  el resto). La paginación global (`pagina`/`total_paginas`) del feed **se elimina** — la
  reemplaza el cap por grupo.
  UI: acordeón Bootstrap (colapsable, expandido por defecto — chevron nativo del componente,
  sin JS propio para eso) + control "Agrupar por: Motivo/Región/Fuente" junto a los controles
  existentes de orden y relevancia. Descartar/seguir/feedback son por oportunidad y ya se
  reflejan en **todas** sus apariciones en la próxima carga (la query excluye descartadas
  ANTES de agrupar, así que ninguna reaparece en ningún grupo); además, cada tarjeta lleva
  `data-oportunidad-key="fuente:codigo"` y un pequeño script (inline, sin librería nueva)
  escucha `htmx:afterRequest` sobre `.../descartar` y remueve del DOM **todas** las
  apariciones al instante (sin esperar un reload), para que no quede una copia obsoleta
  visible en otro grupo tras descartar desde uno.
  Gotcha de implementación: en Jinja, `dict.items` colisiona con el método builtin
  `dict.items()` cuando se accede por punto (`grupo.items` devuelve el método, no la lista) —
  la plantilla usa `grupo['items']` (bracket) en vez de `grupo.items` para ese campo.
  Sin migración (agrupar es lógica de query/plantilla; las razones ya vivían en
  `OportunidadMatch.razones`). Tests: `tests/test_feed_agrupado.py` (unit de
  `agrupar_oportunidades` sin DB + integración de la ruta).
- **F-feed-umbral — umbral de relevancia del feed (este commit):** `get_oportunidades_usuario`
  (`app/api/query.py`) suma un parámetro `min_score` (default `0` = sin piso, para no romper
  a quien llama la función directo — p. ej. la API REST `/api/oportunidades`, fuera de
  alcance de esta fase) y retorna un tercer valor `total_sin_filtro_relevancia` para poder
  mostrar cuántos matches quedan ocultos. La ruta `GET /` (dashboard) sí aplica un piso por
  defecto — `settings.feed_min_score_default` (nuevo, **`40`**) — salvo que la request pase
  `?min_score=`. Control en `index.html`: presets "Alta relevancia" (`60`, fijo), "Media"
  (el default configurable) y "Todas" (`0`), más la línea "Mostrando N · M oculta(s) por baja
  relevancia — ver todas" (también cuando el filtro esconde absolutamente todo).
  **Confianza del default (regla 20/23):** la branch `dev` de Neon solo tenía **10** filas en
  `oportunidades_match` al momento de calibrar (rango de score 23–53) — muestra insuficiente
  para una distribución robusta; no se consultó `production` (fuera del alcance autorizado
  de esta sesión). El valor `40` es **INFERIDO** de la fórmula de scoring
  (`app/matching/engine.py`: `score_texto` 0–60 solo si hay keyword-hit real, más
  `score_urgencia`/`score_competencia`/`score_estructural` 0–35 sin necesidad de texto) más
  el patrón visto en los 10 matches de dev, no de una distribución grande verificada. Queda
  como env var ajustable sin re-deploy (`FEED_MIN_SCORE_DEFAULT`) — **recalibrar con datos
  reales de producción** en cuanto haya volumen para confirmarlo o corregirlo.
- F10/perfiles, F10/dashboard y F10/ficha: verificados server-side (TestClient con ciclo ASGI
  completo + servidor local real con login), **no** con un navegador real (sin herramienta de
  automatización disponible sin instalar dependencia nueva fuera del stack) — recomendado un
  vistazo manual a la interactividad JS/HTMX antes de dar el look final por cerrado.
- **Pendiente operativo:** la migración `e1f4a7c9b2d6` (tabla `match_feedback`, F10 parte 2)
  solo se verificó offline (`alembic ... --sql`); falta correr `alembic upgrade head` contra
  la branch `dev` y luego `production`. La branch `dev` ya estaba **3 migraciones detrás**
  del head antes de esta fase (deuda preexistente, no introducida aquí) — conviene revisar
  el historial completo de `alembic current` vs `alembic heads` antes del próximo upgrade.

## Qué hace hoy
Descubrir oportunidades por keyword/región/**rubro UNSPSC**/organismo; ver ficha
enriquecida con razones legibles del match; recibir **resúmenes consolidados** de nuevas
oportunidades por usuario; **activar alertas/archivar** oportunidades puntuales y recibir
**alertas inmediatas** cuando cambian de estado o están por cerrar; y al adjudicarse, ver el
**análisis de competencia** (proveedores, montos, quién ganó) reconstruido desde datos abiertos.

## Flujo de trabajo (IMPORTANTE — así trabajamos)
- **Cambios de código vía Claude Code:** el asistente genera un **prompt**, el usuario
  lo corre en Claude Code, y se **audita** el resultado. El asistente NO edita archivos
  de código del repo directamente (solo docs/planes).
- Un commit por fase; mensaje en español con prefijo de fase.
- Antes de cerrar una fase: `ruff check .` ; `mypy app` ; `pytest` (verde).
- Migraciones: **dry-run** en una branch Neon creada desde `production` antes de tocar prod.

## Gotchas operacionales (aprendidos; no repetir)
- **Levantar la app en local:** `python -m uvicorn app.api.main:_make_app --factory --reload`.
  NO existe `app.api.main:app` — la instancia de módulo se borró en F-jobs-endpoint y el
  `startCommand` de `render.yaml` usa `--factory`. Usar `app.api.main:app` da
  `Error loading ASGI app. Attribute "app" not found` (el mismo error que tiró prod el 4-sep).
- **`alembic` no lee el `.env`, la app sí.** `alembic/env.py:26` solo pisa `sqlalchemy.url` si
  existe la variable de entorno `DATABASE_URL`; si no está, cae al placeholder de
  `alembic.ini:89` (`driver://user:pass@localhost/dbname`) y el error es
  `NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:driver` — engañoso: no falta un
  driver, está intentando cargar uno llamado "driver". Por eso uvicorn puede arrancar (pydantic
  lee `.env`) mientras `alembic current` falla en la misma ventana. `alembic heads` funciona
  igual porque no se conecta. Setearla desde el propio `.env`:
  `$env:DATABASE_URL = ((Get-Content .env | Where-Object { $_ -match '^DATABASE_URL=' } | Select-Object -First 1) -replace '^DATABASE_URL=','').Trim().Trim('"')`
  y verificar el host sin exponer la clave con `$env:DATABASE_URL -replace '://[^@]*@','://***@'`
  ANTES de cualquier `upgrade`: el comando corre contra lo que diga la variable, sin preguntar.
- `DATABASE_URL` siempre con prefijo **`postgresql+psycopg://`** (psycopg3).
  `app/core/db.py::normalizar_url_driver` lo normaliza para app y para alembic.
- **alembic lee `DATABASE_URL` de la VARIABLE DE ENTORNO, no del `.env`.** En local hay
  que exportarla en la ventana (`$env:DATABASE_URL = "..."`). Tras CADA migración nueva:
  `alembic upgrade head` contra la branch correspondiente.
- Cada ventana de PowerShell empieza limpia: activar venv + setear `$env:DATABASE_URL`.
- En prod las migraciones corren **solas** en el deploy (startCommand: `alembic upgrade head`).
- **Crons** (cron-job.org): pinger a `/api/salud/ping` (keep-alive) + `job=ca` horario +
  `job=all` nocturno (~02:00 Santiago). `job=all` ya incluye `datos-abiertos` y `competencia`.
  El endpoint `/api/jobs/run` es **POST** y exige header `X-Jobs-Token`.
- **`job=ca` arreglado (era 500 persistente, ver `docs/09-compra-agil-500.md` y
  `docs/03-roadmap.md`):** con cursor `NULL` la request a `/v2/compra-agil` salía sin
  filtro real → la API responde 500 → cursor nunca avanzaba → bucle infinito de ERROR.
  Fix: `sync_incremental` ahora manda siempre `estados` a la API. **Pendiente que Boris
  verifique post-deploy** que el cron `ca` pasa de ERROR a OK en los logs de Render y que
  aparecen Compras Ágiles en el dashboard (la primera corrida exitosa recién fija el cursor).
- **Heal de datos tras deploy:** `POST /api/jobs/run?job=all` (o `activas`→`datos-abiertos`→`match`).
- **EOL:** `.gitattributes` fuerza LF (`* text=auto eol=lf`). En esta sesión hubo un
  incidente de CRLF + archivos truncados en el working tree; se recuperó con `git restore .`
  (HEAD estaba intacto). Si vuelve a pasar: `git restore .` recupera todo desde el commit.
- **Verificación local (venv):** los ejecutables `pytest.exe`/`mypy.exe` de
  `.venv\Scripts\` dan "Acceso denegado" (shim bloqueado por antivirus). Correrlos
  SIEMPRE como módulo: `python -m pytest`, `python -m mypy app`, `python -m alembic ...`.
  Además, el sandbox del asistente que genera los prompts NO puede ejecutar el python del
  venv → mypy/pytest/alembic los corre **Boris a mano** en su PowerShell (con
  `$env:DATABASE_URL` apuntando a la branch `dev` de Neon para los tests `@needs_postgres`;
  si la compute de Neon está suspendida, despertarla y reintentar). `ruff check .` sí corre.
- **Changelog acumulativo:** cada fase futura agrega una entrada a `app/changelog.py`
  (fecha, título, descripción en simple) en el MISMO commit — así el panel de novedades
  del home crece solo. Ver F-onboarding.

## Deudas conocidas (pendientes de calidad; ninguna bloquea prod)
- ~~`tests/test_models.py`: el fixture `pg_session` usa `Session(connection=conn)` → debe
  ser `Session(bind=conn)`~~ — **resuelto** (deuda técnica — suite 100% verde, ver arriba).
- ~~`test_match_todos_procesa_todos_perfiles` no se aísla~~ — **resuelto**: ahora compara un
  delta (perfiles ajenos activos antes + los 4 del dataset), no un total absoluto.
- ~~`test_jobs_run_job_ca`: skip (pega a la API real sin mock)~~ — **resuelto**: migrado a
  `respx`, ya no hay skip; de paso se detectó y mockeó otro test (`test_jobs_run_token_correcto`,
  `job="all"`) que también pegaba a la red real.
- ~~`test_fts_encuentra_sin_tilde`/`test_fts_compra_agil` sin filtro por código propio~~ —
  **resuelto** (hallazgo de esta misma fase, quedaba oculto detrás del bug de `pg_session`).
- Branch `dev` tiene perfiles/seguimientos/matches de prueba (ids ~49–52) de la validación →
  limpiar (tarea de DATOS/SQL en `dev`, la hace Boris — no es cambio de código, fuera de
  alcance de la fase de deuda técnica de tests).
- ~~Análisis de competencia: badge "Adjudicatario" en todas las filas; resumen solo lista
  ganadores~~ — **resuelto en F10 parte 3**: `resumen_competencia` ahora incluye también a
  quienes ofertaron y no ganaron (`items_ofertados`/`items_ganados`/`total_adjudicado` por
  proveedor), y el badge "Ganó" solo aparece en la(s) fila(s) ganadora(s).
- Adjudicadas en BD con `fecha_publicacion`/`fecha_cierre` NULL (calidad de datos; revisar
  el refresh de estados terminales — investigación aparte, no mezclar con la limpieza de
  tests de esta fase).
- Compra Ágil: el listado v2 está dejando `fecha_publicacion`/`fecha_cierre` NULL en BD
  para CA, incluidas `publicada`. Fix aplicado en matching: una CA `publicada` se considera
  abierta aunque `fecha_cierre` sea NULL; deuda pendiente: investigar y corregir el parser/
  captura de la fecha real de cierre de CA sin mezclarlo con este fix.
- Migración `e1f4a7c9b2d6` (`MatchFeedback`) sigue sin aplicarse en `dev` (`alembic current`
  = `9a1e6b2c5d7f`, detrás del head) — pendiente que Boris corra `alembic upgrade head`; no
  bloqueó la verificación `@needs_postgres` de esta fase porque ninguno de esos tests toca
  `MatchFeedback`.
- **dev (Neon) con credencial vencida:** el `DATABASE_URL` del `.env` (branch dev, endpoint
  `ep-wandering-tooth-atx4f4q8`) da `password authentication failed for user 'neondb_owner'`.
  Bloquea `alembic` y los `pytest @needs_postgres` locales. Refrescar la connstring desde
  Neon (Branches → dev → Connection string) y reemplazar la línea `DATABASE_URL=` del `.env`.
- **Tests `@needs_postgres` (incluidos los 3 nuevos de CA) sin correr contra Postgres real**
  por lo anterior — se saltan (`20 skipped`). El resto de la suite verde (`512 passed`).
  Correrlos cuando la credencial de dev esté al día.
- **Región en licitaciones (limitación de diseño):** `Licitacion` no guarda región y el filtro
  de región del matching solo aplica a Compra Ágil (`_candidatos_ca`); las licitaciones lo
  ignoran (pasan todas). La región no viene en el listado `activas`. La UI debería aclarar que
  el filtro de región aplica solo a CA.
- ~~**Ítems UNSPSC de licitaciones en cero:** `/salud` mostró `datos_abiertos_lic:
  licitaciones=0 items=0` — el recall por rubro de licitaciones queda cojo.~~
  **Resuelto en F-unspsc-lic.** Diagnóstico: el sync solo miraba el mes vigente, pero
  `lic-da/{yyyy}-{m}.zip` es mensual; licitaciones aún PUBLICADA pueden haber sido
  publicadas en meses anteriores, dejando intersección vacía. Fix: ventana configurable
  mes vigente + `DATOS_ABIERTOS_MESES_ATRAS` anteriores (default 3), cursor por mes
  `datos_abiertos_lic:{anio}-{mes}`, corte temprano cuando ya no quedan objetivos y
  notas de `/salud` con meses escaneados/descargados.
- **Watch de cuota API:** el 2026-07-07 los logs mostraron la cuota diaria (9000) agotada de
  madrugada por jobs de detalle v1 (backfill nocturno + `run_match` c/30 min piden detalle);
  en vivo estaba 201/9000. CA comparte el presupuesto. Si vuelve a agotarse: priorizar/reservar
  cuota para CA y capar el backfill nocturno.

## Cierre de sesión 2026-07-07
- **En vivo en prod hoy:** F-automatch (crear/editar perfil dispara matching), F-passwords
  (cambio/reseteo de contraseña), F-notificaciones (+fix `fecha_match` inmutable), fix CA
  (candidatos incluye publicadas con `fecha_cierre` NULL).
- **Pendiente de push:** `a8d81e0` (F-onboarding: tutorial + novedades), `8b00895` (docs),
  `7fb3662` (fix test). Al pushear, prod aplica sola la migración additiva `f3a9b8c7d6e5`
  (columnas `usuarios.tutorial_visto` y `usuarios.novedades_visto_hasta`). En dev queda
  pendiente por la credencial vencida.

## Roadmap pendiente (detalle en docs/03-roadmap.md)
- **F10 UX:** COMPLETA (perfiles, dashboard, ficha y mail).
- **F11:** feedback like/dislike con reponderación ligera (regresión logística, sin LLM) —
  la señal ya se registra en `MatchFeedback` (F10 parte 2), falta el modelo que la consuma.
- **Candidatos priorizados (sesión 2026-07-07):**
  - Relevancia del correo-resumen: que el conteo "encontramos N" respete `min_score` y no
    infle con ruido rubro/organismo-only.
  - Matches obsoletos al editar un perfil: no se borran los que dejan de aplicar tras
    restringir criterios (ahora visible porque editar dispara matching, F-automatch).
  - Recalibrar `feed_min_score_default` (hoy 40, INFERIDO) con la distribución real de prod.
  - Capturar la `fecha_cierre`/`fecha_publicacion` real de CA desde el v2 (parser) — hoy NULL.
  - ~~Ítems UNSPSC de licitaciones en 0 (ver Deudas) — arregla el recall por rubro.~~
    Resuelto en F-unspsc-lic con ventana mensual y cursores por mes.
  - Tasas de cambio hardcodeadas (UF/UTM/USD/EUR) probablemente desactualizadas.
- **Backlog:** worker offline de anexos en Raspberry Pi (OCR + embeddings), condicionado.

## Mapa de documentos
- `00-estado-actual.md` (este) · `01-analisis-api-mercado-publico.md` (contrato/gotchas API)
- `02-plan-desarrollo-y-auditoria.md` · `03-roadmap.md` (historial de fases + pendientes)
- `04-datos-abiertos.md` (lic-da: ítems/UNSPSC) · `05-competencia.md` (ofertas/ganador)
- `07-plan-anual.md` (PAC: spike + veredicto) · `08-datos-organismos.md` (sector: spike + veredicto)
- `arquitectura.md` · `despliegue.md` · `operacion.md`

*Reglas duras del proyecto (API, free tier, multiusuario, arquitectura): ver `CLAUDE.md`.*
