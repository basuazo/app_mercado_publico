"""F-plat-lote: cotizar varias CA a la vez (lote, solicitudes de precio, precios vencidos, reserva).

Todo offline (SQLite en memoria). La carrera con dos sesiones a la vez, los CHECK y el DDL de la
migración van en test_plat_lote_pg.py.

Mundo: 3 CA de bebidas que piden en formatos distintos (latas de 350 ml, botellas de 1,5 L y packs
de 6 latas) y 2 proveedores: «Vencido» (más barato, pero con precio de hace 30 días) y «Tramos»
(precio vigente con descuento desde 10 packs).
"""

from __future__ import annotations

import io
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.mercado import catalogo as cat
from app.mercado import equipo as eq
from app.mercado import lote as lt
from app.mercado import oferta_propia as op
from app.mercado.proveedores import guardar_producto, hoy_chile, mejor_costo, tramo_para
from app.models.base import Base
from app.models.tables import (
    CaEvento,
    CaLinea,
    CompraAgil,
    CostoHistorial,
    Empresa,
    EmpresaProducto,
    EmpresaProductoMovimiento,
    Lote,
    LoteCa,
    LoteLinea,
    OfertaPropia,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    ProveedorProducto,
    SolicitudLinea,
    SolicitudPrecio,
    Usuario,
)

_PW = "contraseña-segura-test"
PACK = 2.1  # pack 6 × 350 ml en litros


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
    return {COOKIE_NAME: token}, {"X-CSRF-Token": csrf}, csrf


def _ca(s, codigo, nombre, organismo, dias=3):
    s.add(CompraAgil(codigo=codigo, nombre=nombre, estado="publicada", region=16,
                     organismo_nombre=organismo, fecha_cierre=ahora_utc() + timedelta(days=dias),
                     detalle_lineas="completo"))  # fmt: skip


@pytest.fixture()
def mundo(engine):
    hoy = hoy_chile()
    with Session(engine) as s:
        bebida = ProductoCanonico(nombre="Bebida cola", unidad_base="L", activo=True)
        s.add(bebida)
        s.flush()
        f = {
            nom: ProductoFormato(producto_id=bebida.id, nombre=nom, factor_base=fac)
            for nom, fac in (("litro", 1.0), ("lata 350 ml", 0.35), ("botella 1,5 L", 1.5),
                             ("pack 6 latas", PACK))  # fmt: skip
        }
        s.add_all(f.values())
        nuestra = Empresa(nombre="Distribuidora Ñuble", rut="76123456-0", regiones_prioritarias=[16],
                          comunas_prioritarias=[], activa=True)  # fmt: skip
        otra = Empresa(nombre="Otra", rut="11111111-1", regiones_prioritarias=[16],
                       comunas_prioritarias=[], activa=True)  # fmt: skip
        s.add_all([nuestra, otra])
        s.flush()
        vencido = Proveedor(empresa_id=nuestra.id, nombre="Vencido SA", rubros=[], activo=True)
        tramos = Proveedor(empresa_id=nuestra.id, nombre="Tramos Ltda", rubros=[], activo=True,
                           contacto_nombre="Marta")  # fmt: skip
        ajeno = Proveedor(empresa_id=otra.id, nombre="Del otro", rubros=[], activo=True)
        s.add_all([vencido, tramos, ajeno])
        s.flush()
        pp_v, _ = guardar_producto(
            s, vencido, producto_texto="Coca pack", formato_texto="pack 6", precio_neto=2000,
            vigente_desde=hoy - timedelta(days=30), factor_base=PACK, unidad_base="L",
            producto_id=bebida.id, formato_id=f["pack 6 latas"].id,
        )  # fmt: skip
        pp_t, _ = guardar_producto(
            s, tramos, producto_texto="Bebida cola pack", formato_texto="pack 6", precio_neto=2400,
            vigente_desde=hoy, factor_base=PACK, unidad_base="L", producto_id=bebida.id,
            formato_id=f["pack 6 latas"].id, tramos=[{"desde": 10, "precio": 2100}],
        )  # fmt: skip
        pp_ajeno, _ = guardar_producto(
            s, ajeno, producto_texto="Bebida", precio_neto=100, factor_base=1.0, unidad_base="L",
            producto_id=bebida.id, formato_id=f["litro"].id,
        )  # fmt: skip
        users = {}
        for clave, emp in (("boris", nuestra.id), ("gonzalo", nuestra.id), ("ana", otra.id)):
            u = Usuario(email=f"{clave}@t.cl", nombre=clave, password_hash=hash_password(_PW),
                        empresa_id=emp)  # fmt: skip
            s.add(u)
            s.flush()
            users[clave] = u.id
        _ca(s, "CA-1", "Bebidas en lata", "Liceo de Chillán")
        _ca(s, "CA-2", "Bebidas botella", "Hospital de San Carlos", dias=2)
        _ca(s, "CA-3", "Colaciones", "Municipalidad de Bulnes", dias=5)
        _ca(s, "CA-VIEJA", "Cerrada", "Otro", dias=-1)
        s.flush()
        lineas = (
            ("CA-1", 1, f["lata 350 ml"].id, 24.0, 24 * 0.35, "ok", "Bebida lata 350 cc"),
            ("CA-2", 1, f["botella 1,5 L"].id, 10.0, 15.0, "ok", "Bebida 1,5 lt"),
            ("CA-3", 1, f["pack 6 latas"].id, 5.0, 5 * PACK, "dudosa", "Pack 6 bebidas"),
        )
        for codigo, n, fid, cant, cb, est, txt in lineas:
            s.add(CaLinea(ca_codigo=codigo, n=n, producto_id=bebida.id, formato_id=fid,
                          cantidad=cant, cantidad_base=cb, texto_origen=txt, estado=est))  # fmt: skip
        s.add(CaLinea(ca_codigo="CA-3", n=2, producto_id=None, cantidad=3.0,
                      texto_origen="Servilletas", estado="pendiente"))  # fmt: skip
        s.commit()
        return {
            "emp": nuestra.id, "otra": otra.id, "bebida": bebida.id,
            "f": {k: v.id for k, v in f.items()},
            "vencido": vencido.id, "tramos": tramos.id, "ajeno": ajeno.id,
            "pp_v": pp_v.id, "pp_t": pp_t.id, "pp_ajeno": pp_ajeno.id, **users,
        }  # fmt: skip


