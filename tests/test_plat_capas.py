"""F-plat-capas: catálogo en tres capas (Demanda MP, Catálogo Proveedores, Inventario).

Todo offline (SQLite en memoria). `UNIQUE`, `numeric` e `ILIKE` sobre Postgres: test_plat_capas_pg.py.
"""

from __future__ import annotations

import io
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.presentacion import formato_cantidad
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.catalogos.canonico import sugerir_vinculo
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.mercado import catalogo as cat
from app.mercado import importar, vinculo
from app.mercado import oferta as of
from app.mercado import oferta_propia as op
from app.mercado.proveedores import guardar_producto, hoy_chile, mejor_costo
from app.models.base import Base
from app.models.tables import (
    CaLinea,
    CompraAgil,
    CostoHistorial,
    Empresa,
    EmpresaProducto,
    EmpresaProductoMovimiento,
    OfertaPropia,
    ProductoAlias,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    ProveedorProducto,
    Usuario,
)

_PW = "contraseña-segura-test"
# F-plat-lote: un precio con más de 15 días es «vencido» y ya no gana el mejor costo. Las fechas
# del costo son relativas a hoy (antes eran fijas y el test se pudría con el calendario).
_HACE_10 = hoy_chile() - timedelta(days=10)
_HACE_2 = hoy_chile() - timedelta(days=2)


@pytest.fixture()
def engine():
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(e)
    yield e


@pytest.fixture()
def settings():
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url="sqlite:///:memory:",
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
    )


@pytest.fixture()
def client(engine, settings):
    return TestClient(create_app(settings, engine))


def _auth(settings, uid):
    token = create_session_token(settings.secret_key, uid)
    nonce = decode_session_token(settings.secret_key, token)[1]
    csrf = generate_csrf_token(settings.secret_key, nonce)
    return {COOKIE_NAME: token}, {"X-CSRF-Token": csrf, "HX-Request": "true"}, csrf


def _producto(s, nombre, base, formatos=(), alias=()):
    p = ProductoCanonico(nombre=nombre, unidad_base=base, activo=True)
    s.add(p)
    s.flush()
    nombres = {"kg": "kilo", "L": "litro", "u": "unidad"}
    fb = ProductoFormato(producto_id=p.id, nombre=nombres[base], factor_base=1.0)
    s.add(fb)
    fids = {"base": fb}
    for nom, factor in formatos:
        f = ProductoFormato(producto_id=p.id, nombre=nom, factor_base=factor)
        s.add(f)
        fids[nom] = f
    s.flush()
    for a in alias:
        s.add(ProductoAlias(texto_normalizado=a, producto_id=p.id, fuente="humano", confianza=1.0))
    s.flush()
    return p, fids


@pytest.fixture()
def mundo(engine):
    """Catálogo (arroz, bebida, fideos), dos empresas con proveedores, usuarios y la CA-1."""
    with Session(engine) as s:
        arroz, fa = _producto(s, "Arroz grado 1", "kg", [("saco 25 kg", 25.0)], ["arroz"])
        bebida, fb = _producto(s, "Bebida cola", "L", [("pack 6 x 350 ml", 2.1)])
        fideos, ff = _producto(s, "Fideos", "kg", alias=["fideos"])
        nuestra = Empresa(nombre="Nuestra", rut="76.123.456-0", regiones_prioritarias=[16],
                          comunas_prioritarias=[], activa=True)  # fmt: skip
        otra = Empresa(nombre="Otra", rut="11.111.111-1", regiones_prioritarias=[16],
                       comunas_prioritarias=[], activa=True)  # fmt: skip
        s.add_all([nuestra, otra])
        s.flush()
        prov = Proveedor(empresa_id=nuestra.id, nombre="Mayorista", rut="76111111-1", rubros=[],
                         activo=True)  # fmt: skip
        prov_otra = Proveedor(empresa_id=otra.id, nombre="Del otro", rubros=[], activo=True)
        s.add_all([prov, prov_otra])
        users = {}
        for clave, email, emp in (("boris", "boris@t.cl", nuestra.id), ("ana", "ana@t.cl", otra.id)):
            u = Usuario(email=email, nombre=clave, password_hash=hash_password(_PW), empresa_id=emp)
            s.add(u)
            s.flush()
            users[clave] = u.id
        s.add(CompraAgil(codigo="CA-1", nombre="Víveres", estado="publicada", region=16,
                         fecha_cierre=ahora_utc() + timedelta(days=2), detalle_lineas="completo"))  # fmt: skip
        s.flush()
        lineas = (
            (1, arroz.id, fa["saco 25 kg"].id, 20.0, 20.0, "ok", "Arroz grado 1 20 kg"),
            (2, bebida.id, fb["pack 6 x 350 ml"].id, 4.0, 8.4, "ok", "Bebida cola pack"),
            (3, fideos.id, None, 5.0, 5.0, "dudosa", "Fideos 5 kg"),
            (4, None, None, 3.0, None, "pendiente", "Cosa rara"),
        )
        for n, pid, fid, cant, cb, est, txt in lineas:
            s.add(CaLinea(ca_codigo="CA-1", n=n, producto_id=pid, formato_id=fid, cantidad=cant,
                          cantidad_base=cb, texto_origen=txt, estado=est))  # fmt: skip
        s.commit()
        return {
            "arroz": arroz.id, "arroz_saco": fa["saco 25 kg"].id, "arroz_base": fa["base"].id,
            "bebida": bebida.id, "bebida_pack": fb["pack 6 x 350 ml"].id,
            "bebida_base": fb["base"].id, "fideos": fideos.id, "fideos_base": ff["base"].id,
            "emp": nuestra.id, "otra": otra.id, "prov": prov.id, "prov_otra": prov_otra.id,
            **users,
        }  # fmt: skip


def _inv(s, mundo, producto, formato, stock=0.0, empresa=None, **kw):
    ep, _ = op.guardar(s, empresa or mundo["emp"], None, producto_id=mundo[producto],
                       formato_id=mundo[formato], stock=stock, **kw)  # fmt: skip
    return ep


# ===========================================================================
# §1.3 Vínculo de proveedor_productos al canónico
# ===========================================================================


def test_sugerir_vinculo_nombre_exacto_alias_y_ambiguedad(engine, mundo):
    with Session(engine) as s:
        c = cat.cargar_catalogo(s)
    assert sugerir_vinculo(c, "Bebida cola") == (mundo["bebida"], None)  # sin formato en el texto
    assert sugerir_vinculo(c, "Bebida cola", "litro") == (mundo["bebida"], mundo["bebida_base"])
    assert sugerir_vinculo(c, "bebida COLA", "pack 6 x 350 ml") == (
        mundo["bebida"], mundo["bebida_pack"]
    )
    # alias contenido: producto sí, formato 5 kg no existe → sin formato
    assert sugerir_vinculo(c, "Arroz extra 5 kg") == (mundo["arroz"], None)
    assert sugerir_vinculo(c, "arroz", "saco 25 kg") == (mundo["arroz"], mundo["arroz_saco"])
    # ambigua: el texto nombra dos productos → no vincula
    assert sugerir_vinculo(c, "Arroz con fideos") is None
    # sin ninguna coincidencia
    assert sugerir_vinculo(c, "Cuaderno universitario") is None


def test_sugerir_vinculo_nombres_con_numero_no_se_confunden(engine, mundo):
    with Session(engine) as s:
        _producto(s, "Arroz grado 2", "kg")
        s.commit()
        c = cat.cargar_catalogo(s)
        nombres = {p.id: p.nombre for p in c.productos.values()}
    s1 = sugerir_vinculo(c, "Arroz grado 2")
    assert s1 is not None and nombres[s1[0]] == "Arroz grado 2"


