"""F-plat-5 sobre Postgres con la migración aplicada: `ON CONFLICT DO NOTHING` de las predicciones,
UNIQUE (CA, empresa) sin `modelo_id`, JSONB y el UNIQUE de ofertas propias. Aislado: CA `ZZP5-*` y
una empresa propia."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.core.tiempo import ahora_utc
from app.mercado import adjudicacion as adj
from app.mercado import sombra
from app.models.tables import (
    CompraAgil,
    Empresa,
    EvaluacionCache,
    ModeloAdjudicacion,
    OfertaPropia,
    Prediccion,
)
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZP5-"
_EMPRESA = "ZZ Plat5 Tests"


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        ids = select(Empresa.id).where(Empresa.nombre == _EMPRESA)
        s.execute(delete(Prediccion).where(Prediccion.empresa_id.in_(ids)))
        s.execute(delete(OfertaPropia).where(OfertaPropia.empresa_id.in_(ids)))
        s.execute(delete(Empresa).where(Empresa.nombre == _EMPRESA))  # cascada a la caché
        s.execute(delete(ModeloAdjudicacion).where(ModeloAdjudicacion.datos_hasta_mes == "1999-01"))
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like(f"{_PREFIJO}%")))
        s.commit()


def _ev(codigo: str, empresa_id: int, precio: float) -> adj.EvaluacionCA:
    return adj.EvaluacionCA(
        ca_codigo=codigo, empresa_id=empresa_id, comuna=None, comuna_fuente=None,
        mes_referencia="2026-11", lineas=[], logistica=None, costo_productos=None,
        costo_financiero=None, costo_total=None, precio_sugerido=precio, intervalo_80=(1.0, 2.0),
        curva=[adj.PuntoCurva("sugerido", precio, 0.4)], p_ganar={"sugerido": 0.4},
        margen_esperado=None, margen_pct=None, cobertura=0.9, completa=True, modelo="base",
        avisos=[],
    )  # fmt: skip


def test_prediccion_una_por_ca_y_empresa_aunque_cambie_el_modelo(pg_engine):
    ahora = ahora_utc()
    with Session(pg_engine) as s:
        emp = Empresa(nombre=_EMPRESA, regiones_prioritarias=[16])
        s.add(emp)
        s.add(CompraAgil(codigo=f"{_PREFIJO}1", nombre="x", estado="publicada", region=16,
                         fecha_cierre=ahora + timedelta(days=1)))  # fmt: skip
        m1 = ModeloAdjudicacion(tipo="base", datos_hasta_mes="1999-01", n_filas=1,
                                coeficientes={"modelo": {"tipo": "base", "b": -1}}, metricas={})  # fmt: skip
        m2 = ModeloAdjudicacion(tipo="base", datos_hasta_mes="1999-01", n_filas=1,
                                coeficientes={"modelo": {"tipo": "base", "b": -2}}, metricas={})  # fmt: skip
        s.add_all([m1, m2])
        s.commit()
        a1 = adj.ModeloActivo(adj.ModeloBase(-1), adj.TablaN({}, {}), m1.id, "1999-01")
        a2 = adj.ModeloActivo(adj.ModeloBase(-2), adj.TablaN({}, {}), m2.id, "1999-01")
        cod = f"{_PREFIJO}1"
        assert sombra.insertar_prediccion(s, _ev(cod, emp.id, 1000.0), emp.id, None, a1, ahora)
        s.commit()
        # Otro modelo, otro precio: ON CONFLICT DO NOTHING, la predicción no cambia.
        assert not sombra.insertar_prediccion(s, _ev(cod, emp.id, 5.0), emp.id, None, a2, ahora)
        s.commit()
        p = s.execute(select(Prediccion).where(Prediccion.empresa_id == emp.id)).scalar_one()
        assert (p.precio_sugerido, p.modelo_id) == (1000.0, m1.id)
        assert p.detalle["curva"][0]["nombre"] == "sugerido"  # JSONB ida y vuelta
        s.add(EvaluacionCache(ca_codigo=cod, empresa_id=emp.id, evaluada_en=ahora, datos={"a": 1}))
        s.commit()
        assert s.execute(select(func.count()).select_from(EvaluacionCache)
                         .where(EvaluacionCache.empresa_id == emp.id)).scalar_one() == 1  # fmt: skip
        s.add(OfertaPropia(ca_codigo=cod, empresa_id=emp.id, lineas=[], total_neto=1.0,
                           guardada_en=ahora))  # fmt: skip
        s.commit()
        s.add(OfertaPropia(ca_codigo=cod, empresa_id=emp.id, lineas=[], total_neto=2.0,
                           guardada_en=ahora))  # fmt: skip
        with pytest.raises(IntegrityError):
            s.commit()
