# Auditoría — el ping, el free tier y la arquitectura de jobs

**Fecha:** 2026-08-20
**Alcance:** `render.yaml`, `app/api/main.py`, `app/api/routes/api.py`,
`app/ingest/orchestrator.py`, `docs/despliegue.md`, `docs/operacion.md`, más verificación
de las políticas vigentes de Render y Neon en su documentación oficial.
**Precede a:** las auditorías A1 (seguridad, jun-2026) y A3 (calidad, jun-2026), que siguen
válidas y no se repiten acá.

> Convención de confianza (regla 23 de CLAUDE.md): **[V]** verificado en fuente primaria,
> **[I]** inferido, **[?]** pendiente de que Boris lo confirme en un panel al que el
> asistente no tiene acceso.

---

## 1. El diagnóstico de fondo

El ping no es un detalle de operación que se rompió. Es el punto donde la arquitectura
choca con la plataforma, y estaba destinado a romperse.

El diseño actual pone el scheduler (APScheduler `BackgroundScheduler`) **dentro del proceso
web** (`app/api/main.py:67-83`). Render free duerme ese proceso a los 15 minutos sin tráfico
entrante, y con él muere el scheduler. La solución adoptada fue un pinger externo cada 10
minutos a `/api/salud/ping` (`docs/despliegue.md` §3), es decir: mantener el proceso
despierto 24/7 para que los jobs corran.

Ese arreglo tiene tres problemas, y los tres son estructurales:

**a) La cuenta de horas no cierra. [V]**
Render otorga **750 horas de instancia por mes y por workspace**, y un servicio despierto
las consume mientras corre; los servicios dormidos no consumen. Agosto tiene 31 días = **744
horas**. El pinger cada 10 minutos garantiza que el servicio nunca duerma, o sea 744 de 750
horas consumidas. El margen es de **6 horas al mes (0,8%)**. Cada redeploy levanta dos
instancias un rato, y cualquier otro servicio free en el mismo workspace compite por la misma
bolsa. Cuando la bolsa se agota, Render **suspende todos los servicios free del workspace
hasta el primer día del mes siguiente**.

**b) El pinger es exactamente el patrón que Render se reserva el derecho de cortar. [V]**
La documentación de Render dice que puede suspender un servicio free que *inicia* un volumen
inusualmente alto de tráfico hacia internet, y da como ejemplos acceder a bases de datos
externas, invocar APIs externas y transferir datos a almacenamiento externo. Esta app hace
las tres cosas, todo el día, desde un servicio free. La restauración exige pasar a plan
pagado.

**c) El costo real de mantenerse despierto no es solo Render. [V]**
El plan free de Neon da **100 CU-horas por proyecto por mes** (≈400 horas a 0,25 CU) y el
auto-suspend a los 5 minutos **no se puede desactivar**. El job `ca_incremental` corre **cada
30 minutos** (`orchestrator.py:307-316`) y arrastra `match` y `alerts` detrás. Con ese ritmo
Neon casi nunca alcanza a dormirse, y encima el free autoescala hasta 2 CU bajo carga, lo que
quema CU-horas más rápido que la cuenta base. Al agotarse, **Neon suspende el compute del
proyecto hasta el siguiente período de facturación**.

Lo importante: **(a) y (c) se curan solos al cambiar el mes.** Un servicio suspendido por
cuota se ve idéntico a un bug, y vuelve a funcionar sin que nadie arregle nada. Eso hace muy
difícil aprender del incidente, y explica por qué esto puede haber pasado antes sin quedar
registrado.

---

## 2. Qué se observó hoy en producción

- `app-mercado-publico.onrender.com` resuelve a
  `gcp-us-west1-1.origin.onrender.com.cdn.cloudflare.net` → el borde de Render está detrás de
  **Cloudflare**. [V]
- La primera carga de `/api/salud/ping` en un navegador real devolvió una interstitial de
  verificación anti-bot ("Un momento… / Verificación de seguridad en curso"), no el JSON
  `{"status":"ok"}`. Las cargas siguientes fallaron a nivel de red, sin respuesta HTTP
  registrada. [V]
- No se pudo determinar el estado del servicio en Render ni el log de ejecuciones del cron
  desde acá. [?]

