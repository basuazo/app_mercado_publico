"""Un solo punto de verdad para la URL de Postgres de los tests.

Orden de prioridad: ``TEST_DATABASE_URL`` > ``DATABASE_URL`` del entorno >
``DATABASE_URL`` del ``.env``. Siempre normalizada a ``postgresql+psycopg://``.
Si la URL efectiva no es de Postgres, ``DB_URL`` queda vacía y los tests
``postgres`` se saltan con ``MOTIVO``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

from app.core.db import normalizar_url_driver

RAIZ = Path(__file__).resolve().parent.parent
HOST_PROD = "ep-rough-hill"
MOTIVO = (
    "Requiere Postgres con migraciones aplicadas: define TEST_DATABASE_URL o "
    "DATABASE_URL (ver docs/operacion.md)"
)


def es_postgres(url: str) -> bool:
    return url.startswith(("postgresql", "postgres"))


def _leer(nombre: str, env: Mapping[str, str], dotenv: Mapping[str, str | None]) -> str:
    return (env.get(nombre) or dotenv.get(nombre) or "").strip()


def resolver_url(env: Mapping[str, str] | None = None, dotenv_path: Path | None = None) -> str:
    """URL de tests efectiva (normalizada) o '' si no hay ninguna."""
    env = os.environ if env is None else env
    ruta = RAIZ / ".env" if dotenv_path is None else dotenv_path
    dotenv = dotenv_values(ruta) if ruta.exists() else {}
    for nombre in ("TEST_DATABASE_URL", "DATABASE_URL"):
        url = _leer(nombre, env, dotenv)
        if url:
            return normalizar_url_driver(url)
    return ""


def resolver_url_prod(env: Mapping[str, str] | None = None, dotenv_path: Path | None = None) -> str:
    env = os.environ if env is None else env
    ruta = RAIZ / ".env" if dotenv_path is None else dotenv_path
    dotenv = dotenv_values(ruta) if ruta.exists() else {}
    url = _leer("DATABASE_URL_PROD", env, dotenv)
    return normalizar_url_driver(url) if url else ""


def motivo_de_aborto(url: str, url_prod: str = "") -> str | None:
    """Mensaje de error si ``url`` apunta a producción; None si es segura."""
    if not es_postgres(url):
        return None
    host = urlparse(url).hostname or ""
    if HOST_PROD in host:
        return (
            f"La URL de tests apunta al host de producción ({HOST_PROD}...). "
            "Usa la rama dev de Neon o un Postgres local (TEST_DATABASE_URL)."
        )
    if url_prod and url == url_prod:
        return "La URL de tests es idéntica a DATABASE_URL_PROD. Revisa tu .env."
    return None


DB_URL = resolver_url()
TIENE_PG = es_postgres(DB_URL)
