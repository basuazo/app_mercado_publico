"""F-plat-impuestos sobre Postgres con la migración aplicada: UNIQUE (codigo, vigente_desde), los CHECK
de tasa y modo, y las columnas nuevas. La lógica y las rutas están en test_plat_impuestos.py."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, delete, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.models.tables import FamiliaImpuesto, ImpuestoEspecifico
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_COD = "zzimp_test"
_FAM = "ZZIMP familia"


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(delete(ImpuestoEspecifico).where(ImpuestoEspecifico.codigo == _COD))
        s.execute(delete(FamiliaImpuesto).where(FamiliaImpuesto.familia == _FAM))
        s.commit()


def _imp(tasa: float, desde: date) -> ImpuestoEspecifico:
    return ImpuestoEspecifico(codigo=_COD, nombre="Test", tasa=tasa, vigente_desde=desde)


def test_una_tasa_nueva_es_otra_fila_y_no_se_repite_la_vigencia(pg_engine):
    with Session(pg_engine) as s:
        s.add_all([_imp(0.10, date(2014, 10, 1)), _imp(0.12, date(2027, 1, 1))])
        s.commit()
        fila = s.query(ImpuestoEspecifico).filter_by(codigo=_COD).first()
        assert fila is not None and fila.recuperable is True  # default del servidor y del ORM
    with Session(pg_engine) as s:
        s.add(_imp(0.11, date(2014, 10, 1)))
        with pytest.raises(IntegrityError):
            s.commit()


@pytest.mark.parametrize("tasa", [-0.01, 1.0])
def test_tasa_es_una_fraccion(pg_engine, tasa):
    with Session(pg_engine) as s:
        s.add(_imp(tasa, date(2014, 10, 1)))
        with pytest.raises(IntegrityError):
            s.commit()


def test_modo_de_la_familia(pg_engine):
    with Session(pg_engine) as s:
        s.add(FamiliaImpuesto(familia=_FAM, impuesto_codigo=_COD, modo="otro"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_columnas_nuevas_nullable(pg_engine):
    insp = inspect(pg_engine)
    for tabla in ("producto_canonico", "proveedor_productos"):
        col = {c["name"]: c for c in insp.get_columns(tabla)}["impuesto_codigo"]
        assert col["nullable"] is True