**Consecuencia operativa del punto 2, sea cual sea la causa raíz:** un desafío de Cloudflare
delante del endpoint rompe el pinger por definición. cron-job.org y UptimeRobot no ejecutan
JavaScript: reciben la página de desafío (403/503) en vez del JSON. Peor: si el monitor está
configurado para considerar "arriba" cualquier respuesta, reporta OK mientras la request
nunca llega a Render, o sea que el servicio duerme igual y el monitor no avisa.

### Hipótesis ordenadas por probabilidad

| # | Hipótesis | Cómo se confirma | Se cura sola |
|---|---|---|---|
| 1 | Horas de instancia free agotadas → workspace suspendido | Render → panel del servicio: estado "Suspended" y el aviso de free instance hours | Sí, el 1 de septiembre |
| 2 | CU-horas de Neon agotadas → compute suspendido | Neon → Usage del proyecto: CU-hours consumidas vs 100 | Sí, al cerrar el período |
| 3 | Desafío anti-bot de Cloudflare delante del servicio | cron-job.org → historial: códigos 403/503 y cuerpo de respuesta | No |
| 4 | El cron quedó pausado o con la URL vieja | cron-job.org → estado del job y última ejecución OK | No |

Los cuatro se descartan o confirman en **10 minutos** mirando tres paneles: Render (estado +
horas), Neon (CU-hours), cron-job.org (historial de códigos de respuesta). Vale la pena hacerlo
antes de tocar código, porque la hipótesis 1 y la 2 no se arreglan con un fix y la 3 y la 4 sí.

---

## 3. La corrección de fondo: invertir el modelo

Hoy: **el cron mantiene despierto al proceso para que el scheduler interno dispare los jobs.**
Propuesta: **el cron dispara los jobs directamente y el proceso duerme el resto del tiempo.**

El endpoint ya existe y ya está protegido: `POST /api/jobs/run?job=<nombre>` con header
`X-Jobs-Token` (`app/api/routes/api.py:204-260`). Nunca se usó como mecanismo principal, solo
como "job backup" cada hora.

Con el modelo invertido:

- Cada POST del cron **despierta** el servicio (~1 min de arranque en frío), corre el job y lo
  deja dormirse 15 minutos después.
- Horas de instancia estimadas: ~13 despertadas/día × ~16 min ≈ **3,5 h/día ≈ 108 h/mes**,
  contra 744 hoy. **14% de la bolsa** en vez de 99%. [I — depende de la duración real de cada job]
- Las CU-horas de Neon bajan en la misma proporción, porque la base solo despierta cuando hay
  job.
- El scheduler interno deja de ser el mecanismo de producción. Puede quedar para desarrollo
  local (`python -m app.ingest run-scheduler`, que ya existe) o eliminarse del lifespan.
- Desaparece el patrón de tráfico que Render se reserva el derecho de cortar.

**El costo:** el usuario que abre la app fuera de una ventana de job espera ~1 minuto a que
Render la levante. Para una herramienta interna de 3–10 personas que se consulta una o dos
veces al día, es un precio razonable. Si molesta, se compra un plan pagado (§5).

### Cadencia propuesta

La cadencia actual está calibrada para un proceso siempre despierto, donde correr un job
"extra" es gratis. En el modelo invertido cada disparo cuesta horas, así que hay que
justificarlo. `ca` cada 30 minutos son 48 despertadas diarias, y la unidad de decisión acá es
una licitación que cierra en días: nada de valor se pierde bajando el ritmo.

| Job | Hoy | Propuesta | Cron (UTC) |
|---|---|---|---|
| `ca` (+match+alerts) | cada 30 min | cada 2 h, 08–20 Chile | `0 12,14,16,18,20,22,0 * * *` |
| `activas` (+detalles+match+alerts) | 08, 13, 18 Chile | igual | `0 12,17,22 * * *` |
| `nocturno` (datos-abiertos→lifecycle→competencia→backfill) | 23:30 Chile | igual | `30 3 * * *` |
| `resumen` | `DIGEST_HOUR`:05 Chile | igual | `5 12 * * *` |
| `retencion` | 03:00 Chile | igual | `0 7 * * *` |
| `catalogos` | lunes 02:00 Chile | igual | `0 6 * * 1` |

≈13 disparos al día. La ventana nocturna la sigue validando la app con
`ZoneInfo("America/Santiago")` (`en_ventana_nocturna`), no el cron — el cron corre en UTC y no
se le cree la hora (regla 5 de CLAUDE.md).

