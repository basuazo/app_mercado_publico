# Prompt F-actions-1 — workflow reutilizable + canario `ciclo-ca`

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> **ACTUALIZADO 23-sep-2026 — leer antes que lo de abajo.** El motivo de la fase cambió:
> - [V] Cloudflare **ya no bloquea** a cron-job.org (desde el 23-sep sus POST llegan con 200), así
>   que la ingesta hoy sí se dispara sola. Pero ese camino es frágil por otra razón [V]: Render
>   duerme el proceso **15 min después de la última request entrante** aunque haya un job en
>   background, y una corrida de `ca` en hora pico dura 20–60 min. Sin alguien haciendo ping, las
>   corridas mueren a medias. GitHub Actions corre el CLI contra Neon directo: no depende de que
>   Render esté despierto.
> - F-cuota, F-429-concurrencia y F-ca-ventana ya están desplegadas (`26d6f30`, `b0ff832`,
>   `3b6b9b9`). `ca` ahora recorre ventanas y tiene tope de requests por corrida.
> - Los cambios respecto de la versión anterior de este prompt están marcados con **[23-sep]**.
>
> Origen: el 21-sep-2026 **todos** los crons de cron-job.org empezaron a recibir `429` de
> Cloudflare (el borde de Render), incluido el `GET /api/salud/jobs` público. Verificado: la
> app responde 200 a cualquier otro origen y el código no devuelve `429` en ninguna ruta —
> quien limita es el borde, contra las IPs del scheduler de cron-job.org. La salida es sacar
> el disparador de ahí: **GitHub Actions ejecuta el CLI contra Neon directo**, sin pasar por
> Render ni por Cloudflare. Decisión ya tomada en ago-2026 (`F-actions` del roadmap).
>
> **Hecho que manda sobre el diseño:** el repo `basuazo/app_mercado_publico` es **público**.
> Eso da minutos de Actions ilimitados y gratis, pero **los logs de las corridas son
> públicos**. Por eso esta fase incluye el `repr=False` de `Settings` que venía pendiente de
> la deuda A1: es la única capa que falta sobre el enmascarado que ya hace `_SecretFilter` y
> el que hace GitHub con los valores registrados como secret.
>
> **Zona sensible:** ninguna, si te ceñís al alcance. Esta fase NO toca `app/api/`, ni el
> `lifespan`, ni el factory, ni `render.yaml`, ni la lógica de ningún runner.

---

