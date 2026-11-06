"""F-plat-anexos-2 sobre Postgres: la migración junta los anexos duplicados antes del UNIQUE
(las líneas pasan al que se conserva), es reversible, y las restricciones nuevas valen."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, delete, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.models.tables import CaAnexo, CaLinea, CompraAgil
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_REV_ANTERIOR = "a3c8e5d1b7f2"  # F-plat-capas: la anterior a esta fase
_CA = "ZZA2-1-COT26"


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like("ZZA2-%")))
        s.commit()


def test_unique_check_y_estado_reemplazada(pg_engine):
    with Session(pg_engine) as s:
        s.add(CompraAgil(codigo=_CA, nombre="x", region=16))
        s.flush()
        s.add(CaLinea(ca_codigo=_CA, n=1, texto_origen="pan", origen="api", estado="reemplazada"))
        s.add(CaAnexo(ca_codigo=_CA, mime="x", tamano=1, sha256="a" * 64, fuente="masiva",
                      extraccion_estado="hecha"))  # fmt: skip
        s.commit()
        s.add(CaAnexo(ca_codigo=_CA, mime="x", tamano=1, sha256="a" * 64,
                      extraccion_estado="hecha"))  # fmt: skip
        with pytest.raises(IntegrityError):  # mismo archivo, misma CA
            s.commit()
        s.rollback()
        s.add(CaAnexo(ca_codigo=_CA, mime="x", tamano=1, sha256="b" * 64, fuente="otra",
                      extraccion_estado="hecha"))  # fmt: skip
        with pytest.raises(IntegrityError):  # fuente fuera del CHECK
            s.commit()


def _url_de(nombre_db: str) -> str:
    return (
        make_url(normalizar_url_driver(_DB_URL))
        .set(database=nombre_db)
        .render_as_string(hide_password=False)
    )


def test_migracion_junta_duplicados_y_es_reversible():
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
            c.execute(text(
                "INSERT INTO compras_agiles (codigo, nombre, descripcion, estado, total_ofertas, "
                "detalle_fallos, detalle_lineas, creado_en, actualizado_en) VALUES "
                "('ZZA2-M', 'x', '', 'DESCONOCIDO', 0, 0, 'sin_detalle', now(), now())"
            ))  # fmt: skip
            ids = [
                c.execute(
                    text(
                        "INSERT INTO ca_anexos (ca_codigo, nombre_original, mime, tamano, sha256, "
                        "escaneado, extraccion_estado) VALUES ('ZZA2-M', 'a', 'x', 1, :sha, false, "
                        "'hecha') RETURNING id"
                    ),
                    {"sha": sha},
                ).scalar_one()  # fmt: skip
                for sha in ("a" * 64, "a" * 64, "b" * 64)
            ]
            c.execute(text(
                "INSERT INTO ca_lineas (ca_codigo, n, texto_origen, texto_norm, origen, estado, "
                "anexo_id, creado_en, actualizado_en) VALUES ('ZZA2-M', 1000, 'x', 'x', 'anexo', "
                "'dudosa', :a, now(), now())"
            ), {"a": ids[1]})  # fmt: skip
        command.upgrade(cfg, "head")
        with e.connect() as c:
            quedan = c.execute(text("SELECT id FROM ca_anexos ORDER BY id")).scalars().all()
            assert quedan == [ids[0], ids[2]]  # el duplicado se juntó con el de id menor
            assert c.execute(text("SELECT anexo_id FROM ca_lineas")).scalar_one() == ids[0]
            assert c.execute(text("SELECT fuente FROM ca_anexos LIMIT 1")).scalar_one() == "manual"
        insp = inspect(e)
        assert {"fuente", "empresa_id", "extraccion_respaldo"} <= {
            col["name"] for col in insp.get_columns("ca_anexos")
        }
        assert "token_anexos_hash" in {col["name"] for col in insp.get_columns("empresas")}
        assert "documentos" in {col["name"] for col in insp.get_columns("compras_agiles")}
        estado = next(col for col in insp.get_columns("ca_lineas") if col["name"] == "estado")
        assert estado["type"].length == 12
        with e.begin() as c:
            c.execute(text("UPDATE ca_lineas SET estado = 'reemplazada'"))
        command.downgrade(cfg, _REV_ANTERIOR)
        with e.connect() as c:
            assert c.execute(text("SELECT estado FROM ca_lineas")).scalar_one() == "ok"
        assert "fuente" not in {col["name"] for col in inspect(e).get_columns("ca_anexos")}
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