def test_guardar_producto_con_sugerencia_unica_vincula(engine, mundo):
    with Session(engine) as s:
        prov = s.get(Proveedor, mundo["prov"])
        c = cat.cargar_catalogo(s)
        pp, _ = guardar_producto(s, prov, producto_texto="Bebida cola", formato_texto="litro",
                                 precio_neto=900, factor_base=1, unidad_base="L", catalogo=c)  # fmt: skip
        assert (pp.producto_id, pp.formato_id) == (mundo["bebida"], mundo["bebida_base"])
        # `mejor_costo` por producto_id encuentra el producto recién vinculado.
        mc = mejor_costo(s, mundo["emp"], producto_id=mundo["bebida"])
        assert mc is not None and mc.ganador.producto_id == pp.id
        assert mc.ganador.precio_por_unidad_base == pytest.approx(900)


def test_guardar_producto_ambiguo_o_sin_catalogo_queda_null(engine, mundo):
    with Session(engine) as s:
        prov = s.get(Proveedor, mundo["prov"])
        c = cat.cargar_catalogo(s)
        amb, _ = guardar_producto(s, prov, producto_texto="Arroz con fideos", precio_neto=10,
                                  catalogo=c)  # fmt: skip
        keyword, _ = guardar_producto(s, prov, producto_texto="Leche entera semidescremada",
                                      precio_neto=10, catalogo=c)  # fmt: skip
        sin_cat, _ = guardar_producto(s, prov, producto_texto="Bebida cola", formato_texto="2 L",
                                      precio_neto=10)  # fmt: skip
        assert amb.producto_id is None and keyword.producto_id is None
        assert sin_cat.producto_id is None  # sin `catalogo` no hay vínculo automático
        assert mejor_costo(s, mundo["emp"], producto_id=mundo["bebida"]) is None


def test_guardar_producto_explicito_valida_formato_y_unidad(engine, mundo):
    with Session(engine) as s:
        prov = s.get(Proveedor, mundo["prov"])
        with pytest.raises(ValueError, match="no pertenece"):
            guardar_producto(s, prov, producto_texto="X", precio_neto=1,
                             producto_id=mundo["arroz"], formato_id=mundo["bebida_pack"])  # fmt: skip
        with pytest.raises(ValueError, match="no coincide"):
            guardar_producto(s, prov, producto_texto="X", precio_neto=1, factor_base=1,
                             unidad_base="kg", producto_id=mundo["bebida"])  # fmt: skip
        with pytest.raises(ValueError, match="requiere producto_id"):
            guardar_producto(s, prov, producto_texto="X", precio_neto=1,
                             formato_id=mundo["arroz_saco"])  # fmt: skip
        pp, _ = guardar_producto(s, prov, producto_texto="Arroz del sur", precio_neto=1,
                                 factor_base=25, unidad_base="kg", producto_id=mundo["arroz"],
                                 formato_id=mundo["arroz_saco"])  # fmt: skip
        assert (pp.producto_id, pp.formato_id) == (mundo["arroz"], mundo["arroz_saco"])


def test_autovinculo_no_pisa_un_vinculo_existente(engine, mundo):
    with Session(engine) as s:
        prov = s.get(Proveedor, mundo["prov"])
        c = cat.cargar_catalogo(s)
        pp, _ = guardar_producto(s, prov, producto_texto="Bebida cola", precio_neto=10,
                                 producto_id=mundo["fideos"])  # fmt: skip (elegido a mano)
        guardar_producto(s, prov, producto_texto="Bebida cola", precio_neto=11, catalogo=c)
        assert pp.producto_id == mundo["fideos"]


def test_vincular_pendientes_es_idempotente_y_por_empresa(engine, mundo):
    with Session(engine) as s:
        nuestro = s.get(Proveedor, mundo["prov"])
        ajeno = s.get(Proveedor, mundo["prov_otra"])
        a, _ = guardar_producto(s, nuestro, producto_texto="Bebida cola", precio_neto=10)
        b, _ = guardar_producto(s, nuestro, producto_texto="Cuaderno", precio_neto=10)
        c, _ = guardar_producto(s, ajeno, producto_texto="Bebida cola", precio_neto=10)
        assert vinculo.contar_sin_vincular(s, mundo["emp"]) == (2, 2)
        r = vinculo.vincular_pendientes(s, mundo["emp"])
        assert r == {"vinculados": 1, "sin_vincular": 1}
        assert a.producto_id == mundo["bebida"] and b.producto_id is None
        assert c.producto_id is None  # de otra empresa: no se toca
        assert vinculo.contar_sin_vincular(s, mundo["emp"], nuestro.id) == (1, 2)
        assert vinculo.vincular_pendientes(s, mundo["emp"]) == {"vinculados": 0, "sin_vincular": 1}


def test_importar_aplicar_vincula_y_cuenta(engine, mundo):
    planilla = importar.Planilla(precios=[
        importar.FilaPrecio(2, "Mayorista", None, None, None, None, "Bebida cola", "litro", 1.0,
                            "L", 900, date(2026, 11, 3)),
        importar.FilaPrecio(3, "Mayorista", None, None, None, None, "Cuaderno", "", None, None,
                            500, date(2026, 11, 3)),
    ])  # fmt: skip
    with Session(engine) as s:
        r = importar.aplicar(s, mundo["emp"], planilla)
        assert (r.productos_nuevos, r.productos_vinculados, r.sin_vincular) == (2, 1, 1)
        r2 = importar.aplicar(s, mundo["emp"], planilla)  # idempotente: ya vinculado
        assert (r2.productos_nuevos, r2.productos_vinculados, r2.sin_vincular) == (0, 0, 1)
        assert r2.sin_cambios == 2


# ===========================================================================
# Capa «Inventario»: CRUD, ownership y stock
# ===========================================================================


def test_guardar_crea_actualiza_y_es_idempotente(engine, mundo):
    with Session(engine) as s:
        ep, estado = op.guardar(s, mundo["emp"], None, producto_id=mundo["arroz"],
                                formato_id=mundo["arroz_saco"], stock=4, stock_minimo=2,
                                plazo=7, marca="Tucapel", proveedor_habitual_id=mundo["prov"])  # fmt: skip
        assert estado == "nuevo" and ep.stock == 4 and ep.disponible == 4 and ep.activo
        mov = s.execute(select(EmpresaProductoMovimiento)).scalars().all()
        assert [(m.delta, m.motivo) for m in mov] == [(4.0, "ajuste")]
        _, estado = op.guardar(s, mundo["emp"], None, producto_id=mundo["arroz"],
                               formato_id=mundo["arroz_saco"], stock=4, stock_minimo=2)  # fmt: skip
        assert estado == "igual"
        _, estado = op.guardar(s, mundo["emp"], None, producto_id=mundo["arroz"],
                               formato_id=mundo["arroz_saco"], stock=6, marca=None)  # fmt: skip
        assert estado == "actualizado" and ep.stock == 6 and ep.marca is None
        assert ep.stock_minimo == 2  # SIN = no se toca
        assert s.execute(select(func.count()).select_from(EmpresaProductoMovimiento)).scalar_one() == 2


def test_guardar_sin_formato_usa_el_base_y_valida_formato_ajeno(engine, mundo):
    with Session(engine) as s:
        ep, _ = op.guardar(s, mundo["emp"], None, producto_id=mundo["arroz"], formato_id=None)
        assert ep.formato_id == mundo["arroz_base"]
        with pytest.raises(op.ErrorInventario, match="no pertenece"):
            op.guardar(s, mundo["emp"], None, producto_id=mundo["arroz"],
                       formato_id=mundo["bebida_pack"])  # fmt: skip
        with pytest.raises(op.ErrorInventario):
            op.guardar(s, mundo["emp"], None, producto_id=99999, formato_id=None)
        with pytest.raises(op.ErrorInventario, match="Plazo"):
            op.guardar(s, mundo["emp"], None, producto_id=mundo["arroz"], formato_id=None,
                       plazo=9999)  # fmt: skip