def _u(s, mundo, quien="boris"):
    return s.get(Usuario, mundo[quien])


def _crear(s, mundo, codigos=("CA-1", "CA-2", "CA-3"), quien="boris"):
    lote = lt.crear(s, mundo["emp"], _u(s, mundo, quien), list(codigos))
    s.commit()
    return lote.id


def _linea(s, lote_id):
    return s.execute(select(LoteLinea).where(LoteLinea.lote_id == lote_id)).scalar_one()


# ---------------------------------------------------------------------------
# Mejor costo vigente (§2.3) y tramos
# ---------------------------------------------------------------------------


def test_tramo_para_elige_el_mayor_desde_alcanzado():
    tramos = [{"desde": 10, "precio": 900}, {"desde": 50, "precio": 800}, {"desde": "x"}]
    assert tramo_para(tramos, 9) is None
    assert tramo_para(tramos, 10) == (10.0, 900.0)
    assert tramo_para(tramos, 120) == (50.0, 800.0)
    assert tramo_para(None, 100) is None


def test_mejor_costo_prefiere_vigente_y_aplica_tramo(engine, mundo):
    with Session(engine) as s:
        sin_cant = mejor_costo(s, mundo["emp"], producto_id=mundo["bebida"], unidad_base="L")
        assert sin_cant is not None
        # «Vencido» es más barato (2000) pero tiene 30 días > 15: gana el vigente.
        assert sin_cant.ganador.proveedor == "Tramos Ltda"
        assert not sin_cant.vencido and sin_cant.ganador.tramo_aplicado is None
        assert sin_cant.ganador.precio_neto == 2400
        assert [c.proveedor for c in sin_cant.candidatos] == ["Tramos Ltda", "Vencido SA"]
        assert sin_cant.candidatos[1].vencido and sin_cant.candidatos[1].dias == 30
        con_cant = mejor_costo(s, mundo["emp"], producto_id=mundo["bebida"], unidad_base="L",
                               cantidad_base=12 * PACK)  # fmt: skip
        assert con_cant is not None and con_cant.ganador.tramo_aplicado == 10
        assert con_cant.ganador.precio_por_unidad_base == pytest.approx(2100 / PACK)
        libre = mejor_costo(s, mundo["emp"], producto_id=mundo["bebida"], unidad_base="L",
                            permitir_vencidos=True)  # fmt: skip
        assert libre is not None and libre.ganador.proveedor == "Vencido SA"


def test_mejor_costo_solo_vencidos_devuelve_el_mejor_vencido(engine, mundo):
    with Session(engine) as s:
        s.get(Proveedor, mundo["tramos"]).activo = False
        s.flush()
        mc = mejor_costo(s, mundo["emp"], producto_id=mundo["bebida"], unidad_base="L")
        assert mc is not None and mc.vencido and mc.ganador.proveedor == "Vencido SA"
        # La vigencia es de la empresa: con 60 días ya no está vencido.
        s.get(Empresa, mundo["emp"]).dias_vigencia_precio = 60
        s.flush()
        mc = mejor_costo(s, mundo["emp"], producto_id=mundo["bebida"], unidad_base="L")
        assert mc is not None and not mc.vencido


def test_mejor_costo_desempata_por_proveedor_habitual(engine, mundo):
    with Session(engine) as s:
        tramos = s.get(Proveedor, mundo["tramos"])
        otro = Proveedor(empresa_id=mundo["emp"], nombre="Aaa Igual", rubros=[], activo=True)
        s.add(otro)
        s.flush()
        guardar_producto(s, otro, producto_texto="Cola", precio_neto=2400, factor_base=PACK,
                         unidad_base="L", producto_id=mundo["bebida"],
                         formato_id=mundo["f"]["pack 6 latas"])  # fmt: skip
        mc = mejor_costo(s, mundo["emp"], producto_id=mundo["bebida"], unidad_base="L")
        assert mc is not None and mc.ganador.proveedor == "Aaa Igual"  # empate → alfabético
        op.guardar(s, mundo["emp"], None, producto_id=mundo["bebida"],
                   formato_id=mundo["f"]["litro"], proveedor_habitual_id=tramos.id)  # fmt: skip
        mc = mejor_costo(s, mundo["emp"], producto_id=mundo["bebida"], unidad_base="L")
        assert mc is not None and mc.ganador.proveedor == "Tramos Ltda" and mc.ganador.habitual