```
Fase F-actions-1. Los jobs los dispara hoy un cron externo (cron-job.org) por HTTP contra
POST /api/jobs/run, y desde el 21-sep Cloudflare le responde 429 a ese cron: la ingesta está
detenida. Movemos el disparo a GitHub Actions, que ejecuta el CLI `python -m app.ingest
run-once` contra Neon directo. Esta primera fase deja armada la infraestructura y UN solo
workflow, manual, como canario. Lee CLAUDE.md antes de empezar: las reglas 1, 5, 10 y 13
están en juego. No cambies la lógica de negocio de ningún runner.

CONTEXTO QUE YA ESTÁ RESUELTO (verificar, no rehacer)
- app/ingest/__main__.py ya expone `run-once --job=X` para los 12 jobs y ya envuelve cada uno
  en `_run_with_lock(...)`. O sea: el advisory lock (regla 13) sigue tomándose, `job_runs`
  se sigue escribiendo y el dead-man's switch GET /api/salud/jobs sigue funcionando sin
  tocar nada. NO reimplementes eso.
- El CLI NO tiene los compuestos "ciclo-ca" / "ciclo-activas" (viven solo en `_secuencia` de
  app/api/routes/api.py). NO los agregues al CLI: se resuelven como secuencia de jobs en el
  workflow, que además los deja como pasos legibles en el log.
- `nocturno` valida por sí mismo la ventana 22:00–07:00 con ZoneInfo("America/Santiago")
  (regla 5). Esa red de seguridad se mantiene; no la toques.

1. ENDURECER `Settings` PARA LOGS PÚBLICOS (deuda A1, una línea por campo)
   En app/core/settings.py, agregar `repr=False` a los Field() de los campos secretos:
   mp_ticket, database_url, database_url_prod, secret_key, jobs_token, brevo_api_key,
   smtp_password, admin_password. Nada más: no cambies tipos, defaults ni descripciones.
   Test: `repr(Settings(...))` construido con valores de prueba NO contiene ninguno de esos
   valores. Usá `looks_like_secret` de app/core/logging.py si te sirve.

2. WORKFLOW REUTILIZABLE `.github/workflows/_job.yml`
   `on: workflow_call`. Inputs:
     - `jobs` (string, requerido): jobs del CLI a ejecutar EN ORDEN, separados por espacio.
     - `guard_hora_chile` (string, opcional, default ''): si viene, el workflow no hace nada
       salvo que la hora local de Chile sea ésa (formato "08", con cero a la izquierda).
     - `timeout_min` (number, opcional, default 60). **[23-sep]** Subido de 30: con F-ca-ventana
       una corrida de `ca` en hora pico dura 20–60 min y `detalles` puede tardar más con 10500.
   `permissions: contents: read`. Un solo job `run`, `runs-on: ubuntu-latest`,
   `timeout-minutes: ${{ inputs.timeout_min }}`.

   Pasos, en este orden:
   a) **Guardia de hora Chile.** Step `id: guard` que escribe `correr=si|no` en $GITHUB_OUTPUT.
      Sale `si` cuando `guard_hora_chile` está vacío, cuando el evento es `workflow_dispatch`
      (un disparo manual nunca debe quedar bloqueado por la hora), o cuando
      `TZ=America/Santiago date +%H` coincide con el input. Todos los steps siguientes van con
      `if: steps.guard.outputs.correr == 'si'`. Esto existe porque el cron de GitHub es SOLO
      UTC y Chile cambia entre UTC−3 y UTC−4: sin guardia, el horario se corre una hora medio
      año.
   b) **Verificar configuración.** Step que falla con un mensaje claro si falta cualquiera de
      estos, ANTES de instalar nada: secrets DATABASE_URL, MP_TICKET, BREVO_API_KEY,
      SMTP_FROM; variable APP_BASE_URL.
      **[23-sep]** TASA_UF, TASA_UTM, TASA_USD, TASA_EUR y DIGEST_HOUR NO son obligatorias: tienen
      default en settings.py y Render hoy NO las define (usa los defaults). Pásalas desde `vars`
      solo si están definidas; si no, no las exportes. No las valides como requeridas.
      Comprobá que no estén vacíos, sin imprimir jamás su valor (solo el nombre del que
      falta). Motivo: una variable ausente llega a pydantic como cadena vacía y revienta el
      parseo de int/float con un traceback opaco; preferimos el fallo temprano y legible.
   c) `actions/checkout@v4`.
   d) `actions/setup-python@v5` con `python-version: '3.11'`, `cache: 'pip'`,
      `cache-dependency-path: requirements.lock`.
   e) `pip install -r requirements.lock && pip install -e . --no-deps`
      (sin `[dev]`: en Actions no corren tests, y `--no-deps` respeta el lock).
   f) **Ejecutar los jobs.** Un solo step bash que recorre `${{ inputs.jobs }}` llamando
      `python -m app.ingest run-once --job="$j"` por cada uno, envuelto en
      `echo "::group::$j"` / `echo "::endgroup::"`.
      **NO uses `set -e` en el loop.** Si un job falla, los siguientes SÍ tienen que correr:
      `_secuencia` en el endpoint hace exactamente eso (verificado en producción el 21-sep,
      un 504 de la API en `ca` no impidió que corrieran `match` y `alerts`), y cortar la
      cadena por un error transitorio aguas arriba dejaría sin procesar datos que ya estaban
      ingestados. El patrón correcto: acumular los códigos de salida, seguir el loop, y al
      final `exit 1` si alguno falló. Así hay paridad con el endpoint Y el workflow igual
      queda en rojo.
      **[23-sep]** Entre un job y el siguiente, `sleep 60`. Cada job es un proceso nuevo y el
      enfriamiento tras un 504 (F-429-concurrencia) vive en memoria: sin esta pausa, `match`
      arranca justo después de un `ca` cortado por 504 y choca con el 429/10500.

   `env` de ese step:
     DATABASE_URL, MP_TICKET, BREVO_API_KEY, SMTP_FROM  → desde `secrets`
     APP_BASE_URL, DIGEST_HOUR, TASA_UF, TASA_UTM, TASA_USD, TASA_EUR → desde `vars`
     **[23-sep]** Además, desde `vars` y OPCIONALES (si faltan, rige el default del código):
       CA_TAMANO_PAGINA, CA_VENTANA_HORAS, CA_MAX_REQUESTS_POR_CORRIDA, RATE_LIMIT_RPS.
       Hoy Render corre con CA_TAMANO_PAGINA=10 y CA_VENTANA_HORAS=1; los defaults del código
       son 20 y 2. Si Actions corre con valores distintos a Render, la ingesta se comporta
       distinto según quién la dispare. Una variable opcional vacía NO debe llegar a pydantic
       como cadena vacía: si no está definida, no la exportes.
       Antes de cerrar este punto, compara TODOS los campos de app/core/settings.py con lo que
       recibe el step y lista en el commit cualquier campo sin default que falte.
     SECRET_KEY y JOBS_TOKEN → **generados efímeros en el propio step**
       (p. ej. `export SECRET_KEY=$(openssl rand -hex 32)`), con un comentario explicando por
       qué: `Settings` los declara obligatorios, pero el camino del CLI no firma cookies ni
       atiende HTTP, así que no hace falta traer los valores reales de producción a CI.
       Antes de escribirlo, VERIFICÁ leyendo el código que ningún runner alcanzado por el CLI
       usa `settings.secret_key` ni `settings.jobs_token`. Si alguno los usa, no inventes:
       pará y decilo.

3. CÓDIGOS DE SALIDA DEL CLI  (sin esto, Actions queda verde para siempre)
   Problema verificado el 21-sep: `_run_with_lock` atrapa `Exception`, la registra en
   `job_runs` como "error" y **devuelve None sin propagar**. `cmd_run_once` imprime ese None
   y termina en 0. O sea que hoy `python -m app.ingest run-once --job=ca` **sale 0 aunque el
   job haya fallado**. En Actions eso significa que todas las corridas serían verdes incluso
   los días sin ingesta, y el correo de fallo de GitHub —la red de seguridad principal del
   modelo nuevo— nunca se dispararía. Estaba anotado como pendiente de F-actions en el prompt
   de F-invertir-modelo ("exit codes del CLI").

   Arreglo, cuidando de no cambiar el comportamiento del endpoint ni del scheduler:
   - En `_run_with_lock`, agregar un parámetro `propagar: bool = False`. Con el default
     (False) el comportamiento actual queda **idéntico**: registra y devuelve None. Con True,
     registra en job_runs igual y **re-lanza** la excepción.
   - El CLI (`cmd_run_once`) es el único que pasa `propagar=True`. Envolvé la llamada:
     excepción → imprimí el error por stderr y `sys.exit(1)`.
   - **"omitido" no es fallo.** Si el advisory lock está ocupado, `_run_with_lock` devuelve
     None sin que haya habido excepción: eso tiene que salir **0**, con un mensaje claro en
     stdout. Un workflow en rojo porque otro job tenía la llave sería una falsa alarma
     recurrente. Distinguí los dos casos con cuidado — hoy ambos devuelven None.
   - `_ciclo_nocturno` llama a `_run_with_lock` por cada paso interno: esos siguen con el
     default False, para que un paso caído no aborte el ciclo nocturno completo.
   Tests: `run-once` de un job que falla sale 1; uno que corre bien sale 0; uno omitido por
   lock ocupado sale 0; y el endpoint POST /api/jobs/run mantiene exactamente el
   comportamiento de hoy ante un job que falla (sigue con los pasos siguientes de la
   secuencia). Ese último es el test de regresión que importa.

4. WORKFLOW CANARIO `.github/workflows/ciclo-ca.yml`
   `name: ciclo-ca`. Disparo **solo `workflow_dispatch`** — sin `schedule` todavía; los
   horarios entran en F-actions-2, después de verificar que esto conecta a Neon.
   Llama al reutilizable:
     jobs:
       run:
         uses: ./.github/workflows/_job.yml
         secrets: inherit
         with:
           jobs: "ca match alerts"
           timeout_min: 90
   `concurrency: { group: mp-jobs, cancel-in-progress: false }` a nivel de workflow — el
   advisory lock es una llave global y dos workflows simultáneos harían que uno se omita en
   silencio.

   **Convención obligatoria** (de ella depende el test del punto 5): la lista de jobs se
   escribe siempre en UNA línea, con comillas dobles, así:  `jobs: "ca match alerts"`.

5. TEST QUE AMARRA WORKFLOWS Y CLI  (tests/test_workflows.py, offline)
   Recorre `.github/workflows/*.yml`, extrae con regex las líneas `jobs: "..."` y verifica
   que **cada** nombre de la lista esté en `app.ingest.__main__._JOBS`. Sin dependencias
   nuevas: parseo por regex, NO agregues PyYAML.
   Motivo concreto: el cron de `ciclo-activas` en cron-job.org quedó apuntando a
   `?job=<ciclo-activas>` —con los signos `<>` literales— y nadie lo vio hasta hoy. Este test
   hace imposible repetirlo del lado de Actions.
   Agregá también un caso que verifique que `_job.yml` NO pasa SECRET_KEY ni JOBS_TOKEN desde
   `secrets` (debe generarlos en el step).

6. VERIFICACIÓN Y LÍMITES
   - `ruff check .`, `python -m mypy app`, `python -m pytest` (como módulo). Suite verde.
   - NO toques app/api/, el lifespan, `_make_app`, el startCommand ni render.yaml.
   - NO agregues `alembic upgrade head` a ningún workflow: las migraciones las sigue
     aplicando Render en el deploy. Actions solo mueve datos.
   - NO agregues dependencias.
   - NO toques cron-job.org ni la lógica del endpoint /api/jobs/run: se apagan en
     F-actions-3, y hasta entonces quedan como escotilla manual.
   - Un commit, prefijo `F-actions-1:`. Entrada en app/changelog.py en el mismo commit.
   - NO uses `git add -A`: `_to_delete/` no está en .gitignore y contiene un `.env` con
     secretos de producción.
```

---

## Paso operativo (Boris, antes de disparar el canario)

**1. Cargar los secrets en GitHub** (Settings → Secrets and variables → Actions → *Secrets*):

| Secret | Valor |
|---|---|
| `DATABASE_URL` | **el branch production de Neon** — el de `DATABASE_URL_PROD` de tu `.env`, no el de `DATABASE_URL` |
| `MP_TICKET` | el ticket de Mercado Público |
| `BREVO_API_KEY` | la API key de Brevo |
| `SMTP_FROM` | la dirección remitente |

> ⚠️ **La trampa.** Tu `.env` local tiene `DATABASE_URL` apuntando al branch **dev** de Neon.
> Si lo copias tal cual, los jobs van a escribir en dev, la app no va a mostrar nada nuevo y
> vas a estar media hora buscando un bug que no existe. En Actions va el de **production**.

**2. Cargar las variables** (misma pantalla, pestaña *Variables* — no son secretos) **[23-sep]**:
`APP_BASE_URL` = `https://app-mercado-publico.onrender.com`. Las tasas (`TASA_*`) y `DIGEST_HOUR`
**no se cargan**: Render no las define y ambos usan el default del código, así que Actions queda
igual. Si algún día se definen en Render, hay que definirlas también aquí con el mismo valor.
**[23-sep]** Agrega también `CA_TAMANO_PAGINA` = `10` y `CA_VENTANA_HORAS` = `1`, los mismos que
tiene Render hoy. Revisa en Render si hay otras variables de ingesta definidas y cópialas igual.

**3. Disparar el canario a mano** (Actions → `ciclo-ca` → Run workflow) y verificar tres cosas:

- El step de `ca` termina en verde y el log no muestra ningún secreto.
- `GET /api/salud/jobs` refresca `ultimo_ok` de `ca` (y de `resumen` no, que no corrió).
- Entrás al dashboard y ves oportunidades nuevas — confirma que escribió en **production**.

Si el paso 3 falla por conexión, el sospechoso número uno es el `connect_args={"sslmode":
"require"}` de `_make_engine`: ese camino del CLI nunca corrió contra Neon desde fuera de tu
máquina. Es lo único de esta fase que no se puede verificar sin ejecutarlo.

**Mientras tanto [23-sep]:** cron-job.org sigue disparando los jobs contra Render. Funciona, pero
una corrida larga muere si nadie hace ping. No lances el canario mientras haya una corrida de
Render en curso (mira el advisory lock en `/salud`): quedaría `omitido`.