def test_ownership_proveedor_y_producto_de_otra_empresa(engine, mundo):
    with Session(engine) as s:
        with pytest.raises(op.InventarioAjeno):
            op.guardar(s, mundo["emp"], None, producto_id=mundo["arroz"], formato_id=None,
                       proveedor_habitual_id=mundo["prov_otra"])  # fmt: skip
        s.rollback()
        ep = _inv(s, mundo, "arroz", "arroz_saco", 3, empresa=mundo["otra"])
        for f in (
            lambda: op.obtener(s, mundo["emp"], ep.id),
            lambda: op.ajustar_stock(s, mundo["emp"], ep.id, 1, "ajuste", None, None),
            lambda: op.desactivar(s, mundo["emp"], ep.id, None),
            lambda: op.vista_de(s, mundo["emp"], ep.id),
            lambda: op.movimientos(s, mundo["emp"], ep.id),
            lambda: op.actualizar(s, mundo["emp"], ep.id, None, stock_minimo=None, plazo=None,
                                  proveedor_habitual_id=None, marca=None, notas=None, activo=True),
        ):
            with pytest.raises(op.InventarioAjeno):
                f()
        assert ep.stock == 3
        assert op.listar(s, mundo["emp"]) == []


def test_ajustar_stock_nunca_negativo_y_movimiento_en_la_misma_transaccion(engine, mundo):
    with Session(engine) as s:
        ep = _inv(s, mundo, "arroz", "arroz_saco", 3)
        s.commit()
        op.ajustar_stock(s, mundo["emp"], ep.id, -2, "entrega", "CA-1", mundo["boris"])
        assert ep.stock == 1
        with pytest.raises(op.ErrorInventario, match="negativo"):
            op.ajustar_stock(s, mundo["emp"], ep.id, -1.5, "ajuste", None, None)
        assert ep.stock == 1
        s.rollback()  # el movimiento y el stock van en la misma transacción
        assert s.get(EmpresaProducto, ep.id).stock == 3
        movs = s.execute(select(EmpresaProductoMovimiento)).scalars().all()
        assert [(m.delta, m.motivo) for m in movs] == [(3.0, "ajuste")]
        # signos y motivos
        with pytest.raises(op.ErrorInventario):
            op.ajustar_stock(s, mundo["emp"], ep.id, -1, "compra", None, None)
        with pytest.raises(op.ErrorInventario):
            op.ajustar_stock(s, mundo["emp"], ep.id, 1, "entrega", None, None)
        with pytest.raises(op.ErrorInventario):
            op.ajustar_stock(s, mundo["emp"], ep.id, 0, "ajuste", None, None)
        with pytest.raises(op.ErrorInventario):
            op.ajustar_stock(s, mundo["emp"], ep.id, 1, "reserva", None, None)
        with pytest.raises(op.ErrorInventario):
            op.ajustar_stock(s, mundo["emp"], ep.id, float("nan"), "ajuste", None, None)
        op.ajustar_stock(s, mundo["emp"], ep.id, 2.5, "compra", "factura 10", mundo["boris"])
        assert ep.stock == 5.5


def test_desactivar_reactiva_al_volver_a_cargar_y_listar_filtra(engine, mundo):
    with Session(engine) as s:
        ep = _inv(s, mundo, "arroz", "arroz_saco", 3, marca="Tucapel")
        _inv(s, mundo, "bebida", "bebida_pack", 1)
        op.desactivar(s, mundo["emp"], ep.id, None)
        assert [v.producto.nombre for v in op.listar(s, mundo["emp"])] == ["Bebida cola"]
        assert len(op.listar(s, mundo["emp"], solo_activos=False)) == 2
        assert [v.ep.id for v in op.listar(s, mundo["emp"], solo_activos=False, texto="tucapel")] == [ep.id]
        # `%` y `_` del usuario no son comodines
        assert op.listar(s, mundo["emp"], solo_activos=False, texto="%") == []
        _, estado = op.guardar(s, mundo["emp"], None, producto_id=mundo["arroz"],
                               formato_id=mundo["arroz_saco"])  # fmt: skip
        assert ep.activo and estado == "actualizado"


def test_resumen_para_hoy_cuenta_bajo_minimo(engine, mundo):
    with Session(engine) as s:
        _inv(s, mundo, "arroz", "arroz_saco", 1, stock_minimo=2)
        _inv(s, mundo, "bebida", "bebida_pack", 5, stock_minimo=2)
        _inv(s, mundo, "fideos", "fideos_base", 0)
        assert op.resumen(s, mundo["emp"]) == (3, 1)
        assert op.resumen(s, mundo["otra"]) == (0, 0)


# ===========================================================================
# Cobertura de una CA
# ===========================================================================


def test_cobertura_convierte_formatos_a_la_unidad_base(engine, mundo):
    with Session(engine) as s:
        # 3 packs de 6 x 350 ml = 6,3 L; la línea de la CA pide 8,4 L (4 packs): no alcanza.
        _inv(s, mundo, "bebida", "bebida_pack", 3, plazo=5, proveedor_habitual_id=mundo["prov"])
        # 1 L suelto más: 7,3 L; sigue sin alcanzar.
        _inv(s, mundo, "bebida", "bebida_base", 1)
        # arroz: 1 saco de 25 kg cubre los 20 kg
        _inv(s, mundo, "arroz", "arroz_saco", 1)
        cob = op.cobertura_de_ca(s, mundo["emp"], "CA-1")
        lineas = {ln.n: ln.id for ln in s.execute(select(CaLinea)).scalars()}
        assert set(cob) == {lineas[1], lineas[2]}  # fideos no está en el inventario; la 4 no es ok
        c_arroz, c_bebida = cob[lineas[1]], cob[lineas[2]]
        assert c_arroz.en_stock and c_arroz.disponible == pytest.approx(25) and c_arroz.faltante == 0
        assert not c_bebida.en_stock and c_bebida.disponible == pytest.approx(7.3)
        assert c_bebida.faltante == pytest.approx(1.1)
        assert (c_bebida.plazo_dias, c_bebida.proveedor_habitual) == (5, "Mayorista")
        # otra empresa: nada
        assert op.cobertura_de_ca(s, mundo["otra"], "CA-1") == {}


def test_cobertura_descuenta_lo_reservado_y_ignora_inactivos(engine, mundo):
    with Session(engine) as s:
        ep = _inv(s, mundo, "arroz", "arroz_saco", 1)
        ep.reservado = 0.5  # 0,5 sacos = 12,5 kg disponibles
        s.flush()
        (c,) = [v for v in op.cobertura_de_ca(s, mundo["emp"], "CA-1").values()]
        assert not c.en_stock and c.disponible == pytest.approx(12.5)
        op.desactivar(s, mundo["emp"], ep.id, None)
        assert op.cobertura_de_ca(s, mundo["emp"], "CA-1") == {}


