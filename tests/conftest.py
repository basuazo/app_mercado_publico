"""Fixtures compartidos para la suite de tests."""

from __future__ import annotations

import pytest

from app.core.settings import Settings


@pytest.fixture(scope="session")
def db_url() -> str:
    """Retorna DATABASE_URL del entorno y verifica que NO apunte a la branch production.

    Neon genera hostnames con el patrón:
      <user>.<region>-<branch-slug>-<project>.neon.host
    La branch production tiene el slug "-production-" en el hostname.
    Si apunta a prod, el test falla para proteger datos reales.
    """
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception:
        pytest.skip("No hay DATABASE_URL configurada — tests de Postgres omitidos")

    url = settings.database_url
    if not (url.startswith("postgresql") or url.startswith("postgres")):
        # SQLite: no hay riesgo de apuntar a prod
        return url

    # Protección: rechazar si el hostname contiene el slug de la branch production
    # Neon usa "-production-" (o "production" sin prefijo si es la branch principal)
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    forbidden_slugs = ("-production-", "production.")
    for slug in forbidden_slugs:
        if slug in host:
            pytest.fail(
                f"DATABASE_URL apunta a la branch production de Neon ({host}). "
                "Usa la branch 'dev' para tests. Revisa tu .env."
            )

    return url
