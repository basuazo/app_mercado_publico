#!/usr/bin/env bash
# Entorno de Claude Code en la nube (F-ci): Postgres local + dependencias + migraciones.
#
# Idempotente: se puede correr N veces. NO toca Neon ni usa ningún secreto real: la base
# es un Postgres local del contenedor y los secretos son valores de prueba fijos.
#
# Uso (setup script del entorno, o a mano):   bash scripts/dev/setup_nube.sh
# Después:  set -a; . ./.env.nube; set +a   (si no hay .env, el script copia .env.nube a .env
#           y pydantic/tests/pg_url.py lo leen sin exportar nada).
#
# Si el contenedor no permite instalar Postgres (sin apt-get ni sudo), el script lo avisa y
# sale 0: en ese caso se corre `python -m pytest -m "not postgres"` y el resto lo cubre el CI.
set -euo pipefail

cd "$(dirname "$0")/../.."

DB=mp_test
URL="postgresql+psycopg://postgres:postgres@localhost:5432/${DB}"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
como_postgres() { if [ -n "$SUDO" ]; then sudo -u postgres "$@"; else su postgres -c "$(printf '%q ' "$@")"; fi; }

# --- 1. Postgres -------------------------------------------------------------------------
hay_pg=1
if ! command -v pg_ctlcluster >/dev/null 2>&1 && ! command -v pg_ctl >/dev/null 2>&1; then
  echo "» Instalando Postgres (apt-get)…"
  if command -v apt-get >/dev/null 2>&1 \
     && $SUDO apt-get update -qq \
     && DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq postgresql >/dev/null; then
    :
  else
    hay_pg=0
  fi
fi

if [ "$hay_pg" -eq 1 ]; then
  echo "» Arrancando Postgres…"
  $SUDO service postgresql start >/dev/null 2>&1 || {
    # Sin init (algunos contenedores): arrancar cada cluster a mano.
    for c in $(pg_lsclusters -h 2>/dev/null | awk '{print $1"/"$2}'); do
      $SUDO pg_ctlcluster "${c%/*}" "${c#*/}" start || true
    done
  }
  for _ in $(seq 1 20); do pg_isready -q -h localhost && break; sleep 1; done
  if ! pg_isready -q -h localhost; then hay_pg=0; fi
fi

if [ "$hay_pg" -eq 1 ]; then
  como_postgres psql -q -c "ALTER USER postgres PASSWORD 'postgres';"
  if ! como_postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB}'" | grep -q 1; then
    como_postgres createdb "$DB"
  fi
else
  echo "⚠ No se pudo instalar/arrancar Postgres en este entorno."
  echo "  Corre: python -m pytest -m \"not postgres\"   (los tests de Postgres los corre el CI)."
fi

# --- 2. Variables (valores de prueba, ninguno real) --------------------------------------
cat > .env.nube <<EOF
# Generado por scripts/dev/setup_nube.sh. Valores de PRUEBA: nada real, nada de Neon.
DATABASE_URL=${URL}
TEST_DATABASE_URL=${URL}
MP_TICKET=ticket-de-prueba-nube
SECRET_KEY=clave-de-prueba-nube-no-es-secreta-0123456789abcdef
JOBS_TOKEN=token-de-prueba-nube
OPENROUTER_API_KEY=
EOF
if [ ! -f .env ]; then cp .env.nube .env; echo "» .env creado desde .env.nube"; fi

# --- 3. Dependencias ---------------------------------------------------------------------
echo "» Instalando dependencias…"
python -m pip install -q -r requirements.lock
python -m pip install -q -e ".[dev]" --no-deps

# --- 4. Migraciones ----------------------------------------------------------------------
if [ "$hay_pg" -eq 1 ]; then
  echo "» alembic upgrade head…"
  DATABASE_URL="$URL" python -m alembic upgrade head
  echo "✓ Listo: TEST_DATABASE_URL=${URL}  →  python -m pytest -rs"
fi