def test_resumen_por_ca_y_filtro_con_stock(engine, mundo):
    with Session(engine) as s:
        s.add(CompraAgil(codigo="CA-2", nombre="Otra", estado="publicada", region=16,
                         fecha_cierre=ahora_utc() + timedelta(days=3), detalle_lineas="completo"))  # fmt: skip
        s.add(CaLinea(ca_codigo="CA-2", n=1, producto_id=mundo["fideos"], cantidad=2,
                      cantidad_base=2, texto_origen="fideos", estado="ok"))  # fmt: skip
        s.flush()
        _inv(s, mundo, "arroz", "arroz_saco", 1)
        r = op.resumen_por_ca(s, mundo["emp"], ["CA-1", "CA-2", "CA-X"])
        assert set(r) == {"CA-1", "CA-2"}
        assert (r["CA-1"].total, r["CA-1"].en_stock) == (3, 1)  # arroz, bebida y fideos (dudosa)
        assert any("Bebida cola" in f and "8.4 L" in f for f in r["CA-1"].faltan)
        assert any(f.startswith("Fideos 5 kg") for f in r["CA-1"].faltan)
        assert (r["CA-2"].total, r["CA-2"].en_stock) == (1, 0)
        assert op.filtrar_con_stock(["CA-2", "CA-1", "CA-X"], r) == ["CA-1"]
        assert op.resumen_por_ca(s, mundo["otra"], ["CA-1"])["CA-1"].en_stock == 0


# ===========================================================================
# «Stock propio» en mi oferta
# ===========================================================================


def _costos(s, mundo):
    """Mayorista vende el arroz a 25 kg a $20.000 (800/kg); otro proveedor a $22.500 (900/kg)."""
    otro = Proveedor(empresa_id=mundo["emp"], nombre="Segundo", rubros=[], activo=True)
    s.add(otro)
    s.flush()
    pp1 = ProveedorProducto(proveedor_id=mundo["prov"], producto_id=mundo["arroz"],
                            formato_id=mundo["arroz_saco"], producto_texto="Arroz",
                            formato_texto="saco 25 kg", factor_base=25.0, unidad_base="kg",
                            precio_neto_actual=20000.0, vigente_desde=_HACE_10,
                            fuente="manual")  # fmt: skip
    pp2 = ProveedorProducto(proveedor_id=otro.id, producto_id=mundo["arroz"],
                            formato_id=mundo["arroz_saco"], producto_texto="Arroz",
                            formato_texto="saco 25 kg", factor_base=25.0, unidad_base="kg",
                            precio_neto_actual=22500.0, vigente_desde=_HACE_2,
                            fuente="manual")  # fmt: skip
    s.add_all([pp1, pp2])
    s.flush()
    s.add_all([
        CostoHistorial(proveedor_producto_id=pp1.id, precio_neto=20000, vigente_desde=_HACE_10, fuente="manual"),
        CostoHistorial(proveedor_producto_id=pp2.id, precio_neto=22500, vigente_desde=_HACE_2, fuente="manual"),
    ])  # fmt: skip
    s.flush()
    return pp1, pp2, otro


def test_costo_del_stock_propio_habitual_y_si_no_el_ultimo_historial(engine, mundo):
    with Session(engine) as s:
        pp1, pp2, otro = _costos(s, mundo)
        ep = _inv(s, mundo, "arroz", "arroz_saco", 2, proveedor_habitual_id=mundo["prov"])
        sp = op.stock_propio(s, mundo["emp"], mundo["arroz"], "kg", 20.0)
        assert sp is not None and sp.empresa_producto_id == ep.id
        assert sp.costo_por_base == pytest.approx(800) and sp.proveedor == "Mayorista"
        # sin habitual: el último costo_historial de CUALQUIER proveedor (el de 3-nov: 900/kg)
        ep.proveedor_habitual_id = None
        s.flush()
        sp = op.stock_propio(s, mundo["emp"], mundo["arroz"], "kg", 20.0)
        assert sp is not None and sp.costo_por_base == pytest.approx(900) and sp.proveedor == "Segundo"
        # sin ningún costo en la empresa: ofrece el stock pero sin costo
        sp = op.stock_propio(s, mundo["otra"], mundo["arroz"], "kg", 20.0)
        assert sp is None
        # no cubre → no se ofrece
        assert op.stock_propio(s, mundo["emp"], mundo["arroz"], "kg", 60.0) is None


def _ca_oferta(s, mundo, codigo="CA-1"):
    ca = s.get(CompraAgil, codigo)
    return ca, of.lineas_de(s, codigo)


def test_armar_ofrece_stock_propio_y_calcula_costo(engine, mundo):
    with Session(engine) as s:
        _costos(s, mundo)
        ep = _inv(s, mundo, "arroz", "arroz_saco", 2, proveedor_habitual_id=mundo["prov"])
        ca, lns = _ca_oferta(s, mundo)
        linea = next(ln for ln in lns if ln.n == 1)
        entradas = of.validar_entradas(s, mundo["emp"], "CA-1",
                                       {linea.id: ("1500", f"stock:{ep.id}")})  # fmt: skip
        assert entradas[linea.id].origen == "stock" and entradas[linea.id].proveedor_producto_id is None
        lo = next(x for x in of.armar(s, mundo["emp"], ca, None, None, entradas) if x.n == 1)
        assert lo.origen == "stock" and lo.proveedor == "Stock propio"
        assert lo.proveedor_producto_id is None and lo.empresa_producto_id == ep.id
        assert lo.costo_por_base == pytest.approx(800) and lo.costo_total == pytest.approx(16000)
        assert lo.stock is not None and lo.stock.disponible == pytest.approx(50)
        # sin elegir: «Mejor costo» como siempre (el más barato por kg), y el stock queda ofrecido
        lo2 = next(x for x in of.armar(s, mundo["emp"], ca, None, None) if x.n == 1)
        assert lo2.origen is None and lo2.proveedor == "Mayorista" and lo2.stock is not None


def test_validar_entradas_stock_de_otra_empresa_o_inexistente_es_ajeno(engine, mundo):
    with Session(engine) as s:
        ajeno = _inv(s, mundo, "arroz", "arroz_saco", 2, empresa=mundo["otra"])
        _, lns = _ca_oferta(s, mundo)
        lid = lns[0].id
        for valor in (f"stock:{ajeno.id}", "stock:99999"):
            with pytest.raises(of.ProveedorAjeno):
                of.validar_entradas(s, mundo["emp"], "CA-1", {lid: ("10", valor)})
        with pytest.raises(of.ErrorOferta):
            of.validar_entradas(s, mundo["emp"], "CA-1", {lid: ("10", "stock:abc")})


def test_stock_elegido_que_ya_no_alcanza_vuelve_al_mejor_costo(engine, mundo):
    with Session(engine) as s:
        _costos(s, mundo)
        ep = _inv(s, mundo, "arroz", "arroz_saco", 2)
        ca, lns = _ca_oferta(s, mundo)
        lid = next(ln.id for ln in lns if ln.n == 1)
        entradas = of.validar_entradas(s, mundo["emp"], "CA-1", {lid: ("1500", f"stock:{ep.id}")})
        op.ajustar_stock(s, mundo["emp"], ep.id, -2, "entrega", None, None)  # se acabó
        lo = next(x for x in of.armar(s, mundo["emp"], ca, None, None, entradas) if x.n == 1)
        assert lo.origen is None and lo.stock is None and lo.proveedor == "Mayorista"
        assert lo.aviso == of.AVISO_SIN_STOCK and lo.costo_por_base == pytest.approx(800)
        # también al re-armar desde lo guardado (`previas`)
        o = OfertaPropia(ca_codigo="CA-1", empresa_id=mundo["emp"], version=1, total_neto=0,
                         lineas=[{"linea_id": lid, "precio_unitario": 1500, "proveedor_producto_id": None,
                                  "origen": "stock", "empresa_producto_id": ep.id}])  # fmt: skip
        lo = next(x for x in of.armar(s, mundo["emp"], ca, None, o) if x.n == 1)
        assert lo.aviso == of.AVISO_SIN_STOCK and lo.proveedor == "Mayorista"
        sin_eleccion = next(x for x in of.armar(s, mundo["emp"], ca, None, None) if x.n == 1)
        assert sin_eleccion.aviso is None


