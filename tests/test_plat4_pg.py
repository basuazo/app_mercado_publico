"""F-plat-4 sobre Postgres con la migración aplicada: job de punta a punta, JSONB y cascadas.

Aislado del resto de la base de tests: región 99 y meses de 2031."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.mercado import evaluacion as ev
from app.models.tables import (
    CompraAgil,
    EvaluacionModelo,
    ModeloAdjudicacion,
    PrecioIndice,
    ProductoCanonico,
)
from tests import pg_url
from tests.test_plat4 import _ca, _linea, _oferta

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZP4-"
_REGION = 99


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like(f"{_PREFIJO}%")))  # cascada
        s.execute(delete(ProductoCanonico).where(ProductoCanonico.nombre.like(f"{_PREFIJO}%")))
        s.execute(delete(EvaluacionModelo).where(EvaluacionModelo.mes.like("2031-%")))
        s.execute(
            delete(ModeloAdjudicacion).where(ModeloAdjudicacion.datos_hasta_mes.like("2031-%"))
        )
        s.commit()


def test_mercado_indices_en_postgres(pg_engine):
    with Session(pg_engine) as s:
        p = ProductoCanonico(nombre=f"{_PREFIJO}Leche", unidad_base="L", unspsc_sugerido="50131701",
                             activo=True)  # fmt: skip
        s.add(p)
        s.flush()
        for m in ("2031-04", "2031-05", "2031-06", "2031-07"):
            for i in range(52):
                cod = f"{_PREFIJO}{m}-{i}"
                _ca(s, cod, m, region=_REGION, organismo="Municipalidad de Pinto")
                _linea(s, cod, 1, p)
                _oferta(s, cod, "1-9", True, 1000 + i)
                _oferta(s, cod, "2-7", False, 1200 + i)
        s.commit()
        r = ev.run_indices(s, [_REGION], completo=True, mes_hoy="2031-08")
        estados = {e["mes"]: e["estado"] for e in r["evaluacion"]}
        assert estados["2031-07"] == "evaluado"
        assert estados["2031-05"] == "sin_datos_suficientes"
        assert s.get(CompraAgil, f"{_PREFIJO}2031-07-0").comuna_inferida == "Pinto"
        ambitos = set(
            s.execute(select(PrecioIndice.ambito).where(PrecioIndice.producto_id == p.id)).scalars()
        )
        assert ambitos == {"comuna:Pinto", "provincia:Diguillín", f"region:{_REGION}"}
        conf = s.execute(
            select(EvaluacionModelo.detalle).where(
                EvaluacionModelo.mes == "2031-07", EvaluacionModelo.modelo == "base",
                EvaluacionModelo.metrica == "brier_2mas",
            )
        ).scalar_one()  # fmt: skip
        assert conf["confiabilidad"]  # JSONB ida y vuelta
        activos = (
            s.execute(select(ModeloAdjudicacion).where(ModeloAdjudicacion.activo.is_(True)))
            .scalars()
            .all()
        )
        assert len(activos) == 1 and activos[0].datos_hasta_mes == "2031-07"
        assert "modelo" in activos[0].coeficientes
        # Borrar el producto borra su índice (FK en cascada).
        s.delete(p)
        s.commit()
        assert s.execute(select(func.count()).select_from(PrecioIndice)
                         .where(PrecioIndice.producto_id == p.id)).scalar_one() == 0  # fmt: skip
