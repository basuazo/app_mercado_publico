"""F-dis-datos: datos de «Hoy» (te necesita, cifras, embudo, novedades), señales por CA, el panel
lateral de la Mesa y el refactor `ventas.condiciones_seguimiento`.

Todo offline (SQLite en memoria). `AHORA` es fijo: martes 10-nov-2026 15:00 UTC = 12:00 en Chile
(horario de verano, UTC−3). Semana de Chile: lunes 9 a domingo 15 de noviembre.

Mundo: empresa «Nuestra» (región 16, vigencia de precios 15 días) con Boris (admin) y Gonzalo; empresa
«Otra» (región 5) con Ana. Productos: arroz (precio al día), aceite (precio de hace 23 días), fideos
(precio viejo que no pide ninguna CA) y azúcar (sin precio).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.mercado import equipo as eq
from app.mercado import hoy
from app.mercado import ventas as vt
from app.mercado.panel_ca import FALTAN_LINEAS_ANEXO, SIN_EVALUACION, panel
from app.mercado.preferencias import vista_de
from app.mercado.proveedores import guardar_producto
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaAnexo,
    CaComentario,
    CaEvento,
    CaLinea,
    CaRequisito,
    CaTrabajo,
    CompraAgil,
    Empresa,
    EvaluacionCache,
    FamiliaImpuesto,
    ImpuestoEspecifico,
    Lote,
    LoteCa,
    LoteLinea,
    OfertaPropia,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    Usuario,
    Venta,
)

_PW = "contraseña-segura-test"
AHORA = datetime(2026, 11, 10, 15, 0)  # martes 12:00 en Chile
HOY = date(2026, 11, 10)


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
        nuestra = Empresa(nombre="Nuestra", rut="76123456-0", regiones_prioritarias=[16],
                          comunas_prioritarias=["Chillán"], activa=True)  # fmt: skip
        otra = Empresa(nombre="Otra", rut="77777777-7", regiones_prioritarias=[5],
                       comunas_prioritarias=[], activa=True)  # fmt: skip
        s.add_all([nuestra, otra])
        s.flush()
        users = {}
        for clave, emp, rol in (
            ("boris", nuestra.id, RolUsuario.ADMIN),
            ("gonzalo", nuestra.id, RolUsuario.USUARIO),
            ("ana", otra.id, RolUsuario.USUARIO),
            ("sinemp", None, RolUsuario.ADMIN),
        ):
            u = Usuario(email=f"{clave}@t.cl", nombre=clave.capitalize(), rol=rol,
                        password_hash=hash_password(_PW), empresa_id=emp)  # fmt: skip
            s.add(u)
            s.flush()
            users[clave] = u.id
        prods = {}
        for nombre in ("Arroz", "Aceite", "Fideos", "Azúcar"):
            p = ProductoCanonico(nombre=nombre, unidad_base="kg", activo=True)
            s.add(p)
            s.flush()
            f = ProductoFormato(producto_id=p.id, nombre="kg", factor_base=1.0)
            s.add(f)
            s.flush()
            prods[nombre] = (p.id, f.id)
        molino = Proveedor(empresa_id=nuestra.id, nombre="Molino", rubros=[], activo=True)
        s.add(molino)
        s.flush()
        for nombre, dias, precio in (("Arroz", 2, 1000), ("Aceite", 23, 3150), ("Fideos", 40, 900)):
            pid, fid = prods[nombre]
            guardar_producto(
                s, molino, producto_texto=nombre, formato_texto="kg", precio_neto=precio,
                vigente_desde=HOY - timedelta(days=dias), factor_base=1.0, unidad_base="kg",
                producto_id=pid, formato_id=fid,
            )  # fmt: skip
        s.commit()
        return {"emp": nuestra.id, "otra": otra.id, "molino": molino.id, "prods": prods, **users}


def _ca(s, codigo, cierre, *, region=16, monto=100_000.0, publicada=None, n_documentos=0,
        estado="publicada", direccion="Calle 1, Chillán"):  # fmt: skip
    ca = CompraAgil(codigo=codigo, nombre=f"Nombre {codigo}", estado=estado, region=region,
                    organismo_nombre=f"Organismo {codigo}", fecha_cierre=cierre,
                    monto_disponible_clp=monto, fecha_publicacion=publicada,
                    n_documentos=n_documentos, direccion_entrega=direccion)  # fmt: skip
    s.add(ca)
    s.flush()
    return ca


def _trabajo(s, emp, codigo, estado, **kw):
    t = CaTrabajo(empresa_id=emp, ca_codigo=codigo, estado=estado, version=1, **kw)
    s.add(t)
    s.flush()
    return t


def _linea(s, codigo, n, producto_id=None, *, estado="ok", cantidad=10.0, anexo_id=None,
           origen="api"):  # fmt: skip
    ln = CaLinea(ca_codigo=codigo, n=n, producto_id=producto_id, cantidad=cantidad,
                 cantidad_base=cantidad, texto_origen=f"línea {n}", estado=estado,
                 anexo_id=anexo_id, origen=origen)  # fmt: skip
    s.add(ln)
    s.flush()
    return ln


def _lote(s, emp, nombre, codigos, *, estado="abierto", sin_costo=(), limite=None):
    lote = Lote(empresa_id=emp, nombre=nombre, estado=estado, version=1, fecha_limite=limite)
    s.add(lote)
    s.flush()
    for c in codigos:
        s.add(LoteCa(lote_id=lote.id, ca_codigo=c))
    for pid, fid in sin_costo:
        s.add(LoteLinea(lote_id=lote.id, producto_id=pid, formato_id=fid, cantidad_base=5.0,
                        a_comprar_base=5.0, costo_unitario_sugerido=None, detalle=[]))  # fmt: skip
    s.flush()
    return lote


def _evento_enviada(s, emp, codigo, uid, cuando):
    s.add(CaEvento(empresa_id=emp, ca_codigo=codigo, usuario_id=uid, tipo=eq.EV_ESTADO,
                   datos={"de": "lista", "a": "enviada"}, creado_en=cuando))  # fmt: skip
    s.flush()


# ---------------------------------------------------------------------------
# «Te necesita»
# ---------------------------------------------------------------------------


def test_cierres_hoy_urgente_y_manana_atencion_con_lo_que_falta(engine, mundo):
    emp = mundo["emp"]
    arroz = mundo["prods"]["Azúcar"]
    with Session(engine) as s:
        _ca(s, "CA-HOY", AHORA + timedelta(hours=3))  # 15:00 en Chile, hoy
        _ca(s, "CA-MAN", AHORA + timedelta(hours=22))  # mañana 10:00 en Chile
        _ca(s, "CA-LEJ", AHORA + timedelta(days=3))  # fuera de las 24 h
        for c in ("CA-HOY", "CA-MAN", "CA-LEJ"):
            _trabajo(s, emp, c, eq.COTIZANDO)
        _lote(s, emp, "L14", ["CA-HOY"], sin_costo=[arroz])
        s.add(CaRequisito(empresa_id=emp, ca_codigo="CA-MAN", texto="Declaración jurada",
                          obligatorio=True, cumplido=False, origen="manual"))  # fmt: skip
        s.commit()
        ps = [p for p in hoy.te_necesita(s, emp, mundo["boris"], [16], AHORA) if p.tipo == "cierres"]
    assert [p.ca_codigo for p in ps] == ["CA-HOY", "CA-MAN"]
    h, m = ps
    assert (h.nivel, h.senal) == ("urgente", "Cierra hoy 15:00")
    assert "lote L14, falta precio de 1 producto" in h.detalle and "oferta sin armar" in h.detalle
    assert h.accion == "Terminar oferta" and h.url.startswith("/lotes/")
    assert (m.nivel, m.senal) == ("atencion", "Cierra mañana 10:00")
    assert "1 requisito obligatorio pendiente" in m.detalle and m.accion == "Armar oferta"


def test_precios_vencidos_solo_de_productos_pedidos_y_falta_costo_por_lote(engine, mundo):
    emp = mundo["emp"]
    aceite, azucar = mundo["prods"]["Aceite"], mundo["prods"]["Azúcar"]
    with Session(engine) as s:
        _ca(s, "CA-1", AHORA + timedelta(days=5))
        _ca(s, "CA-2", AHORA + timedelta(days=6))
        _trabajo(s, emp, "CA-1", eq.COTIZANDO)
        _linea(s, "CA-1", 1, aceite[0])
        _linea(s, "CA-2", 1, aceite[0])
        _linea(s, "CA-2", 2, mundo["prods"]["Arroz"][0])  # arroz al día: no entra
        _lote(s, emp, "L7", ["CA-2"], sin_costo=[azucar])
        s.commit()
        ps = [p for p in hoy.te_necesita(s, emp, mundo["boris"], [16], AHORA) if p.tipo == "precios"]
    vencidos = [p for p in ps if p.senal.startswith("Precio desactualizado")]
    assert len(vencidos) == 1  # fideos está vencido pero ninguna CA lo pide
    v = vencidos[0]
    assert v.senal == "Precio desactualizado · 23 días" and v.titulo == "Aceite"
    assert "Molino · último costo neto $3.150 · lo piden 2 CA" in v.detalle
    assert "vigencia de la empresa: 15 días" in v.detalle
    assert v.accion == "Pedir precio" and v.vence == AHORA + timedelta(days=5)
    falta = [p for p in ps if p.senal.startswith("Falta costo")]
    assert len(falta) == 1 and falta[0].senal == "Falta costo · 1 línea"
    assert falta[0].titulo == "Azúcar" and falta[0].detalle.startswith("Lote L7")


def test_vigencia_configurada_de_la_empresa_manda(engine, mundo):
    emp = mundo["emp"]
    with Session(engine) as s:
        s.get(Empresa, emp).dias_vigencia_precio = 30
        _ca(s, "CA-1", AHORA + timedelta(days=5))
        _trabajo(s, emp, "CA-1", eq.PRIORIZADA)
        _linea(s, "CA-1", 1, mundo["prods"]["Aceite"][0])
        s.commit()
        ps = hoy.te_necesita(s, emp, mundo["boris"], [16], AHORA)
    assert not [p for p in ps if p.tipo == "precios"]  # 23 días < 30


def test_lineas_y_anexos_agrupados_una_sola_vez(engine, mundo):
    emp = mundo["emp"]
    with Session(engine) as s:
        _ca(s, "CA-L", AHORA + timedelta(days=2))
        anexo = CaAnexo(ca_codigo="CA-L", mime="application/pdf", tamano=10, sha256="a" * 64)
        s.add(anexo)
        s.flush()
        for n in (1000, 1001):
            _linea(s, "CA-L", n, estado="pendiente", anexo_id=anexo.id, origen="anexo")
        _linea(s, "CA-L", 1002, estado="dudosa")
        _ca(s, "CA-A1", AHORA + timedelta(days=1), n_documentos=2)
        _ca(s, "CA-A2", AHORA + timedelta(days=4), n_documentos=1)
        _ca(s, "CA-A3", AHORA + timedelta(days=4), n_documentos=1, region=5)  # otro territorio
        s.commit()
        ps = hoy.te_necesita(s, emp, mundo["boris"], [16], AHORA)
    lineas = [p for p in ps if p.tipo == "lineas"]
    anexos = [p for p in ps if p.tipo == "anexos"]
    assert len(lineas) == 1 and lineas[0].senal == "3 líneas por revisar"
    assert lineas[0].titulo == "Líneas leídas de 1 anexo" and lineas[0].url == "/catalogo/revision"
    assert len(anexos) == 1 and anexos[0].senal == "2 anexos sin leer"
    assert anexos[0].detalle == "Nombre CA-A1 · Nombre CA-A2"
    assert anexos[0].vence == AHORA + timedelta(days=1)
    assert hoy.conteo_por_tipo(ps) == {"cierres": 0, "precios": 0, "lineas": 1, "anexos": 1, "ventas": 0}


def test_ventas_desde_condiciones_seguimiento(engine, mundo):
    emp = mundo["emp"]
    with Session(engine) as s:
        _ca(s, "CA-V1", AHORA - timedelta(days=10), estado="adjudicada")
        _ca(s, "CA-V2", AHORA - timedelta(days=20), estado="adjudicada")
        _ca(s, "CA-V3", AHORA - timedelta(days=20), estado="adjudicada")
        s.add_all([
            Venta(empresa_id=emp, ca_codigo="CA-V1", estado="adjudicada", version=1,
                  creado_en=AHORA - timedelta(days=5), actualizado_en=AHORA),
            Venta(empresa_id=emp, ca_codigo="CA-V2", estado="facturada", version=1,
                  pago_esperado_en=HOY - timedelta(days=3), creado_en=AHORA, actualizado_en=AHORA),
            Venta(empresa_id=emp, ca_codigo="CA-V3", estado="oc_recibida", version=1,
                  entrega_fecha_comprometida=HOY, creado_en=AHORA, actualizado_en=AHORA),
        ])  # fmt: skip
        s.commit()
        ps = [p for p in hoy.te_necesita(s, emp, mundo["boris"], [16], AHORA) if p.tipo == "ventas"]
    por_ca = {p.ca_codigo: p for p in ps}
    assert por_ca["CA-V1"].senal == "OC sin registrar · 5 días"
    assert por_ca["CA-V1"].accion == "Registrar OC" and por_ca["CA-V1"].url.startswith("/ventas/")
    assert por_ca["CA-V2"].senal == "Pago atrasado · 3 días"
    assert (por_ca["CA-V3"].senal, por_ca["CA-V3"].nivel) == ("Entrega hoy", "urgente")
    assert ps[0].ca_codigo == "CA-V3"  # urgente primero


def test_orden_urgente_luego_vence_y_none_al_final():
    def p(tipo, nivel, vence):
        return hoy.Pendiente(tipo, nivel, "s", "t", "d", "a", "/", None, vence)

    t0 = AHORA
    lista = [
        p("lineas", "atencion", None),
        p("precios", "atencion", t0 + timedelta(days=2)),
        p("ventas", "urgente", None),
        p("cierres", "urgente", t0 + timedelta(hours=1)),
        p("anexos", "atencion", t0 + timedelta(days=1)),
        p("cierres", "atencion", None),
    ]
    orden = [(x.tipo, x.nivel) for x in hoy.ordenar(lista)]
    assert orden == [
        ("cierres", "urgente"), ("ventas", "urgente"), ("anexos", "atencion"),
        ("precios", "atencion"), ("cierres", "atencion"), ("lineas", "atencion"),
    ]  # fmt: skip


def test_otra_empresa_no_ve_nada_de_la_primera_y_sin_datos_lista_vacia(engine, mundo):
    emp = mundo["emp"]
    with Session(engine) as s:
        _ca(s, "CA-1", AHORA + timedelta(hours=3), n_documentos=1)
        _trabajo(s, emp, "CA-1", eq.COTIZANDO)
        _linea(s, "CA-1", 1, mundo["prods"]["Aceite"][0])
        _lote(s, emp, "L1", ["CA-1"], sin_costo=[mundo["prods"]["Azúcar"]])
        s.add(Venta(empresa_id=emp, ca_codigo="CA-1", estado="adjudicada", version=1,
                    creado_en=AHORA - timedelta(days=9), actualizado_en=AHORA))  # fmt: skip
        s.commit()
        assert hoy.te_necesita(s, emp, mundo["boris"], [16], AHORA)  # la primera sí ve
        assert hoy.te_necesita(s, mundo["otra"], mundo["ana"], [5], AHORA) == []
        # Aun mirando el mismo territorio, nada comercial de la primera (cierres, precios, ventas).
        ajenos = hoy.te_necesita(s, mundo["otra"], mundo["ana"], [16], AHORA)
        assert {p.tipo for p in ajenos} <= {"lineas", "anexos"}
        nueva = Empresa(nombre="Vacía", regiones_prioritarias=[], comunas_prioritarias=[], activa=True)
        s.add(nueva)
        s.commit()
        assert hoy.te_necesita(s, nueva.id, mundo["boris"], [], AHORA) == []


# ---------------------------------------------------------------------------
# Refactor: `condiciones_seguimiento` y las alarmas emiten lo mismo que antes
# ---------------------------------------------------------------------------


def _alarmas_antes(s, empresa_id, ahora):
    """Copia de la lógica de `generar_alarmas_seguimiento` ANTES del refactor (sin emitir)."""
    from app.mercado import requisitos as rq

    hoy_c = vt.hoy_de(ahora)
    out = []
    for (codigo,) in s.execute(
        select(CaTrabajo.ca_codigo)
        .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
        .where(
            CaTrabajo.empresa_id == empresa_id,
            CaTrabajo.estado.in_((eq.COTIZANDO, eq.LISTA)),
            CompraAgil.fecha_cierre > ahora,
            CompraAgil.fecha_cierre <= ahora + timedelta(hours=vt.HORAS_REQUISITOS),
            select(CaRequisito.id)
            .where(
                CaRequisito.empresa_id == CaTrabajo.empresa_id,
                CaRequisito.ca_codigo == CaTrabajo.ca_codigo,
                CaRequisito.obligatorio.is_(True),
                CaRequisito.cumplido.is_(False),
            )
            .exists(),
        )
    ):
        out.append((eq.EV_REQ_PENDIENTES, codigo, {"pendientes": len(rq.pendientes(s, empresa_id, codigo))}))
    for v in s.execute(
        select(Venta).where(Venta.empresa_id == empresa_id, Venta.estado.in_(vt.EN_CURSO))
    ).scalars():
        datos = {"venta_id": v.id, "estado": v.estado}
        if v.estado == vt.ADJUDICADA and v.creado_en < ahora - timedelta(days=vt.DIAS_OC_SIN_REGISTRAR):
            out.append((eq.EV_OC_SIN_REGISTRAR, v.ca_codigo, datos))
        if (
            v.estado in vt.PENDIENTES_DE_ENTREGA
            and v.entrega_fecha_comprometida is not None
            and v.entrega_fecha_comprometida <= hoy_c + timedelta(days=vt.DIAS_ENTREGA_VENCE)
        ):
            out.append((eq.EV_ENTREGA_VENCE, v.ca_codigo, datos))
        if v.estado == vt.ENTREGADA:
            entregada = v.entrega_fecha_real or vt.hoy_de(v.actualizado_en)
            if entregada < hoy_c - timedelta(days=vt.DIAS_FACTURA_PENDIENTE):
                out.append((eq.EV_FACTURA_PENDIENTE, v.ca_codigo, datos))
        if v.estado == vt.FACTURADA and v.pago_esperado_en is not None and v.pago_esperado_en < hoy_c:
            out.append((eq.EV_PAGO_ATRASADO, v.ca_codigo, datos))
    return out


def test_regresion_alarmas_de_seguimiento_emiten_lo_mismo(engine, mundo):
    emp = mundo["emp"]
    with Session(engine) as s:
        _ca(s, "CA-R", AHORA + timedelta(hours=5))
        _trabajo(s, emp, "CA-R", eq.LISTA)
        s.add(CaRequisito(empresa_id=emp, ca_codigo="CA-R", texto="Boleta", obligatorio=True,
                          cumplido=False, origen="manual"))  # fmt: skip
        s.add(CaRequisito(empresa_id=emp, ca_codigo="CA-R", texto="Ficha", obligatorio=True,
                          cumplido=False, origen="manual"))  # fmt: skip
        casos = [
            ("adjudicada", {"creado_en": AHORA - timedelta(days=4)}),
            ("comprando", {"entrega_fecha_comprometida": HOY + timedelta(days=1)}),
            ("entregada", {"entrega_fecha_real": HOY - timedelta(days=8)}),
            ("facturada", {"pago_esperado_en": HOY - timedelta(days=1)}),
            ("pagada", {"pago_esperado_en": HOY - timedelta(days=1)}),  # no está en curso
            ("adjudicada", {"creado_en": AHORA - timedelta(days=1)}),  # aún no
        ]
        for i, (estado, extra) in enumerate(casos):
            _ca(s, f"CA-S{i}", AHORA - timedelta(days=30), estado="adjudicada")
            datos = {"creado_en": AHORA - timedelta(days=1), "actualizado_en": AHORA, **extra}
            s.add(Venta(empresa_id=emp, ca_codigo=f"CA-S{i}", estado=estado, version=1, **datos))
        s.commit()
        esperado = _alarmas_antes(s, emp, AHORA)
        assert vt.condiciones_seguimiento(s, emp, AHORA) == esperado
        cuenta = vt.generar_alarmas_seguimiento(s, emp, AHORA)
        s.commit()
        emitidos = sorted(
            (e.tipo, e.ca_codigo, e.datos)
            for e in s.execute(select(CaEvento).where(CaEvento.usuario_id.is_(None))).scalars()
        )
        assert emitidos == sorted(esperado)
        assert cuenta == {
            eq.EV_REQ_PENDIENTES: 1, eq.EV_OC_SIN_REGISTRAR: 1, eq.EV_ENTREGA_VENCE: 1,
            eq.EV_FACTURA_PENDIENTE: 1, eq.EV_PAGO_ATRASADO: 1,
        }  # fmt: skip
        # Idempotente: una segunda corrida no emite nada nuevo.
        assert sum(vt.generar_alarmas_seguimiento(s, emp, AHORA).values()) == 0


# ---------------------------------------------------------------------------
# Cifras
# ---------------------------------------------------------------------------


def _por_clave(cs):
    return {c.clave: c for c in cs}


def test_cifras_hoy_semana_y_mes(engine, mundo):
    emp, boris = mundo["emp"], mundo["boris"]
    with Session(engine) as s:
        _ca(s, "P1", AHORA + timedelta(days=20), monto=1_000_000, publicada=AHORA - timedelta(hours=1))
        _ca(s, "P2", AHORA + timedelta(days=2), monto=500_000, publicada=AHORA - timedelta(days=1))
        _ca(s, "P3", AHORA + timedelta(hours=4), monto=200_000, publicada=AHORA - timedelta(days=3))
        _ca(s, "X", AHORA + timedelta(days=2), monto=9_000_000, publicada=AHORA)  # sin trabajo
        _trabajo(s, emp, "P1", eq.PRIORIZADA)
        _trabajo(s, emp, "P2", eq.COTIZANDO)
        _trabajo(s, emp, "P3", eq.LISTA)
        _ca(s, "E1", AHORA - timedelta(days=1), estado="cerrada")
        _trabajo(s, emp, "E1", eq.ENVIADA, resultado=eq.GANADA)
        _evento_enviada(s, emp, "E1", boris, AHORA - timedelta(hours=2))
        _ca(s, "V1", AHORA - timedelta(days=9), estado="adjudicada")
        o = OfertaPropia(ca_codigo="V1", empresa_id=emp, lineas=[], total_neto=600_000, version=1,
                         margen_realizado=90_000)  # fmt: skip
        s.add(o)
        s.flush()
        s.add(Venta(empresa_id=emp, ca_codigo="V1", oferta_id=o.id, estado="oc_recibida", version=1,
                    oc_monto=600_000, creado_en=datetime(2026, 11, 3, 15), actualizado_en=AHORA))  # fmt: skip
        _ca(s, "V2", AHORA - timedelta(days=9), estado="adjudicada")
        s.add(Venta(empresa_id=emp, ca_codigo="V2", estado="anulada", version=1, oc_monto=1e6,
                    creado_en=datetime(2026, 11, 4, 15), actualizado_en=AHORA))  # fmt: skip
        s.commit()
        h = _por_clave(hoy.cifras(s, emp, [16], "hoy", AHORA))
        w = _por_clave(hoy.cifras(s, emp, [16], "semana", AHORA))
        m = _por_clave(hoy.cifras(s, emp, [16], "mes", AHORA))
    assert (h["en_juego"].valor, h["en_juego"].nota) == (1_700_000, "3 CA priorizadas")
    assert (w["en_juego"].valor, w["en_juego"].nota) == (700_000, "2 CA priorizadas")
    assert w["en_juego"].etiqueta == "En juego esta semana"
    assert m["en_juego"].etiqueta == "Adjudicado en noviembre"
    assert (m["en_juego"].valor, m["en_juego"].nota) == (600_000, "1 OC · margen 15,0 %")
    assert (h["nuevas"].valor, h["nuevas"].nota) == (2, "vs. ayer")  # P1 y X
    assert h["nuevas"].delta == 1.0  # ayer solo P2: +100 %
    assert (h["cierran"].valor, h["cierran"].nota) == (1, "0 sin oferta lista")  # P3 lista
    assert (w["cierran"].valor, w["cierran"].nota) == (2, "1 sin oferta lista")
    assert (m["cierran"].valor, m["cierran"].nota) == (3, "2 sin oferta lista")
    assert (h["enviadas"].valor, h["enviadas"].nota) == (1, "1 adjudicada")
    assert m["nuevas"].nota == "vs. octubre" and m["nuevas"].delta is None  # octubre sin CA


def test_cifras_delta_none_si_el_anterior_es_cero_y_periodo_invalido(engine, mundo):
    with Session(engine) as s:
        _ca(s, "N1", AHORA + timedelta(days=3), publicada=AHORA - timedelta(hours=2))
        s.commit()
        c = _por_clave(hoy.cifras(s, mundo["emp"], [16], "semana", AHORA))
        assert (c["nuevas"].valor, c["nuevas"].delta, c["nuevas"].nota) == (1, None, "vs. semana pasada")
        with pytest.raises(ValueError):
            hoy.cifras(s, mundo["emp"], [16], "anio", AHORA)


def test_tasa_de_adjudicacion_solo_con_cinco_resultados(engine, mundo):
    emp, boris = mundo["emp"], mundo["boris"]
    with Session(engine) as s:
        for i, res in enumerate([eq.GANADA, eq.GANADA, eq.PERDIDA, eq.PERDIDA]):
            _ca(s, f"T{i}", AHORA - timedelta(days=2), estado="cerrada")
            _trabajo(s, emp, f"T{i}", eq.ENVIADA, resultado=res)
            _evento_enviada(s, emp, f"T{i}", boris, datetime(2026, 11, 2, 15))
        s.commit()
        assert _por_clave(hoy.cifras(s, emp, [16], "mes", AHORA))["enviadas"].nota == "2 adjudicadas"
        _ca(s, "T9", AHORA - timedelta(days=2), estado="cerrada")
        _trabajo(s, emp, "T9", eq.ENVIADA, resultado=eq.PERDIDA)
        _evento_enviada(s, emp, "T9", boris, datetime(2026, 11, 5, 15))
        s.commit()
        c = _por_clave(hoy.cifras(s, emp, [16], "mes", AHORA))["enviadas"]
    assert (c.valor, c.nota) == (5, "tasa de adjudicación 40 %")


def test_rangos_en_hora_de_chile_con_cambios_de_horario():
    # Septiembre: el domingo 6-sep-2026 a las 00:00 (UTC−4) el reloj salta a 01:00 (UTC−3).
    assert hoy.rango("hoy", datetime(2026, 9, 6, 12)) == (
        datetime(2026, 9, 6, 4), datetime(2026, 9, 7, 3),  # día de 23 h
    )  # fmt: skip
    assert hoy.rango("hoy", datetime(2026, 9, 6, 12), anterior=True) == (
        datetime(2026, 9, 5, 4), datetime(2026, 9, 6, 4),
    )  # fmt: skip
    # Abril: el domingo 5-abr-2026 a las 00:00 (UTC−3) el reloj vuelve a las 23:00 del sábado.
    assert hoy.rango("hoy", datetime(2026, 4, 5, 3, 30)) == (
        datetime(2026, 4, 4, 3), datetime(2026, 4, 5, 4),  # el sábado dura 25 h
    )  # fmt: skip
    # Semana de lunes a domingo y mes calendario, en invierno (UTC−4).
    assert hoy.rango("semana", datetime(2026, 7, 15, 12)) == (
        datetime(2026, 7, 13, 4), datetime(2026, 7, 20, 4),
    )  # fmt: skip
    assert hoy.rango("mes", datetime(2026, 12, 31, 12)) == (
        datetime(2026, 12, 1, 3), datetime(2027, 1, 1, 3),
    )  # fmt: skip


def test_nuevas_cuenta_por_dia_de_chile_no_de_utc(engine, mundo):
    ahora = datetime(2026, 11, 7, 2, 0)  # viernes 6-nov 23:00 en Chile; en UTC ya es sábado
    with Session(engine) as s:
        _ca(s, "M1", ahora + timedelta(days=3), publicada=datetime(2026, 11, 7, 1, 0))  # vie 22:00
        _ca(s, "M2", ahora + timedelta(days=3), publicada=datetime(2026, 11, 6, 2, 30))  # jue 23:30
        _ca(s, "M3", ahora + timedelta(days=3), publicada=datetime(2026, 11, 6, 3, 30))  # vie 00:30
        s.commit()
        c = _por_clave(hoy.cifras(s, mundo["emp"], [16], "hoy", ahora))["nuevas"]
    assert (c.valor, c.delta) == (2, 1.0)  # M1 y M3 hoy; M2 ayer


def test_nuevas_en_el_cambio_de_horario_de_abril(engine, mundo):
    ahora = datetime(2026, 4, 5, 3, 45)  # sábado 4-abr 23:45 (UTC−4, la hora repetida)
    with Session(engine) as s:
        _ca(s, "A1", ahora + timedelta(days=3), publicada=datetime(2026, 4, 5, 3, 30))  # sáb 23:30
        _ca(s, "A2", ahora + timedelta(days=3), publicada=datetime(2026, 4, 5, 2, 30))  # sáb 23:30 (−3)
        _ca(s, "A3", ahora + timedelta(days=3), publicada=datetime(2026, 4, 5, 4, 10))  # dom 00:10
        s.commit()
        c = _por_clave(hoy.cifras(s, mundo["emp"], [16], "hoy", ahora))["nuevas"]
    assert c.valor == 2


# ---------------------------------------------------------------------------
# Embudo
# ---------------------------------------------------------------------------


def test_embudo(engine, mundo):
    emp, boris = mundo["emp"], mundo["boris"]
    with Session(engine) as s:
        _ca(s, "P1", AHORA + timedelta(days=5), monto=1_000_000)
        _ca(s, "C1", AHORA + timedelta(days=5), monto=300_000)
        _ca(s, "L1", AHORA + timedelta(hours=30), monto=50_000)  # mañana
        _ca(s, "L2", AHORA + timedelta(days=4), monto=70_000)
        _ca(s, "VIEJA", AHORA - timedelta(days=1), monto=5e6)  # priorizada pero ya cerró
        _trabajo(s, emp, "P1", eq.PRIORIZADA)
        _trabajo(s, emp, "VIEJA", eq.PRIORIZADA)
        _trabajo(s, emp, "C1", eq.COTIZANDO)
        _trabajo(s, emp, "L1", eq.LISTA)
        _trabajo(s, emp, "L2", eq.LISTA)
        _lote(s, emp, "L5", ["C1"], sin_costo=[mundo["prods"]["Azúcar"]])
        for c, res in (("E1", eq.GANADA), ("E2", eq.PERDIDA), ("E3", None)):
            _ca(s, c, AHORA - timedelta(hours=1), monto=10_000, estado="cerrada")
            _trabajo(s, emp, c, eq.ENVIADA, resultado=res)
            _evento_enviada(s, emp, c, boris, AHORA - timedelta(days=1))
        _ca(s, "E0", AHORA - timedelta(days=9), estado="cerrada")
        _trabajo(s, emp, "E0", eq.ENVIADA)
        _evento_enviada(s, emp, "E0", boris, AHORA - timedelta(days=8))  # semana pasada
        s.commit()
        e = {x.clave: x for x in hoy.embudo(s, emp, AHORA)}
    assert [x for x in e] == ["priorizada", "cotizando", "lista", "enviada"]
    assert (e["priorizada"].etiqueta, e["priorizada"].n, e["priorizada"].monto) == ("Priorizada", 1, 1e6)
    assert e["priorizada"].nota == "$1.000.000 en juego"
    assert e["cotizando"].nota == "1 lote activo · 1 precio por pedir"
    assert (e["lista"].etiqueta, e["lista"].n, e["lista"].nota) == ("Lista para enviar", 2, "1 cierran mañana o antes")
    assert (e["enviada"].n, e["enviada"].monto) == (3, 30_000)
    assert e["enviada"].nota == "esta semana · 1 ganada · 1 perdida"


# ---------------------------------------------------------------------------
# Señales por CA
# ---------------------------------------------------------------------------


def _mundo_senales(s, mundo, codigos):
    emp = mundo["emp"]
    s.add_all([
        ImpuestoEspecifico(codigo="ila_baja", nombre="ILA", tasa=0.10, recuperable=True,
                           vigente_desde=date(2020, 1, 1)),
        FamiliaImpuesto(familia="Bebidas", impuesto_codigo="ila_baja", modo="clasificar"),
    ])  # fmt: skip
    bebida = ProductoCanonico(nombre="Bebida zero", unidad_base="L", familia="Bebidas", activo=True)
    s.add(bebida)
    s.flush()
    for i, c in enumerate(codigos):
        cierre = AHORA + timedelta(hours=3) if i == 0 else AHORA + timedelta(days=3)
        _ca(s, c, cierre, n_documentos=1)
        _linea(s, c, 1, mundo["prods"]["Arroz"][0])  # con costo al día
        _linea(s, c, 2, mundo["prods"]["Aceite"][0])  # costo de hace 23 días
        _linea(s, c, 3, mundo["prods"]["Azúcar"][0])  # sin costo
        _linea(s, c, 4, bebida.id, estado="dudosa")  # por revisar e impuesto por clasificar
        s.add(CaRequisito(empresa_id=emp, ca_codigo=c, texto="Boleta", obligatorio=True,
                          cumplido=False, origen="manual"))  # fmt: skip
    s.commit()


def test_senales_por_ca(engine, mundo):
    with Session(engine) as s:
        _mundo_senales(s, mundo, ["S0", "S1"])
        _ca(s, "S2", AHORA + timedelta(days=3))  # limpia
        _linea(s, "S2", 1, mundo["prods"]["Arroz"][0])
        s.commit()
        r = hoy.senales_por_ca(s, mundo["emp"], ["S0", "S1", "S2"], AHORA)
        ajena = hoy.senales_por_ca(s, mundo["otra"], ["S1"], AHORA)
    s0 = [(x.tipo, x.nivel, x.texto) for x in r["S0"]]
    assert s0 == [
        ("cierra_hoy", "urgente", "Cierra hoy"),
        ("requisitos", "urgente", "Requisitos pendientes · 1"),
        ("falta_costo", "atencion", "Falta costo · 1 línea"),
        ("precio_vencido", "atencion", "Precio desactualizado · 23 días"),
        ("impuesto_por_clasificar", "atencion", "Impuesto por clasificar"),
        ("lineas_por_revisar", "atencion", "1 línea por revisar"),
    ]
    assert [x.tipo for x in r["S1"]][0] == "requisitos" and r["S1"][0].nivel == "atencion"
    assert r["S2"] == []
    # Otra empresa: sin sus costos ni requisitos, todo lo que tenga producto «falta costo».
    tipos_ajena = {x.tipo for x in ajena["S1"]}
    assert "requisitos" not in tipos_ajena and "precio_vencido" not in tipos_ajena
    assert "Falta costo · 3 líneas" in [x.texto for x in ajena["S1"]]


def test_senal_anexo_sin_leer(engine, mundo):
    with Session(engine) as s:
        _ca(s, "AN", AHORA + timedelta(days=2), n_documentos=1)  # sin líneas: espera anexo
        s.commit()
        r = hoy.senales_por_ca(s, mundo["emp"], ["AN"], AHORA)
    assert [(x.tipo, x.texto) for x in r["AN"]] == [("anexo_sin_leer", "Anexo sin leer")]


def test_senales_con_numero_fijo_de_consultas(engine, mundo):
    codigos = [f"Q{i}" for i in range(8)]
    with Session(engine) as s:
        _mundo_senales(s, mundo, codigos)
    consultas: list[str] = []

    def _contar(conn, cursor, statement, *args):
        consultas.append(statement)

    def _medir(cods):
        consultas.clear()
        with Session(engine) as s:
            event.listen(engine, "before_cursor_execute", _contar)
            try:
                hoy.senales_por_ca(s, mundo["emp"], cods, AHORA)
            finally:
                event.remove(engine, "before_cursor_execute", _contar)
        return len(consultas)

    uno, todos = _medir(codigos[:1]), _medir(codigos)
    assert uno == todos
    assert todos <= 15


# ---------------------------------------------------------------------------
# Desde tu última visita
# ---------------------------------------------------------------------------


def test_novedades(engine, mundo):
    emp, boris, gonzalo, ana = mundo["emp"], mundo["boris"], mundo["gonzalo"], mundo["ana"]
    desde = AHORA - timedelta(hours=5)
    with Session(engine) as s:
        _ca(s, "N1", AHORA + timedelta(days=3), publicada=AHORA - timedelta(hours=1))
        _linea(s, "N1", 1, mundo["prods"]["Arroz"][0])  # calza: la empresa tiene costo de arroz
        _ca(s, "N2", AHORA + timedelta(days=3), publicada=AHORA - timedelta(hours=1))  # no calza
        _linea(s, "N2", 1, None, estado="pendiente")
        _ca(s, "N3", AHORA + timedelta(days=3), publicada=AHORA - timedelta(days=2))  # antes
        _linea(s, "N3", 1, mundo["prods"]["Arroz"][0])
        _ca(s, "G1", AHORA - timedelta(days=1), estado="cerrada")
        _trabajo(s, emp, "G1", eq.ENVIADA, resultado=eq.GANADA, resultado_en=AHORA - timedelta(hours=2))
        _ca(s, "G2", AHORA - timedelta(days=1), estado="cerrada")
        _trabajo(s, mundo["otra"], "G2", eq.ENVIADA, resultado=eq.GANADA,
                 resultado_en=AHORA - timedelta(hours=2))  # fmt: skip
        com = CaComentario(empresa_id=emp, ca_codigo="G1", usuario_id=gonzalo, texto="@Boris",
                           creado_en=AHORA - timedelta(hours=1))  # fmt: skip
        s.add(com)
        s.flush()
        for dest in (boris, gonzalo):
            s.add(CaEvento(empresa_id=emp, ca_codigo="G1", usuario_id=gonzalo, tipo=eq.EV_MENCION,
                           destinatario_id=dest, datos={"comentario_id": com.id},
                           creado_en=AHORA - timedelta(hours=1)))  # fmt: skip
        _evento_enviada(s, emp, "N3", gonzalo, AHORA - timedelta(hours=3))
        _evento_enviada(s, emp, "N1", boris, AHORA - timedelta(hours=3))  # propia: no cuenta
        _evento_enviada(s, mundo["otra"], "N1", ana, AHORA - timedelta(hours=3))  # ajena
        s.commit()
        textos = [(n.texto, n.n, n.url) for n in hoy.novedades(s, emp, boris, desde, AHORA)]
        de_ana = [n.texto for n in hoy.novedades(s, mundo["otra"], ana, desde, AHORA)]
    assert ("1 CA nueva que calzan", 1, "/mesa") in textos
    assert ("1 ganada · Nombre G1", 1, "/ventas") in textos
    assert ("1 mención a ti", 1, "/actividad") in textos
    assert ("Gonzalo envió 1 oferta", 1, "/actividad") in textos
    assert not any("Boris envió" in t for t, _n, _u in textos)
    assert all(n > 0 for _t, n, _u in textos)
    # La otra empresa no ve nada de la primera (su ganada sí).
    assert de_ana == ["1 ganada · Nombre G2"]


def test_novedades_listas_de_precios_y_menciones_solo_propias(engine, mundo):
    emp = mundo["emp"]
    ahora = ahora_utc()
    with Session(engine) as s:
        # Los tres precios del mundo se guardaron recién: «Molino actualizó 3 precios».
        r = hoy.novedades(s, emp, mundo["gonzalo"], ahora - timedelta(hours=1), ahora)
        assert [(n.texto, n.url) for n in r] == [("Molino actualizó 3 precios", f"/proveedores/{mundo['molino']}")]
        assert hoy.novedades(s, mundo["otra"], mundo["ana"], ahora - timedelta(hours=1), ahora) == []


def test_desde_ultima_visita_sin_visto_en_y_marcar_visto(engine, mundo):
    with Session(engine) as s:
        assert hoy.desde_ultima_visita(s, mundo["boris"], AHORA) == AHORA - timedelta(hours=24)
        from app.mercado.preferencias import guardar_vista

        guardar_vista(s, mundo["boris"], "hoy", {"periodo": "semana"})
        hoy.marcar_visto(s, mundo["boris"], AHORA)
        s.commit()
        assert vista_de(s, mundo["boris"], "hoy") == {"periodo": "semana", "visto_en": AHORA.isoformat()}
        assert hoy.desde_ultima_visita(s, mundo["boris"], AHORA + timedelta(hours=1)) == AHORA
        # Otro usuario no hereda la marca.
        assert hoy.desde_ultima_visita(s, mundo["gonzalo"], AHORA) == AHORA - timedelta(hours=24)


# ---------------------------------------------------------------------------
# Panel lateral
# ---------------------------------------------------------------------------


def test_panel_sin_cache_y_sin_lineas_que_espera_anexo(engine, mundo):
    ahora = ahora_utc()
    with Session(engine) as s:
        _ca(s, "PA", ahora + timedelta(days=2), n_documentos=1)
        _ca(s, "PB", ahora + timedelta(days=2))
        _linea(s, "PB", 1, mundo["prods"]["Azúcar"][0])
        s.commit()
        a = panel(s, mundo["emp"], "PA", ahora)
        b = panel(s, mundo["emp"], "PB", ahora)
        assert panel(s, mundo["emp"], "NO-EXISTE", ahora) is None
    assert a.lineas == [] and a.espera_anexo and a.cifras.motivo == FALTAN_LINEAS_ANEXO
    assert a.cifras.oferta is None and a.cifras.p_ganar is None
    assert [x.tipo for x in a.senales] == ["anexo_sin_leer"]
    assert b.cifras.motivo == SIN_EVALUACION
    assert b.lineas[0].marca == "sin_costo" and b.lineas[0].costo_unitario is None
    assert b.trabajo.estado == "nueva" and b.trabajo.version == 0
    assert b.comuna == "Chillán" and b.fuente == "Fuente: Dirección ChileCompra"


def test_panel_con_cache_oferta_propia_lote_y_bruto(engine, mundo):
    emp = mundo["emp"]
    ahora = ahora_utc()
    with Session(engine) as s:
        _ca(s, "PC", ahora + timedelta(days=2), monto=200_000)
        ln = _linea(s, "PC", 1, mundo["prods"]["Arroz"][0], cantidad=10.0)
        t = _trabajo(s, emp, "PC", eq.COTIZANDO, responsable_id=mundo["gonzalo"])
        t.version = 4
        lote = _lote(s, emp, "L9", ["PC"])
        s.add(EvaluacionCache(ca_codigo="PC", empresa_id=emp, evaluada_en=ahora, datos={
            "precio_sugerido": 15_000.0, "costo_total": 10_000.0, "margen_pct": 0.3333,
            "p_ganar": {"sugerido": 0.42}, "n_ok": 1, "n_sin_costo": 0, "modelo": "base",
        }))  # fmt: skip
        s.commit()
        sin_oferta = panel(s, emp, "PC", ahora)
        s.add(OfertaPropia(ca_codigo="PC", empresa_id=emp, total_neto=12_500.0, version=1,
                           lineas=[{"linea_id": ln.id, "precio_unitario": 1250.0,
                                    "proveedor_producto_id": None}]))  # fmt: skip
        s.commit()
        con_oferta = panel(s, emp, "PC", ahora)
    c = sin_oferta.cifras
    assert (c.oferta, c.oferta_propia, c.margen_pct, c.p_ganar) == (15_000.0, False, 0.3333, 0.42)
    assert c.pct_disponible == 0.075 and (c.costos_con, c.costos_de) == (1, 1)
    assert c.p_nota == "para el precio sugerido · modelo base" and c.motivo is None
    assert sin_oferta.trabajo.lote_id == lote.id and sin_oferta.trabajo.version == 4
    assert sin_oferta.trabajo.responsable == "Gonzalo" and sin_oferta.trabajo.etiqueta == "Cotizando"
    linea = sin_oferta.lineas[0]
    assert linea.costo_unitario == 1000.0 and linea.costo_unitario_bruto == 1190.0  # IVA 19 %
    assert linea.marca is None and linea.proveedor == "Molino"
    c2 = con_oferta.cifras
    assert (c2.oferta, c2.oferta_propia, c2.margen_pct) == (12_500.0, True, 0.2)
    assert c2.p_nota == "calculada para el precio sugerido, no para tu oferta"
    assert con_oferta.lineas[0].precio_unitario == 1250.0
    assert con_oferta.lineas[0].precio_unitario_bruto == 1487.5


def test_ruta_panel_404_fuera_de_territorio_y_render(client, engine, mundo, settings):
    ahora = ahora_utc()
    with Session(engine) as s:
        _ca(s, "R16", ahora + timedelta(days=2), n_documentos=1)
        _ca(s, "R13", ahora + timedelta(days=2), region=13)  # nadie la tiene en su territorio
        s.commit()
    cookies, _ = _auth(settings, mundo["gonzalo"])
    r = client.get("/mesa/ca/R16/panel", cookies=cookies)
    assert r.status_code == 200
    assert "Fuente: Dirección ChileCompra" in r.text and "r-senal" in r.text
    assert "/mesa/ca/R16/comprador" in r.text and "espera su anexo" in r.text
    assert client.get("/mesa/ca/R13/panel", cookies=cookies).status_code == 404
    assert client.get("/mesa/ca/NADA/panel", cookies=cookies).status_code == 404
    sin, _ = _auth(settings, mundo["sinemp"])
    assert client.get("/mesa/ca/R16/panel", cookies=sin).status_code == 403


# ---------------------------------------------------------------------------
# /hoy/previa y /hoy/visto
# ---------------------------------------------------------------------------


def test_hoy_previa_solo_admin(client, engine, mundo, settings):
    with Session(engine) as s:
        _ca(s, "H1", ahora_utc() + timedelta(hours=2))
        _trabajo(s, mundo["emp"], "H1", eq.LISTA)
        s.commit()
    admin, _ = _auth(settings, mundo["boris"])
    r = client.get("/hoy/previa?periodo=semana&tipo=cierres", cookies=admin)
    assert r.status_code == 200
    for texto in ("Te necesita", "Cierran esta semana", "Embudo", "Lista para enviar",
                  "Desde tu última visita", "Fuente: Dirección ChileCompra", "Nombre H1"):  # fmt: skip
        assert texto in r.text
    assert client.get("/hoy/previa?periodo=xx&tipo=yy", cookies=admin).status_code == 200
    usuario, _ = _auth(settings, mundo["gonzalo"])
    assert client.get("/hoy/previa", cookies=usuario).status_code == 403
    sin, _ = _auth(settings, mundo["sinemp"])
    r = client.get("/hoy/previa", cookies=sin)
    assert r.status_code == 200 and "no tiene una empresa activa" in r.text


def test_hoy_visto_guarda_visto_en_con_csrf(client, engine, mundo, settings):
    cookies, csrf = _auth(settings, mundo["gonzalo"])
    assert client.post("/hoy/visto", cookies=cookies, data={"csrf_token": "malo"}).status_code == 403
    r = client.post("/hoy/visto", cookies=cookies, data={"csrf_token": csrf},
                    headers={"HX-Request": "true"})  # fmt: skip
    assert r.status_code == 200 and r.text == ""
    with Session(engine) as s:
        visto = vista_de(s, mundo["gonzalo"], "hoy")["visto_en"]
        assert datetime.fromisoformat(visto) <= ahora_utc()
        assert vista_de(s, mundo["boris"], "hoy") == {}
    r = client.post("/hoy/visto", cookies=cookies, data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