# ---------------------------------------------------------------------------
# Agregación (§1.2)
# ---------------------------------------------------------------------------


def test_agregacion_en_unidad_base_y_redondeo_al_formato_de_compra(engine, mundo):
    with Session(engine) as s:
        lote_id = _crear(s, mundo)
        ll = _linea(s, lote_id)
        total = 24 * 0.35 + 15.0 + 5 * PACK  # 33,9 L
        assert ll.cantidad_base == pytest.approx(total)
        assert ll.formato_id == mundo["f"]["pack 6 latas"]  # el más usado por los proveedores
        assert ll.cantidad_formato == 17  # ceil(33,9 / 2,1 = 16,14)
        assert ll.en_stock_base == 0 and ll.a_comprar_base == pytest.approx(total)
        assert {d["ca"] for d in ll.detalle} == {"CA-1", "CA-2", "CA-3"}
        # Con 16+ packs aplica el tramo de «Tramos» (desde 10): 2100 por pack.
        assert ll.proveedor_sugerido_id == mundo["tramos"]
        assert ll.costo_unitario_sugerido == pytest.approx(2100 / PACK)
        assert not ll.precio_vencido
        lote = s.get(Lote, lote_id)
        cierre_ca2 = s.get(CompraAgil, "CA-2").fecha_cierre
        assert lote.fecha_limite == cierre_ca2  # el cierre más cercano
        assert lote.nombre.startswith("Lote del ")


def test_lineas_sin_producto_se_apartan_y_no_bloquean(engine, mundo):
    with Session(engine) as s:
        lote_id = _crear(s, mundo)
        v = lt.vista(s, mundo["emp"], lote_id)
        assert v.sin_producto_lineas == 1 and v.sin_producto_cas == 1
        assert len(v.lineas) == 1  # el lote se armó igual


def test_formato_de_compra_sin_proveedores_es_el_base(engine, mundo):
    with Session(engine) as s:
        for pp in s.execute(select(ProveedorProducto)).scalars():
            pp.formato_id = None
        s.flush()
        lote_id = _crear(s, mundo)
        ll = _linea(s, lote_id)
        assert ll.formato_id == mundo["f"]["litro"] and ll.cantidad_formato == 34


def test_stock_propio_se_descuenta(engine, mundo):
    with Session(engine) as s:
        op.guardar(s, mundo["emp"], None, producto_id=mundo["bebida"],
                   formato_id=mundo["f"]["botella 1,5 L"], stock=4)  # 6 L  # fmt: skip
        s.commit()
        lote_id = _crear(s, mundo)
        ll = _linea(s, lote_id)
        assert ll.en_stock_base == pytest.approx(6.0)
        assert ll.a_comprar_base == pytest.approx(33.9 - 6.0)


def test_cambiar_lineas_de_una_ca_recalcula_el_lote(engine, mundo):
    with Session(engine) as s:
        lote_id = _crear(s, mundo)
        ln = s.execute(select(CaLinea).where(CaLinea.ca_codigo == "CA-2")).scalar_one()
        ln.cantidad, ln.cantidad_base = 20.0, 30.0
        cat.recalcular_detalle_lineas(s, ["CA-2"])  # lo llaman todas las ediciones de líneas
        s.commit()
        assert _linea(s, lote_id).cantidad_base == pytest.approx(24 * 0.35 + 30.0 + 5 * PACK)


# ---------------------------------------------------------------------------
# Una CA en un solo lote abierto; estados de ca_trabajo
# ---------------------------------------------------------------------------


def test_una_ca_en_un_solo_lote_abierto(engine, mundo):
    with Session(engine) as s1:
        primero = _crear(s1, mundo, ["CA-1"])
    with Session(engine) as s2:  # otra persona, otra sesión
        with pytest.raises(lt.CaEnOtroLote) as exc:
            lt.crear(s2, mundo["emp"], _u(s2, mundo, "gonzalo"), ["CA-2", "CA-1"])
        assert exc.value.lote_id == primero
        s2.rollback()
        assert s2.execute(select(func.count()).select_from(Lote)).scalar_one() == 1
        # Cerrado el primero, la CA puede entrar a otro lote.
        lt.cambiar_estado(s2, mundo["emp"], _u(s2, mundo), primero, lt.CERRADO, None)
        s2.commit()
        assert _crear(s2, mundo, ["CA-1"]) != primero


def test_solo_ca_vigentes_de_las_regiones_de_la_empresa(engine, mundo):
    with Session(engine) as s:
        with pytest.raises(lt.ErrorLote, match="CA-VIEJA"):
            lt.crear(s, mundo["emp"], _u(s, mundo), ["CA-1", "CA-VIEJA"])
        s.rollback()
        s.get(CompraAgil, "CA-1").region = 8
        s.flush()
        with pytest.raises(lt.ErrorLote):
            lt.crear(s, mundo["emp"], _u(s, mundo), ["CA-1"])


