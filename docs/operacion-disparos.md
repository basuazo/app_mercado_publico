# Operación — cómo se disparan los jobs (cron-job.org → GitHub Actions)

*Vigente desde F-actions-3 (disparo externo), 25-sep-2026.*

Los jobs corren en **GitHub Actions**, que ejecuta el CLI contra Neon production. Quien los
dispara es **cron-job.org**: llama a la API de GitHub a la hora exacta, en hora de Chile. Los
workflows ya no tienen `schedule`, porque GitHub los atrasaba de 2 a 5,5 h y descartaba
corridas (evidencia en `docs/archivo/prompts/prompt-F-actions-3-disparo-externo.md`).

Render solo sirve la app web. Los crons viejos de cron-job.org que llamaban a
`/api/jobs/run` en Render quedan **pausados**.

---

## 1. Orden la primera vez

1. Push del commit de F-actions-3 (el que quita los `schedule`). Confirma en GitHub que llegó.
2. Crea el token (sección 2).
3. Crea los 7 crons en cron-job.org (sección 3).
4. Prueba uno a mano (sección 4).

Hazlo todo en la misma sesión: entre el push y los crons, no corre nada solo.

---

## 2. Token de GitHub (una vez al año)

1. GitHub → foto de perfil → **Settings** → **Developer settings** → **Personal access tokens**
   → **Fine-grained tokens** → **Generate new token**.
2. Completa:
   - **Token name:** `cron-job-dispatch-mp`
   - **Expiration:** 1 año (o lo máximo que ofrezca). Anota la fecha en tu calendario: cuando
     venza, los disparos empiezan a fallar con 401.
   - **Repository access:** *Only select repositories* → `basuazo/app_mercado_publico`.
   - **Permissions → Repository permissions → Actions:** *Read and write*. Nada más. (*Metadata:
     Read-only* se agrega sola y es obligatoria.)
3. **Generate token** y copia el valor (empieza con `github_pat_`). GitHub no lo vuelve a
   mostrar. No lo pegues en el repo, en el chat ni en ningún archivo.

Qué puede hacer este token: disparar y cancelar workflows de este repo, y ver sus corridas.
Qué **no** puede hacer: leer los secrets, modificar el código ni tocar otros repos.

---

## 3. Los 7 crons en cron-job.org

Para cada fila de la tabla, en cron-job.org → **Create cronjob**:

**Pestaña principal**
- **Title:** el de la tabla (p. ej. `GH ca`).
- **URL:**
  `https://api.github.com/repos/basuazo/app_mercado_publico/actions/workflows/ARCHIVO/dispatches`
  (reemplaza `ARCHIVO` por el de la tabla, p. ej. `ca.yml`).
- **Execution schedule:** *Custom*, con la hora de la tabla.
- Activa **Notify me when execution fails**.

**Pestaña Advanced**
- **Time zone:** `America/Santiago` (así el cambio de hora de Chile se maneja solo).
- **Request method:** `POST`
- **Headers** (cuatro):
  - `Authorization` = `Bearer github_pat_...` (tu token)
  - `Accept` = `application/vnd.github+json`
  - `X-GitHub-Api-Version` = `2022-11-28`
  - `Content-Type` = `application/json`
- **Request body:** `{"ref":"main"}`

| Title | ARCHIVO | Hora de Chile |
|---|---|---|
| GH ca | `ca.yml` | cada hora, minuto 05 (todos los días, las 24 h) |
| GH ciclo-match | `ciclo-match.yml` | 08:50, 10:50, 12:50, 14:50, 16:50, 18:50, 20:50 |
| GH ciclo-activas | `ciclo-activas.yml` | 10:15, 14:15, 19:15 |
| GH nocturno | `nocturno.yml` | 01:10 |
| GH retencion | `retencion.yml` | 05:40 |
| GH catalogos | `catalogos.yml` | lunes 06:35 (catalogos + plan-anual + vocabulario-rubros, en ese orden) |
| GH resumen | `resumen.yml` | 08:30 |
| GH mercado | `mercado.yml` | cada 15 min (00, 15, 30, 45), todos los días — **F-merc-1, sin crear todavía** |
| GH mercado-oc | `mercado-oc.yml` | 07:20 Chile, diario — **F-merc-2, sin crear todavía** (sin API: no cuenta para la cuota; si `oc-da` da 403 desde Actions, se carga desde el PC) |
| GH mercado-nocturno | `mercado-nocturno.yml` | 22:30 — **F-merc-1, sin crear todavía** |
| GH mercado-indices | `mercado-indices.yml` | 07:45 Chile, diario — **F-plat-4, sin crear todavía** (sin API; corre `mercado-indices` y luego `mercado-competencia`; ver nota abajo) |

**`mercado-indices` (F-plat-4).** De noche no hay hueco: el lock lo tienen `mercado-nocturno` (22:30–06:45),
`nocturno` (01:10), `retencion` (05:40) y `mercado-oc` (07:20); a las 04:00 saldría siempre `omitido`. A las 07:45
choca con el disparo de `mercado` de la misma hora: el workflow espera el lock hasta 10 min. Medir la duración de
las primeras corridas (`duracion_s` en el resultado y en `job_runs`): si pasa de ~10 min, los `mercado` que caigan
en medio salen `omitido` (aceptado una vez al día; se ponen al día desde el cursor). Las metas no cumplidas salen
como `::warning::` sin poner el job en rojo. La carga completa (`--completo`) se corre desde el PC.

