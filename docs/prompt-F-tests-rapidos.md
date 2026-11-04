# Prompt — F-tests-rapidos: la suite contra Neon dev en menos tiempo · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet** (solo tests y docs; no toca la lógica de la app). Un commit
> `F-tests-rapidos: ...` encima de HEAD, sin push. Va **antes** de F-merc-1c (esa fase agrega tests a
> `tests/test_mercado.py`; con la suite rápida se cierra antes).

## 0. Problema
`python -m pytest -rs` contra Neon dev tarda demasiado: ~1.625 tests, ~122 de Postgres.

## 1. Medir antes de proponer (obligatorio)
- Exportar `DATABASE_URL` de dev con `postgresql+psycopg://` y correr `python -m pytest -rs --durations=30`.
- Anotar: tiempo total; cuánto suman los tests de Postgres y cuánto los de SQLite; si el tiempo está en setup,
  call o teardown (`--durations` lo separa); y cuánto tarda la **primera** conexión a Neon (arranque en frío de la
  compute de dev: medirlo aparte con un `SELECT 1` cronometrado).
- Con esos números se elige. No optimizar a ciegas.

## 2. Lo que hay hoy (revisado contra el código el 4-nov, Cowork) — leer antes de elegir
- **Los tests de Postgres no crean esquema**: usan la base dev **con las migraciones de Alembic aplicadas**
  (`test_indices_pg.py` verifica índices de migración; `create_all` no los reproduce). Cada módulo `*_pg.py` tiene
  su `pg_engine` de alcance **módulo** y limpia **por DELETE de sus filas con prefijo** (`GUARD-%`, `MM1-%`,
  correos de prueba) antes y después de cada test. No hay `drop_all` ni `TRUNCATE`. → La opción 1 no ahorra
  "crear y borrar tablas" en Postgres; lo que puede ahorrar son los DELETE de limpieza (varios round-trips a Neon
  por test) y los engines por módulo.
- **El `create_all` por test está en los de SQLite** (~25 módulos crean `Base.metadata.create_all(e)` en su
  fixture). Si la medición muestra que SQLite pesa, crear el esquema SQLite una vez por sesión (o por módulo) y
  aislar con transacción/SAVEPOINT es la misma opción 1 aplicada donde sí rinde.
- **Límite de la opción 1**: muchas funciones de la app reciben un `Engine` y abren su propia `Session(engine)`, y
  varias hacen `commit()`. Una transacción externa sobre **una** conexión solo las aísla si el test les pasa algo
  ligado a esa conexión. Con SQLAlchemy 2 se puede: `Session(bind=conn, join_transaction_mode="create_savepoint")`,
  pero si la función crea su propia `Session(engine)` no se ve la transacción externa. **No cambiar firmas de la app
  para esto** (no tocar la lógica). Donde no se pueda aislar sin tocar la app, dejar el DELETE por prefijo.
- **Excepciones que necesitan conexiones reales** (opción 1 no aplica): advisory lock y AUTOCOMMIT
  (`test_lock_espera.py`, `test_cuota.py` ~l. 420–480, `test_ca_explorar_pg.py` ~l. 274 con
  `pg_advisory_unlock`, y lo que use `orchestrator._adquirir_lock` / `_run_with_lock`). Conservan su fixture y
  limpian lo que escriben.
- **Detección de Postgres repartida**: cada módulo lee `os.environ.get("DATABASE_URL")` **al importarse** y arma su
  propio `needs_postgres = skipif(...)` (≈16 archivos; `grep -l 'DATABASE_URL' tests/*.py`). `Settings` lee `.env`
  pero `os.environ` no: por eso "con el `.env` tal cual se saltan". La normalización ya existe
  (`app/core/db.py: normalizar_url_driver`), solo no se usa en todos lados.
- **Salvaguarda actual** (`conftest.db_url`): compara `DATABASE_URL` con `DATABASE_URL_PROD` como texto. No detecta
  la misma base con otra cadena (otro usuario, `-pooler`, parámetros distintos).
- Es probable que el costo dominante de Postgres sea **latencia de red a Neon** (cada round-trip ~100–200 ms desde
  Chile) y el arranque en frío de dev [I: confirmarlo con la medición]. Si es así, una rama de Neon "tests" aísla
  pero **no acelera**; lo que acelera es un Postgres local (opción 2 con `TEST_DATABASE_URL` local) o menos
  round-trips por test.