---

## 4. Lo que hay que arreglar ANTES de mover el cron

El modelo invertido no funciona hoy tal cual. Estos cinco puntos son bloqueantes, y cuatro de
ellos son bugs que ya existen aunque no se cambie nada.

**[BLOQUEANTE-1] `/api/jobs/run` no toma el advisory lock.**
El endpoint llama `run_sync_ca(settings, engine)` y compañía **directo**, sin pasar por
`_run_with_lock` (`api.py:229-250` vs `orchestrator.py:224`). La regla 13 de CLAUDE.md dice que
cada ciclo de ingesta toma `pg_advisory_lock`. Hoy el riesgo es concreto: el "job backup"
horario del cron puede solaparse con el job de 30 minutos del scheduler interno, gastando cuota
API el doble sobre el mismo trabajo. Si el cron pasa a ser el mecanismo principal, esto deja de
ser un riesgo y se vuelve el camino normal. Fix: envolver cada entrada del dict `_jobs` y cada
paso de `_full_cycle` en `_run_with_lock`.

**[BLOQUEANTE-2] `retencion` y `catalogos` no son alcanzables por el endpoint.**
El dict `_jobs` tiene nueve entradas: `ca`, `activas`, `detalles`, `datos-abiertos`,
`lifecycle`, `match`, `competencia`, `alerts`, `resumen`. Los jobs `retencion` y `catalogos`
existen en el scheduler (`orchestrator.py:355-373`) pero **no en el endpoint**, así que solo
corren si el proceso está despierto a las 03:00 y los lunes a las 02:00. Sin eso, la purga de
retención nunca corre y los 0,5 GB de Neon se llenan solos. De paso: `docs/operacion.md` §9
documenta `POST /api/jobs/run?job=retencion` como el modo de forzar la purga — **eso devuelve
400 hoy**. Es una discrepancia entre doc y código, no un malentendido.

**[BLOQUEANTE-3] `_full_cycle` no incluye `resumen`.**
`job=all` corre activas → detalles → datos-abiertos → lifecycle → match → competencia →
alerts, y ahí termina. En un mundo donde el cron solo llama `all`, **nunca se envía el
correo-resumen**. Hoy queda cubierto porque el scheduler interno tiene su propio job `resumen`.

**[BLOQUEANTE-4] No hay un job `nocturno` en el endpoint.**
El guard de ventana horaria vive en `_ciclo_nocturno` (`orchestrator.py:264-290`), que no está
expuesto. Llamar los pasos por separado desde el cron **saltea el guard** y puede disparar el
backfill pesado en horario diurno, contra la regla 5. Hay que exponer `job=nocturno` →
`_ciclo_nocturno`, con el guard intacto.

**[BLOQUEANTE-5] Un job largo puede morir a mitad de camino.**
Render duerme el servicio 15 minutos después de la última request **entrante**, sin importar si
el proceso está ocupado. El `BackgroundTask` de `_full_cycle` puede pasarse de ese plazo y
recibir SIGTERM. Mitigante real: los jobs son idempotentes y con cursor en Postgres (reglas 10 y
"jobs idempotentes"), así que un corte no corrompe, se retoma. Aun así conviene: partir el ciclo
nocturno en disparos separados en vez de un `all` monolítico, y no confiar en que un job de >10
min termine.

---

## 5. La pregunta incómoda: ¿sigue siendo correcto el objetivo de $0?

El objetivo "costo de operación $0" está escrito como regla dura del proyecto. Vale revisarlo,
porque el §1 muestra que ese $0 no es gratis: se paga en fragilidad, en dos modos de falla
invisibles que se curan solos, y en horas tuyas diagnosticando.

Las opciones reales, verificadas:

**A. Modelo invertido, sigue en free — $0.**
Es la recomendación. Baja el consumo de 99% a ~14% de la bolsa de horas, saca el patrón de
tráfico que Render puede cortar, y usa un endpoint que ya existe. Exige los cinco fixes del §4.
Costo: arranque en frío de ~1 min para el usuario.