**`mercado-competencia` (F-plat-comp).** Mismo workflow, después de `mercado-indices` (`jobs: "mercado-indices
mercado-competencia"`; 1 min de pausa entre jobs y otra espera del lock de hasta 10 min, porque el `mercado` de
las 08:00 puede tomarlo en medio). Sin API. Recalcula `competencia_mes` y `competencia_comprador_mes` del mes
anterior y del actual (`--completo`: todos los meses con ofertas), corre el juez de predicción de competidores de
los últimos 6 meses y el Brier con/sin variables de competencia del último mes cerrado (`evaluacion_modelo`, modelo
`competencia`; `mercado-indices` no borra esas filas). **Medido el 5-nov** en Postgres local con volumen sintético
(1.200 CA/mes × 7 ofertas): job diario **5,3 s** (recálculo de 2 meses 3,1 s). La suma diaria la domina
`mercado-indices` (19 min el `--completo`); `timeout_min` del workflow pasó de 40 a 60 por la segunda espera del
lock, no por la duración. Avisos como `::warning::`: cuadratura de `n_ganadas` fuera de ±0,5 %, top 1 bajo la meta
del 60 %, ningún mes con datos suficientes, tablas > 20 MB (regla 11).

**`mercado-mesa` (F-plat-5).** Va en `mercado.yml`, después de `mercado-ca` y `mercado-catalogo` (`jobs: "mercado-ca
mercado-catalogo mercado-mesa"`), así que corre cada 15 min con el mismo lock. Sin API. Para cada empresa activa evalúa
las CA vigentes de sus regiones (`evaluar_ca`, por lotes de 50), reescribe `evaluaciones_cache`, crea las predicciones
del modo sombra y cierra las que ya tienen resultado. **Sin medir todavía** con volumen real: mirar `duracion_s` en
`job_runs` las primeras corridas con ~400 vigentes y 1 empresa. Si pasa de **2 min**, sacarlo a un workflow propio
cada hora (`mercado-mesa.yml`, `jobs: "mercado-mesa"`) y quitarlo de `mercado.yml`. Sin modelo activo sale un
`::warning::` y no crea predicciones (la caché se escribe igual).

Los dos `GH mercado*` (F-merc-1) se crean recién cuando Boris aplique la migración en Neon y haga push
(pasos en `00-estado.md`). Usan el mismo advisory lock que el resto: de noche `mercado-nocturno` lo
tiene hasta ~06:45 y `mercado` sale `omitido` tras 5 min de espera (normal; se pone al día de mañana).
Con la ingesta de la app en pausa (decisiones.md, 31-oct) no compiten con nada más.

**Cursores del backfill** (`sync_state`, fuente `mercado_backfill:AAAA-MM`): `AAAA-MM-DD|página` (en
curso), `listado` (quedan detalles fallidos), `completo` o `vacio` (mes terminado con 0 CA en la región).
Un mes `completo` o `vacio` no se vuelve a pedir. Para repetir uno (p. ej. si `vacio` resultó ser un
problema de la API), borrar su cursor en Neon (SQL Editor) y la noche siguiente se lista de cero:

```sql
DELETE FROM sync_state WHERE fuente = 'mercado_backfill:2025-03';
```

Si los 3 primeros meses con requests de una corrida dan 0 CA y ninguno tuvo datos, el backfill no los
marca `vacio`: corta con el aviso "revisar filtros" (más probable un filtro roto que tres meses vacíos).

Consejo: crea `GH ca` completo, pruébalo (sección 4) y después usa **Copy/Clone** para los
demás. Solo cambian el título, el archivo de la URL y la hora.

`ciclo-ca.yml` no lleva cron: queda manual, para canarios.

---

## 4. Prueba

1. En cron-job.org, abre `GH ca` y usa **Test run** (o *Execute now*).
2. La respuesta correcta es **HTTP 204** (sin cuerpo). cron-job.org la marca como éxito.
3. En GitHub → Actions debe aparecer una corrida nueva de `ca` en pocos segundos, con el
   evento *workflow_dispatch* ("Manually run by basuazo").

Si falla:
- **401:** token mal copiado, sin "Bearer ", o vencido.
- **403:** el token no tiene *Actions: Read and write* o no incluye este repo.
- **404:** nombre de archivo mal escrito en la URL, o el token no ve el repo.
- **422:** el body no es `{"ref":"main"}`, o el workflow no tiene `workflow_dispatch`.

---

## 5. Qué queda vivo en cron-job.org

- Los 7 crons **GH …** (activos).
- El monitor de `GET https://app-mercado-publico.onrender.com/api/salud/jobs` (activo). Avisa si
  la ingesta se detiene por cualquier motivo, incluido un token vencido.
- Los crons viejos que llamaban a `/api/jobs/run` en Render: pausados. Se pueden borrar cuando se
  decida qué hacer con el endpoint de Render (backlog).

## 6. Mantenimiento

- **Renovar el token** antes de que venza: generar uno nuevo y reemplazar el header
  `Authorization` en los 7 crons.
- **Cambiar un horario:** solo en cron-job.org. Si cambia `DIGEST_HOUR`, mover `GH resumen`.
- **Pausar todo** (emergencia): desactivar los 7 crons **GH …**. Las corridas en curso terminan
  solas; para cortarlas, cancélalas en Actions.