## 3. Opciones, en orden de preferencia
1. **Esquema una vez por sesión + transacción externa y SAVEPOINT revertido por test**, en vez de crear tablas o
   limpiar por test. En Postgres: engine de sesión (uno solo, no uno por módulo) sobre la base migrada; en SQLite:
   `create_all` una vez. Excepciones: §2 (lock, AUTOCOMMIT, varias conexiones) y funciones que abren su propia
   `Session(engine)`: esas siguen con su fixture y su DELETE por prefijo.
2. **`TEST_DATABASE_URL`** con prioridad sobre `DATABASE_URL`, para apuntar a una rama de Neon solo de tests o a un
   Postgres local. Documentar en `docs/operacion.md`: (a) crear la rama `tests` desde dev en la consola de Neon y
   aplicar `alembic upgrade head` (desde PowerShell, exportando la URL, porque Alembic no lee `.env`); (b) Postgres
   local con Docker (`postgres:16`, extensión `unaccent`, `alembic upgrade head`). **Boris tiene Docker [V, 4-nov]**:
   documentar la variante local como la principal (comando `docker run` con volumen y puerto, y la línea de
   `TEST_DATABASE_URL` para `.env`); la rama de Neon queda como alternativa.
   La base de tests **debe estar migrada**: al iniciar la sesión, si `alembic_version` no coincide con la cabeza
   del repo, abortar con un mensaje claro ("aplica `alembic upgrade head` en la base de tests").
3. **pytest-xdist** solo si 1 y 2 no alcanzan. Dependencia nueva: justificarla según `CLAUDE.md` (proponer antes de
   agregarla) y aislar por worker (esquema SQLite propio por worker; en Postgres, `search_path` por worker
   requiere migrar cada esquema: evaluar su costo antes, o correr los de Postgres sin xdist).

## 4. Incluir siempre
- **Un solo punto de verdad para la URL de tests** en `tests/conftest.py`: lee `TEST_DATABASE_URL`, luego
  `DATABASE_URL` del entorno, luego `.env` (con `python-dotenv`, ya en el stack, sin pisar el entorno); normaliza
  `postgres://` y `postgresql://` → `postgresql+psycopg://` con `normalizar_url_driver`. Los ~16 módulos usan ese
  helper en vez de leer `os.environ` al importarse (cambiar **solo** cómo deciden si hay Postgres, no lo que
  verifican).
- **Marcador `postgres`** registrado en `[tool.pytest.ini_options] markers` de `pyproject.toml` y aplicado a todo test
  que use Postgres (reemplaza o acompaña a los `needs_postgres` locales), para correr `-m "not postgres"` (ciclo
  rápido) y `-m postgres` por separado.
- **Salvaguarda de producción**: si el **host** de la URL efectiva contiene `ep-rough-hill`, la sesión aborta en
  `pytest_sessionstart` con error claro (`pytest.exit(..., returncode=2)`), antes de abrir ninguna conexión. Mantener
  también la comparación con `DATABASE_URL_PROD`. Test unitario del guardia (sin conectarse).
- Sin Postgres disponible (ninguna URL de Postgres): los tests `postgres` se saltan con un motivo único y claro;
  **con** Postgres configurado, 0 saltados.

## 5. Reglas
- No borrar tests, no marcarlos skip, no cambiar lo que verifican. No tocar `app/` (salvo que algo de `app/` sea
  imprescindible y trivial: proponerlo antes).
- Mismo número de tests **pasados** antes y después, con 0 fallos y 0 saltados contra Postgres.
- `git add` solo de `tests/`, `pyproject.toml`, `docs/operacion.md`, `docs/00-estado.md` (backlog: tachar "tests de
  Postgres no normalizan") y este prompt. Sin entrada en `app/changelog.py` (no es cambio visible).

## 6. Cierre
`ruff check .`, `python -m mypy app`, `python -m pytest -rs` completo contra Postgres (0 fallos, 0 errores, 0
saltados) y además `-m "not postgres"` y `-m postgres` por separado.

## 7. Reportar
- Tiempo total antes y después, mismo comando y misma base (y, si se usó, el tiempo con `TEST_DATABASE_URL` local
  o rama `tests`, aparte).
- Los 10 tests más lentos antes y después, con fase (setup/call/teardown).
- Conteo pasados/saltados de ambas corridas.
- Arranque en frío de Neon medido.
- Opción elegida y por qué, con los números que la justifican; y qué quedó sin aislar por SAVEPOINT y por qué.
