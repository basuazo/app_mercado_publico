"""F-plat-lote sobre Postgres con la migración aplicada: la carrera de dos sesiones que agregan la
misma CA a dos lotes, los CHECK/UNIQUE de las tablas nuevas, `tramos` JSONB y las columnas nuevas
de `empresas`. La lógica y las rutas están en test_plat_lote.py (SQLite)."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.password import hash_password
from app.core.db import normalizar_url_driver
from app.core.tiempo import ahora_utc
from app.mercado import lote as lt
from app.mercado.proveedores import guardar_producto
from app.models.tables import (
    CaEvento,
    CaLinea,
    CompraAgil,
    Empresa,
    Lote,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    ProveedorProducto,
    SolicitudPrecio,
    Usuario,
)
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZLOTE "
_CA = "ZZLOTE-"


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(delete(Usuario).where(Usuario.email.like("zzlote%")))
        # La empresa arrastra lotes (y sus hijas), proveedores, ca_trabajo y ca_eventos.
        s.execute(delete(Empresa).where(Empresa.nombre.like(f"{_PREFIJO}%")))
        s.execute(delete(CaEvento).where(CaEvento.ca_codigo.like(f"{_CA}%")))
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like(f"{_CA}%")))
        s.execute(delete(ProductoCanonico).where(ProductoCanonico.nombre.like(f"{_PREFIJO}%")))
        s.commit()


def _mundo(s) -> dict[str, int]:
    p = ProductoCanonico(nombre=f"{_PREFIJO}Bebida", unidad_base="L", activo=True)
    s.add(p)
    s.flush()
    pack = ProductoFormato(producto_id=p.id, nombre="pack 6", factor_base=2.1)
    e = Empresa(nombre=f"{_PREFIJO}A", rut="99999994-3", regiones_prioritarias=[16], activa=True)
    s.add_all([pack, e])
    s.flush()
    prov = Proveedor(empresa_id=e.id, nombre=f"{_PREFIJO}Prov", rubros=[], activo=True)
    s.add(prov)
    s.flush()
    guardar_producto(s, prov, producto_texto="Cola pack", precio_neto=2400, factor_base=2.1,
                     unidad_base="L", producto_id=p.id, formato_id=pack.id,
                     tramos=[{"desde": 10, "precio": 2100}])  # fmt: skip
    users = []
    for i in (1, 2):
        u = Usuario(email=f"zzlote{i}@t.cl", nombre=f"u{i}", password_hash=hash_password("x" * 12),
                    empresa_id=e.id)  # fmt: skip
        s.add(u)
        users.append(u)
    for codigo in (f"{_CA}1", f"{_CA}2"):
        s.add(CompraAgil(codigo=codigo, nombre="Bebidas", estado="publicada", region=16,
                         fecha_cierre=ahora_utc() + timedelta(days=3), detalle_lineas="completo"))  # fmt: skip
    s.flush()
    for codigo in (f"{_CA}1", f"{_CA}2"):
        s.add(CaLinea(ca_codigo=codigo, n=1, producto_id=p.id, formato_id=pack.id, cantidad=5,
                      cantidad_base=10.5, texto_origen="Bebida pack", estado="ok"))  # fmt: skip
    s.commit()
    return {"emp": e.id, "u1": users[0].id, "u2": users[1].id, "prov": prov.id, "producto": p.id}


def test_carrera_dos_sesiones_agregan_la_misma_ca_a_lotes_distintos(pg_engine):
    with Session(pg_engine) as s:
        m = _mundo(s)
    resultado: dict[str, object] = {}

    def otra_persona() -> None:
        with Session(pg_engine) as s2:
            try:
                lt.crear(s2, m["emp"], s2.get(Usuario, m["u2"]), [f"{_CA}2", f"{_CA}1"])
                s2.commit()
                resultado["creado"] = True
            except lt.CaEnOtroLote as exc:
                s2.rollback()
                resultado["choque"] = exc.codigo

    with Session(pg_engine) as s1:
        lt.crear(s1, m["emp"], s1.get(Usuario, m["u1"]), [f"{_CA}1"])  # toma la fila de la empresa
        hilo = threading.Thread(target=otra_persona)
        hilo.start()
        time.sleep(0.5)
        assert hilo.is_alive()  # la segunda sesión espera el bloqueo de la empresa
        s1.commit()
    hilo.join(timeout=15)
    assert not hilo.is_alive()
    assert resultado == {"choque": f"{_CA}1"}
    with Session(pg_engine) as s:
        lotes = s.execute(select(Lote).where(Lote.empresa_id == m["emp"])).scalars().all()
        assert len(lotes) == 1


def test_check_unique_jsonb_y_columnas_de_empresa(pg_engine):
    with Session(pg_engine) as s:
        m = _mundo(s)
        e = s.get(Empresa, m["emp"])
        assert e.dias_vigencia_precio == 15 and e.margen_objetivo == pytest.approx(0.15)  # defaults
        pp = s.execute(
            select(ProveedorProducto).where(ProveedorProducto.proveedor_id == m["prov"])
        ).scalar_one()
        assert pp.tramos == [{"desde": 10.0, "precio": 2100.0}]
        lote = lt.crear(s, m["emp"], s.get(Usuario, m["u1"]), [f"{_CA}1", f"{_CA}2"])
        s.commit()
        sol = s.execute(select(SolicitudPrecio).where(SolicitudPrecio.lote_id == lote.id)).scalar_one()
        assert sol.proveedor_id == m["prov"]
        with pytest.raises(IntegrityError), s.begin_nested():
            s.get(Lote, lote.id).estado = "perdido"
            s.flush()
        s.rollback()
        with pytest.raises(IntegrityError), s.begin_nested():
            s.add(SolicitudPrecio(lote_id=lote.id, proveedor_id=m["prov"], estado="borrador"))
            s.flush()
        s.rollback()
        with pytest.raises(IntegrityError), s.begin_nested():
            s.get(SolicitudPrecio, sol.id).estado = "perdida"
            s.flush()
        s.rollback()
        # Borrar la empresa se lleva el lote y sus hijas (ON DELETE CASCADE).
        lote_id = lote.id
        s.execute(delete(Usuario).where(Usuario.email.like("zzlote%")))
        s.execute(delete(Empresa).where(Empresa.id == m["emp"]))
        s.commit()
        assert s.get(Lote, lote_id) is None
        assert s.execute(
            select(SolicitudPrecio.id).where(SolicitudPrecio.lote_id == lote_id)
        ).first() is None
