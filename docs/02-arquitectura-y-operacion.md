# Arquitectura y operación — mp-oportunidades

*Vigente al 08-oct-2026. Reemplaza a `archivo/arquitectura.md` e `archivo/ingesta.md`, que
describían APScheduler en Render y SMTP. Procedimientos puntuales (rotar secretos, recuperar
admin, backup/restore, rollback): `operacion.md`. Disparo de jobs: `operacion-disparos.md`.*

## 1. Piezas

```
cron-job.org ──(workflow_dispatch, hora de Chile)──▶ GitHub Actions ──CLI──▶ Neon production
                                                         │                      ▲
                                    API MP v1/v2, Datos Abiertos (ZIP), PAC     │
Render (free) — solo web FastAPI + Jinja2/HTMX ─────────────────────────────────┘
Brevo (API REST HTTPS) ◀── resumen diario y alertas (desde Actions)
```

- **Render** sirve la web. Duerme a los 15 min sin requests, a propósito (F-invertir-modelo).
  Migra Alembic al arrancar; el `startCommand` real vive en el dashboard de Render, no en
  `render.yaml`. Levantar en local: `python -m uvicorn app.api.main:_make_app --factory --reload`.
- **GitHub Actions** corre los jobs (`python -m app.ingest run-once --job=X --esperar-lock-min=N`)
  contra Neon production. Los workflows no tienen `schedule`: los dispara cron-job.org.
  Plantilla común `.github/workflows/_job.yml`; convención: `jobs: "a b c"` en una línea
  (`tests/test_workflows.py`).
- **Neon**: branch `production` (host `ep-rough-hill-…`, Render + Actions) y `dev`
  (host `ep-dawn-sunset-…`, local + tests). Free: 0,5 GB y 100 CU-horas/mes.
- **Correo**: API REST de Brevo (`BREVO_API_KEY`); SMTP queda solo para desarrollo local.
- **Scheduler en proceso** (APScheduler): existe pero está apagado por defecto
  (`scheduler_en_proceso`). `POST /api/jobs/run` + `JOBS_TOKEN` siguen en el código, sin crons
  que los llamen (limpieza pendiente).

## 2. Jobs (hora de Chile)

| Workflow | Cuándo | Jobs | Espera de lock |
|---|---|---|---|
| `ca.yml` | cada hora :05 | `ca` | 25 min |
| `ciclo-match.yml` | 08:50–20:50 c/2 h | `match alerts detalles-match` | 25 |
| `ciclo-activas.yml` | 10:15 · 14:15 · 19:15 | `activas detalles match alerts` | 30 |
| `nocturno.yml` | 01:10 | `nocturno` (datos-abiertos → estados → lifecycle → competencia → backfill → rellenar-organismo → detalles-match) | 30 |
| `retencion.yml` | 05:40 | `retencion` | — |
| `catalogos.yml` | lunes 06:35 | `catalogos plan-anual vocabulario-rubros` | 30 |
| `resumen.yml` | 08:30 | `resumen` | 45 |
| `ciclo-ca.yml` | manual | `ca match alerts detalles-match` | 0 |

- **Un solo `pg_advisory_lock`** para todos los jobs; el CLI **espera** el lock (no omite).
- **Cuota:** contador persistido en Postgres, presupuesto 9.000/día. 429 con código 10500 =
  concurrencia (3 reintentos 30/60/120 s y cortar); otro 429 = tope diario; tras 504/timeout,
  60 s de enfriamiento (ver CLAUDE.md regla 3 y `01-analisis-api-mercado-publico.md`).
- **Backfill** solo entre 22:00 y 07:00 de Chile, validado en código.
- Detalle de cómo se configuran los crons y el token de GitHub: `operacion-disparos.md`.

## 3. Datos

- Licitaciones (v1), Compras Ágiles (v2, cursor por ventanas de `fecha_ultimo_cambio`),
  detalles solo de lo que tiene match o está guardado (`raw_json` solo con match), ítems UNSPSC y
  competencia desde `lic-da` (sin cuota), Plan Anual (ZIP completo, máx. 1 carga cada 28 días,
  guarda de 70 % de espacio), catálogo de organismos, vocabulario de rubros.
- **Vigencia única** (`app/core/vigencia.py`): por fecha de cierre antes que por estado; CA sin
  cierre = vigente solo si se publicó hace ≤ 7 días. La usan matching, feed, explorador y detalles.
- **FTS**: `tsv` indexado con `unaccent`; toda tsquery = `websearch_to_tsquery('spanish',
  inmutable_unaccent(:q))`.
- **Retención** (05:40): purga `raw_json` e ítems de terminales > 90 días y `job_runs` viejos. No
  borra filas de CA/licitaciones (ver auditoría 14, R4).

## 4. Flujo de trabajo

- Prompts de fase en `docs/prompt-F-*.md` (se redactan y auditan en Cowork) → Claude Code los
  ejecuta → un commit `F-xxx: …` sin push → auditoría en Cowork → push. Correcciones antes del
  push: `git commit --amend`.
- Cierre de fase: `ruff check .`, `python -m mypy app`, `python -m pytest -rs` con 0 fallos, 0
  errores, 0 saltados (contra dev) + entrada en `app/changelog.py` en el mismo commit.
- `git add` solo de lo de la fase; nunca `git add -A`. Desde Cowork: `git --no-optional-locks`.
- Push de fase con migración: justo después de que arranque un `ca` de los :05; confirmar en el
  log de Render `Running upgrade <a> -> <b>`. Render migra al arrancar; Actions no.
- Modelos: Sonnet para fases acotadas; Opus para lo que toca cuota/429/lock o borra datos.
- Scripts de solo lectura contra producción en `data/` (gitignored), los corre Boris.

## 5. Gotchas (no repetir)

- **`DATABASE_URL` siempre `postgresql+psycopg://`**; con `postgresql://` los tests de Postgres
  fallan por driver. Alembic y los tests **no leen `.env`**: exportar la variable en la ventana.
  ```powershell
  # dev (tests). Para producción: ^DATABASE_URL_PROD=
  $env:DATABASE_URL = ((Get-Content .env | Where-Object { $_ -match '^DATABASE_URL=' }) -replace '^DATABASE_URL=','' -replace '^["'']|["'']$','' -replace '^postgres(ql)?://','postgresql+psycopg://')
  python data\diag_conexion.py   # confirma host y cabeza de Alembic
  Remove-Item Env:DATABASE_URL
  ```
  El error `Can't load plugin: sqlalchemy.dialects:driver` significa que la variable no está.
- Herramientas como módulo (`python -m pytest|mypy|alembic`): los `.exe` del venv los bloquea el
  antivirus.
- `.gitattributes` fuerza LF; si aparecen archivos truncados/CRLF, `git restore .`.
- Los `.txt` de `Tee-Object` quedan en UTF-16; usar `$env:PYTHONIOENCODING = "utf-8"`.
- Lock ocupado por la tarde: jobs a mano justo después de un `ca` o de noche (21:00–00:30).
- No agregar consultas a la base en `/api/salud/ping`: mantendría Neon despierta 24/7.
- Secrets de Actions: `DATABASE_URL` debe ser la de **production** (trampa conocida: copiar la de
  dev).
