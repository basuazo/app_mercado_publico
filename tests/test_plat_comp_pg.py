"""F-plat-comp sobre Postgres con la migración aplicada: job de punta a punta, ILIKE, JSONB y bytes.

Aislado del resto de la base de tests: región 99 y meses de 2031."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.mercado import competencia as comp
from app.models.tables import (
    CompetenciaCompradorMes,
    CompetenciaMes,
    CompraAgil,
    EvaluacionModelo,
)
from tests import pg_url
from tests.test_plat_comp import _ca, _of

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZPC-"
_REGION = 99
_AHORA = datetime(2031, 8, 20, 12, 0)


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
        s.execute(delete(CompetenciaMes).where(CompetenciaMes.mes.like("2031-%")))
        s.execute(delete(CompetenciaCompradorMes).where(CompetenciaCompradorMes.mes.like("2031-%")))
        s.execute(delete(EvaluacionModelo).where(EvaluacionModelo.mes.like("2031-%")))
        s.commit()


def test_mercado_competencia_en_postgres(pg_engine):
    with Session(pg_engine) as s:
        for m in ("2031-06", "2031-07"):
            for i in range(30):
                cod = f"{_PREFIJO}{m}-{i}"
                _ca(s, cod, m, region=_REGION, org="61.111.111-1", unidad=f"Unidad {i % 3}")
                _of(s, cod, "76.111.111-1", 1000 + i, True, nombre="Alfa Ñandú SpA")
                _of(s, cod, "76222222-2", 1100 + i, False, nombre="Beta Ltda")
        s.commit()
        r = comp.run_competencia_mercado(
            s, [_REGION], completo=True, mes_hoy="2031-08", ahora=_AHORA
        )
        assert r["recalculo"]["meses"] == ["2031-06", "2031-07", "2031-08"]
        assert all(c["cumple"] for c in r["recalculo"]["cuadratura"])
        julio = next(j for j in r["juez_competidores"] if j["mes"] == "2031-07")
        assert julio["top1_oferta"] == 1.0 and julio["datos_insuficientes"] is True
        assert r["aporte_modelo"]["meses_evaluables"] == 0
        assert r["bytes"]["competencia_mes"]["filas"] >= 6
        assert r["bytes"]["competencia_mes"]["bytes_por_fila"] > 0
        alfa = s.execute(
            select(func.sum(CompetenciaMes.n_ganadas)).where(CompetenciaMes.rut == "76111111-1",
                                                             CompetenciaMes.mes.like("2031-%"))
        ).scalar_one()  # fmt: skip
        assert alfa == 60
        # ILIKE con tildes y eñe sobre Postgres; JSONB del juez ida y vuelta.
        enc = comp.buscar_competidores(s, "ñandú", [_REGION], mes_hoy="2031-08")
        assert [c["rut"] for c in enc] == ["76111111-1"]
        det = s.execute(
            select(EvaluacionModelo.detalle).where(
                EvaluacionModelo.mes == "2031-07", EvaluacionModelo.modelo == "competencia",
                EvaluacionModelo.metrica == "top1_oferta",
            )
        ).scalar_one()  # fmt: skip
        assert det["cas_2mas"] == 30 and det["meta"] == 0.6
        # Idempotente también en Postgres.
        antes = s.execute(select(func.count()).select_from(CompetenciaMes)
                          .where(CompetenciaMes.mes.like("2031-%"))).scalar_one()  # fmt: skip
        comp.run_competencia_mercado(s, [_REGION], mes_hoy="2031-08", ahora=_AHORA)
        despues = s.execute(select(func.count()).select_from(CompetenciaMes)
                            .where(CompetenciaMes.mes.like("2031-%"))).scalar_one()  # fmt: skip
        assert antes == despues
