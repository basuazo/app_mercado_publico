# ¿Se puede seguir en $0? — evaluación de alternativas

**Fecha:** 2026-08-20
**Pregunta:** agotar las opciones para seguir en free; si ninguna sirve, la ruta que cueste menos.
**Contexto:** `docs/11-auditoria-ping-y-arquitectura.md` §1 y §5.

> **[V]** verificado en fuente primaria · **[I]** inferido · **[?]** pendiente de confirmar

**Respuesta corta: sí, se puede seguir en $0, y la opción que lo logra es mejor que la
arquitectura actual en casi todos los ejes.** No es "aguantar el free tier con parches": es
mover los jobs a donde correr en batch es gratis por diseño.

---

## 1. El hallazgo que cambia el análisis

`app/ingest/__main__.py` ya expone un CLI completo:

```
python -m app.ingest run-once --job=activas|ca|detalles|lifecycle|catalogos|
                                    retencion|match|alerts|resumen|datos-abiertos|competencia
```

Los once jobs corren desde línea de comandos, sin la app web, sin el scheduler, sin HTTP. Ese
CLI se construyó en F3 y nunca se usó en producción. Es la pieza que habilita la opción de
abajo.

---

## 2. Opción D — GitHub Actions corre los jobs (recomendada, $0)

**El diseño:** el servicio web de Render free queda **solo como interfaz de usuario** y duerme
cuando nadie la usa. Los jobs se van a GitHub Actions, en workflows programados que ejecutan el
CLI contra la misma base Neon.

```
Antes:  cron-job.org --ping--> Render (despierto 24/7) --> scheduler interno --> jobs --> Neon
Ahora:  GitHub Actions (cron) --> python -m app.ingest run-once --job=X --> Neon
        Render free (dormido) --se despierta cuando Boris abre la app--> Neon
```

**Lo que gana:**

| | Hoy | Opción A (cron→endpoint) | Opción D (Actions) |
|---|---|---|---|
| Horas de instancia Render / 750 | ~744 (99%) | ~108 (14%) [I] | solo uso real, ~10–30 [I] |
| Keepalive | cada 10 min | no | no |
| Tráfico saliente desde Render free | todo el día | en cada job | **ninguno** |
| Job cortado a mitad de camino | — | sí, a los 15 min | no (límite 6 h en Actions) [V] |
| Historial de corridas y aviso de fallas | no hay | hay que construirlo | **incluido** [V] |
| Costo | $0 | $0 | $0 |

Tres cosas merecen subrayarse:

**a) Saca la app del supuesto que Render se reserva cortar.** La doc de Render dice que puede
suspender un servicio free que inicia un volumen inusualmente alto de tráfico hacia internet, y
menciona explícitamente invocar APIs externas y acceder a bases externas. [V] Con la opción D
esas llamadas ocurren en los runners de GitHub, no en Render. El riesgo desaparece, no se
reduce.

**b) Resuelve gratis el §6 de la auditoría.** El problema de fondo del incidente no fue el
ping, fue que nadie se enteró. GitHub Actions trae de fábrica historial de corridas, logs
retenidos y notificación por correo cuando un workflow programado falla. Eso es exactamente el
dead-man's switch que en la opción A había que construir (tabla `job_runs` + endpoint +
monitor). Acá viene incluido.

**c) Neon deja de estar en el filo.** El plan free da 100 CU-horas por proyecto y el
auto-suspend a los 5 min no se puede desactivar. [V] Con ~13 despertadas cortas al día en vez de
un job cada 30 minutos, el consumo cae muy por debajo del techo.

**Lo que cuesta:**

- **Los secretos viven en un lugar más.** `DATABASE_URL`, `MP_TICKET` y `BREVO_API_KEY` pasan a
  ser también GitHub repo secrets. Con A1 [CRÍTICO-1] abierto, esto **exige** rotar primero y
  usar valores nuevos. No es un detalle que se deje para después.
- **Minutos de Actions:** cuenta free = 2.000 min/mes en repos privados; en repos públicos los
  runners estándar son gratis sin tope. [V] Estimación: ~13 corridas/día × ~2 min ≈ 780 min/mes.
  Cabe, con margen, pero es una bolsa compartida con cualquier otro repo privado tuyo. [I]
- **Workflows programados se desactivan tras 60 días sin actividad en el repo.** [V] Con
  desarrollo activo no aparece; si el proyecto se congela unos meses, hay que reactivarlos a
  mano (o dejar un workflow keepalive que haga un commit vacío).
- **El cron de Actions es best-effort**, puede atrasarse minutos en horas de carga alta. Para
  una licitación que cierra en días, da igual. La ventana nocturna la sigue validando la app con
  `ZoneInfo("America/Santiago")`, no el cron.
- **El runtime queda en dos lugares.** Es el costo conceptual real: hay que mirar dos paneles.
  Se compensa con que uno de los dos (Actions) es el que avisa solo.

**Lo que hay que arreglar antes:** además de los bloqueantes 1–4 del §4 de la auditoría, la
opción D descubre uno propio — **el CLI no expone `nocturno` ni el backfill**. `_JOBS` en
`__main__.py` tiene once entradas y ninguna es el ciclo nocturno, así que llamar
`datos-abiertos`, `lifecycle` y `competencia` por separado **saltea el guard
`en_ventana_nocturna` y deja fuera `run_backfill_fecha`** (`orchestrator.py:264-290`). Sin
eso, el backfill del día anterior nunca corre. Hay que agregar `nocturno` al CLI y al endpoint.

