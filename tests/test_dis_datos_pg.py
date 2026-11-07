"""F-dis-datos sobre Postgres con la migración aplicada: el SQL que SQLite no ejercita igual
(`datos` JSONB de `ca_eventos`, `COUNT(*) FILTER`, la `UNION` dentro del `IN` de novedades) y el
número fijo de consultas de `senales_por_ca`. La lógica está en test_dis_datos.py (SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, event
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.core.tiempo import ahora_utc
from app.mercado import equipo as eq
from app.mercado import hoy
from app.mercado.panel_ca import panel
from app.mercado.proveedores import guardar_producto
from app.models.enums import RolUsuario
from app.models.tables import (
    CaEvento,
    CaLinea,
    CaTrabajo,
    CompraAgil,
    Empresa,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    Usuario,
)
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZDIS "
_CA = "ZZDIS-"
_EMAIL = "zzdis-"
_REGION = 99  # región inexistente: no se cruza con datos de otros tests


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(delete(Usuario).where(Usuario.email.like(f"{_EMAIL}%")))
        s.execute(delete(Empresa).where(Empresa.nombre.like(f"{_PREFIJO}%")))
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like(f"{_CA}%")))
        s.execute(delete(ProductoCanonico).where(ProductoCanonico.nombre.like(f"{_PREFIJO}%")))
        s.commit()


def _mundo(e, ahora: datetime) -> dict[str, int]:
    with Session(e) as s:
        emp = Empresa(nombre=f"{_PREFIJO}emp", regiones_prioritarias=[_REGION],
                      comunas_prioritarias=[], activa=True)  # fmt: skip
        s.add(emp)
        s.flush()
        u1 = Usuario(email=f"{_EMAIL}a@t.cl", nombre="A", password_hash="x", empresa_id=emp.id,
                     rol=RolUsuario.ADMIN)  # fmt: skip
        u2 = Usuario(email=f"{_EMAIL}b@t.cl", nombre="B", password_hash="x", empresa_id=emp.id,
                     rol=RolUsuario.USUARIO)  # fmt: skip
        s.add_all([u1, u2])
        arroz = ProductoCanonico(nombre=f"{_PREFIJO}arroz", unidad_base="kg", activo=True)
        s.add(arroz)
        s.flush()
        kg = ProductoFormato(producto_id=arroz.id, nombre="kg", factor_base=1.0)
        s.add(kg)
        prov = Proveedor(empresa_id=emp.id, nombre=f"{_PREFIJO}prov", rubros=[], activo=True)
        s.add(prov)
        s.flush()
        hoy_c = hoy.hoy_en_chile(ahora)
        guardar_producto(
            s, prov, producto_texto="arroz", formato_texto="kg", precio_neto=1000,
            vigente_desde=hoy_c - timedelta(days=30), factor_base=1.0, unidad_base="kg",
            producto_id=arroz.id, formato_id=kg.id,
        )  # fmt: skip
        for i in range(6):
            codigo = f"{_CA}{i}"
            s.add(CompraAgil(codigo=codigo, nombre=f"CA {i}", estado="publicada", region=_REGION,
                             fecha_cierre=ahora + timedelta(hours=3 + 24 * i),
                             fecha_publicacion=ahora - timedelta(hours=1),
                             monto_disponible_clp=100_000, n_documentos=1))  # fmt: skip
            s.flush()
            s.add(CaLinea(ca_codigo=codigo, n=1, producto_id=arroz.id, cantidad=5, cantidad_base=5,
                          texto_origen="arroz", estado="ok"))  # fmt: skip
            s.add(CaLinea(ca_codigo=codigo, n=2, cantidad=1, cantidad_base=1,
                          texto_origen="otro", estado="pendiente"))  # fmt: skip
            s.add(CaTrabajo(empresa_id=emp.id, ca_codigo=codigo, estado=eq.COTIZANDO, version=1))
        s.add(CaEvento(empresa_id=emp.id, ca_codigo=f"{_CA}0", usuario_id=u2.id, tipo=eq.EV_ESTADO,
                       datos={"de": "lista", "a": "enviada"},
                       creado_en=ahora - timedelta(minutes=30)))  # fmt: skip
        s.commit()
        return {"emp": emp.id, "u1": u1.id, "u2": u2.id}


def test_datos_de_hoy_en_postgres(pg_engine):
    ahora = ahora_utc()
    ids = _mundo(pg_engine, ahora)
    with Session(pg_engine) as s:
        ps = hoy.te_necesita(s, ids["emp"], ids["u1"], [_REGION], ahora)
        tipos = hoy.conteo_por_tipo(ps)
        assert tipos["cierres"] == 1 and tipos["precios"] == 1 and tipos["lineas"] == 1
        c = {x.clave: x for x in hoy.cifras(s, ids["emp"], [_REGION], "mes", ahora)}
        assert c["enviadas"].valor == 1 and c["cierran"].valor >= 1
        e = {x.clave: x for x in hoy.embudo(s, ids["emp"], ahora)}
        assert e["cotizando"].n == 6 and e["enviada"].n == 1
        nov = hoy.novedades(s, ids["emp"], ids["u1"], ahora - timedelta(hours=2), ahora)
        textos = [n.texto for n in nov]
        assert "6 CA nuevas que calzan" in textos and "B envió 1 oferta" in textos
        p = panel(s, ids["emp"], f"{_CA}0", ahora)
        assert p is not None and p.lineas[0].marca == "vencido" and p.lineas[0].dias_vencido == 30


def test_senales_con_numero_fijo_de_consultas_en_postgres(pg_engine):
    ahora = ahora_utc()
    ids = _mundo(pg_engine, ahora)
    contador: list[str] = []

    def _contar(conn, cursor, statement, *args):
        contador.append(statement)

    def _medir(codigos: list[str]) -> int:
        contador.clear()
        with Session(pg_engine) as s:
            event.listen(pg_engine, "before_cursor_execute", _contar)
            try:
                r = hoy.senales_por_ca(s, ids["emp"], codigos, ahora)
            finally:
                event.remove(pg_engine, "before_cursor_execute", _contar)
        assert all(any(x.tipo == "precio_vencido" for x in v) for v in r.values())
        return len(contador)

    assert _medir([f"{_CA}0"]) == _medir([f"{_CA}{i}" for i in range(6)])