**B. Render Starter — US$7/mes.**
Sin spin-down, sin techo de horas de instancia, el scheduler interno funciona como está
diseñado, no hace falta ping ni cron externo. Neon free sigue siendo el límite que puede
morder, así que la cadencia igual conviene bajarla. Es la opción honesta si la app pasa a ser
parte del plan comercial y no un experimento.
*Nota: los Cron Jobs nativos de Render **no** son free — la doc oficial declara un mínimo de
US$1/mes por cron job service. [V]* La fuente de terceros que dice lo contrario está
equivocada.

**C. Mover el proceso siempre-despierto a Oracle Cloud Always Free — $0.**
Sigue existiendo y sigue siendo always-free, aunque en julio de 2026 Oracle **redujo la
asignación de Ampere A1 de 4 OCPU/24 GB a 2 OCPU/12 GB sin anuncio público**. [V] Da un proceso
always-on real y gratis. **No la recomiendo**: agrega un servidor que administrar a mano, sin
deploys gestionados, con riesgo de reclamación de instancias idle, y el cuello de botella
seguiría siendo Neon. El ahorro son US$7/mes; el costo son horas tuyas.

**Recomendación: A ahora. B cuando la app sea carga real del plan comercial. C no.**

---

## 6. El hallazgo que hace falta arreglar aunque se resuelva el ping

**[ALTO] No hay observabilidad de los jobs. Es la razón por la que esto se descubrió tarde.**

`_run_with_lock` captura **toda** excepción, la loguea y devuelve `None`
(`orchestrator.py:246-248`). Un job que falla en forma persistente se ve igual que un job que
no tuvo trabajo. `/api/salud` muestra estado, pero exige login de admin y que alguien se acuerde
de mirar. No hay nada que avise.

Esto ya cobró su precio: el fix del 500 de Compra Ágil quedó anotado en
`docs/00-estado-actual.md` como "pendiente que Boris verifique post-deploy que el cron pasa de
ERROR a OK", y esa verificación es manual porque no hay otra forma.

Propuesta concreta y barata:

1. Tabla `job_runs` (`job`, `iniciado_en`, `terminado_en`, `estado`, `resultado_json`,
   `error`). `_run_with_lock` escribe una fila por corrida. Da historial, y de paso responde
   "¿cuándo corrió esto por última vez?" sin leer logs de Render.
2. Endpoint público `GET /api/salud/jobs` que devuelve **503** si algún job crítico no tiene una
   corrida OK dentro de su ventana esperada, y 200 si todo está al día. Sin secretos, igual que
   `/api/salud/ping`.
3. Un monitor de cron-job.org apuntado a ese endpoint, con notificación por correo en respuesta
   no-2xx. Eso es un dead-man's switch: avisa cuando los jobs se detienen, sea porque el
   servicio está suspendido, porque Neon se agotó, o porque el cron se cayó.

Es el único cambio de esta auditoría que convierte "me di cuenta semanas después" en "me llegó
un correo el mismo día".

---

## 7. Hallazgos menores, con archivo y línea

**[MEDIO-1] `_make_app()` se ejecuta dos veces.**
`app/api/main.py:133` crea una instancia de módulo (`app = _make_app()`) y `render.yaml`
arranca con `--factory app.api.main:_make_app`, que la crea otra vez. La instancia de módulo
nunca sirve tráfico y su lifespan nunca corre — no arranca un segundo scheduler — pero duplica
el objeto FastAPI, el registro de rutas y el `Engine` en una instancia de 512 MB, y obliga a
tener todas las env vars presentes para cualquier import del módulo. Fix: borrar la línea 133 o
protegerla.

**[MEDIO-2] `docs/operacion.md` §9 afirma que el pinger mantiene despierta a Neon.**
Es falso: `/api/salud/ping` devuelve un dict literal y no toca la base
(`app/api/routes/api.py:37-39`). Ese es el **diseño correcto** — un ping que consultara la base
quemaría CU-horas de Neon 24/7 y agotaría el plan free. El problema es que la doc invita a
"mejorar" el ping justo en la dirección que rompería todo. Corregir el texto.

**[MEDIO-3] Nada monitorea las CU-horas de Neon.**
`/salud` reporta `base_datos.porcentaje`, o sea el **tamaño** contra 0,5 GB. El límite que
probablemente muerda primero son las **100 CU-horas**, y no se mide en ninguna parte. Como
mínimo, dejarlo en el runbook como chequeo mensual; mejor, exponerlo en `/salud`.

