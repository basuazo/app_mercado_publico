# F-secretos — rotación de credenciales y cierre de A1

*Redactado 4-sep-2026. Precede a F-actions.*

## Por qué ahora

Los siete secretos de producción estuvieron en texto plano en disco durante meses
(hallazgo A1 [CRÍTICO-1], jun-2026). Verificado el 4-sep-2026:

- `.env` **nunca** fue rastreado por git, en ninguna rama (`git log --all -- .env` vacío).
- `audits/` está en `.gitignore:60` y sin rastrear.
- Los valores viven en exactamente dos archivos locales: `.env` y
  `audits/AUDIT-FINAL-A1-seguridad.md`.

**No hay que reescribir historial de git.** La exposición es de disco local.

La razón para rotar igual y ahora es F-actions: esa fase carga estas credenciales como
secretos de repositorio en GitHub Actions. Rotar primero significa que el valor que
estuvo expuesto en disco ya está muerto antes de entrar a una superficie nueva.

---

## Parte A — Rotación manual (la hace Boris, en este orden)

El orden importa: cada paso rompe algo distinto y hay que verificar antes de seguir.

### A1. MP_TICKET
1. api.mercadopublico.cl → solicitar ticket nuevo.
2. Render → Environment → `MP_TICKET` → Save Changes (redeploya solo).
3. Local: `.env`.
4. **Verificar:** `python scripts/smoke_test.py` contra la branch `dev`. Si da 401, el
   ticket nuevo no está activo — no sigas al paso siguiente.

### A2. Contraseñas de Neon (las dos branches)
1. Consola de Neon → por cada branch (`production` y `dev`) → rol `neondb_owner` →
   Reset password. Copiar la connstring nueva de cada una.
2. Render → `DATABASE_URL` = la de **production**.
3. Local `.env`: `DATABASE_URL` = la de **dev**, `DATABASE_URL_PROD` = la de production.
4. **Prefijo:** siempre `postgresql+psycopg://` (docs/operacion.md §13). Neon entrega
   `postgresql://`; `normalizar_url_driver` lo corrige, pero escribilo bien igual.
5. **Verificar:** `/api/salud` responde y `python -m alembic current` contra `dev` devuelve
   `f3a9b8c7d6e5`.

> El reset corta las conexiones abiertas. Render reconecta en el redeploy que dispara el
> Save Changes.

### A3. SECRET_KEY
1. `python -c "import secrets; print(secrets.token_hex(32))"`
2. Render + `.env`.
3. **Esto cierra todas las sesiones, incluida la tuya.** Hacelo cuando no estés a mitad de
   algo en la app.
4. **Verificar:** volver a entrar a `/login`.

### A4. JOBS_TOKEN
1. `python -c "import secrets; print(secrets.token_hex(32))"`
2. Render + `.env`.
3. **cron-job.org: actualizar el header `X-Jobs-Token` en los crons `job=ca` y `job=all`.**
   El pinger a `/api/salud/ping` no lo usa. Si te salteás esto, los dos crons empiezan a
   devolver 401 y los jobs dejan de correr **sin ningún aviso**.
4. **Verificar:** historial de ejecuciones en cron-job.org → 200, no 401.

### A5. BREVO_API_KEY
1. Brevo → SMTP & API → crear key nueva, **borrar la vieja**.
2. Render + `.env`. `SMTP_PASSWORD` está deprecado en producción (Render bloquea TCP
   saliente a puertos SMTP) pero rotalo igual si la cuenta lo sigue teniendo activo.
3. **Verificar:** logs de Brevo tras el próximo `resumen`.

### A6. ADMIN_PASSWORD
Ya rotada en la app el 4-sep-2026 vía `/perfiles` → Ajustes de tu cuenta. Falta solo
alinear la variable de entorno: Render + `.env` con el valor nuevo.

> Recordar por qué: el seed (`app/models/seeds.py:94`) solo corre si la tabla `usuarios`
> está vacía. Cambiar la env var no cambia ninguna contraseña existente; sirve para que un
> seed futuro use la correcta.

