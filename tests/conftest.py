"""Fixtures compartidos para la suite de tests."""

from __future__ import annotations

import os

import pytest

from app.clients.base import reset_rate_limiter_compartido
from app.core.settings import Settings
from tests import pg_url


def pytest_sessionstart(session: pytest.Session) -> None:
    """Guardia de producción y de migraciones, antes de que corra ningún test."""
    # Los tests nunca ven la key real de OpenRouter del .env: una variable de entorno
    # (aun vacía) manda sobre el .env en pydantic-settings, así que cubre todo
    # Settings() de la suite, no solo el de un fixture.
    os.environ["OPENROUTER_API_KEY"] = ""
    os.environ["LLM_MODELO_EXTRACCION"] = ""
    url = pg_url.resolver_url()
    problema = pg_url.motivo_de_aborto(url, pg_url.resolver_url_prod())
    if problema:
        pytest.exit(f"ABORTO (guardia de producción): {problema}", returncode=2)
    if not pg_url.es_postgres(url):
        return
    # Una sola URL para todo: Settings() dentro de los tests ve la misma base.
    os.environ["DATABASE_URL"] = url
    if session.config.getoption("markexpr", "").replace(" ", "") == "notpostgres":
        return  # ciclo rápido: no toca la base, no hace falta que esté arriba
    _exigir_migrada(url)


def _exigir_migrada(url: str) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    cfg = Config(str(pg_url.RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(pg_url.RAIZ / "alembic"))
    cabeza = set(ScriptDirectory.from_config(cfg).get_heads())
    engine = create_engine(url)
    try:
        with engine.connect() as c:
            actual = {r[0] for r in c.execute(text("SELECT version_num FROM alembic_version"))}
    except Exception as exc:
        pytest.exit(
            f"No pude leer alembic_version en la base de tests ({type(exc).__name__}). "
            "Aplica `alembic upgrade head` en la base de tests.",
            returncode=2,
        )
    finally:
        engine.dispose()
    if actual != cabeza:
        pytest.exit(
            f"La base de tests está en {sorted(actual)} y el repo en {sorted(cabeza)}: "
            "aplica `alembic upgrade head` en la base de tests.",
            returncode=2,
        )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Marca `postgres` a todo test que necesita Postgres (para -m postgres / -m "not postgres")."""
    for item in items:
        usa_pg = "pg_engine" in getattr(item, "fixturenames", ())
        for m in item.iter_markers("skipif"):
            razon = str(m.kwargs.get("reason", ""))
            if "DATABASE_URL" in razon and "ostgres" in razon:
                usa_pg = True
        if usa_pg:
            item.add_marker(pytest.mark.postgres)


@pytest.fixture(autouse=True)
def _limiter_compartido_limpio():
    """Cada test arranca con el RateLimiter del proceso sin crear.

    Si no, el primer test fijaría la tasa para toda la suite y el bucket vacío
    de uno haría esperar al siguiente.
    """
    reset_rate_limiter_compartido()
    yield
    reset_rate_limiter_compartido()


@pytest.fixture(scope="session")
def db_url() -> str:
    """Retorna DATABASE_URL del entorno y verifica que NO sea igual a DATABASE_URL_PROD.

    Si ambas URLs son idénticas el entorno apunta a la branch production de Neon
    y los tests fallan para proteger datos reales.
    """
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception:
        pytest.skip("No hay DATABASE_URL configurada — tests de Postgres omitidos")

    url = settings.database_url
    if not (url.startswith("postgresql") or url.startswith("postgres")):
        # SQLite: no hay riesgo de apuntar a prod
        return url

    prod_url = getattr(settings, "database_url_prod", "")
    if prod_url and url.strip() == prod_url.strip():
        pytest.fail(
            "DATABASE_URL es idéntica a DATABASE_URL_PROD — los tests apuntarían "
            "a la branch production de Neon. Revisa tu .env."
        )

    return url