def test_agregar_pasa_a_cotizando_y_quitar_vuelve_al_estado_previo(engine, mundo):
    with Session(engine) as s:
        u = _u(s, mundo)
        eq.cambiar_estado(s, mundo["emp"], u, "CA-2", eq.PRIORIZADA, 0)
        s.commit()
        lote_id = _crear(s, mundo, ["CA-1", "CA-2"])
        assert eq.trabajo_de(s, mundo["emp"], "CA-2").estado == eq.COTIZANDO
        assert eq.trabajo_de(s, mundo["emp"], "CA-1").estado == eq.COTIZANDO
        ev = s.execute(
            select(CaEvento).where(CaEvento.ca_codigo == "CA-2", CaEvento.tipo == eq.EV_ESTADO)
            .order_by(CaEvento.id.desc())
        ).scalars().first()  # fmt: skip
        assert ev.datos["lote_id"] == lote_id and ev.datos["a"] == eq.COTIZANDO
        version = s.get(Lote, lote_id).version
        lt.quitar_ca(s, mundo["emp"], u, lote_id, "CA-2", version)
        lt.quitar_ca(s, mundo["emp"], u, lote_id, "CA-1", None)
        s.commit()
        assert eq.trabajo_de(s, mundo["emp"], "CA-2").estado == eq.PRIORIZADA
        assert eq.trabajo_de(s, mundo["emp"], "CA-1").estado == eq.NUEVA
        assert s.execute(select(func.count()).select_from(LoteLinea)).scalar_one() == 0


# ---------------------------------------------------------------------------
# Solicitudes, respuesta y planilla (§1.3, §2.2)
# ---------------------------------------------------------------------------


def _solicitud(s, lote_id):
    return s.execute(select(SolicitudPrecio).where(SolicitudPrecio.lote_id == lote_id)).scalar_one()


def test_solicitud_por_proveedor_y_texto_sin_datos_del_comprador(engine, mundo):
    with Session(engine) as s:
        lote_id = _crear(s, mundo)
        sol = _solicitud(s, lote_id)
        assert sol.proveedor_id == mundo["tramos"] and sol.estado == lt.BORRADOR
        sl = s.execute(select(SolicitudLinea)).scalar_one()
        assert sl.cantidad_formato == 17
        texto = sol.texto_generado
        assert "Hola Marta" in texto and "Distribuidora Ñuble" in texto
        assert "Bebida cola · pack 6 latas · 17" in texto
        for prohibido in ("CA-1", "CA-2", "CA-3", "Liceo", "Hospital", "Municipalidad",
                          "Bebidas en lata", "Colaciones"):  # fmt: skip
            assert prohibido not in texto
        lote = s.get(Lote, lote_id)
        assert lt._fecha_respuesta(lote) in texto  # fecha_limite − 1 día


def test_respuesta_registra_costo_con_historial_y_recalcula(engine, mundo):
    with Session(engine) as s:
        u = _u(s, mundo)
        lote_id = _crear(s, mundo)
        sol = _solicitud(s, lote_id)
        lt.marcar_enviada(s, mundo["emp"], u, sol.id, "whatsapp", sol.version)
        s.commit()
        assert s.get(Lote, lote_id).estado == lt.SOLICITADO
        sl = s.execute(select(SolicitudLinea)).scalar_one()
        r = lt.validar_respuesta("1.900", "", True, "3")
        n = lt.registrar_respuesta(s, mundo["emp"], u, sol.id, {sl.id: r}, sol.version)
        s.commit()
        assert n == 1
        pp = s.get(ProveedorProducto, mundo["pp_t"])
        assert pp.precio_neto_actual == 1900 and pp.fuente == "solicitud"
        assert pp.tramos is None  # la respuesta no trajo tramos: los viejos se borran
        hist = s.execute(
            select(CostoHistorial).where(CostoHistorial.proveedor_producto_id == pp.id)
            .order_by(CostoHistorial.id)
        ).scalars().all()  # fmt: skip
        assert [h.precio_neto for h in hist] == [2400, 1900] and hist[-1].fuente == "solicitud"
        ll = _linea(s, lote_id)
        assert ll.costo_unitario_sugerido == pytest.approx(1900 / PACK)
        lote = s.get(Lote, lote_id)
        assert lote.estado == lt.CON_PRECIOS
        assert sol.estado == lt.RESPONDIDA and sl.plazo_dias == 3 and sl.disponible is True


def test_respuesta_con_mismo_precio_confirma_y_deja_de_estar_vencido(engine, mundo):
    with Session(engine) as s:
        s.get(Proveedor, mundo["tramos"]).activo = False
        s.flush()
        u = _u(s, mundo)
        lote_id = _crear(s, mundo)
        ll = _linea(s, lote_id)
        assert ll.precio_vencido and ll.dias_precio == 30
        sol = _solicitud(s, lote_id)
        assert sol.proveedor_id == mundo["vencido"]
        sl = s.execute(select(SolicitudLinea)).scalar_one()
        lt.registrar_respuesta(s, mundo["emp"], u, sol.id, {sl.id: lt.Respuesta(2000.0)}, None)
        s.commit()
        pp = s.get(ProveedorProducto, mundo["pp_v"])
        assert pp.vigente_desde == hoy_chile()
        assert not _linea(s, lote_id).precio_vencido
        n_hist = s.execute(
            select(func.count()).select_from(CostoHistorial)
            .where(CostoHistorial.proveedor_producto_id == pp.id)
        ).scalar_one()  # fmt: skip
        assert n_hist == 2  # el precio confirmado queda en el historial


