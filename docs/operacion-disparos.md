# Operación — cómo se disparan los jobs (cron-job.org → GitHub Actions)

*Vigente desde F-actions-3 (disparo externo), 25-sep-2026.*

Los jobs corren en **GitHub Actions**, que ejecuta el CLI contra Neon production. Quien los
dispara es **cron-job.org**: llama a la API de GitHub a la hora exacta, en hora de Chile. Los
workflows ya no tienen `schedule`, porque GitHub los atrasaba de 2 a 5,5 h y descartaba
corridas (evidencia en `docs/prompt-F-actions-3-disparo-externo.md`).

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
| GH catalogos | `catalogos.yml` | lunes 06:35 |
| GH resumen | `resumen.yml` | 08:30 |

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
