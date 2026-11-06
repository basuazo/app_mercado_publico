"""F-plat-seguimiento sobre Postgres con la migración aplicada: el correlativo de cotizaciones bajo
concurrencia real (FOR UPDATE sobre la empresa) y los UNIQUE/CHECK de las tablas nuevas. La lógica y
las rutas están en test_plat_seguimiento.py (SQLite)."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.mercado import cotizacion as cot
from app.models.tables import CaRequisito, CompraAgil, Empresa, OfertaPropia, Venta
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZSEG "
_CA = "ZZSEG-"


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(delete(Empresa).where(Empresa.nombre.like(f"{_PREFIJO}%")))
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like(f"{_CA}%")))
        s.commit()


def _mundo(e, n_ofertas: int) -> tuple[int, list[int]]:
    with Session(e) as s:
        emp = Empresa(nombre=f"{_PREFIJO}emp", regiones_prioritarias=[16], comunas_prioritarias=[])
        s.add(emp)
        s.flush()
        ids = []
        for i in range(n_ofertas):
            s.add(CompraAgil(codigo=f"{_CA}{i}", nombre="x", estado="publicada", region=16))
            s.flush()
            o = OfertaPropia(ca_codigo=f"{_CA}{i}", empresa_id=emp.id, lineas=[], total_neto=0, version=1)
            s.add(o)
            s.flush()
            ids.append(o.id)
        s.commit()
        return emp.id, ids


def test_correlativo_bajo_concurrencia_no_repite_numeros(pg_engine):
    emp, oferta_ids = _mundo(pg_engine, 6)
    numeros: list[str] = []
    errores: list[Exception] = []

    def _pedir(oid: int) -> None:
        try:
            with Session(pg_engine) as s:
                o = s.get(OfertaPropia, oid)
                numeros.append(cot.asignar_numero(s, emp, o))
                s.commit()
        except Exception as exc:  # pragma: no cover
            errores.append(exc)

    hilos = [threading.Thread(target=_pedir, args=(i,)) for i in oferta_ids]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert not errores
    assert len(set(numeros)) == 6
    with Session(pg_engine) as s:
        assert s.get(Empresa, emp).ultimo_correlativo == 6


def test_unique_de_numero_y_check_de_estado_de_venta(pg_engine):
    emp, oferta_ids = _mundo(pg_engine, 2)
    with Session(pg_engine) as s:
        for oid in oferta_ids:
            s.get(OfertaPropia, oid).numero_cotizacion = "COT-2026-1"
        with pytest.raises(IntegrityError):
            s.commit()
    with Session(pg_engine) as s:
        s.add(Venta(empresa_id=emp, ca_codigo=f"{_CA}0", estado="inventado", version=1))
        with pytest.raises(IntegrityError):
            s.commit()
    with Session(pg_engine) as s:
        s.add(CaRequisito(empresa_id=emp, ca_codigo=f"{_CA}0", texto="x", origen="otro"))
        with pytest.raises(IntegrityError):
            s.commit()