**[MEDIO-4] Las tasas de cambio están congeladas.**
`tasa_uf=37000`, `tasa_utm=65000`, `tasa_usd=950`, `tasa_eur=1030`
(`app/core/settings.py:52-55`), valores plausibles para 2025. A agosto de 2026 están
desactualizados, y como los montos entran al filtro de perfiles y al score de competencia, el
matching está sesgado. Ya está anotado como candidato en el roadmap; conviene subirlo de
prioridad porque afecta resultados, no solo presentación. `mindicador.cl` entrega UF, UTM y
dólar sin autenticación y sin costo — un job diario liviano lo resuelve.

**[MEDIO-5] Deriva de migraciones con `alembic upgrade head` automático en prod.**
`render.yaml` corre `alembic upgrade head` en el startCommand, y la branch `dev` de Neon está
detrás del head por la credencial vencida (`docs/00-estado-actual.md`). La combinación es la
riesgosa: prod aplica sola una migración que nunca se probó contra un Postgres real. Refrescar
la connstring de `dev` no es una tarea de limpieza, es lo que restituye la red de seguridad.

**[BAJO-1] `feed_min_score_default = 40` sigue siendo INFERIDO.**
Calibrado sobre 10 filas de `dev`. Es el parámetro que decide qué ve el usuario en el
dashboard. Con datos de prod ya disponibles, recalibrarlo es media hora.

---

## 8. Revisión de las auditorías anteriores

**A1 [CRÍTICO-1] sigue abierto y es el hallazgo más grave del proyecto.**
`.env` tiene credenciales de producción reales y vigentes (ticket MP, dos connstrings de Neon
con usuario y contraseña, `SECRET_KEY`, `JOBS_TOKEN`, contraseña de Brevo, contraseña de
admin). A1 recomendó rotarlas **todas** en junio de 2026. Dos meses después conviene confirmar
si se hizo, porque el propio informe de auditoría las transcribe en texto plano — y `.gitignore`
excluye `audits/`, así que ese archivo no está versionado pero sí está en disco.

El escenario que A1 describía como riesgo ("copia, snapshot, IDE upload, compartir pantalla")
ocurrió durante esta misma auditoría: para poder leer el repo se generó un tar del árbol de
trabajo, que incluyó `.env`. Quedó movido a `_to_delete/_mp_snapshot.tar.gz` para que lo borres.
Mientras `.env` tenga valores de producción, cualquier operación sobre la carpeta los arrastra.

**A1 [MEDIO-1] (XSS vía `{{ p.nombre }}` en `onsubmit`) es un falso positivo.**
El informe sostiene que Jinja2 no escapa la comilla simple. Sí la escapa: MarkupSafe convierte
`'` en `&#39;` (verificado ejecutando `markupsafe.escape` sobre el payload exacto del informe,
`'; alert(1); var x='`). Con el autoescape que activa `Jinja2Templates`, el atributo no se rompe.
No gastes tiempo ahí. Los otros hallazgos de A1 no se revisaron en esta pasada y se asumen
vigentes.

---

## 9. Orden de trabajo propuesto

**Paso 0 — diagnóstico, antes de tocar código (10 min, lo haces tú).**
Render: estado del servicio y horas de instancia consumidas. Neon: CU-horas del mes.
cron-job.org: historial de códigos de respuesta del keepalive. Con eso queda claro si estamos
ante suspensión por cuota (hipótesis 1 o 2, se cura sola el 1 de septiembre), ante el desafío de
Cloudflare (3) o ante un cron caído (4).

**Paso 1 — los cinco bloqueantes del §4.** Son bugs reales hoy, independientes de la decisión
de arquitectura, y habilitan el modelo invertido.

**Paso 2 — invertir el modelo (§3).** Crons nuevos con la cadencia propuesta, quitar el
keepalive, sacar el `BackgroundScheduler` del lifespan.

**Paso 3 — observabilidad (§6).** `job_runs` + `/api/salud/jobs` + monitor con aviso por correo.

**Paso 4 — los menores del §7**, empezando por las tasas de cambio (afectan resultados) y la
credencial de `dev` (restituye la red de seguridad de migraciones).

**Transversal — cerrar A1 [CRÍTICO-1]:** rotar los secretos y dejar `.env` sin valores de
producción.

Cada paso es una fase con su commit, según el flujo del proyecto: el asistente redacta el
prompt, tú lo corres en Claude Code, y se audita el resultado.