def test_guardar_oferta_con_stock_propio_deja_el_origen_en_el_json(client, engine, settings, mundo):
    with Session(engine) as s:
        _costos(s, mundo)
        ep = _inv(s, mundo, "arroz", "arroz_saco", 2, proveedor_habitual_id=mundo["prov"])
        lns = of.lineas_de(s, "CA-1")
        lid = next(ln.id for ln in lns if ln.n == 1)
        s.commit()
        ep_id = ep.id
    cookies, headers, _ = _auth(settings, mundo["boris"])
    data = {f"precio_{lid}": "1500", f"prov_{lid}": f"stock:{ep_id}", "version": "0"}
    r = client.post("/mesa/ca/CA-1/simular", data=data, cookies=cookies, headers=headers)
    assert r.status_code == 200 and "Stock propio" not in r.text  # el simulador solo responde cifras
    r = client.post("/mesa/ca/CA-1/oferta", data=data, cookies=cookies, headers=headers)
    assert r.status_code == 200 and "Oferta guardada" in r.text
    assert f'value="stock:{ep_id}" selected' in r.text and "en stock:" in r.text
    with Session(engine) as s:
        o = s.execute(select(OfertaPropia)).scalar_one()
        fila = next(d for d in o.lineas if d["linea_id"] == lid)
        assert fila["origen"] == "stock" and fila["empresa_producto_id"] == ep_id
        assert fila["proveedor_producto_id"] is None
    # La exportación acepta la oferta con «Stock propio».
    r = client.post("/mesa/ca/CA-1/exportar", data={}, cookies=cookies, headers=headers)
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_ficha_precio_no_ofrece_stock_ajeno_y_forma_invalida_es_404(client, engine, settings, mundo):
    with Session(engine) as s:
        ajeno = _inv(s, mundo, "arroz", "arroz_saco", 2, empresa=mundo["otra"])
        lns = of.lineas_de(s, "CA-1")
        lid = lns[0].id
        s.commit()
        ajeno_id = ajeno.id
    cookies, headers, _ = _auth(settings, mundo["boris"])
    r = client.get("/mesa/ca/CA-1/precio", cookies=cookies, headers=headers)
    assert r.status_code == 200 and "Stock propio" not in r.text
    r = client.post("/mesa/ca/CA-1/simular", data={f"precio_{lid}": "10", f"prov_{lid}": f"stock:{ajeno_id}"},
                    cookies=cookies, headers=headers)  # fmt: skip
    assert r.status_code == 404


# ===========================================================================
# Fusión de productos del catálogo con inventario
# ===========================================================================


def test_fusionar_productos_junta_el_inventario_sin_chocar_con_el_unique(engine, mundo):
    with Session(engine) as s:
        otro, fo = _producto(s, "Arroz tipo 2", "kg", [("saco 25 kg", 25.0)])
        a = _inv(s, mundo, "arroz", "arroz_saco", 3)
        op.ajustar_stock(s, mundo["emp"], a.id, 1, "compra", None, None)
        b, _ = op.guardar(s, mundo["emp"], None, producto_id=otro.id, formato_id=fo["saco 25 kg"].id,
                          stock=2)  # fmt: skip
        solo, _ = op.guardar(s, mundo["emp"], None, producto_id=otro.id, formato_id=fo["base"].id,
                             stock=7)  # fmt: skip
        s.commit()
        cat.fusionar_productos(s, otro.id, mundo["arroz"])
        s.commit()
        filas = {(v.ep.formato_id): v.ep for v in op.listar(s, mundo["emp"])}
        assert filas[mundo["arroz_saco"]].stock == 6  # 4 + 2 sumados en una sola fila
        assert filas[mundo["arroz_saco"]].id == a.id
        assert s.get(EmpresaProducto, b.id) is None
        movs = s.execute(select(EmpresaProductoMovimiento).where(
            EmpresaProductoMovimiento.empresa_producto_id == a.id)).scalars().all()
        assert len(movs) == 3  # los dos de `a` (alta y compra) y el de `b`
        # el formato base del origen se unifica con el base del destino (mismo factor)
        assert filas[mundo["arroz_base"]].id == solo.id and filas[mundo["arroz_base"]].stock == 7
        assert all(ep.producto_id == mundo["arroz"] for ep in filas.values())


# ===========================================================================
# Importación de inventario
# ===========================================================================


