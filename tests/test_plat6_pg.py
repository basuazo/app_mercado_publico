"""F-plat-6 sobre Postgres con la migración aplicada: job de punta a punta, upsert con
`ON CONFLICT … RETURNING` que conserva el estado, JSONB, borrado acotado a regiones y bytes.

Aislado del resto de la base de tests: regiones 98 y 99, meses de 2031 y códigos `ZZP6-`."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.mercado import abastecimiento as ab
from app.mercado import demanda as dm
from app.models.tables import (
    DemandaNombre,
    DemandaSerie,
    Empresa,
    EvaluacionModelo,
    OcLineaCa,
    Pronostico,
    Recomendacion,
)
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZP6-"
_REGION, _OTRA = 99, 98
_HOY = "2031-08"
_AHORA = datetime(2031, 8, 20, 12, 0)
_LECHE = "50131701"


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    regiones = [_REGION, _OTRA]
    with Session(e) as s:
        s.execute(delete(OcLineaCa).where(OcLineaCa.codigo_oc.like(f"{_PREFIJO}%")))
        s.execute(delete(DemandaSerie).where(DemandaSerie.region.in_(regiones)))
        s.execute(delete(Pronostico).where(Pronostico.region.in_(regiones)))
        s.execute(delete(DemandaNombre).where(DemandaNombre.region.in_(regiones)))
        s.execute(delete(Empresa).where(Empresa.nombre.like(f"{_PREFIJO}%")))  # cascada
        s.execute(
            delete(EvaluacionModelo).where(
                EvaluacionModelo.modelo == "demanda", EvaluacionModelo.mes.like("2031-%")
            )
        )
        s.commit()


def _oc(s: Session, codigo: str, mes: str, *, region: int = _REGION, cantidad: float = 100) -> None:
    y, m = (int(x) for x in mes.split("-"))
    s.add(OcLineaCa(
        codigo_oc=f"{_PREFIJO}{codigo}", id_item="1", mes=mes, fecha_creacion=datetime(y, m, 10),
        rut_unidad="61999999-9" if region == _REGION else "61888888-8",
        unidad_compra="Abastecimiento", organismo="Hospital Ñandú", comuna_compra="Chillán",
        region_compra=region, rut_proveedor="76111111-1", proveedor="Alfa SpA",
        codigo_producto=_LECHE, producto_generico="Leche", grupo="Alimentos",
        especificacion="leche", cantidad=cantidad, unidad_medida="Litro", precio_neto=1000,
        total_linea_neto=cantidad * 1000, precio_base=1000, base="L", moneda="CLP",
    ))  # fmt: skip


def test_mercado_demanda_en_postgres(pg_engine):
    with Session(pg_engine) as s:
        meses = [dm.sumar_meses("2029-08", k) for k in range(24)]  # ago-2029 a jul-2031
        for i, m in enumerate(meses):
            _oc(s, f"A{i}", m)
            _oc(s, f"B{i}", m, region=_OTRA)
        empresa = Empresa(nombre=f"{_PREFIJO}E", regiones_prioritarias=[_REGION])
        s.add(empresa)
        s.commit()
        r = dm.run_demanda(s, [_REGION, _OTRA], completo=True, mes_hoy=_HOY, ahora=_AHORA)
        assert r["series"]["e_pct_cantidad_coincide"] == 1.0
        assert r["bytes"]["demanda_series"]["filas"] >= 48
        assert r["bytes"]["demanda_series"]["bytes_por_fila"] > 0
        assert r["backtest"]["unspsc"]["elegibles"] > 0
        assert r["pronosticos"]["filas"] > 0
        # Borrado acotado: recalcular solo la región 99 deja intacta la 98.
        n98 = s.execute(
            select(func.count()).select_from(DemandaSerie).where(DemandaSerie.region == _OTRA)
        ).scalar_one()
        dm.run_demanda(s, [_REGION], mes_hoy=_HOY, ahora=_AHORA)
        assert (
            s.execute(
                select(func.count()).select_from(DemandaSerie).where(DemandaSerie.region == _OTRA)
            ).scalar_one()
            == n98
        )
        # JSONB del comprador ida y vuelta.
        nombre = s.execute(
            select(DemandaNombre).where(DemandaNombre.clave == "61999999-9")
        ).scalar_one()
        assert nombre.nombre == "Hospital Ñandú" and nombre.datos["proveedores"][0]["rut"]

        # Upsert con ON CONFLICT … RETURNING: re-ejecutar no duplica ni resucita.
        r1 = ab.recomendar(s, empresa.id, mes_hoy=_HOY, ver={}, ahora=_AHORA)
        assert r1["generadas"] > 0 and r1["nuevas"] == r1["generadas"]
        recs = list(
            s.execute(select(Recomendacion).where(Recomendacion.empresa_id == empresa.id)).scalars()
        )
        assert any(x.comprador == "" for x in recs)  # precio firme / hueco: sin comprador
        descartada = recs[0]
        ab.cambiar_estado(descartada, "descartada", None)
        s.commit()
        r2 = ab.recomendar(s, empresa.id, mes_hoy=_HOY, ver={}, ahora=_AHORA + timedelta(hours=1))
        assert r2["nuevas"] == 0 and r2["actualizadas"] == r1["generadas"]
        s.expire_all()
        assert s.get(Recomendacion, descartada.id).estado == "descartada"
        assert (
            s.execute(
                select(func.count())
                .select_from(Recomendacion)
                .where(Recomendacion.empresa_id == empresa.id)
            ).scalar_one()
            == r1["generadas"]
        )
