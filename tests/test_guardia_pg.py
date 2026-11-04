"""Guardia de producción y resolución de la URL de tests (sin conectarse a nada)."""

from __future__ import annotations

from pathlib import Path

from tests import pg_url

SIN_ENV = Path("no-existe.env")


def test_host_de_produccion_aborta():
    url = "postgresql+psycopg://u:p@ep-rough-hill-abc123.sa-east-1.aws.neon.tech/neondb"
    assert "producción" in (pg_url.motivo_de_aborto(url) or "")


def test_host_de_produccion_con_pooler_y_otros_parametros_tambien_aborta():
    url = "postgresql+psycopg://otro:x@ep-rough-hill-abc123-pooler.sa-east-1.aws.neon.tech/neondb?sslmode=require"
    assert pg_url.motivo_de_aborto(url) is not None


def test_url_identica_a_prod_aborta():
    url = "postgresql+psycopg://u:p@host-dev/db"
    assert pg_url.motivo_de_aborto(url, url_prod=url) is not None


def test_dev_local_y_sqlite_no_abortan():
    assert (
        pg_url.motivo_de_aborto(
            "postgresql+psycopg://u:p@localhost:5432/t", "postgresql+psycopg://u:p@x/y"
        )
        is None
    )
    assert pg_url.motivo_de_aborto("sqlite:///:memory:") is None


def test_prioridad_test_database_url_y_normalizacion():
    env = {"TEST_DATABASE_URL": "postgres://u:p@loc/t", "DATABASE_URL": "postgresql://u:p@otro/d"}
    assert pg_url.resolver_url(env, SIN_ENV) == "postgresql+psycopg://u:p@loc/t"
    assert pg_url.resolver_url({"DATABASE_URL": "postgresql://u:p@otro/d"}, SIN_ENV) == (
        "postgresql+psycopg://u:p@otro/d"
    )
    assert pg_url.resolver_url({}, SIN_ENV) == ""


def test_lee_el_dotenv_sin_pisar_el_entorno(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("DATABASE_URL=postgresql://u:p@desde-dotenv/d\n", encoding="utf-8")
    assert pg_url.resolver_url({}, dotenv) == "postgresql+psycopg://u:p@desde-dotenv/d"
    assert pg_url.resolver_url({"DATABASE_URL": "postgresql://u:p@entorno/d"}, dotenv).endswith(
        "@entorno/d"
    )
