"""F-plat-1 sobre Postgres: migración con datos previos, restricciones y job sobre la base real."""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.mercado import catalogo as cat
from app.models.tables import (
    CaLinea,
    CaProducto,
    CompraAgil,
    Empresa,
    ProductoAlias,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    ProveedorProducto,
)
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZPLAT1 "
_REV_ANTERIOR = "d3b7a9e1c5f8"  # F-plat-3: la anterior a esta fase


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like("ZZ1-%")))
        s.execute(delete(ProductoAlias).where(ProductoAlias.texto_normalizado.like("zzplat1%")))
        s.execute(delete(Empresa).where(Empresa.nombre.like(f"{_PREFIJO}%")))
        s.execute(delete(ProductoCanonico).where(ProductoCanonico.nombre.like(f"{_PREFIJO}%")))
        s.commit()


def test_restricciones_del_catalogo(pg_engine):
    with Session(pg_engine) as s:
        p = cat.crear_producto(s, nombre=f"{_PREFIJO}Quinoa", unidad_base="kg")
        s.commit()
        # nombre único
        with pytest.raises(cat.CatalogoError):
            cat.crear_producto(s, nombre=f"{_PREFIJO}quinoa", unidad_base="kg")
        s.rollback()
        s.add(ProductoCanonico(nombre=f"{_PREFIJO}Quinoa", unidad_base="kg"))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
        # factor > 0 y formato único por producto
        s.add(ProductoFormato(producto_id=p.id, nombre="malo", factor_base=0))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
        s.add(ProductoFormato(producto_id=p.id, nombre="kilo", factor_base=1))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
        # alias único por texto
        a = dict(producto_id=p.id, fuente="humano", confianza=1.0, veces_visto=1)
        s.add(ProductoAlias(texto_normalizado="zzplat1 quinoa", **a))
        s.add(ProductoAlias(texto_normalizado="zzplat1 quinoa", **a))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_ca_lineas_unico_por_ca_y_n_y_cascada(pg_engine):
    with Session(pg_engine) as s:
        s.add(CompraAgil(codigo="ZZ1-1", nombre="x", region=16))
        s.flush()
        s.add(
            CaLinea(
                ca_codigo="ZZ1-1", n=1, texto_origen="a", texto_norm="a", origen="api", estado="ok"
            )
        )
        s.flush()
        s.add(
            CaLinea(
                ca_codigo="ZZ1-1", n=1, texto_origen="b", texto_norm="b", origen="api", estado="ok"
            )
        )
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
        s.add(CompraAgil(codigo="ZZ1-2", nombre="x", region=16))
        s.flush()
        s.add(
            CaLinea(
                ca_codigo="ZZ1-2", n=1, texto_origen="a", texto_norm="a", origen="api", estado="ok"
            )
        )
        s.commit()
        s.execute(delete(CompraAgil).where(CompraAgil.codigo == "ZZ1-2"))  # cascada a las líneas
        s.commit()
        assert s.execute(select(CaLinea).where(CaLinea.ca_codigo == "ZZ1-2")).first() is None


def test_borrar_producto_deja_el_costo_del_proveedor(pg_engine):
    """`proveedor_productos.producto_id` es SET NULL: borrar un producto no borra el costo."""
    with Session(pg_engine) as s:
        e = Empresa(
            nombre=f"{_PREFIJO}E", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True
        )
        s.add(e)
        s.flush()
        prov = Proveedor(empresa_id=e.id, nombre="P", rubros=[], despacha=False, activo=True)
        s.add(prov)
        p = cat.crear_producto(s, nombre=f"{_PREFIJO}Chía", unidad_base="kg")
        s.flush()
        pp = ProveedorProducto(
            proveedor_id=prov.id, producto_id=p.id, formato_id=p.formatos[0].id,
            producto_texto="Chía", precio_neto_actual=5000, vigente_desde=date(2026, 11, 1),
        )  # fmt: skip
        s.add(pp)
        s.commit()
        s.execute(delete(ProductoCanonico).where(ProductoCanonico.id == p.id))
        s.commit()
        s.refresh(pp)
        assert (pp.producto_id, pp.formato_id) == (None, None)
        assert pp.precio_neto_actual == 5000