def test_evento_precio_vencido_una_vez_por_lote(engine, mundo):
    with Session(engine) as s:
        s.get(Proveedor, mundo["tramos"]).activo = False
        s.commit()
        lote_id = _crear(s, mundo)
        lt.recalcular_lote(s, mundo["emp"], lote_id)
        lt.recalcular_lote(s, mundo["emp"], lote_id)
        s.commit()
        evs = s.execute(select(CaEvento).where(CaEvento.tipo == eq.EV_PRECIO_VENCIDO)).scalars().all()
        assert len(evs) == 1 and evs[0].ca_codigo == f"lote:{lote_id}" and evs[0].usuario_id is None
        desc = eq.describir_eventos(s, evs, mundo["boris"], ahora_utc())[0]
        assert desc["url"] == f"/lotes/{lote_id}" and "vencido" in desc["despues"]
        # Llega a la campana de la otra persona del equipo.
        g = s.get(Usuario, mundo["gonzalo"])
        assert any(e.tipo == eq.EV_PRECIO_VENCIDO for e in eq.listar_no_leidos(s, g, mundo["emp"]))


def test_tramos_de_la_respuesta_y_validacion(engine, mundo):
    with Session(engine) as s:
        r = lt.validar_respuesta("2.300", "10:2.200; 50:2000", None, "")
        assert r.precio == 2300 and r.tramos == [{"desde": 10, "precio": 2200},
                                                 {"desde": 50, "precio": 2000}]  # fmt: skip
        for malo in (("-5", "", None, ""), ("abc", "", None, ""), ("100", "10", None, ""),
                     ("100", "", None, "999")):  # fmt: skip
            with pytest.raises(lt.ErrorLote):
                lt.validar_respuesta(*malo)
        u = _u(s, mundo)
        lote_id = _crear(s, mundo)
        sol = _solicitud(s, lote_id)
        sl = s.execute(select(SolicitudLinea)).scalar_one()
        lt.registrar_respuesta(s, mundo["emp"], u, sol.id, {sl.id: r}, None)
        s.commit()
        assert s.get(ProveedorProducto, mundo["pp_t"]).tramos == r.tramos
        # 16,14 packs a comprar: aplica el tramo «desde 10» (2200 por pack).
        assert _linea(s, lote_id).costo_unitario_sugerido == pytest.approx(2200 / PACK)


def test_xlsx_ida_y_vuelta(engine, mundo):
    with Session(engine) as s:
        u = _u(s, mundo)
        lote_id = _crear(s, mundo)
        sol = _solicitud(s, lote_id)
        data = lt.xlsx_solicitud(s, mundo["emp"], sol.id)
        wb = load_workbook(io.BytesIO(data))
        ws = wb["precios"]
        filas = list(ws.iter_rows(values_only=True))
        assert filas[0][0] == "id" and filas[1][1] == "Bebida cola" and filas[1][3] == 17
        todo = " ".join(str(c) for h in wb.worksheets for f in h.iter_rows(values_only=True)
                        for c in f if c is not None)  # fmt: skip
        assert "CA-1" not in todo and "Liceo" not in todo
        ws.cell(row=2, column=5, value="1.950")
        ws.cell(row=2, column=6, value="si")
        ws.cell(row=2, column=7, value=2)
        ws.cell(row=2, column=8, value="20:1900")
        ws.append([999999, "intruso", "", 1, 5])  # una fila de otra solicitud no rompe el resto
        buf = io.BytesIO()
        wb.save(buf)
        respuestas, errores = lt.leer_xlsx_respuesta(s, mundo["emp"], sol.id, buf.getvalue())
        assert len(respuestas) == 1 and len(errores) == 1 and "999999" in errores[0]
        r = next(iter(respuestas.values()))
        assert r.precio == 1950 and r.disponible is True and r.plazo_dias == 2
        assert r.tramos == [{"desde": 20, "precio": 1900}]
        lt.registrar_respuesta(s, mundo["emp"], u, sol.id, respuestas, None)
        s.commit()
        assert s.get(ProveedorProducto, mundo["pp_t"]).precio_neto_actual == 1950


# ---------------------------------------------------------------------------
# Ofertas, reserva y cierre (§2.4, §2.5, §1.5)
# ---------------------------------------------------------------------------


def test_armar_ofertas_con_los_costos_del_lote(engine, mundo):
    with Session(engine) as s:
        u = _u(s, mundo)
        lote_id = _crear(s, mundo)
        ll = _linea(s, lote_id)
        # Fijar a mano el proveedor vencido: el lote respeta la elección al armar.
        lt.cambiar_proveedor(s, mundo["emp"], lote_id, ll.id, f"pp:{mundo['pp_v']}")
        s.commit()
        assert _linea(s, lote_id).proveedor_fijado
        r = lt.armar_ofertas(s, mundo["emp"], u, lote_id, s.get(Lote, lote_id).version)
        s.commit()
        assert r.armadas == 3 and r.sin_oferta == ["CA-3"]
        o1 = s.execute(select(OfertaPropia).where(OfertaPropia.ca_codigo == "CA-2")).scalar_one()
        linea = o1.lineas[0]
        assert linea["proveedor_producto_id"] == mundo["pp_v"]
        # Sin predicción: costo × (1 + 15 %) / cantidad = (2000/2,1 × 15 L × 1,15) / 10.
        assert linea["precio_unitario"] == pytest.approx(round(2000 / PACK * 15 * 1.15 / 10, 2))
        assert eq.trabajo_de(s, mundo["emp"], "CA-2").estado == eq.COTIZANDO


