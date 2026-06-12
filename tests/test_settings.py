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