### A7. Limpieza local
1. Reescribir `.env` con los valores nuevos.
2. `audits/AUDIT-FINAL-A1-seguridad.md` → reemplazar los siete valores por `***` y anotar
   "rotados el 4-sep-2026". El archivo está gitignoreado, así que es higiene de disco, no
   un commit.

---

## Parte B — Prompt para Claude Code

> Contexto: leer `docs/00-estado-actual.md` y este archivo antes de empezar. Todos los
> secretos de producción fueron rotados a mano (Parte A); esta tarea es solo código y docs.
> No hay que tocar `.env` ni ninguna consola externa.

### B1. `repr=False` en los campos sensibles de `Settings`
`app/core/settings.py`: agregar `repr=False` a los ocho campos sensibles — `mp_ticket`,
`database_url`, `database_url_prod`, `secret_key`, `jobs_token`, `brevo_api_key`,
`smtp_password`, `admin_password`. Es la mitad pendiente de la remediación de A1; la otra
mitad (`_SecretFilter._ENV_VARS` en `app/core/logging.py`) ya está aplicada y cubre los
ocho — verificalo antes de tocarla, y no la dupliques.

Agregar un test que construya un `Settings` con valores centinela y afirme que
`repr(settings)` no contiene ninguno de los ocho. Que el test recorra la lista de campos,
no ocho asserts a mano, para que un campo sensible nuevo lo rompa.

### B2. `docs/operacion.md` — dos runbooks que faltan
Hoy hay §2 (MP_TICKET), §3 (SECRET_KEY) y §4 (JOBS_TOKEN), pero ninguno para Neon ni para
Brevo. Agregar las dos secciones con el procedimiento de la Parte A (numeración nueva al
final, no renumerar las existentes, que están referenciadas desde otros docs).

En §4, agregar explícito el paso de cron-job.org: rotar `JOBS_TOKEN` sin actualizar el
header `X-Jobs-Token` de los crons los deja en 401 silencioso.

### B3. `docs/00-estado-actual.md`
Cerrar A1 [CRÍTICO-1]: dejar registrado que los siete secretos se rotaron el 4-sep-2026,
que se verificó que `.env` nunca estuvo en el historial de git y que `audits/` está
gitignoreado — o sea que no hubo ni hay que reescribir historial.

### B4. Verificación
`python -m ruff check .` · `python -m mypy app` · `python -m pytest -q`. Un commit,
mensaje con prefijo `F-secretos:`.

### Fuera de alcance
No toques el scheduler, el endpoint de jobs ni `_full_cycle`. Los tres hallazgos de la
auditoría de F-jobs-endpoint van en F-actions (ver abajo).

---

## Deriva a F-actions (no perder)

1. **Exit codes del CLI.** `_run_with_lock` traga excepciones y devuelve `None`;
   `cmd_run_once` no llama `sys.exit`. Un job que revienta sale con código 0 → GitHub
   Actions lo marca verde y nunca notifica. Hay que distinguir tres desenlaces (ok / lock
   ocupado / error) y salir ≠ 0 solo en error.
2. **Crons escalonados.** `_LOCK_KEY` es una constante global única
   (`orchestrator.py:43`), así que dos jobs *distintos* que arranquen en el mismo minuto
   colisionan y uno se omite en silencio. No agendar todo en `:00`.
3. **Hora de `nocturno`.** `en_ventana_nocturna` es 22:00–07:00 Chile. Los crons de Actions
   son UTC, Chile cambia de offset dos veces al año y Actions atrasa los schedules hasta
   ~30 min en peak. Apuntar a ~01:00 Chile, al centro de la ventana.
4. **Hora del resumen.** `run_resumen` entró a `_full_cycle`, y el cron `job=all` de las
   ~02:00 ahora manda el correo-resumen a esa hora en vez de a `DIGEST_HOUR` (08:00).
   Decidir: mover el cron a la mañana, o sacar `resumen` de `_full_cycle` y darle su propio
   workflow a las 08:00.
