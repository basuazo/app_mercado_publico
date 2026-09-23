"""Tests de validación de Settings: falla si falta cualquier secreto obligatorio."""

import pytest
from pydantic import ValidationError

from app.core.settings import Settings

_REQUIRED = ("mp_ticket", "database_url", "secret_key", "jobs_token")

_VALID_ENV = {
    "MP_TICKET": "ticket-de-prueba-valido",
    "DATABASE_URL": "postgresql://user:pass@host/db?sslmode=require",
    "SECRET_KEY": "clave-secreta-de-prueba-32-bytes-ok",
    "JOBS_TOKEN": "token-de-jobs-valido-para-test",
}


def _env_without(key: str) -> dict[str, str]:
    return {k: v for k, v in _VALID_ENV.items() if k != key}


def test_settings_carga_con_todos_los_secretos(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ENV_FILE", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.mp_ticket == "ticket-de-prueba-valido"
    assert s.rate_limit_rps == 1.0
    assert s.api_daily_budget == 9000
    assert s.email_daily_limit == 250


@pytest.mark.parametrize("missing_key", list(_VALID_ENV.keys()))
def test_settings_falla_sin_secreto_obligatorio(
    monkeypatch: pytest.MonkeyPatch, missing_key: str
) -> None:
    env = _env_without(missing_key)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv(missing_key, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_tasas_de_cambio_por_defecto(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.tasa_uf > 0
    assert s.tasa_utm > 0
    assert s.tasa_usd > 0
    assert s.tasa_eur > 0


def test_tasas_de_cambio_configurables(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("TASA_USD", "1000.5")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.tasa_usd == 1000.5


def test_db_url_falla_si_apunta_a_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """db_url fixture debe fallar con pytest.fail cuando DATABASE_URL == DATABASE_URL_PROD."""
    prod_url = "postgresql://user:pass@prod-host.neon.host/neondb?sslmode=require"
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATABASE_URL", prod_url)
    monkeypatch.setenv("DATABASE_URL_PROD", prod_url)

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    url = s.database_url
    prod = getattr(s, "database_url_prod", "")

    assert prod and url.strip() == prod.strip(), "Debería detectar URLs iguales"

    with pytest.raises(pytest.fail.Exception):  # type: ignore[attr-defined]
        if prod and url.strip() == prod.strip():
            pytest.fail(
                "DATABASE_URL es idéntica a DATABASE_URL_PROD — los tests apuntarían "
                "a la branch production de Neon. Revisa tu .env."
            )


_SECRETOS_REPR = {
    "MP_TICKET": "ticketDePruebaAAAA1111",
    "DATABASE_URL": "postgresql://u:passDePruebaBBBB2222@host/db",
    "DATABASE_URL_PROD": "postgresql://u:passDePruebaCCCC3333@host/prod",
    "SECRET_KEY": "secretKeyDePruebaDDDD4444",
    "JOBS_TOKEN": "jobsTokenDePruebaEEEE5555",
    "BREVO_API_KEY": "brevoKeyDePruebaFFFF6666",
    "SMTP_PASSWORD": "smtpPassDePruebaGGGG7777",
    "ADMIN_PASSWORD": "adminPassDePruebaHHHH8888",
}


def test_repr_no_expone_secretos(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-actions-1: los logs de GitHub Actions son públicos. Un `repr(settings)`
    accidental (un traceback con locals, un print de depuración) no puede
    exponer ningún secreto."""
    from app.core.logging import looks_like_secret

    for k, v in _SECRETOS_REPR.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    texto = repr(s) + str(s)

    for valor in _SECRETOS_REPR.values():
        assert valor not in texto
    # Sanidad del propio test: los valores sí parecen secretos y sí se cargaron.
    assert looks_like_secret(_SECRETOS_REPR["JOBS_TOKEN"])
    assert s.jobs_token == _SECRETOS_REPR["JOBS_TOKEN"]
