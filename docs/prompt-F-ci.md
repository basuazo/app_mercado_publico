# Prompt — F-ci (tests en GitHub Actions, migraciones con aprobación y Claude Code en la nube) · 6-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit en la rama `F-ci`. **Esta fase sí hace push de su rama** (no a `main`):
> es la que habilita el trabajo remoto. Sin migración. [V] verificado, [I] inferido.

## Por qué
Hoy cada fase depende del PC de Boris: `alembic upgrade head` y `pytest` contra Postgres corren solo en su terminal, y
Claude Code no llega a Neon. Objetivo: que una sesión de Claude Code en la nube (iniciada desde el teléfono) pueda
programar, probar con Postgres real, empujar su rama y dejar todo listo para que Boris audite y fusione desde el
teléfono. Las migraciones a Neon pasan por un workflow con aprobación, nunca desde una sesión de código.

## 1. CI: `.github/workflows/ci.yml`
- Disparo: `push` a ramas `F-**` y `fix/**`, y `pull_request` hacia `main`. Sin `schedule`.
- Servicio `postgres:16` (trae `unaccent` en contrib; la migración lo crea). Python 3.11 como `_job.yml` [V].
- Pasos: `pip install -r requirements.lock && pip install -e .[dev] --no-deps` (agregar a `requirements.lock` lo de dev si
  falta, o instalar `.[dev]` normal; elegir lo que funcione y anotarlo) → `alembic upgrade head` con
  `DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/mp_test` → `ruff check .` → `python -m mypy app` →
  `TEST_DATABASE_URL=… python -m pytest -rs` → `node --test extension/lib.test.js`.
- **Sin secretos**: `MP_TICKET`, `SECRET_KEY`, `JOBS_TOKEN`, etc. con valores de prueba fijos en el workflow (no reales).
  Ningún paso toca Neon ni la red de ChileCompra (los tests de red ya van con respx).
- Criterio de verde = el del CLAUDE.md: 0 fallos, 0 errores, 0 saltados (fallar si `pytest` informa saltados).
- Concurrency por rama con `cancel-in-progress: true`. Timeout 30 min (local ~6,5 min [V, operacion.md §15]).

## 2. Migraciones: `.github/workflows/migrar.yml`
- Solo `workflow_dispatch` con input `destino` (`dev` | `prod`) y `ref` (rama o SHA, por defecto `main`).
- `dev` usa el secreto nuevo `DATABASE_URL_DEV`; `prod` usa `DATABASE_URL` (el de los jobs [V]) y corre en el
  environment de GitHub **`produccion`** (Boris lo crea con su aprobación obligatoria: se aprueba desde la app de GitHub).
- Pasos: `alembic current` → `alembic heads` → (si hay más de una cabeza, fallar) → `alembic upgrade head` → `alembic current`.
  Nunca imprimir la URL (el logger y `alembic` no deben mostrarla; usar `::add-mask::` por si acaso).
- `concurrency: group: migrar-${{ inputs.destino }}`, sin cancelar. Un solo destino por corrida.

## 3. Claude Code en la nube: script de entorno
- `scripts/dev/setup_nube.sh` (idempotente): instala Postgres si falta (`apt-get install -y postgresql`), lo arranca,
  crea `mp_test` con usuario `postgres`/`postgres`, `pip install -e .[dev]`, `alembic upgrade head` y exporta
  `DATABASE_URL` y `TEST_DATABASE_URL` en un `.env.nube` que `tests/pg_url.py` ya prioriza vía entorno. No toca Neon.
  [I] Que el entorno de Claude Code en la nube permita `apt-get`: verificarlo en esta misma sesión; si no, documentar
  que en la nube se corre `pytest -m "not postgres"` y el resto lo cubre el CI.
- `docs/operacion.md` §15 nuevo apartado «c) En Claude Code en la nube»: cómo configurar el entorno (setup script
  `bash scripts/dev/setup_nube.sh`), qué variables NO poner (ningún secreto real) y el flujo de abajo.

## 4. Flujo de trabajo nuevo (proponer el cambio en `CLAUDE.md`, «Flujo de trabajo»)
Reemplazar «sin push hasta auditarla» por:
1. Cada fase en su rama `F-<nombre>`; Claude Code corre ruff/mypy/pytest en su entorno y **hace push de la rama**.
2. Abre un PR hacia `main` con el resumen de la fase, la migración (si hay) y los «Pasos de Boris».
3. El CI debe quedar verde. Boris audita el PR y lo fusiona (desde el teléfono si quiere). **Nunca push directo a `main`.**
4. Si la fase trae migración: tras fusionar, Boris dispara `migrar` → `prod` y aprueba en el environment `produccion`
   **antes** de que el deploy de Render sirva el código nuevo (anotar en el PR el orden exacto).
5. Sigue vigente: nunca dos fases con migración al mismo tiempo; una cabeza de Alembic.
Anotar en `docs/decisiones.md` (fecha, Boris) y actualizar `docs/diseno/orden-ejecucion.md` (regla «sin push»).

## 5. Tests y cierre
Validar los YAML (`python -c "import yaml…"` o `actionlint` si está). El CI de esta misma rama debe correr verde: es la
prueba de la fase. Entrada en `app/changelog.py` no aplica (no es visible). Commit:
`F-ci: tests en Actions, migraciones con aprobación y entorno en la nube`, push de `F-ci` y PR.

## 6. Pasos de Boris (se pueden hacer desde el teléfono, en github.com)
1. Settings → Secrets → Actions: crear `DATABASE_URL_DEV` (la URL de la rama dev de Neon).
2. Settings → Environments: crear `produccion` con «Required reviewers» = Boris.
3. Revisar el PR de `F-ci`, ver el CI verde y fusionar.
4. Correr `migrar` → `dev` una vez (deja dev al día: hoy está en `a7c3e9d1b4f2`).
5. En Claude Code (web o app): configurar el entorno del repo con el setup script de §3.