def _xlsx(hojas: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for nombre, filas in hojas.items():
        ws = wb.create_sheet(nombre)
        for f in filas:
            ws.append(f)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ENCAB = ["producto", "formato", "stock", "stock_minimo", "plazo_reposicion_dias", "marca",
         "proveedor_habitual", "notas"]  # fmt: skip


def test_parsear_inventario_solo_xlsx_y_exige_la_hoja():
    with pytest.raises(importar.PlanillaInvalida, match="xlsx"):
        importar.parsear_inventario(b"producto,stock\nA,1", "inventario.csv")
    with pytest.raises(importar.PlanillaInvalida, match="inventario"):
        importar.parsear_inventario(_xlsx({"precios": [["proveedor"]]}), "x.xlsx")


def test_plantilla_de_inventario_se_descarga_y_se_lee():
    data = importar.generar_plantilla_inventario_xlsx()
    p = importar.parsear_inventario(data, "plantilla.xlsx")
    assert p.filas == [] and p.ejemplos == 1 and p.errores == []  # solo la fila EJEMPLO


def test_importar_inventario_una_fila_mala_no_rompe_el_lote(engine, mundo):
    data = _xlsx({"inventario": [
        ENCAB,
        ["Arroz grado 1", "saco 25 kg", 4, 2, 7, "Tucapel", "Mayorista", "se pide los lunes"],
        ["Bebida cola", "", "1.5", None, None, None, "no existe SA", None],
        ["Cosa que no existe", "", 3, None, None, None, None, None],
        ["Arroz grado 1", "bolsa mágica", 1, None, None, None, None, None],
        ["Fideos", "", -3, None, None, None, None, None],
        ["", "", 1, None, None, None, None, None],
        ["EJEMPLO Arroz (borrar)", "", 1, None, None, None, None, None],
    ]})
    p = importar.parsear_inventario(data, "inv.xlsx")
    assert len(p.filas) == 4 and p.ejemplos == 1
    assert any("stock debe ser" in e and "fila 6" in e for e in p.errores)
    assert any("falta el producto" in e for e in p.errores)
    with Session(engine) as s:
        r = importar.aplicar_inventario(s, mundo["emp"], p, mundo["boris"])
        assert (r.nuevos, r.actualizados, r.sin_cambios) == (2, 0, 0)
        assert any("Cosa que no existe" in e for e in r.errores)
        assert any("bolsa mágica" in e and "saco 25 kg" in e for e in r.errores)  # con las opciones
        assert any("no existe SA" in a for a in r.avisos)
        vista = {v.producto.nombre: v.ep for v in op.listar(s, mundo["emp"])}
        a = vista["Arroz grado 1"]
        assert (a.stock, a.stock_minimo, a.plazo_reposicion_dias, a.marca, a.proveedor_habitual_id) == (
            4, 2, 7, "Tucapel", mundo["prov"]
        )
        assert a.notas == "se pide los lunes" and a.formato_id == mundo["arroz_saco"]
        b = vista["Bebida cola"]
        assert b.stock == 1.5 and b.formato_id == mundo["bebida_base"]  # formato en blanco = base
        assert b.proveedor_habitual_id is None
        # idempotente: la misma planilla no cambia nada ni agrega movimientos
        antes = s.execute(select(func.count()).select_from(EmpresaProductoMovimiento)).scalar_one()
        r2 = importar.aplicar_inventario(s, mundo["emp"], p, mundo["boris"])
        assert (r2.nuevos, r2.actualizados, r2.sin_cambios) == (0, 0, 2)
        assert s.execute(select(func.count()).select_from(EmpresaProductoMovimiento)).scalar_one() == antes


def test_importar_inventario_stock_de_la_planilla_es_el_final(engine, mundo):
    with Session(engine) as s:
        ep = _inv(s, mundo, "arroz", "arroz_saco", 10)
        data = _xlsx({"inventario": [ENCAB, ["Arroz grado 1", "saco 25 kg", 4, None, None, None, None, None]]})
        r = importar.aplicar_inventario(s, mundo["emp"], importar.parsear_inventario(data, "i.xlsx"))
        assert r.actualizados == 1 and ep.stock == 4
        ultimo = s.execute(select(EmpresaProductoMovimiento).order_by(EmpresaProductoMovimiento.id.desc())).scalars().first()
        assert (ultimo.delta, ultimo.motivo, ultimo.referencia) == (-6.0, "ajuste", "planilla")


def test_parsear_precios_es_opcional_si_viene_inventario():
    data = _xlsx({"inventario": [ENCAB, ["Arroz grado 1", "", 1, None, None, None, None, None]]})
    p = importar.parsear(data, "libro.xlsx")
    assert p.precios == [] and any("inventario" in e for e in p.errores)
    with pytest.raises(importar.PlanillaInvalida, match="precios"):
        importar.parsear(_xlsx({"otra": [["a"]]}), "libro.xlsx")


def test_importar_inventario_ambiguo_da_error_de_fila(engine, mundo):
    with Session(engine) as s:
        data = _xlsx({"inventario": [ENCAB, ["Arroz con fideos", "", 1, None, None, None, None, None]]})
        r = importar.aplicar_inventario(s, mundo["emp"], importar.parsear_inventario(data, "i.xlsx"))
        assert r.nuevos == 0 and len(r.errores) == 1
        assert "Arroz grado 1" in r.errores[0]  # propone opciones del catálogo


# ===========================================================================
# Web: menú, páginas y ownership
# ===========================================================================


def _nav(html: str) -> str:
    return html.split("</aside>")[0]


def test_menu_tiene_las_tres_capas_bajo_el_encabezado_catalogo(client, settings, mundo):
    cookies, _, _ = _auth(settings, mundo["boris"])
    html = client.get("/cuenta", cookies=cookies).text
    nav = _nav(html)
    assert 'nav-lateral__seccion" id="nav-catalogo">Catálogo<' in nav
    pos = [nav.index(t) for t in ("Demanda MP", "Catálogo Proveedores", "Inventario", "Logística")]
    assert pos == sorted(pos)
    hrefs = set(re.findall(r'href="([^"]+)"', nav))
    assert {"/catalogo", "/proveedores", "/inventario", "/proveedores/logistica"} <= hrefs
    # la versión móvil también (offcanvas)
    assert 'id="nav-catalogo-m"' in html
    # y las entradas ya no se llaman «Proveedores» a secas
    assert ">Proveedores<" not in nav


def test_menu_marca_la_entrada_activa_y_titulos(client, settings, mundo):
    cookies, _, _ = _auth(settings, mundo["boris"])
    for ruta, titulo in (("/catalogo", "Demanda MP"), ("/proveedores", "Catálogo Proveedores"),
                         ("/inventario", "Inventario")):  # fmt: skip
        r = client.get(ruta, cookies=cookies)
        assert r.status_code == 200
        assert f"<title>{titulo} — Rastrillo</title>" in r.text and f"<h2 class=\"mb-0\">{titulo}</h2>" in r.text
        activa = re.findall(r'class="nav-lateral__item active" href="([^"]+)"', _nav(r.text))
        assert activa == [ruta]


def test_menu_sin_empresa_no_ofrece_las_capas(client, engine, settings):
    with Session(engine) as s:
        u = Usuario(email="x@t.cl", password_hash=hash_password(_PW), empresa_id=None)
        s.add(u)
        s.commit()
        uid = u.id
    cookies, _, _ = _auth(settings, uid)
    nav = _nav(client.get("/cuenta", cookies=cookies).text)
    assert "/inventario" not in nav and "Demanda MP" not in nav
    r = client.get("/inventario", cookies=cookies)
    assert r.status_code == 200 and "todavía no tiene una empresa" in r.text
    assert client.get("/inventario/plantilla.xlsx", cookies=cookies).status_code == 403


def test_inventario_lista_buscar_y_bajo_minimo(client, engine, settings, mundo):
    with Session(engine) as s:
        _inv(s, mundo, "arroz", "arroz_saco", 1, stock_minimo=3, marca="Tucapel")
        _inv(s, mundo, "bebida", "bebida_pack", 9)
        _inv(s, mundo, "fideos", "fideos_base", 1, empresa=mundo["otra"])
        s.commit()
    cookies, _, _ = _auth(settings, mundo["boris"])
    r = client.get("/inventario", cookies=cookies)
    assert "Arroz grado 1" in r.text and "Bebida cola" in r.text and "Fideos" not in r.text
    assert "bajo mínimo" in r.text and 'data-inventario="bajo-minimo"' in r.text
    r = client.get("/inventario?q=tucap", cookies=cookies)
    assert "Arroz grado 1" in r.text and "Bebida cola" not in r.text
    assert "Fuente: Dirección ChileCompra" in r.text


def test_inventario_crud_web_con_csrf_y_movimientos(client, engine, settings, mundo):
    cookies, headers, csrf = _auth(settings, mundo["boris"])
    form = {"producto_id": str(mundo["arroz"]), "formato_id": str(mundo["arroz_saco"]),
            "stock": "4", "stock_minimo": "2", "plazo": "7", "proveedor_id": str(mundo["prov"]),
            "marca": "Tucapel", "notas": "", "csrf_token": csrf}  # fmt: skip
    # sin CSRF → 403
    assert client.post("/inventario", data={**form, "csrf_token": ""}, cookies=cookies).status_code == 403
    r = client.post("/inventario", data=form, cookies=cookies, headers=headers)
    assert r.status_code == 200 and r.headers["HX-Redirect"].startswith("/inventario?mensaje=")
    with Session(engine) as s:
        ep = s.execute(select(EmpresaProducto)).scalar_one()
        assert (ep.empresa_id, ep.stock, ep.plazo_reposicion_dias) == (mundo["emp"], 4, 7)
        ep_id = ep.id
    # repetido → 422 con aviso
    r = client.post("/inventario", data=form, cookies=cookies, headers=headers)
    assert r.status_code == 422 and "ya tienes" in r.text.lower()
    # número inválido → 422
    r = client.post("/inventario", data={**form, "stock": "abc"}, cookies=cookies, headers=headers)
    assert r.status_code == 422 and "No es un número" in r.text
    # sin producto → 422
    r = client.post("/inventario", data={**form, "producto_id": ""}, cookies=cookies, headers=headers)
    assert r.status_code == 422 and "Elige un producto" in r.text
    # editar en línea
    assert client.get(f"/inventario/{ep_id}/form", cookies=cookies).status_code == 200
    r = client.post(f"/inventario/{ep_id}", data={"csrf_token": csrf, "stock_minimo": "5", "plazo": "3",
                    "proveedor_id": "", "marca": "Otra", "notas": "n", "activo": "1"}, cookies=cookies, headers=headers)  # fmt: skip
    assert r.status_code == 200 and "Otra" in r.text and f'id="ep-{ep_id}"' in r.text
    r = client.post(f"/inventario/{ep_id}", data={"csrf_token": csrf, "stock_minimo": "x"},
                    cookies=cookies, headers=headers)  # fmt: skip
    assert r.status_code == 422
    # ajustar stock
    assert client.get(f"/inventario/{ep_id}/ajuste", cookies=cookies).status_code == 200
    r = client.post(f"/inventario/{ep_id}/stock", data={"csrf_token": csrf, "delta": "-1,5", "motivo": "entrega",
                    "referencia": "CA-1"}, cookies=cookies, headers=headers)  # fmt: skip
    assert r.status_code == 200
    r = client.post(f"/inventario/{ep_id}/stock", data={"csrf_token": csrf, "delta": "-99", "motivo": "ajuste"},
                    cookies=cookies, headers=headers)  # fmt: skip
    assert r.status_code == 422 and "negativo" in r.text
    r = client.get(f"/inventario/{ep_id}/movimientos", cookies=cookies)
    assert "Entrega (sale)" in r.text and "CA-1" in r.text and "-1,5" in r.text
    with Session(engine) as s:
        ep = s.get(EmpresaProducto, ep_id)
        assert ep.stock == 2.5 and ep.marca == "Otra"
    # inventario sin CSRF en las demás mutaciones
    for ruta in (f"/inventario/{ep_id}", f"/inventario/{ep_id}/stock"):
        assert client.post(ruta, data={"delta": "1"}, cookies=cookies).status_code == 403


def test_inventario_otra_empresa_es_404_en_todas_las_rutas(client, engine, settings, mundo):
    with Session(engine) as s:
        ep = _inv(s, mundo, "arroz", "arroz_saco", 5, empresa=mundo["otra"])
        s.commit()
        ep_id = ep.id
    cookies, headers, csrf = _auth(settings, mundo["boris"])
    for ruta in (f"/inventario/{ep_id}/fila", f"/inventario/{ep_id}/form",
                 f"/inventario/{ep_id}/ajuste", f"/inventario/{ep_id}/movimientos"):  # fmt: skip
        assert client.get(ruta, cookies=cookies).status_code == 404, ruta
    assert client.post(f"/inventario/{ep_id}", data={"csrf_token": csrf, "stock_minimo": "1"},
                       cookies=cookies, headers=headers).status_code == 404  # fmt: skip
    assert client.post(f"/inventario/{ep_id}/stock", data={"csrf_token": csrf, "delta": "1", "motivo": "ajuste"},
                       cookies=cookies, headers=headers).status_code == 404  # fmt: skip
    # proveedor habitual de otra empresa al crear → 404
    r = client.post("/inventario", data={"csrf_token": csrf, "producto_id": str(mundo["fideos"]),
                    "proveedor_id": str(mundo["prov_otra"])}, cookies=cookies, headers=headers)  # fmt: skip
    assert r.status_code == 404
    with Session(engine) as s:
        assert s.get(EmpresaProducto, ep_id).stock == 5
        assert s.execute(select(func.count()).select_from(EmpresaProducto)).scalar_one() == 1


def test_inventario_plantilla_e_importacion_web(client, engine, settings, mundo):
    cookies, headers, csrf = _auth(settings, mundo["boris"])
    r = client.get("/inventario/plantilla.xlsx", cookies=cookies)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert "inventario_plantilla.xlsx" in r.headers["content-disposition"]
    assert client.get("/inventario/importar", cookies=cookies).status_code == 200
    data = _xlsx({"inventario": [ENCAB, ["Arroz grado 1", "saco 25 kg", 4, 2, None, None, None, None],
                                 ["Nada", "", 1, None, None, None, None, None]]})
    r = client.post("/inventario/importar", data={"csrf_token": csrf}, cookies=cookies,
                    files={"archivo": ("inv.xlsx", data, "application/octet-stream")})  # fmt: skip
    assert r.status_code == 200 and "Vista previa" in r.text and "no pude identificar" in r.text
    with Session(engine) as s:  # la vista previa no guarda nada
        assert s.execute(select(func.count()).select_from(EmpresaProducto)).scalar_one() == 0
    b64 = re.search(r'name="archivo_b64" value="([^"]+)"', r.text).group(1)
    r = client.post("/inventario/importar/confirmar", data={"csrf_token": csrf, "nombre_archivo": "inv.xlsx",
                    "archivo_b64": b64}, cookies=cookies)  # fmt: skip
    assert r.status_code == 200 and "Importación aplicada" in r.text
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(EmpresaProducto)).scalar_one() == 1
    r = client.post("/inventario/importar", data={"csrf_token": csrf}, cookies=cookies,
                    files={"archivo": ("inv.csv", b"a,b", "text/csv")})  # fmt: skip
    assert r.status_code == 422 and "xlsx" in r.text
    assert client.post("/inventario/importar", data={"csrf_token": "mal"}, cookies=cookies,
                       files={"archivo": ("inv.xlsx", data, "x")}).status_code == 403  # fmt: skip