def test_armar_ofertas_usa_stock_propio_si_cubre(engine, mundo):
    with Session(engine) as s:
        op.guardar(s, mundo["emp"], None, producto_id=mundo["bebida"],
                   formato_id=mundo["f"]["botella 1,5 L"], stock=40,
                   proveedor_habitual_id=mundo["tramos"])  # 60 L  # fmt: skip
        s.commit()
        lote_id = _crear(s, mundo)
        ll = _linea(s, lote_id)
        assert ll.a_comprar_base == 0 and ll.en_stock_base == pytest.approx(33.9)
        assert s.execute(select(func.count()).select_from(SolicitudPrecio)).scalar_one() == 0
        lt.armar_ofertas(s, mundo["emp"], _u(s, mundo), lote_id, None)
        s.commit()
        o = s.execute(select(OfertaPropia).where(OfertaPropia.ca_codigo == "CA-1")).scalar_one()
        assert o.lineas[0]["origen"] == "stock"


def _listas(s, mundo, codigos=("CA-1", "CA-2", "CA-3")):
    for c in codigos:
        t = eq.trabajo_de(s, mundo["emp"], c)
        eq.cambiar_estado(s, mundo["emp"], _u(s, mundo), c, eq.LISTA, t.version if t else 0)
    s.commit()


def test_reserva_no_supera_el_stock_y_se_libera_al_cerrar(engine, mundo):
    with Session(engine) as s:
        u = _u(s, mundo)
        ep, _ = op.guardar(s, mundo["emp"], None, producto_id=mundo["bebida"],
                           formato_id=mundo["f"]["botella 1,5 L"], stock=4)  # fmt: skip
        s.commit()
        lote_id = _crear(s, mundo)
        assert _linea(s, lote_id).en_stock_base == pytest.approx(6.0)
        with pytest.raises(lt.ErrorLote, match="Faltan 3 CA"):
            lt.cambiar_estado(s, mundo["emp"], u, lote_id, lt.OFERTADO, None)
        s.rollback()
        _listas(s, mundo)
        # Alguien entregó 3 botellas entre medio: quedan 1,5 L y se reserva solo eso.
        op.ajustar_stock(s, mundo["emp"], ep.id, -3, "entrega", "CA-99", u.id)
        s.commit()
        lote = s.get(Lote, lote_id)
        avisos = lt.cambiar_estado(s, mundo["emp"], u, lote_id, lt.OFERTADO, lote.version)
        s.commit()
        ep = s.get(EmpresaProducto, ep.id)
        assert ep.reservado == pytest.approx(1.0) and ep.reservado <= ep.stock
        assert s.get(Lote, lote_id).estado == lt.OFERTADO
        movs = s.execute(
            select(EmpresaProductoMovimiento).where(
                EmpresaProductoMovimiento.referencia == f"lote:{lote_id}")
        ).scalars().all()  # fmt: skip
        assert [(m.motivo, m.delta) for m in movs] == [("reserva", 1.0)]
        assert avisos == [] or "reservó" in avisos[0]
        # Ofertado: el agregado y la fecha límite ya no se mueven.
        with pytest.raises(lt.ErrorLote):
            lt.recalcular_lote(s, mundo["emp"], lote_id)
        s.rollback()
        lt.cambiar_estado(s, mundo["emp"], u, lote_id, lt.CERRADO, None)
        s.commit()
        ep = s.get(EmpresaProducto, ep.id)
        assert ep.reservado == 0 and ep.stock == pytest.approx(1.0)
        assert op.liberar(s, mundo["emp"], f"lote:{lote_id}", u.id) == 0  # idempotente
        with pytest.raises(lt.ErrorLote, match="no se reabre"):
            lt.cambiar_estado(s, mundo["emp"], u, lote_id, lt.ABIERTO, None)


def test_volver_atras_desde_ofertado_libera_la_reserva(engine, mundo):
    with Session(engine) as s:
        u = _u(s, mundo)
        ep, _ = op.guardar(s, mundo["emp"], None, producto_id=mundo["bebida"],
                           formato_id=mundo["f"]["botella 1,5 L"], stock=4)  # fmt: skip
        s.commit()
        lote_id = _crear(s, mundo)
        _listas(s, mundo)
        lt.cambiar_estado(s, mundo["emp"], u, lote_id, lt.OFERTADO, None)
        s.commit()
        assert s.get(EmpresaProducto, ep.id).reservado == pytest.approx(4.0)
        lt.cambiar_estado(s, mundo["emp"], u, lote_id, lt.CON_PRECIOS, None)
        s.commit()
        assert s.get(EmpresaProducto, ep.id).reservado == 0


def test_version_del_lote_y_de_la_solicitud(engine, mundo):
    with Session(engine) as s:
        u = _u(s, mundo)
        lote_id = _crear(s, mundo)
        vieja = s.get(Lote, lote_id).version
        lt.quitar_ca(s, mundo["emp"], u, lote_id, "CA-3", vieja)
        s.commit()
        with pytest.raises(lt.ConflictoLote):
            lt.cambiar_estado(s, mundo["emp"], u, lote_id, lt.CERRADO, vieja)
        s.rollback()
        sol = _solicitud(s, lote_id)
        lt.marcar_enviada(s, mundo["emp"], u, sol.id, "correo", sol.version)
        s.commit()
        with pytest.raises(lt.ConflictoLote):
            lt.marcar_sin_respuesta(s, mundo["emp"], sol.id, sol.version - 1)
        s.rollback()
        lt.marcar_sin_respuesta(s, mundo["emp"], sol.id, sol.version)
        s.commit()
        assert s.get(Lote, lote_id).estado == lt.CON_PRECIOS


