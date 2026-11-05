"""F-plat-6-fix: inserciones por lote en UNA sentencia y UPDATE de comunas por lote.

Offline (SQLite en memoria). Un listener `before_cursor_execute` cuenta las sentencias y marca si
alguna fue executemany: con psycopg contra Neon cada fila de un executemany costaba un viaje.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core import lotes
from app.mercado import indice as idx
from app.models.base import Base
from app.models.tables import CompraAgil, DemandaSerie, EvaluacionModelo


@pytest.fixture()
def engine():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)  # fmt: skip
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def s(engine):
    with Session(engine) as session:
        yield session


@contextmanager
def _sentencias(engine: Engine, prefijo: str) -> Iterator[list[bool]]:
    """Lista con un `executemany` (bool) por cada sentencia que empieza con `prefijo`."""
    vistas: list[bool] = []

    def _ver(conn: Any, cursor: Any, statement: str, params: Any, context: Any, many: bool) -> None:
        if statement.lstrip().upper().startswith(prefijo):
            vistas.append(many)

    event.listen(engine, "before_cursor_execute", _ver)
    try:
        yield vistas
    finally:
        event.remove(engine, "before_cursor_execute", _ver)


def _serie(i: int) -> dict[str, Any]:
    return {"comprador": f"R{i}", "nivel": "unspsc", "item": f"5013{i:04d}|L", "mes": "2026-08",
            "region": 16, "n_oc": 1, "n_ca": 0, "cantidad_base": 1.0, "base": "L",
            "monto_neto": 1000.0, "precio_mediana": None, "n_proveedores": 1}  # fmt: skip


def test_filas_por_lote_respeta_el_tope_de_parametros():
    assert lotes.filas_por_lote(10) == lotes.MAX_FILAS
    assert lotes.filas_por_lote(100) == lotes.MAX_PARAMETROS // 100
    assert lotes.filas_por_lote(10**6) == 1
    assert lotes.filas_por_lote(0) == lotes.MAX_FILAS
    n_cols = len(DemandaSerie.__table__.columns)
    assert lotes.lote_de(DemandaSerie) * n_cols <= lotes.MAX_PARAMETROS
    assert lotes.lote_de(DemandaSerie.__table__) == lotes.lote_de(DemandaSerie)


def test_n_mayor_que_el_lote_genera_ceil_n_sobre_lote_sentencias_sin_executemany(engine, s):
    lote = lotes.lote_de(DemandaSerie)
    n = 2 * lote + 37
    with _sentencias(engine, "INSERT") as vistas:
        hechas = lotes.insertar_por_lotes(s, DemandaSerie, [_serie(i) for i in range(n)])
        s.commit()
    assert hechas == math.ceil(n / lote) == 3
    assert vistas == [False] * 3  # una sentencia por lote, ninguna executemany
    assert s.execute(select(func.count()).select_from(DemandaSerie)).scalar_one() == n


def test_lote_chico_y_lista_vacia(engine, s, monkeypatch):
    monkeypatch.setattr(lotes, "MAX_FILAS", 4)
    with _sentencias(engine, "INSERT") as vistas:
        assert lotes.insertar_por_lotes(s, DemandaSerie, []) == 0
        assert lotes.insertar_por_lotes(s, DemandaSerie, [_serie(i) for i in range(9)]) == 3
        s.commit()
    assert vistas == [False] * 3
    filas = s.execute(select(DemandaSerie).order_by(DemandaSerie.comprador)).scalars().all()
    assert [f.comprador for f in filas] == [f"R{i}" for i in range(9)]
    assert all(f.precio_mediana is None and f.monto_neto == 1000.0 for f in filas)


def test_json_y_none_quedan_igual_que_antes(engine, s):
    """`detalle` (JSON) con dict y con None: el VALUES multi-fila no cambia lo que se guarda."""
    ahora = datetime(2026, 11, 5, 12)
    filas = [
        {"mes": "2026-08", "modelo": "demanda", "metrica": "precision_unspsc", "valor": 0.5,
         "n": 10, "detalle": {"nivel": "unspsc", "elegibles": 10}, "empresa_id": None,
         "calculado_en": ahora},
        {"mes": "2026-08", "modelo": "demanda", "metrica": "recall_unspsc", "valor": None,
         "n": 0, "detalle": None, "empresa_id": None, "calculado_en": ahora},
    ]  # fmt: skip
    lotes.insertar_por_lotes(s, EvaluacionModelo, filas)
    s.commit()
    por_metrica = {e.metrica: e for e in s.execute(select(EvaluacionModelo)).scalars()}
    assert por_metrica["precision_unspsc"].detalle == {"nivel": "unspsc", "elegibles": 10}
    assert por_metrica["recall_unspsc"].valor is None
    assert por_metrica["recall_unspsc"].detalle is None


def _ca(s: Session, codigo: str, organismo: str, rut: str) -> None:
    s.add(CompraAgil(codigo=codigo, nombre=codigo, estado="cerrada", region=16,
                     organismo_nombre=organismo, organismo_rut=rut))  # fmt: skip


def test_inferir_comunas_actualiza_por_lote_sin_tocar_actualizado_en(engine, s, monkeypatch):
    monkeypatch.setattr(idx, "LOTE_UPDATE", 3)
    comunas = ["Quirihue", "Coihueco", "Yungay", "Bulnes", "Pinto", "San Carlos", "Coelemu"]
    for i, c in enumerate(comunas):
        _ca(s, f"CA-{i}", f"Municipalidad de {c}", f"{i + 1}-9")
    _ca(s, "CA-X", "Servicio de Salud", "99-9")  # sin comuna: no cambia
    s.commit()
    antes = {ca.codigo: ca.actualizado_en for ca in s.execute(select(CompraAgil)).scalars()}
    with _sentencias(engine, "UPDATE") as vistas:
        stats = idx.inferir_comunas(s, [16], todas=True)
    assert stats["cambiadas"] == len(comunas)
    assert vistas == [False] * math.ceil(len(comunas) / 3)  # 3 UPDATE, no 7
    s.expire_all()
    for i, c in enumerate(comunas):
        ca = s.get(CompraAgil, f"CA-{i}")
        assert (ca.comuna_inferida, ca.comuna_fuente) == (c, "texto")
    assert s.get(CompraAgil, "CA-X").comuna_inferida is None
    for codigo, t in antes.items():
        assert s.get(CompraAgil, codigo).actualizado_en == t
    # Segunda pasada: nada cambió, ningún UPDATE.
    with _sentencias(engine, "UPDATE") as vistas:
        assert idx.inferir_comunas(s, [16], todas=True)["cambiadas"] == 0
    assert vistas == []