def test_hoy_muestra_la_tarjeta_de_inventario(client, engine, settings, mundo):
    with Session(engine) as s:
        _inv(s, mundo, "arroz", "arroz_saco", 1, stock_minimo=3)
        _inv(s, mundo, "bebida", "bebida_pack", 9)
        s.commit()
    cookies, _, _ = _auth(settings, mundo["boris"])
    r = client.get("/", cookies=cookies)
    assert 'data-hoy="inventario">2 ' in r.text and 'data-hoy="bajo-minimo">1<' in r.text
    assert 'href="/inventario"' in r.text


# --- Mesa ---------------------------------------------------------------------------


def test_mesa_muestra_cobertura_y_filtra_con_stock(client, engine, settings, mundo):
    with Session(engine) as s:
        s.add(CompraAgil(codigo="CA-2", nombre="Sin nada nuestro", estado="publicada", region=16,
                         fecha_cierre=ahora_utc() + timedelta(days=3), detalle_lineas="completo"))  # fmt: skip
        s.add(CaLinea(ca_codigo="CA-2", n=1, producto_id=mundo["fideos"], cantidad=2,
                      cantidad_base=2, texto_origen="fideos", estado="ok"))  # fmt: skip
        _inv(s, mundo, "arroz", "arroz_saco", 1)
        s.commit()
    cookies, _, _ = _auth(settings, mundo["boris"])
    r = client.get("/mesa", cookies=cookies)
    assert r.status_code == 200
    assert "Tengo: 1 de 3 líneas en stock" in r.text and "falta:" in r.text and "Bebida cola" in r.text
    assert "Tengo: 0 de 1 línea en stock" in r.text
    assert 'name="con_stock"' in r.text
    r = client.get("/mesa?con_stock=1", cookies=cookies)
    assert "Víveres" in r.text and "Sin nada nuestro" not in r.text
    assert "con_stock=1" not in client.get("/mesa", cookies=cookies).text
    r = client.get("/mesa/conteo?con_stock=1", cookies=cookies)
    assert r.status_code == 200 and ">1<" in r.text.replace(" ", "")
    # la otra empresa no tiene inventario: ni línea ni resultados con el filtro
    ca, _, _ = _auth(settings, mundo["ana"])
    r = client.get("/mesa", cookies=ca)
    assert 'data-mesa="stock"' not in r.text
    assert "No hay Compras Ágiles vigentes" in client.get("/mesa?con_stock=1", cookies=ca).text


def test_mesa_calcula_la_cobertura_por_lote_y_no_por_tarjeta(client, engine, settings, mundo):
    from sqlalchemy import event

    with Session(engine) as s:
        for i in range(2, 8):
            s.add(CompraAgil(codigo=f"CA-{i}", nombre=f"CA {i}", estado="publicada", region=16,
                             fecha_cierre=ahora_utc() + timedelta(days=3), detalle_lineas="completo"))  # fmt: skip
            s.add(CaLinea(ca_codigo=f"CA-{i}", n=1, producto_id=mundo["fideos"], cantidad=2,
                          cantidad_base=2, texto_origen="fideos", estado="ok"))  # fmt: skip
        _inv(s, mundo, "arroz", "arroz_saco", 1)
        s.commit()
    cookies, _, _ = _auth(settings, mundo["boris"])
    consultas: list[str] = []
    event.listen(engine, "before_cursor_execute",
                 lambda c, cur, stmt, p, ctx, many: consultas.append(stmt))  # fmt: skip
    client.get("/mesa", cookies=cookies)
    sobre_inventario = [q for q in consultas if "FROM empresa_productos" in q]
    sobre_lineas = [q for q in consultas if "FROM ca_lineas" in q and "producto_id IS NOT NULL" in q]
    assert len(sobre_inventario) <= 2 and len(sobre_lineas) <= 2  # 8 tarjetas, no 8 consultas