def test_ownership_en_logica(engine, mundo):
    with Session(engine) as s:
        lote_id = _crear(s, mundo)
        sol = _solicitud(s, lote_id)
        ana = _u(s, mundo, "ana")
        with pytest.raises(lt.LoteAjeno):
            lt.vista(s, mundo["otra"], lote_id)
        with pytest.raises(lt.LoteAjeno):
            lt.marcar_enviada(s, mundo["otra"], ana, sol.id, "correo", None)
        with pytest.raises(lt.LoteAjeno):
            lt.xlsx_solicitud(s, mundo["otra"], sol.id)
        ll = _linea(s, lote_id)
        with pytest.raises(lt.LoteAjeno):
            lt.cambiar_proveedor(s, mundo["emp"], lote_id, ll.id, f"pp:{mundo['pp_ajeno']}")
        with pytest.raises(lt.LoteAjeno):
            lt.cambiar_proveedor(s, mundo["emp"], lote_id, ll.id, f"prov:{mundo['ajeno']}")


def test_proveedor_sin_precio_recibe_la_solicitud(engine, mundo):
    with Session(engine) as s:
        nuevo = Proveedor(empresa_id=mundo["emp"], nombre="Nuevo SpA", rubros=[], activo=True)
        s.add(nuevo)
        s.commit()
        lote_id = _crear(s, mundo)
        ll = _linea(s, lote_id)
        lt.cambiar_proveedor(s, mundo["emp"], lote_id, ll.id, f"prov:{nuevo.id}")
        s.commit()
        ll = _linea(s, lote_id)
        assert ll.proveedor_sugerido_id == nuevo.id and ll.costo_unitario_sugerido is None
        sols = s.execute(select(SolicitudPrecio)).scalars().all()
        assert [x.proveedor_id for x in sols] == [nuevo.id]  # el borrador de «Tramos» se borró
        # Volver a «automático» devuelve el mejor costo vigente.
        lt.cambiar_proveedor(s, mundo["emp"], lote_id, ll.id, "")
        s.commit()
        assert _linea(s, lote_id).proveedor_sugerido_id == mundo["tramos"]


def test_en_trabajo_y_resumen_hoy(engine, mundo):
    with Session(engine) as s:
        u = _u(s, mundo)
        lote_id = _crear(s, mundo, ["CA-1", "CA-2"])
        eq.cambiar_estado(s, mundo["emp"], u, "CA-3", eq.PRIORIZADA, 0)
        s.commit()
        t = lt.en_trabajo(s, mundo["emp"], ahora_utc())
        assert [lo.id for lo, _ in t.lotes] == [lote_id]
        assert [c["codigo"] for c in t.sin_lote] == ["CA-3"]
        assert t.cierran_24h == []
        r = lt.resumen_hoy(s, mundo["emp"])
        assert r["lotes"] == 1 and r["cas"] == 2 and r["cierre"] == s.get(CompraAgil, "CA-2").fecha_cierre
        assert lt.resumen_hoy(s, mundo["otra"])["lotes"] == 0


def test_purgar_lotes_cerrados_viejos(engine, mundo):
    with Session(engine) as s:
        lote_id = _crear(s, mundo)
        lt.cambiar_estado(s, mundo["emp"], _u(s, mundo), lote_id, lt.CERRADO, None)
        s.commit()
        assert lt.purgar_cerrados(s, ahora_utc()) == 0
        assert lt.purgar_cerrados(s, ahora_utc() + timedelta(days=731)) == 1
        s.commit()
        for tabla in (Lote, LoteCa, LoteLinea, SolicitudPrecio, SolicitudLinea):
            assert s.execute(select(func.count()).select_from(tabla)).scalar_one() == 0


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------


