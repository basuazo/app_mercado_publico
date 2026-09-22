"""Fixtures compartidos para la suite de tests."""

from __future__ import annotations

import pytest

from app.clients.base import reset_rate_limiter_compartido
from app.core.settings import Settings


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
