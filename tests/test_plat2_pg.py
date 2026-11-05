"""F-plat-2 sobre Postgres: upsert mensual de `llm_uso` (ON CONFLICT), restricción de estado de
`ca_anexos`, bytes por fila (regla 11) y migración reversible."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, delete, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients import openrouter as orc
from app.core.db import normalizar_url_driver
from app.core.settings import Settings
from app.models.tables import CaAnexo, CompraAgil, LlmCache, LlmUso
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_REV_ANTERIOR = "b6e2d8a4f1c7"  # F-merc-3: la anterior a esta fase
_CA = "ZZ2-1-COT26"


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like("ZZ2-%")))
        s.execute(delete(LlmUso).where(LlmUso.mes.like("ZZ%")))
        s.execute(delete(LlmCache).where(LlmCache.modelo == "zz-prueba"))
        s.commit()


def test_llm_uso_upsert_en_postgres(pg_engine, monkeypatch):
    monkeypatch.setattr(orc, "_mes_chile", lambda: "ZZ-01")
    s = Settings(
        mp_ticket="T", database_url=_DB_URL, secret_key="k" * 32, jobs_token="j",
        openrouter_api_key="x", llm_modelo_extraccion="zz-prueba",
    )  # fmt: skip
    c = orc.OpenRouterClient(s, pg_engine)
    c._registrar_uso(100, 10, 0.5, False)
    c._registrar_uso(50, 5, 0.25, True)
    with Session(pg_engine) as ses:
        uso = ses.get(LlmUso, "ZZ-01")
        assert uso is not None
        assert (uso.requests, uso.tokens_in, uso.tokens_out, uso.estimado) == (2, 150, 15, True)
        assert uso.costo_usd == pytest.approx(0.75)
    assert c.gasto_del_mes() == pytest.approx(0.75)


def test_ca_anexos_restriccion_y_bytes_por_fila(pg_engine):
    with Session(pg_engine) as s:
        s.add(CompraAgil(codigo=_CA, nombre="x", region=16))
        s.flush()
        s.add(
            CaAnexo(
                ca_codigo=_CA, nombre_original="anexo.pdf", mime="application/pdf", tamano=300_000,
                sha256="a" * 64, paginas=2, escaneado=False, texto_extraido="Arroz 1 kg | 20 | bolsa\n" * 80,
                extraccion_estado="hecha", extraccion_modelo="zz-prueba", extraccion_costo_usd=0.001,
            )
        )  # fmt: skip
        s.commit()
        # Regla 11: la fila pesa lo que su texto (comprimido por TOAST), nunca el archivo.
        bytes_fila = s.execute(
            text("SELECT pg_column_size(t.*) FROM ca_anexos t WHERE ca_codigo = :c"), {"c": _CA}
        ).scalar_one()
        assert bytes_fila < 4_000
        s.add(CaAnexo(ca_codigo=_CA, mime="x", tamano=1, sha256="b", extraccion_estado="otro"))
        with pytest.raises(IntegrityError):
            s.commit()


def _url_de(nombre_db: str) -> str:
    return (
        make_url(normalizar_url_driver(_DB_URL))
        .set(database=nombre_db)
        .render_as_string(hide_password=False)
    )


def test_migracion_reversible():
    from alembic.config import Config

    from alembic import command

    nombre_db = f"mp_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(normalizar_url_driver(_DB_URL), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{nombre_db}"'))
    url = _url_de(nombre_db)
    previo = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    cfg = Config(str(pg_url.RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(pg_url.RAIZ / "alembic"))
    e = create_engine(url)
    try:
        command.upgrade(cfg, _REV_ANTERIOR)
        with e.begin() as c:
            c.execute(
                text(
                    "INSERT INTO compras_agiles (codigo, nombre, descripcion, estado, total_ofertas, detalle_fallos, detalle_lineas, creado_en, actualizado_en) VALUES ('ZZ2-M', 'x', '', 'DESCONOCIDO', 0, 0, 'sin_detalle', now(), now())"
                )
            )
        command.upgrade(cfg, "head")
        insp = inspect(e)
        assert {"ca_anexos", "llm_uso", "llm_cache"} <= set(insp.get_table_names())
        assert "anexo_id" in {c["name"] for c in insp.get_columns("ca_lineas")}
        assert "anexo_resuelto_en" in {c["name"] for c in insp.get_columns("compras_agiles")}
        with e.connect() as c:
            assert (
                c.execute(text("SELECT anexo_resuelto_en FROM compras_agiles")).scalar_one() is None
            )
        command.downgrade(cfg, _REV_ANTERIOR)
        assert "ca_anexos" not in inspect(e).get_table_names()
        command.upgrade(cfg, "head")
    finally:
        e.dispose()
        if previo is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previo
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{nombre_db}" WITH (FORCE)'))
        admin.dispose()
