"""F-plat-3 sobre Postgres con la migración aplicada: restricciones, cascadas y JSONB."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.core.settings import Settings
from app.mercado.proveedores import guardar_producto
from app.mercado.territorio import regiones_activas
from app.models.tables import (
    CostoHistorial,
    Empresa,
    Proveedor,
    ProveedorProducto,
    TarifaLogistica,
    Usuario,
)
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZPLAT3 "


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(delete(Usuario).where(Usuario.email.like("zzplat3-%")))
        s.execute(delete(Empresa).where(Empresa.nombre.like(f"{_PREFIJO}%")))  # cascada a lo demás
        s.commit()


def test_cadena_empresa_proveedor_producto_historial_y_cascada(pg_engine):
    with Session(pg_engine) as s:
        e = Empresa(nombre=f"{_PREFIJO}A", rut="99999991-9", regiones_prioritarias=[16, 8],
                    comunas_prioritarias=["Chillán"], activa=True)  # fmt: skip
        s.add(e)
        s.flush()
        u = Usuario(email="zzplat3-u@t.cl", password_hash="x", empresa_id=e.id)
        s.add(u)
        p = Proveedor(empresa_id=e.id, nombre="P", rubros=["lácteos"], despacha=True, activo=True)
        s.add(p)
        s.flush()
        pp, _ = guardar_producto(s, p, producto_texto="Leche", formato_texto="caja 1 L",
                                 precio_neto=890, factor_base=1, unidad_base="L", usuario_id=u.id)  # fmt: skip
        guardar_producto(s, p, producto_texto="Leche", formato_texto="caja 1 L", precio_neto=950,
                         factor_base=1, unidad_base="L", usuario_id=u.id)  # fmt: skip
        s.add(TarifaLogistica(empresa_id=e.id, ambito="provincia", nombre="Itata",
                              costo_fijo_clp=1, costo_por_kg_clp=0.5))  # fmt: skip
        s.commit()
        assert s.scalars(select(Proveedor)).first().rubros == ["lácteos"]
        assert s.get(Empresa, e.id).regiones_prioritarias == [16, 8]
        assert len(s.scalars(select(CostoHistorial).where(CostoHistorial.proveedor_producto_id == pp.id)).all()) == 2
        eid = e.id

        # Restricciones únicas: producto+formato por proveedor, tarifa por empresa, RUT de empresa.
        for objeto in (
            ProveedorProducto(proveedor_id=p.id, producto_texto="Leche", formato_texto="caja 1 L",
                              precio_neto_actual=1, vigente_desde=pp.vigente_desde),
            TarifaLogistica(empresa_id=eid, ambito="provincia", nombre="Itata",
                            costo_fijo_clp=0, costo_por_kg_clp=0),
            Empresa(nombre=f"{_PREFIJO}B", rut="99999991-9", regiones_prioritarias=[16],
                    comunas_prioritarias=[], activa=True),
        ):  # fmt: skip
            with pytest.raises(IntegrityError):
                s.add(objeto)
                s.flush()
            s.rollback()

        # Borrar la empresa arrastra proveedores, productos, historial y tarifas; el usuario
        # queda sin empresa (SET NULL), no se borra.
        s.execute(delete(Empresa).where(Empresa.id == eid))
        s.commit()
        assert s.scalars(select(Proveedor).where(Proveedor.empresa_id == eid)).all() == []
        assert all(h.proveedor_producto_id != pp.id for h in s.scalars(select(CostoHistorial)))
        assert s.scalars(select(Usuario).where(Usuario.email == "zzplat3-u@t.cl")).one().empresa_id is None


def test_regiones_activas_en_postgres(pg_engine):
    settings = Settings(mp_ticket="T", database_url=_DB_URL, secret_key="k" * 32, jobs_token="j")
    with Session(pg_engine) as s:
        base = regiones_activas(s, settings)
        s.add(Empresa(nombre=f"{_PREFIJO}R", regiones_prioritarias=[16, 2],
                      comunas_prioritarias=[], activa=True))  # fmt: skip
        s.commit()
        assert regiones_activas(s, settings) == sorted(set(base) | {2, 16})
