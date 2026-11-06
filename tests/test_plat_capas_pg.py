"""F-plat-capas sobre Postgres con la migración aplicada: `UNIQUE`, `numeric`, `CHECK`, `ILIKE`
y cascadas de la capa «Inventario». El resto (lógica, rutas) está en test_plat_capas.py."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.mercado import oferta_propia as op
from app.models.tables import (
    Empresa,
    EmpresaProducto,
    EmpresaProductoMovimiento,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
)
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZCAPAS "


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(delete(Empresa).where(Empresa.nombre.like(f"{_PREFIJO}%")))  # cascada a lo demás
        s.execute(delete(ProductoCanonico).where(ProductoCanonico.nombre.like(f"{_PREFIJO}%")))
        s.commit()


def _mundo(s):
    p = ProductoCanonico(nombre=f"{_PREFIJO}Arroz", unidad_base="kg", activo=True)
    s.add(p)
    s.flush()
    base = ProductoFormato(producto_id=p.id, nombre="kilo", factor_base=1.0)
    saco = ProductoFormato(producto_id=p.id, nombre="saco 25 kg", factor_base=25.0)
    e1 = Empresa(nombre=f"{_PREFIJO}A", rut="99999992-7", regiones_prioritarias=[16], activa=True)
    e2 = Empresa(nombre=f"{_PREFIJO}B", rut="99999993-5", regiones_prioritarias=[16], activa=True)
    s.add_all([base, saco, e1, e2])
    s.flush()
    return p, base, saco, e1, e2


def test_unique_empresa_producto_formato_y_misma_llave_en_otra_empresa(pg_engine):
    with Session(pg_engine) as s:
        p, _, saco, e1, e2 = _mundo(s)
        s.add(EmpresaProducto(empresa_id=e1.id, producto_id=p.id, formato_id=saco.id))
        s.flush()
        s.add(EmpresaProducto(empresa_id=e2.id, producto_id=p.id, formato_id=saco.id))  # otra empresa
        s.flush()
        with pytest.raises(IntegrityError), s.begin_nested():
            s.add(EmpresaProducto(empresa_id=e1.id, producto_id=p.id, formato_id=saco.id))
            s.flush()
        s.rollback()


def test_numeric_guarda_decimales_exactos_y_check_de_stock_y_motivo(pg_engine):
    with Session(pg_engine) as s:
        p, _, saco, e1, _ = _mundo(s)
        ep, _ = op.guardar(s, e1.id, None, producto_id=p.id, formato_id=saco.id, stock=0.125,
                           stock_minimo=0.5)  # fmt: skip
        ep2_id = ep.id
        s.commit()
        s.expire_all()
        ep = s.get(EmpresaProducto, ep2_id)
        assert ep.stock == 0.125 and ep.stock_minimo == 0.5 and isinstance(ep.stock, float)
        # `stock >= 0` lo exige la base aunque la lógica ya lo valide
        with pytest.raises(IntegrityError), s.begin_nested():
            ep.stock = -1
            s.flush()
        s.rollback()
        ep = s.get(EmpresaProducto, ep2_id)
        with pytest.raises(IntegrityError), s.begin_nested():
            ep.reservado = -0.5
            s.flush()
        s.rollback()
        ep = s.get(EmpresaProducto, ep2_id)
        with pytest.raises(IntegrityError), s.begin_nested():
            s.add(EmpresaProductoMovimiento(empresa_producto_id=ep.id, delta=1, motivo="robo"))
            s.flush()
        s.rollback()


def test_ajustar_stock_nunca_negativo_en_postgres(pg_engine):
    with Session(pg_engine) as s:
        p, _, saco, e1, _ = _mundo(s)
        ep, _ = op.guardar(s, e1.id, None, producto_id=p.id, formato_id=saco.id, stock=2)
        s.commit()
        with pytest.raises(op.ErrorInventario):
            op.ajustar_stock(s, e1.id, ep.id, -2.001, "ajuste", None, None)
        s.rollback()
        op.ajustar_stock(s, e1.id, ep.id, -0.5, "entrega", "CA-1", None)
        s.commit()
        assert s.get(EmpresaProducto, ep.id).stock == 1.5
        movs = s.execute(
            select(EmpresaProductoMovimiento.delta).where(
                EmpresaProductoMovimiento.empresa_producto_id == ep.id
            ).order_by(EmpresaProductoMovimiento.id)
        ).scalars().all()
        assert movs == [2.0, -0.5]


def test_listar_ilike_escapa_comodines_y_no_distingue_mayusculas(pg_engine):
    with Session(pg_engine) as s:
        p, _, saco, e1, _ = _mundo(s)
        op.guardar(s, e1.id, None, producto_id=p.id, formato_id=saco.id, stock=1, marca="Tucapel 100%")
        s.flush()
        assert len(op.listar(s, e1.id, texto="ZZCAPAS arroz")) == 1
        assert len(op.listar(s, e1.id, texto="tucapel")) == 1
        assert len(op.listar(s, e1.id, texto="100%")) == 1
        assert op.listar(s, e1.id, texto="%zzz") == []  # el `%` es literal, no comodín
        assert op.listar(s, e1.id, texto="_") == []  # ni el `_`
        s.rollback()


def test_resumen_con_filter_y_cascada_al_borrar_la_empresa(pg_engine):
    with Session(pg_engine) as s:
        p, base, saco, e1, _ = _mundo(s)
        op.guardar(s, e1.id, None, producto_id=p.id, formato_id=saco.id, stock=1, stock_minimo=3)
        op.guardar(s, e1.id, None, producto_id=p.id, formato_id=base.id, stock=9, stock_minimo=3)
        prov = Proveedor(empresa_id=e1.id, nombre="P", rubros=[], activo=True)
        s.add(prov)
        s.flush()
        assert op.resumen(s, e1.id) == (2, 1)
        ep_ids = [v.ep.id for v in op.listar(s, e1.id)]
        assert len(ep_ids) == 2
        s.commit()
        s.execute(delete(Empresa).where(Empresa.id == e1.id))
        s.commit()
        assert s.execute(
            select(EmpresaProducto.id).where(EmpresaProducto.id.in_(ep_ids))
        ).first() is None
        # los movimientos se van con su producto (ON DELETE CASCADE)
        assert s.execute(
            select(EmpresaProductoMovimiento.id).where(
                EmpresaProductoMovimiento.empresa_producto_id.in_(ep_ids)
            )
        ).first() is None
