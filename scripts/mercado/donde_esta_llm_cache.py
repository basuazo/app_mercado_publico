"""¿En qué base del .env quedó la caché de la evaluación de anexos? Solo lee (SELECT)."""
import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text

env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
cands = {k: v for k, v in env.items() if k.startswith("DATABASE") and v}
if os.environ.get("DATABASE_URL"):
    cands["(DATABASE_URL de la sesión de PowerShell)"] = os.environ["DATABASE_URL"]
for nombre, url in cands.items():
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    try:
        with create_engine(url, connect_args={"connect_timeout": 15}).connect() as c:
            existe = c.execute(text("select to_regclass('llm_cache') is not null")).scalar()
            filas = c.execute(text("select count(*) from llm_cache")).scalar() if existe else "-"
            alembic = c.execute(text("select version_num from alembic_version")).scalar() if c.execute(text("select to_regclass('alembic_version') is not null")).scalar() else "-"
        print(f"{nombre:45} llm_cache={'sí' if existe else 'no'} filas={filas} alembic={alembic}")
    except Exception as exc:  # noqa: BLE001
        print(f"{nombre:45} error: {type(exc).__name__}")
