"""F-plat-impuestos: impuestos específicos en el catálogo; neto sin impuestos, bruto con todos.

Todo offline (SQLite en memoria). El DDL de la migración va en test_plat_impuestos_pg.py.

Mundo: el ILA bajo (10 %) y alto (18 %) desde el 1-oct-2014; la familia «Bebidas, jugos y agua» en
modo `clasificar`; dos bebidas («Bebida cola», alta, y «Bebida cola zero», sin código: por
clasificar) y un arroz sin impuesto; una empresa con admin y usuario y otra empresa.
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.mercado import cotizacion as cot
from app.mercado import importar
from app.mercado import impuestos as imp
from app.mercado import lote as lt
from app.mercado import oferta as of
from app.mercado import ventas as vt
from app.mercado.mesa_dia import tarjetas
from app.mercado.proveedores import guardar_producto, hoy_chile
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaLinea,
    CompraAgil,
    CostoHistorial,
    Empresa,
    FamiliaImpuesto,
    ImpuestoEspecifico,
    OfertaPropia,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    ProveedorProducto,
    Usuario,
    Venta,
    VentaCompra,
)

_PW = "contraseña-segura-test"
_FAM = "Bebidas, jugos y agua"
_DESDE = date(2014, 10, 1)


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
    return {COOKIE_NAME: token}, csrf


@pytest.fixture()
def mundo(engine):
    with Session(engine) as s:
        s.add_all([
            ImpuestoEspecifico(codigo="ila_analcoholica_baja", nombre="ILA bebidas analcohólicas",
                               tasa=0.10, recuperable=True, vigente_desde=_DESDE),
            ImpuestoEspecifico(codigo="ila_analcoholica_alta", nombre="ILA alto azúcar",
                               tasa=0.18, recuperable=True, vigente_desde=_DESDE),
            FamiliaImpuesto(familia=_FAM, impuesto_codigo="ila_analcoholica_baja", modo="clasificar"),
        ])  # fmt: skip
        cola = ProductoCanonico(nombre="Bebida cola", unidad_base="L", familia=_FAM,
                                impuesto_codigo="ila_analcoholica_alta")  # fmt: skip
        zero = ProductoCanonico(nombre="Bebida cola zero", unidad_base="L", familia=_FAM)
        arroz = ProductoCanonico(nombre="Arroz grado 1", unidad_base="kg")
        s.add_all([cola, zero, arroz])
        s.flush()
        b_cola = ProductoFormato(producto_id=cola.id, nombre="botella 1,5 L", factor_base=1.5)
        b_zero = ProductoFormato(producto_id=zero.id, nombre="botella 1,5 L", factor_base=1.5)
        saco = ProductoFormato(producto_id=arroz.id, nombre="saco 25 kg", factor_base=25.0)
        s.add_all([b_cola, b_zero, saco])
        nuestra = Empresa(nombre="Aralia", rut="76123456-0", regiones_prioritarias=[16],
                          comunas_prioritarias=[], activa=True)  # fmt: skip
        otra = Empresa(nombre="Otra", rut="77777777-7", regiones_prioritarias=[16],
                       comunas_prioritarias=[], activa=True)  # fmt: skip
        s.add_all([nuestra, otra])
        s.flush()
        dist = Proveedor(empresa_id=nuestra.id, nombre="Distribuidora X", rubros=[], activo=True)
        s.add(dist)
        s.flush()
        users = {}
        for clave, emp, rol in (
            ("boris", nuestra.id, RolUsuario.ADMIN),
            ("gonzalo", nuestra.id, RolUsuario.USUARIO),
            ("ana", otra.id, RolUsuario.USUARIO),
        ):
            u = Usuario(email=f"{clave}@t.cl", nombre=clave, password_hash=hash_password(_PW),
                        empresa_id=emp, rol=rol)  # fmt: skip
            s.add(u)
            s.flush()
            users[clave] = u.id
        s.commit()
        return {
            "emp": nuestra.id, "otra": otra.id, "cola": cola.id, "zero": zero.id,
            "arroz": arroz.id, "b_cola": b_cola.id, "b_zero": b_zero.id, "saco": saco.id,
            "dist": dist.id, **users,
        }  # fmt: skip


# ---------------------------------------------------------------------------
# Resolución y vigencia
# ---------------------------------------------------------------------------


def test_resolucion_producto_familia_nada_y_clasificar(engine, mundo):
    hoy = hoy_chile()
    with Session(engine) as s:
        t = imp.cargar(s)
        cola = imp.impuesto_de(s, mundo["cola"], hoy, t)
        assert isinstance(cola, imp.Impuesto) and cola.tasa == 0.18  # el código del producto
        assert imp.impuesto_de(s, mundo["zero"], hoy, t) is imp.POR_CLASIFICAR  # clasificar sin código
        assert imp.impuesto_de(s, mundo["arroz"], hoy, t) is None  # ni producto ni familia
        # Una familia `aplica`: todos sus productos lo llevan sin código propio.
        imp.guardar_regla_familia(s, _FAM, "ila_analcoholica_baja", "aplica", None)
        assert imp.impuesto_de(s, mundo["zero"], hoy).tasa == 0.10
        # `ninguno` en el producto: explícitamente sin impuesto, aunque la familia lo tenga.
        imp.asignar_a_productos(s, [mundo["zero"]], imp.SIN_IMPUESTO)
        assert imp.impuesto_de(s, mundo["zero"], hoy) is None
        assert imp.ids_por_clasificar(s) == set()


def test_pp_sin_vinculo_usa_la_pista_y_con_vinculo_manda_el_catalogo(engine, mundo):
    hoy = hoy_chile()
    with Session(engine) as s:
        sin = ProveedorProducto(proveedor_id=mundo["dist"], producto_texto="Jugo X", formato_texto="",
                                precio_neto_actual=100, vigente_desde=hoy,
                                impuesto_codigo="ila_analcoholica_baja")  # fmt: skip
        con = ProveedorProducto(proveedor_id=mundo["dist"], producto_texto="Cola", formato_texto="",
                                precio_neto_actual=100, vigente_desde=hoy, producto_id=mundo["cola"],
                                impuesto_codigo="ila_analcoholica_baja")  # fmt: skip
        s.add_all([sin, con])
        s.flush()
        assert imp.impuesto_de(s, sin, hoy).tasa == 0.10
        assert imp.impuesto_de(s, con, hoy).tasa == 0.18
        t = imp.cargar(s)
        assert imp.pista_contradice(t, "ila_analcoholica_baja", "ila_analcoholica_alta", _FAM)
        assert not imp.pista_contradice(t, "ila_analcoholica_baja", None, _FAM)  # clasificar: calla


def test_una_tasa_nueva_no_cambia_los_costos_anteriores(engine, mundo):
    with Session(engine) as s:
        s.add(ImpuestoEspecifico(codigo="ila_analcoholica_alta", nombre="ILA alto azúcar",
                                 tasa=0.20, vigente_desde=date(2027, 1, 1)))  # fmt: skip
        s.flush()
        t = imp.cargar(s)
        assert imp.impuesto_de(s, mundo["cola"], date(2026, 12, 31), t).tasa == 0.18
        assert imp.impuesto_de(s, mundo["cola"], date(2027, 1, 1), t).tasa == 0.20
        # Antes de la primera vigencia conocida: la más antigua (nunca se inventa una tasa).
        assert imp.impuesto_de(s, mundo["cola"], date(2010, 1, 1), t).tasa == 0.18


# ---------------------------------------------------------------------------
# Neto ↔ bruto
# ---------------------------------------------------------------------------


def _imp(tasa: float, recuperable: bool = True) -> imp.Impuesto:
    return imp.Impuesto("x", "X", tasa, recuperable, _DESDE)


@pytest.mark.parametrize("impuesto", [None, imp.POR_CLASIFICAR, _imp(0.10), _imp(0.18), _imp(0.315)])
def test_bruto_y_neto_desde_bruto_son_inversos_a_peso(impuesto):
    for b in [1, 99, 890, 1290, 1990, 27500, 123457, 9_999_999]:
        assert round(imp.bruto(imp.neto_desde_bruto(b, impuesto), impuesto)) == b
    for n in [1, 689.92, 1000, 27500]:
        assert imp.neto_desde_bruto(imp.bruto(n, impuesto), impuesto) == pytest.approx(n, abs=0.01)


def test_iva_y_especifico_sobre_el_mismo_neto_y_no_recuperable_queda_en_el_neto():
    assert imp.bruto(1000, _imp(0.10)) == pytest.approx(1290)  # 1000 + 190 + 100
    assert imp.bruto(1000, None) == pytest.approx(1190)
    # No recuperable: el impuesto es costo, el neto ya lo incluye y el bruto solo suma IVA.
    no_rec = _imp(0.10, recuperable=False)
    assert imp.neto_desde_bruto(1190, no_rec) == 1000
    assert imp.bruto(1000, no_rec) == pytest.approx(1190)


def test_totales_una_fila_por_impuesto_recuperable():
    t = imp.totales([(1000.0, _imp(0.10)), (500.0, None), (2000.0, _imp(0.10, False)), (None, None)])
    assert (t.neto, t.iva) == (3500, 665)
    assert [(e.codigo, e.monto) for e in t.especificos] == [("x", 100)]
    assert t.bruto == 3500 + 665 + 100


# ---------------------------------------------------------------------------
# Importación de precios
# ---------------------------------------------------------------------------

_ENC = "proveedor;producto;formato;factor_base;unidad_base;precio_neto;vigente_desde;precio_bruto;impuesto"


def _csv(*filas: str) -> bytes:
    return ("\n".join([_ENC, *filas]) + "\n").encode("utf-8")


def _importar(s: Session, mundo, data: bytes) -> importar.Resumen:
    p = importar.parsear(data, "precios.csv")
    return importar.aplicar(s, mundo["emp"], p)


def _pp(s: Session, texto: str) -> ProveedorProducto:
    return s.execute(select(ProveedorProducto).where(ProveedorProducto.producto_texto == texto)).scalar_one()


def test_importar_solo_bruto_usa_el_impuesto_del_catalogo(engine, mundo):
    hoy = hoy_chile().isoformat()
    with Session(engine) as s:
        r = _importar(s, mundo, _csv(f"Distribuidora X;Bebida cola;botella 1,5 L;1.5;L;;{hoy};1935;"))
        assert not r.errores, r.errores
        pp = _pp(s, "Bebida cola")
        assert pp.producto_id == mundo["cola"]
        assert pp.precio_neto_actual == round(1935 / 1.37, 2)  # 1 + 19 % + 18 %


def test_importar_solo_neto_no_cambia(engine, mundo):
    hoy = hoy_chile().isoformat()
    with Session(engine) as s:
        r = _importar(s, mundo, _csv(f"Distribuidora X;Bebida cola;botella 1,5 L;1.5;L;1412;{hoy};;"))
        assert not r.errores and not r.avisos
        assert _pp(s, "Bebida cola").precio_neto_actual == 1412


def test_importar_ambos_que_cuadran_y_que_no(engine, mundo):
    hoy = hoy_chile().isoformat()
    with Session(engine) as s:
        r = _importar(s, mundo, _csv(
            f"Distribuidora X;Bebida cola;botella 1,5 L;1.5;L;1412;{hoy};1935;",  # 1935/1,37 = 1412,4
            f"Distribuidora X;Arroz grado 1;saco 25 kg;25;kg;20000;{hoy};30000;",  # 30000/1,19 ≠ 20000
        ))  # fmt: skip
        assert not r.errores
        assert len(r.avisos) == 1 and "no cuadra" in r.avisos[0] and "fila 3" in r.avisos[0]
        assert _pp(s, "Arroz grado 1").precio_neto_actual == round(30000 / 1.19, 2)  # manda el bruto


def test_importar_pista_sin_vinculo_y_pista_que_contradice(engine, mundo):
    hoy = hoy_chile().isoformat()
    with Session(engine) as s:
        r = _importar(s, mundo, _csv(
            f"Distribuidora X;Néctar raro marca Z;caja 1 L;1;L;;{hoy};1290;ila_analcoholica_baja",
            f"Distribuidora X;Bebida cola;botella 1,5 L;1.5;L;;{hoy};1370;ila_analcoholica_baja",
            f"Distribuidora X;Agua tónica;;1;L;;{hoy};1000;impuesto_inventado",
        ))  # fmt: skip
        sin = _pp(s, "Néctar raro marca Z")
        assert sin.producto_id is None and sin.impuesto_codigo == "ila_analcoholica_baja"
        assert sin.precio_neto_actual == 1000  # 1290 / 1,29
        assert _pp(s, "Bebida cola").precio_neto_actual == 1000  # manda el catálogo: 1370 / 1,37
        assert any("el catálogo dice otra cosa" in a for a in r.avisos)
        assert len(r.errores) == 1 and "impuesto_inventado" in r.errores[0]


def test_importar_clasificar_sin_codigo_entra_neto_sin_iva_y_se_cuenta(engine, mundo):
    hoy = hoy_chile().isoformat()
    with Session(engine) as s:
        r = _importar(s, mundo, _csv(f"Distribuidora X;Bebida cola zero;botella 1,5 L;1.5;L;;{hoy};1190;"))
        assert r.por_clasificar == 1 and not r.errores
        assert _pp(s, "Bebida cola zero").precio_neto_actual == 1000


def test_segunda_subida_con_bruto_corrige_el_neto_sin_duplicar(engine, mundo):
    hoy = hoy_chile().isoformat()
    fila = f"Distribuidora X;Bebida cola zero;botella 1,5 L;1.5;L;;{hoy};1290;"
    with Session(engine) as s:
        _importar(s, mundo, _csv(fila))
        s.commit()
        assert _pp(s, "Bebida cola zero").precio_neto_actual == round(1290 / 1.19, 2)
        imp.asignar_a_productos(s, [mundo["zero"]], "ila_analcoholica_baja")  # se clasifica
        s.commit()
        r = _importar(s, mundo, _csv(fila))
        s.commit()
        assert r.precios_cambiados == 1 and r.productos_nuevos == 0 and r.por_clasificar == 0
        assert _pp(s, "Bebida cola zero").precio_neto_actual == 1000
        assert s.execute(select(func.count()).select_from(ProveedorProducto)).scalar_one() == 1
        r3 = _importar(s, mundo, _csv(fila))  # tercera vez: idempotente
        assert r3.sin_cambios == 1 and not r3.hay_cambios
        assert s.execute(select(func.count()).select_from(CostoHistorial)).scalar_one() == 2


def test_plantilla_explica_las_columnas_nuevas():
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(importar.generar_plantilla_xlsx()))
    encabezado = [c.value for c in wb["precios"][1]]
    assert "precio_bruto" in encabezado and "impuesto" in encabezado
    texto = " ".join(str(f[0]) for f in wb["Instrucciones"].iter_rows(values_only=True))
    assert "precio_bruto" in texto and "impuesto por clasificar" in texto


# ---------------------------------------------------------------------------
# Oferta, cotización formal y caja
# ---------------------------------------------------------------------------


def _ca_bebidas(s: Session, mundo) -> CompraAgil:
    ca = CompraAgil(codigo="CA-B", nombre="Bebidas", estado="publicada", region=16,
                    organismo_nombre="Liceo", fecha_cierre=ahora_utc() + timedelta(days=3),
                    detalle_lineas="completo")  # fmt: skip
    s.add(ca)
    s.add_all([
        CaLinea(ca_codigo="CA-B", n=1, producto_id=mundo["cola"], formato_id=mundo["b_cola"],
                cantidad=10.0, cantidad_base=15.0, texto_origen="Bebida cola 1,5 L", estado="ok"),
        CaLinea(ca_codigo="CA-B", n=2, producto_id=mundo["arroz"], formato_id=mundo["saco"],
                cantidad=1.0, cantidad_base=25.0, texto_origen="Arroz", estado="ok"),
    ])  # fmt: skip
    s.flush()
    return ca


def _docx_texto(data: bytes) -> str:
    return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)


def test_oferta_guarda_el_impuesto_y_el_docx_lo_desglosa(engine, mundo):
    with Session(engine) as s:
        ca = _ca_bebidas(s, mundo)
        u = s.get(Usuario, mundo["boris"])
        lineas = of.armar(s, mundo["emp"], ca, None, None)
        lineas[0].precio_unitario, lineas[1].precio_unitario = 1000.0, 20000.0
        o = of.guardar(s, mundo["emp"], u, ca, lineas, plazo=3, notas="", version=0)
        assert o.total_neto == 30000  # sigue siendo neto
        guardadas = {d["linea_id"]: d for d in o.lineas}
        assert guardadas[lineas[0].linea_id]["impuesto"]["tasa"] == 0.18
        assert guardadas[lineas[1].linea_id]["impuesto"] is None
        # Cambia la tasa del catálogo: la oferta guardada conserva la suya.
        s.get(ProductoCanonico, mundo["cola"]).impuesto_codigo = "ila_analcoholica_baja"
        s.flush()
        releidas = of.armar(s, mundo["emp"], ca, None, o)
        assert releidas[0].impuesto.tasa == 0.18
        o.numero_cotizacion = "COT-2026-1"
        texto = _docx_texto(cot.docx(s.get(Empresa, mundo["emp"]), ca, o, releidas))
        assert "Total neto: $30.000" in texto
        assert "IVA 19 %: $5.700" in texto
        assert "ILA alto azúcar 18 %: $1.800" in texto  # 10 × 1.000 × 18 %
        assert "Total bruto: $37.500" in texto


def test_docx_sin_impuesto_especifico_no_trae_esa_fila(engine, mundo):
    with Session(engine) as s:
        ca = _ca_bebidas(s, mundo)
        lineas = [lo for lo in of.armar(s, mundo["emp"], ca, None, None) if lo.producto_id == mundo["arroz"]]
        lineas[0].precio_unitario = 20000.0
        o = OfertaPropia(ca_codigo="CA-B", empresa_id=mundo["emp"], lineas=[], total_neto=20000,
                         numero_cotizacion="COT-2026-2", validez_dias=30)  # fmt: skip
        texto = _docx_texto(cot.docx(s.get(Empresa, mundo["emp"]), ca, o, lineas))
        assert "ILA" not in texto
        assert "Total bruto: $23.800" in texto


def test_caja_compromete_en_bruto_con_el_impuesto_del_catalogo(engine, mundo):
    hoy = hoy_chile()
    with Session(engine) as s:
        _ca_bebidas(s, mundo)
        v = Venta(empresa_id=mundo["emp"], ca_codigo="CA-B", estado="comprando")
        s.add(v)
        s.flush()
        s.add(VentaCompra(venta_id=v.id, proveedor_id=mundo["dist"], producto_id=mundo["cola"],
                          descripcion="Cola", cantidad=10, precio_neto=1000.0, fecha=hoy,
                          pagada=False, pagar_en=hoy + timedelta(days=5)))  # fmt: skip
        s.flush()
        c = vt.caja(s, mundo["emp"], hoy)
        assert c.comprometido[0].neto == 10000
        assert c.comprometido_total == pytest.approx(13700)  # 10.000 × (1 + 19 % + 18 %)
        assert vt.caja(s, mundo["otra"], hoy).comprometido_total == 0


# ---------------------------------------------------------------------------
# Lote: se pide en bruto, se guarda el neto
# ---------------------------------------------------------------------------


def test_lote_respuesta_en_bruto_guarda_el_neto(engine, mundo):
    with Session(engine) as s:
        ca = _ca_bebidas(s, mundo)
        u = s.get(Usuario, mundo["boris"])
        dist = s.get(Proveedor, mundo["dist"])
        guardar_producto(s, dist, producto_texto="Cola", formato_texto="1,5 L", precio_neto=900,
                         vigente_desde=hoy_chile(), factor_base=1.5, unidad_base="L",
                         producto_id=mundo["cola"], formato_id=mundo["b_cola"])  # fmt: skip
        s.commit()
        lote = lt.crear(s, mundo["emp"], u, [ca.codigo])
        s.commit()
        sol = next(
            vs.s for vs in lt.vista(s, mundo["emp"], lote.id).solicitudes if vs.proveedor.id == dist.id
        )
        texto = lt.generar_texto(s, sol, lote)
        assert "con IVA e impuestos incluidos" in texto and "neto" not in texto
        fila = next(f for f in lt.filas_de(s, sol) if f.lote_linea.producto_id == mundo["cola"])
        r = lt.validar_respuesta("1.370", "20:1233", "si", "", bruto=True)
        lt.registrar_respuesta(s, mundo["emp"], u, sol.id, {fila.solicitud_linea.id: r}, None)
        s.commit()
        pp = s.execute(select(ProveedorProducto).where(ProveedorProducto.producto_id == mundo["cola"])).scalar_one()
        assert pp.precio_neto_actual == 1000  # 1.370 / 1,37
        assert pp.tramos == [{"desde": 20, "precio": 900}]  # 1.233 / 1,37
        fila = next(f for f in lt.filas_de(s, sol) if f.lote_linea.producto_id == mundo["cola"])
        assert fila.precio_bruto == 1370 and fila.tramos_txt == "20:1233"  # se muestra en bruto


# ---------------------------------------------------------------------------
# Señales y ownership
# ---------------------------------------------------------------------------


def test_mesa_marca_lineas_por_clasificar(engine, mundo):
    with Session(engine) as s:
        _ca_bebidas(s, mundo)
        s.add(CaLinea(ca_codigo="CA-B", n=3, producto_id=mundo["zero"], formato_id=mundo["b_zero"],
                      cantidad=5.0, cantidad_base=7.5, texto_origen="Zero", estado="ok"))  # fmt: skip
        s.flush()
        (t,) = tarjetas(s, ["CA-B"], [], {}, ahora_utc())
        assert t["n_por_clasificar"] == 1


def test_por_clasificar_por_proveedor_es_por_empresa(engine, mundo):
    with Session(engine) as s:
        dist = s.get(Proveedor, mundo["dist"])
        guardar_producto(s, dist, producto_texto="Zero", precio_neto=900, producto_id=mundo["zero"])
        s.flush()
        assert imp.por_clasificar_por_proveedor(s, mundo["emp"]) == {mundo["dist"]: 1}
        assert imp.por_clasificar_por_proveedor(s, mundo["otra"]) == {}


def test_solo_admin_edita_los_impuestos_del_catalogo(client, settings, engine, mundo):
    for clave, esperado in (("gonzalo", 403), ("ana", 403), ("boris", 303)):
        cookies, csrf = _auth(settings, mundo[clave])
        client.cookies.clear()
        for k, v in cookies.items():
            client.cookies.set(k, v)
        r = client.post("/catalogo/impuestos/productos", follow_redirects=False,
                        data={"csrf_token": csrf, "producto_id": str(mundo["zero"]),
                              "impuesto_codigo": "ila_analcoholica_baja"})  # fmt: skip
        assert r.status_code == esperado, clave
        r = client.post("/catalogo/impuestos/familia", follow_redirects=False,
                        data={"csrf_token": csrf, "familia": "Lácteos y huevos",
                              "impuesto_codigo": "ila_analcoholica_baja", "modo": "aplica"})  # fmt: skip
        assert r.status_code == esperado, clave
    with Session(engine) as s:
        assert s.get(ProductoCanonico, mundo["zero"]).impuesto_codigo == "ila_analcoholica_baja"
        assert s.get(FamiliaImpuesto, "Lácteos y huevos").modo == "aplica"


def test_pantallas_muestran_impuestos(client, settings, engine, mundo):
    cookies, _csrf = _auth(settings, mundo["boris"])
    for k, v in cookies.items():
        client.cookies.set(k, v)
    r = client.get("/catalogo?tab=impuestos")
    assert r.status_code == 200
    assert "ila_analcoholica_alta" in r.text and "Bebida cola zero" in r.text  # por clasificar
    r = client.get(f"/catalogo/productos/{mundo['cola']}")
    assert r.status_code == 200 and "ILA alto azúcar 18 %" in r.text
    with Session(engine) as s:
        dist = s.get(Proveedor, mundo["dist"])
        guardar_producto(s, dist, producto_texto="Cola", precio_neto=1000, producto_id=mundo["cola"])
        guardar_producto(s, dist, producto_texto="Zero", precio_neto=900, producto_id=mundo["zero"])
        s.commit()
    r = client.get(f"/proveedores/{mundo['dist']}")
    assert r.status_code == 200 and "$1.370" in r.text and "impuesto por clasificar" in r.text
    r = client.get("/proveedores?por_clasificar=1")
    assert r.status_code == 200 and "Distribuidora X" in r.text and "1 por clasificar" in r.text


def test_un_codigo_inexistente_no_se_asigna(engine, mundo):
    with Session(engine) as s, pytest.raises(imp.ErrorImpuesto):
        imp.asignar_a_productos(s, [mundo["arroz"]], "no_existe")
    with Session(engine) as s, pytest.raises(imp.ErrorImpuesto):
        imp.guardar_regla_familia(s, _FAM, "ila_analcoholica_baja", "otro_modo", None)