En cambio los bloqueantes 2, 3 y 5 dejan de ser bloqueantes bajo la opción D: el CLI ya tiene
`retencion` y `catalogos`, cada job es su propio paso del workflow (no depende de que
`_full_cycle` incluya `resumen`), y no hay spin-down que corte el job. Siguen siendo bugs que
conviene arreglar para el camino manual de reparación, pero ya no bloquean.

---

## 3. Las otras opciones free que se descartaron

| Opción | Por qué no |
|---|---|
| **Cron Jobs nativos de Render** | No son free: la doc oficial declara un mínimo de **US$1/mes por cron job service**. [V] Con 6 schedules distintos son US$6/mes y ni siquiera resuelven la observabilidad. Hay fuentes de terceros que los dan como gratis; están equivocadas. |
| **Oracle Cloud Always Free** | Sigue existiendo y sigue siendo always-free, pero en julio de 2026 Oracle **bajó la asignación Ampere A1 de 4 OCPU/24 GB a 2 OCPU/12 GB sin anuncio público**. [V] Da un proceso always-on gratis de verdad, pero agrega un servidor a administrar a mano, sin deploys gestionados y con riesgo de reclamación de instancias idle. El cuello de botella seguiría siendo Neon. Ahorra US$7/mes a cambio de horas tuyas. |
| **Fly.io free tier** | Ya no existe para clientes nuevos; solo lo conservan cuentas legadas de los planes Hobby/Scale. [V] |
| **Seguir con el keepalive, bajando la frecuencia** | No sirve: el spin-down es a los 15 min, así que cualquier ping que lo evite mantiene el servicio despierto 24/7 y consume las mismas 744 horas. No hay una frecuencia intermedia que ayude. |

---

## 4. Si igual hay que pagar: la escala de menor a mayor

Solo tiene sentido si el arranque en frío de ~1 min molesta a los usuarios, o si la app pasa a
ser carga real del plan comercial y no se tolera ninguna fragilidad.

| # | Qué | Costo | Esfuerzo | Qué resuelve |
|---|---|---|---|---|
| 1 | **Opción D** | **$0** | medio (una fase) | Todo menos el arranque en frío del usuario |
| 2 | **Render Starter** | US$7/mes | mínimo — cambiar el plan en el panel | Sin spin-down ni techo de horas; el scheduler interno funciona como fue diseñado; no hace falta ping ni cron externo |
| 3 | Fly.io, máquina 512 MB always-on | US$3,32/mes [V] | alto — migrar de plataforma | Lo mismo que Starter, US$3,7 más barato |
| 4 | VPS (Hetzner CX22 y similares) | ~€3,8/mes | muy alto — sysadmin completo | Lo mismo, y de paso saca a Neon del cuadro |
| 5 | Neon Launch | US$19/mes | mínimo | Solo si muerden los 0,5 GB o las 100 CU-horas. **Todavía no es el caso** |

**La opción 3 no la recomiendo aunque sea más barata que la 2.** Ahorrar US$3,7 al mes —
US$44 al año — no paga una migración de plataforma más el aprendizaje de Fly. La 4 menos aún.
Si en algún momento hay que pagar, el salto correcto es directo a Render Starter: son dos
clicks y ninguna línea de código.

---

## 5. Recomendación

**Opción D, con el endpoint arreglado como camino manual de reparación.**

No son excluyentes y conviene tener los dos:

- **GitHub Actions** dispara los jobs programados. Es el mecanismo de producción.
- **`POST /api/jobs/run`** (con los bloqueantes arreglados) queda como el "heal post-deploy" y
  para forzar un ciclo fuera de horario, que es para lo que sirve.
- **Se elimina el keepalive** y el `BackgroundScheduler` sale del lifespan de la app. El
  scheduler queda disponible para desarrollo local vía `run-scheduler`, que ya existe.

Presupuesto: **US$0/mes.** El siguiente escalón, si hace falta, es Render Starter a US$7/mes,
y la decisión se puede tomar después sin rehacer nada — la opción D no la bloquea.

**Condición previa no negociable:** rotar los secretos de A1 [CRÍTICO-1] **antes** de cargarlos
en GitHub. La opción D agrega un lugar donde viven credenciales; hacerlo con las actuales sería
propagar un hallazgo crítico abierto en vez de cerrarlo.

---

## 6. Orden de trabajo actualizado

| Fase | Qué | Depende de |
|---|---|---|
| **F-jobs-endpoint** | Los bloqueantes: advisory lock en todos los caminos, `nocturno`/`retencion`/`catalogos` en endpoint y CLI, `resumen` en `_full_cycle`, borrar el `_make_app()` duplicado, corregir `operacion.md` | nada — son bugs reales hoy, sirven en cualquier escenario |
| **F-secretos** | Rotar todo lo del `.env`, dejarlo sin valores de producción | nada |
| **F-actions** | Workflows programados con la cadencia del §3 de la auditoría, quitar el keepalive, sacar el scheduler del lifespan | F-jobs-endpoint + F-secretos |
| **F-tasas** | UF/UTM/USD/EUR desde `mindicador.cl` en vez de constantes de 2025 | nada |

El prompt de **F-jobs-endpoint** está en `docs/prompt-F-jobs-endpoint.md` y no depende de que
decidas la arquitectura: arregla lo que está roto hoy y habilita las dos opciones.