def test_mesa_cotizar_seleccionadas_crea_el_lote(client, settings, mundo):
    cookies, _, csrf = _auth(settings, mundo["boris"])
    client.cookies.update(cookies)
    r = client.get("/mesa")
    assert r.status_code == 200
    assert 'form="form-lote"' in r.text and 'id="form-lote"' in r.text
    r = client.post("/lotes", data={"codigos": ["CA-1", "CA-2"], "csrf_token": csrf},
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and re.match(r"^/lotes/\d+\?mensaje=", r.headers["location"])
    lote_id = int(r.headers["location"].split("/")[2].split("?")[0])
    r = client.get(f"/lotes/{lote_id}")
    assert r.status_code == 200
    assert 'data-lote="agregado"' in r.text and "Tramos Ltda" in r.text
    assert 'hx-select="#lote-contenido"' in r.text
    # La mesa marca las CA que ya están en un lote y ofrece «agregar al lote actual».
    r = client.get("/mesa")
    assert f'href="/lotes/{lote_id}"' in r.text and "Agregar a «" in r.text
    # Otra vez CA-1 en un lote nuevo → 409 con el nombre del lote.
    r = client.post("/lotes", data={"codigos": ["CA-1"], "csrf_token": csrf})
    assert r.status_code == 409 and "ya está en el lote" in r.text
    # Agregar CA-3 al lote actual.
    r = client.post("/lotes", data={"codigos": ["CA-3"], "lote_id": str(lote_id),
                                    "csrf_token": csrf}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303


def test_rutas_csrf_y_ownership(client, settings, mundo, engine):
    with Session(engine) as s:
        lote_id = _crear(s, mundo)
        sid = _solicitud(s, lote_id).id
    cookies, _, csrf = _auth(settings, mundo["boris"])
    client.cookies.update(cookies)
    assert client.post("/lotes", data={"codigos": ["CA-1"]}).status_code == 403  # sin CSRF
    cookies_a, _, csrf_a = _auth(settings, mundo["ana"])
    client.cookies.update(cookies_a)
    assert client.get(f"/lotes/{lote_id}").status_code == 404
    assert client.get(f"/lotes/{lote_id}/solicitudes/{sid}/planilla.xlsx").status_code == 404
    r = client.post(f"/lotes/{lote_id}/cas/CA-1/quitar", data={"csrf_token": csrf_a})
    assert r.status_code == 404
    r = client.post(f"/lotes/{lote_id}/solicitudes/{sid}/enviada",
                    data={"canal": "correo", "csrf_token": csrf_a})  # fmt: skip
    assert r.status_code == 404
    assert "Distribuidora" not in client.get("/trabajo").text


def test_rutas_flujo_solicitud_y_proveedor_htmx(client, settings, mundo, engine):
    with Session(engine) as s:
        lote_id = _crear(s, mundo)
        sol = _solicitud(s, lote_id)
        sid, version = sol.id, sol.version
        llid = _linea(s, lote_id).id
        slid = s.execute(select(SolicitudLinea)).scalar_one().id
    cookies, headers, csrf = _auth(settings, mundo["boris"])
    client.cookies.update(cookies)
    r = client.get(f"/lotes/{lote_id}/solicitudes/{sid}/planilla.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    r = client.post(f"/lotes/{lote_id}/solicitudes/{sid}/enviada",
                    data={"canal": "whatsapp", "version": str(version), "csrf_token": csrf},
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    # Versión vieja → 409 con la pantalla del lote.
    r = client.post(f"/lotes/{lote_id}/solicitudes/{sid}/respuesta",
                    data={f"precio_{slid}": "1800", "version": str(version), "csrf_token": csrf})  # fmt: skip
    assert r.status_code == 409 and "cambió este lote" in r.text
    r = client.post(f"/lotes/{lote_id}/solicitudes/{sid}/respuesta",
                    data={f"precio_{slid}": "abc", "version": str(version + 1), "csrf_token": csrf})  # fmt: skip
    assert r.status_code == 422
    r = client.post(f"/lotes/{lote_id}/solicitudes/{sid}/respuesta",
                    data={f"precio_{slid}": "1.800", f"disp_{slid}": "si", f"plazo_{slid}": "2",
                          "version": str(version + 1), "csrf_token": csrf},
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    r = client.get(f"/lotes/{lote_id}")
    assert "Con precios" in r.text
    r = client.post(f"/lotes/{lote_id}/lineas/{llid}/proveedor",
                    data={"proveedor": f"pp:{mundo['pp_v']}", "csrf_token": csrf},
                    headers={**headers, "HX-Request": "true"})  # fmt: skip
    assert r.status_code == 200 and 'id="lote-contenido"' in r.text
    assert 'data-lote="vencido"' in r.text  # el elegido es un precio vencido: ámbar
    r = client.get("/trabajo")
    assert r.status_code == 200 and f'href="/lotes/{lote_id}"' in r.text
    assert client.get("/lotes").status_code == 200


def test_hoy_proveedores_y_ficha_muestran_precios_vencidos(client, settings, mundo, engine):
    with Session(engine) as s:
        _crear(s, mundo, ["CA-1"])
    cookies, _, _ = _auth(settings, mundo["boris"])
    client.cookies.update(cookies)
    r = client.get("/")
    assert 'data-hoy="lotes"' in r.text and 'data-hoy="vencidos"' in r.text
    assert "1 precio por actualizar" in r.text
    r = client.get("/proveedores?vencidos=1")
    assert "Vencido SA" in r.text and "Tramos Ltda" not in r.text
    r = client.get("/mesa/ca/CA-1/precio")  # la sección «Precio y mi oferta» de la ficha
    assert r.status_code == 200
    assert "precio de hace 30 días" in r.text  # el candidato vencido, en el selector
    nav = client.get("/mesa").text
    assert 'href="/trabajo"' in nav and "En trabajo" in nav


def test_admin_edita_vigencia_y_margen(client, settings, mundo, engine):
    with Session(engine) as s:
        admin = Usuario(email="admin@t.cl", nombre="admin", password_hash=hash_password(_PW),
                        rol="admin", empresa_id=None)  # fmt: skip
        s.add(admin)
        s.commit()
        admin_id = admin.id
    cookies, _, csrf = _auth(settings, admin_id)
    client.cookies.update(cookies)
    base = {"nombre": "Distribuidora Ñuble", "rut": "76123456-0", "regiones": ["16"],
            "activa": "1", "csrf_token": csrf}  # fmt: skip
    r = client.post(f"/admin/empresas/{mundo['emp']}",
                    data={**base, "dias_vigencia_precio": "0", "margen_objetivo": "20"})  # fmt: skip
    assert r.status_code == 422
    r = client.post(f"/admin/empresas/{mundo['emp']}",
                    data={**base, "dias_vigencia_precio": "10", "margen_objetivo": "20"},
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    with Session(engine) as s:
        e = s.get(Empresa, mundo["emp"])
        assert e.dias_vigencia_precio == 10 and e.margen_objetivo == pytest.approx(0.20)