# --- Proveedores: vínculo al canónico en el formulario, la ficha y la acción -------------


def test_selector_generalizado_con_destino(client, settings, mundo):
    cookies, _, _ = _auth(settings, mundo["boris"])
    r = client.get("/catalogo/buscar-producto?destino=prod-1-nuevo&opcional=1&q=arroz", cookies=cookies)
    assert r.status_code == 200
    assert 'id="fmt-prod-1-nuevo"' in r.text and "/catalogo/formatos?destino=prod-1-nuevo" in r.text
    assert "Sin vincular" in r.text and "Arroz grado 1" in r.text and " required" not in r.text
    # sin texto y opcional: no preselecciona el primero
    r = client.get("/catalogo/buscar-producto?destino=x&opcional=1", cookies=cookies)
    assert "selected" not in r.text.split("<select")[1].split("</select>")[0]
    # compatibilidad: con linea_id sigue igual
    r = client.get("/catalogo/buscar-producto?linea_id=7&q=arroz", cookies=cookies)
    assert 'id="fmt-7"' in r.text and "linea_id=7" in r.text and " required" in r.text
    r = client.get(f"/catalogo/formatos?destino=abc&producto_id={mundo['arroz']}", cookies=cookies)
    assert r.status_code == 200 and "saco 25 kg" in r.text
    for malo in ('"><script>', "a b", "x" * 41):
        assert client.get("/catalogo/buscar-producto", params={"destino": malo}, cookies=cookies).status_code == 422


def test_formulario_de_producto_del_proveedor_vincula_a_mano_y_solo(client, engine, settings, mundo):
    cookies, headers, csrf = _auth(settings, mundo["boris"])
    r = client.get(f"/proveedores/{mundo['prov']}/productos/form", cookies=cookies)
    assert r.status_code == 200 and "Producto del catálogo (Demanda MP)" in r.text
    assert f"prod-{mundo['prov']}-nuevo" in r.text and "Sin vincular" in r.text
    base = {"csrf_token": csrf, "precio_neto": "900", "vigente_desde": "2026-11-03"}
    url = f"/proveedores/{mundo['prov']}/productos"
    # elegido a mano
    r = client.post(url, data={**base, "producto_texto": "Cosa", "factor_base": "25", "unidad_base": "kg",
                    "producto_id": str(mundo["arroz"]), "formato_id": str(mundo["arroz_saco"])},
                    cookies=cookies, headers=headers)  # fmt: skip
    assert r.status_code == 200 and r.headers["HX-Redirect"] == f"/proveedores/{mundo['prov']}"
    # sin elegir: sugerencia única por el nombre
    r = client.post(url, data={**base, "producto_texto": "Bebida cola", "formato_texto": "litro"},
                    cookies=cookies, headers=headers)  # fmt: skip
    assert r.status_code == 200
    # sin elegir y sin coincidencia
    client.post(url, data={**base, "producto_texto": "Cuaderno"}, cookies=cookies, headers=headers)
    # un vínculo que no calza → 422 con el motivo, sin guardar
    r = client.post(url, data={**base, "producto_texto": "Malo", "producto_id": str(mundo["arroz"]),
                    "formato_id": str(mundo["bebida_pack"])}, cookies=cookies, headers=headers)  # fmt: skip
    assert r.status_code == 422 and "no pertenece" in r.text
    with Session(engine) as s:
        pps = {pp.producto_texto: pp for pp in s.execute(select(ProveedorProducto)).scalars()}
        assert set(pps) == {"Cosa", "Bebida cola", "Cuaderno"}
        assert (pps["Cosa"].producto_id, pps["Cosa"].formato_id) == (mundo["arroz"], mundo["arroz_saco"])
        assert pps["Bebida cola"].producto_id == mundo["bebida"]
        assert pps["Cuaderno"].producto_id is None
    # la ficha muestra la columna «Demanda MP» y el contador
    r = client.get(f"/proveedores/{mundo['prov']}", cookies=cookies)
    assert "Arroz grado 1" in r.text and 'data-sin-vincular="1"' in r.text and "Vincular pendientes (1)" in r.text


def test_vincular_pendientes_web_exige_csrf_y_solo_toca_mi_empresa(client, engine, settings, mundo):
    with Session(engine) as s:
        guardar_producto(s, s.get(Proveedor, mundo["prov"]), producto_texto="Bebida cola", precio_neto=1)
        guardar_producto(s, s.get(Proveedor, mundo["prov_otra"]), producto_texto="Bebida cola", precio_neto=1)
        s.commit()
    cookies, headers, csrf = _auth(settings, mundo["boris"])
    assert client.post("/proveedores/vincular-pendientes", data={}, cookies=cookies).status_code == 403
    r = client.post("/proveedores/vincular-pendientes", data={"csrf_token": csrf, "volver": "https://malo.cl"},
                    cookies=cookies, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and r.headers["location"].startswith("/proveedores?mensaje=")
    r = client.post("/proveedores/vincular-pendientes", data={"csrf_token": csrf, "volver": f"/proveedores/{mundo['prov']}"},
                    cookies=cookies, follow_redirects=False)  # fmt: skip
    assert r.headers["location"].startswith(f"/proveedores/{mundo['prov']}?mensaje=")
    with Session(engine) as s:
        por_emp = {
            e: s.execute(select(ProveedorProducto.producto_id).join(Proveedor).where(Proveedor.empresa_id == e)).scalar_one()
            for e in (mundo["emp"], mundo["otra"])
        }
    assert por_emp == {mundo["emp"]: mundo["bebida"], mundo["otra"]: None}
    # la lista muestra el botón solo si queda algo pendiente
    assert "action=\"/proveedores/vincular-pendientes\"" not in client.get("/proveedores", cookies=cookies).text
    ca, _, _ = _auth(settings, mundo["ana"])
    assert "Vincular pendientes (1)" in client.get("/proveedores", cookies=ca).text


def test_importar_web_de_precios_muestra_el_vinculo(client, engine, settings, mundo):
    cookies, headers, csrf = _auth(settings, mundo["boris"])
    data = _xlsx({"precios": [
        ["proveedor", "rut", "comuna", "contacto", "telefono", "producto", "formato", "factor_base",
         "unidad_base", "precio_neto", "vigente_desde"],
        ["Mayorista", None, None, None, None, "Bebida cola", "litro", 1, "L", 900, "2026-11-03"],
        ["Mayorista", None, None, None, None, "Cuaderno", "", None, None, 500, "2026-11-03"],
    ]})
    r = client.post("/proveedores/importar", data={"csrf_token": csrf}, cookies=cookies,
                    files={"archivo": ("p.xlsx", data, "application/octet-stream")})  # fmt: skip
    assert r.status_code == 200 and "Vinculados al catálogo" in r.text and "sin vincular: 1" in r.text


def test_formato_cantidad_no_redondea_el_stock_a_enteros():
    assert [formato_cantidad(v) for v in (2.5, 1500, 0, -1.5, 0.125, None, 1234.5)] == [
        "2,5", "1.500", "0", "-1,5", "0,125", "—", "1.234,5",
    ]