def test_procesar_catalogo_sobre_postgres(pg_engine):
    with Session(pg_engine) as s:
        s.add(CompraAgil(codigo="ZZ1-3", nombre="x", region=16, monto_disponible_clp=100000))
        s.flush()
        s.add(CaProducto(ca_codigo="ZZ1-3", codigo_producto="50181901", nombre="Marraqueta",
                         descripcion="marraqueta", cantidad=40, unidad="Kilogramo"))  # fmt: skip
        s.commit()
        r1 = cat.procesar_catalogo(s, [16], inicial=True)
        r2 = cat.procesar_catalogo(s, [16], inicial=True)
        assert r1["lineas_nuevas"] >= 1 and r2["lineas_nuevas"] == 0
        (ln,) = s.execute(select(CaLinea).where(CaLinea.ca_codigo == "ZZ1-3")).scalars()
        assert ln.estado == "dudosa" and ln.cantidad_base == 40
        # El reproceso no mueve `actualizado_en` de la CA por la bandera.
        ca = s.get(CompraAgil, "ZZ1-3")
        antes = ca.actualizado_en
        cat.procesar_catalogo(s, [16], inicial=True)
        s.refresh(ca)
        assert ca.actualizado_en == antes and ca.detalle_lineas == "incompleto"


def _url_de(nombre_db: str) -> str:
    return (
        make_url(normalizar_url_driver(_DB_URL))
        .set(database=nombre_db)
        .render_as_string(hide_password=False)
    )


def test_migracion_con_proveedor_productos_existente():
    """Sube a la revisión anterior, carga un producto con `producto_id` suelto (sin FK) y
    migra a head: no falla, el id suelto queda en NULL y la FK existe; el downgrade vuelve."""
    from alembic.config import Config

    from alembic import command

    nombre_db = f"mp_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(normalizar_url_driver(_DB_URL), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{nombre_db}"'))
    url = _url_de(nombre_db)
    previo = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    cfg = Config(str(pg_url.RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(pg_url.RAIZ / "alembic"))
    e = create_engine(url)
    try:
        command.upgrade(cfg, _REV_ANTERIOR)
        with e.begin() as c:
            c.execute(
                text(
                    "INSERT INTO empresas (nombre, regiones_prioritarias, comunas_prioritarias, activa, creado_en, actualizado_en) VALUES ('E', '[16]', '[]', true, now(), now())"
                )
            )
            c.execute(
                text(
                    "INSERT INTO proveedores (empresa_id, nombre, rubros, despacha, activo, creado_en, actualizado_en) VALUES (1, 'P', '[]', false, true, now(), now())"
                )
            )
            c.execute(
                text(
                    "INSERT INTO proveedor_productos (proveedor_id, producto_id, producto_texto, formato_texto, precio_neto_actual, vigente_desde, fuente, disponible, creado_en, actualizado_en) VALUES (1, 5, 'Leche', 'caja', 890, '2026-11-01', 'manual', true, now(), now())"
                )
            )
            c.execute(
                text(
                    "INSERT INTO compras_agiles (codigo, nombre, descripcion, estado, total_ofertas, detalle_fallos, creado_en, actualizado_en) VALUES ('ZZ1-M', 'x', '', 'DESCONOCIDO', 0, 0, now(), now())"
                )
            )
        command.upgrade(cfg, "head")
        with e.connect() as c:
            fila = c.execute(
                text("SELECT producto_id, formato_id, precio_neto_actual FROM proveedor_productos")
            ).one()
            assert fila == (None, None, 890.0)  # el costo se conserva
            assert (
                c.execute(text("SELECT detalle_lineas FROM compras_agiles")).scalar_one()
                == "sin_detalle"
            )
        insp = inspect(e)
        fks = {fk["referred_table"] for fk in insp.get_foreign_keys("proveedor_productos")}
        assert {"producto_canonico", "producto_formato", "proveedores"} <= fks
        assert {"producto_canonico", "producto_formato", "producto_alias", "ca_lineas"} <= set(
            insp.get_table_names()
        )
        command.downgrade(cfg, _REV_ANTERIOR)
        assert "ca_lineas" not in inspect(e).get_table_names()
        command.upgrade(cfg, "head")  # reversible e idempotente al volver a subir
    finally:
        e.dispose()
        if previo is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previo
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{nombre_db}" WITH (FORCE)'))
        admin.dispose()
