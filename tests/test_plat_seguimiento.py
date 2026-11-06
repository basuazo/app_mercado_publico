"""F-plat-seguimiento: checklist administrativo, cotización formal (DOCX), ventas y caja.

Todo offline (SQLite en memoria). El correlativo bajo concurrencia real y el DDL de la migración van
en test_plat_seguimiento_pg.py.

Mundo: una empresa con un admin y un usuario (más otra empresa con Ana), el arroz en sacos de 25 kg,
un proveedor con precio, 10 sacos en el inventario y dos CA: «CA-1» (4 sacos = 100 kg, más una línea
`reemplazada` que nunca cuenta) y «CA-2».
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from docx import Document
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core import retencion
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.mercado import cotizacion as cot
from app.mercado import equipo as eq
from app.mercado import lote as lt
from app.mercado import oferta as of
from app.mercado import oferta_propia as op
from app.mercado import requisitos as rq
from app.mercado import ventas as vt
from app.mercado.proveedores import guardar_producto, hoy_chile
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaAnexo,
    CaCotizacion,
    CaEvento,
    CaLinea,
    CaRequisito,
    CaTrabajo,
    CompraAgil,
    CostoHistorial,
    Empresa,
    EmpresaProducto,
    EmpresaProductoMovimiento,
    OfertaPropia,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    Usuario,
    Venta,
    VentaCompra,
)

_PW = "contraseña-segura-test"
_RUT = "76123456-0"
_RUT_OTRO = "77777777-7"


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


@pytest.fixture()
def mundo(engine):
    hoy = hoy_chile()
    with Session(engine) as s:
        arroz = ProductoCanonico(nombre="Arroz grado 1", unidad_base="kg", activo=True)
        s.add(arroz)
        s.flush()
        saco = ProductoFormato(producto_id=arroz.id, nombre="saco 25 kg", factor_base=25.0)
        s.add(saco)
        nuestra = Empresa(nombre="Distribuidora Ñuble", rut=_RUT, regiones_prioritarias=[16],
                          comunas_prioritarias=[], activa=True, direccion="Av. Libertad 100",
                          contacto="ventas@dn.cl", giro="Abarrotes",
                          condiciones_cotizacion="Pago a 30 días.")  # fmt: skip
        otra = Empresa(nombre="Otra", rut=_RUT_OTRO, regiones_prioritarias=[16],
                       comunas_prioritarias=[], activa=True)  # fmt: skip
        s.add_all([nuestra, otra])
        s.flush()
        molino = Proveedor(empresa_id=nuestra.id, nombre="Molino SA", rubros=[], activo=True,
                           plazo_pago_dias=15)  # fmt: skip
        s.add(molino)
        s.flush()
        pp, _ = guardar_producto(
            s, molino, producto_texto="Arroz saco", formato_texto="25 kg", precio_neto=20000,
            vigente_desde=hoy, factor_base=25.0, unidad_base="kg", producto_id=arroz.id,
            formato_id=saco.id,
        )  # fmt: skip
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
        for codigo, cierre_dias in (("CA-1", 3), ("CA-2", 4)):
            s.add(CompraAgil(codigo=codigo, nombre=f"Arroz {codigo}", estado="publicada", region=16,
                             organismo_nombre="Liceo de Chillán", unidad_compra="Casino",
                             direccion_entrega="Liceo 123, Chillán", plazo_entrega_dias=5,
                             fecha_cierre=ahora_utc() + timedelta(days=cierre_dias),
                             detalle_lineas="completo",
                             documentos=[{"id": "2", "nombre": "Lista de productos.pdf"}]))  # fmt: skip
        s.flush()
        l1 = CaLinea(ca_codigo="CA-1", n=1, producto_id=arroz.id, formato_id=saco.id, cantidad=4.0,
                     cantidad_base=100.0, texto_origen="Arroz 25 kg", estado="ok")
        l2 = CaLinea(ca_codigo="CA-1", n=2, producto_id=arroz.id, formato_id=saco.id, cantidad=50.0,
                     cantidad_base=1250.0, texto_origen="Arroz viejo", estado="reemplazada")
        l3 = CaLinea(ca_codigo="CA-2", n=1, producto_id=arroz.id, formato_id=saco.id, cantidad=2.0,
                     cantidad_base=50.0, texto_origen="Arroz 25 kg", estado="ok")
        s.add_all([l1, l2, l3])
        s.flush()
        op.guardar(s, nuestra.id, None, producto_id=arroz.id, formato_id=saco.id, stock=10,
                   proveedor_habitual_id=molino.id)  # fmt: skip
        ep = s.execute(select(EmpresaProducto)).scalar_one()
        s.commit()
        return {
            "emp": nuestra.id, "otra": otra.id, "arroz": arroz.id, "saco": saco.id,
            "molino": molino.id, "pp": pp.id, "ep": ep.id, "l1": l1.id, "l2": l2.id, "l3": l3.id,
            **users,
        }  # fmt: skip


def _u(s, mundo, quien="boris"):
    return s.get(Usuario, mundo[quien])


def _oferta(s, mundo, codigo="CA-1", *, stock=True, linea="l1", cantidad=4.0, precio=24000.0):
    """Oferta guardada: una línea (stock propio o del proveedor)."""
    d = {"linea_id": mundo[linea], "precio_unitario": precio, "proveedor_producto_id": None}
    if stock:
        d |= {"origen": "stock", "empresa_producto_id": mundo["ep"]}
    else:
        d["proveedor_producto_id"] = mundo["pp"]
    o = OfertaPropia(ca_codigo=codigo, empresa_id=mundo["emp"], lineas=[d],
                     total_neto=precio * cantidad, plazo_entrega_dias=5, version=1)  # fmt: skip
    s.add(o)
    s.flush()
    return o


def _estado(s, mundo, codigo, nuevo, quien="boris", **kw):
    t = eq.trabajo_de(s, mundo["emp"], codigo)
    return eq.cambiar_estado(s, mundo["emp"], _u(s, mundo, quien), codigo, nuevo,
                             t.version if t else 0, **kw)  # fmt: skip


def _plantilla(s, mundo):
    rq.sembrar_plantilla(s, mundo["emp"])
    s.commit()


def _venta(s, mundo, codigo="CA-1", **extra):
    v = vt.crear_desde_ganada(s, mundo["emp"], codigo)
    for k, val in extra.items():
        setattr(v, k, val)
    s.commit()
    return v


# ---------------------------------------------------------------------------
# Requisitos: plantilla, semilla y bloqueo de «enviada»
# ---------------------------------------------------------------------------


def test_sembrar_plantilla_es_idempotente_y_no_pisa_la_editada(engine, mundo):
    with Session(engine) as s:
        assert rq.sembrar_plantilla(s, mundo["emp"]) == len(rq.PLANTILLA_INICIAL)
        assert rq.sembrar_plantilla(s, mundo["emp"]) == 0
        items = rq.plantilla(s, mundo["emp"])
        assert items[0].texto == rq.TEXTO_COTIZACION and items[0].obligatorio
        assert not items[-1].obligatorio  # lo de «documentos vigentes» es opcional
        rq.editar_plantilla(s, mundo["emp"], items[2].id, activo=False)
        assert len(rq.plantilla(s, mundo["emp"], solo_activos=True)) == len(items) - 1
        with pytest.raises(rq.RequisitoAjeno):
            rq.editar_plantilla(s, mundo["otra"], items[0].id, texto="x")
        with pytest.raises(rq.ErrorRequisito):
            rq.agregar_a_plantilla(s, mundo["emp"], items[0].texto, True)


def test_semilla_de_la_ca_copia_plantilla_y_documentos_administrativos(engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
        s.get(CompraAgil, "CA-1").documentos = [
            {"id": "1", "nombre": "Formulario de oferta.pdf"},
            {"id": "2", "nombre": "Lista de productos.pdf"},
        ]
        s.add(CaAnexo(ca_codigo="CA-1", nombre_original="Declaración jurada simple.docx",
                      mime="x", tamano=1, sha256="a" * 64))  # fmt: skip
        s.add(CaAnexo(ca_codigo="CA-1", nombre_original="Anexo productos.xlsx", mime="x",
                      tamano=1, sha256="b" * 64))  # fmt: skip
        s.commit()
        nuevos = rq.sembrar_ca(s, mundo["emp"], "CA-1")
        s.commit()
        textos = [r.texto for r in rq.listar(s, mundo["emp"], "CA-1")]
        assert nuevos == len(textos) == len(rq.PLANTILLA_INICIAL) + 2
        assert "Documento de la CA: Formulario de oferta.pdf" in textos
        assert "Documento de la CA: Declaración jurada simple.docx" in textos
        # los anexos de productos no generan requisito
        assert not any("Lista de productos" in t or "Anexo productos" in t for t in textos)
        origenes = {r.origen for r in rq.listar(s, mundo["emp"], "CA-1")}
        assert origenes == {"plantilla", "documento_ca"}
        assert rq.sembrar_ca(s, mundo["emp"], "CA-1") == 0  # idempotente
        # la otra empresa no tiene plantilla: solo recibe los documentos de la CA, y lo suyo es suyo
        assert rq.sembrar_ca(s, mundo["otra"], "CA-1") == 2
        assert {r.origen for r in rq.listar(s, mundo["otra"], "CA-1")} == {"documento_ca"}
        assert len(rq.listar(s, mundo["emp"], "CA-1")) == len(textos)
        assert [r.texto for r in rq.listar(s, mundo["otra"], "CA-2")] == []  # CA-2 sin documentos


def test_priorizar_siembra_y_enviada_se_bloquea_con_obligatorios_pendientes(engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
        _estado(s, mundo, "CA-1", eq.PRIORIZADA)
        s.commit()
        assert len(rq.listar(s, mundo["emp"], "CA-1")) >= len(rq.PLANTILLA_INICIAL)
        with pytest.raises(eq.RequisitosPendientes) as exc:
            _estado(s, mundo, "CA-1", eq.ENVIADA)
        s.rollback()
        assert "Cotización formal adjunta" in str(exc.value)
        assert "Cotización formal adjunta" in exc.value.pendientes
        assert eq.trabajo_de(s, mundo["emp"], "CA-1").estado == eq.PRIORIZADA
        # marcar todos los obligatorios → se puede enviar
        for r in rq.listar(s, mundo["emp"], "CA-1"):
            rq.marcar(s, mundo["emp"], mundo["boris"], r.id, True)
        s.commit()
        assert rq.pendientes(s, mundo["emp"], "CA-1") == []
        assert _estado(s, mundo, "CA-1", eq.ENVIADA).estado == eq.ENVIADA


def test_enviada_directo_desde_nueva_tambien_se_bloquea(engine, mundo):
    """Saltarse «priorizar» no esquiva el checklist: se siembra al intentar enviar."""
    with Session(engine) as s:
        _plantilla(s, mundo)
        with pytest.raises(eq.RequisitosPendientes):
            _estado(s, mundo, "CA-2", eq.ENVIADA)
        s.rollback()
        assert eq.trabajo_de(s, mundo["emp"], "CA-2") is None


def test_admin_fuerza_con_motivo_y_queda_en_el_evento_un_usuario_no(engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
        _estado(s, mundo, "CA-1", eq.PRIORIZADA)
        s.commit()
        with pytest.raises(eq.RequisitosPendientes):  # el usuario normal no puede forzar
            _estado(s, mundo, "CA-1", eq.ENVIADA, quien="gonzalo", forzar_motivo="urge")
        s.rollback()
        with pytest.raises(eq.RequisitosPendientes):  # el admin sin motivo tampoco
            _estado(s, mundo, "CA-1", eq.ENVIADA)
        s.rollback()
        _estado(s, mundo, "CA-1", eq.ENVIADA, forzar_motivo="El comprador lo pidió por correo")
        s.commit()
        ev = s.execute(
            select(CaEvento).where(CaEvento.tipo == "estado").order_by(CaEvento.id.desc())
        ).scalars().first()
        assert ev.datos["a"] == "enviada"
        assert ev.datos["forzado"] == "El comprador lo pidió por correo"
        textos = eq.describir_eventos(s, [ev], mundo["boris"], ahora_utc())
        assert "El comprador lo pidió por correo" in textos[0]["despues"]


def test_empresa_sin_plantilla_envia_sin_trabas(engine, mundo):
    with Session(engine) as s:
        assert _estado(s, mundo, "CA-1", eq.ENVIADA).estado == eq.ENVIADA


def test_marcar_agregar_quitar_y_ownership(engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
        _estado(s, mundo, "CA-1", eq.PRIORIZADA)
        s.commit()
        r = rq.listar(s, mundo["emp"], "CA-1")[0]
        rq.marcar(s, mundo["emp"], mundo["gonzalo"], r.id, True)
        assert r.cumplido and r.cumplido_por_id == mundo["gonzalo"] and r.cumplido_en
        rq.marcar(s, mundo["emp"], mundo["gonzalo"], r.id, False)
        assert not r.cumplido and r.cumplido_por_id is None
        with pytest.raises(rq.RequisitoAjeno):
            rq.marcar(s, mundo["otra"], mundo["ana"], r.id, True)
        m = rq.agregar_manual(s, mundo["emp"], "CA-1", "Declaración jurada", True)
        assert m.origen == "manual"
        with pytest.raises(rq.ErrorRequisito):
            rq.agregar_manual(s, mundo["emp"], "CA-1", "Declaración jurada", True)
        with pytest.raises(rq.ErrorRequisito):
            rq.agregar_manual(s, mundo["emp"], "CA-1", "   ", True)
        with pytest.raises(rq.RequisitoAjeno):
            rq.quitar(s, mundo["otra"], m.id)
        rq.quitar(s, mundo["emp"], m.id)
        assert all(x.texto != "Declaración jurada" for x in rq.listar(s, mundo["emp"], "CA-1"))


def test_purga_de_requisitos_solo_de_ca_descartadas_viejas(engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
        _estado(s, mundo, "CA-1", eq.DESCARTADA, motivo="no aplica")
        _estado(s, mundo, "CA-2", eq.PRIORIZADA)
        s.commit()
        n_total = s.execute(select(func.count()).select_from(CaRequisito)).scalar_one()
        ahora = ahora_utc()
        assert rq.purgar(s, ahora) == 0  # recién descartada: no se purga
        t = eq.trabajo_de(s, mundo["emp"], "CA-1")
        t.actualizado_en = ahora - timedelta(days=400)
        s.commit()
        # CA-1 se descartó desde `nueva`: se sembró al intentar?  No: solo siembra en estados de trabajo
        borrados = rq.purgar(s, ahora)
        s.commit()
        assert borrados == 0 or borrados < n_total
        assert s.execute(
            select(func.count()).select_from(CaRequisito).where(CaRequisito.ca_codigo == "CA-2")
        ).scalar_one() > 0
        # una CA que se sembró y luego se descartó hace > 12 meses sí pierde su checklist
        _estado(s, mundo, "CA-2", eq.DESCARTADA, motivo="se cayó")
        s.commit()
        t2 = eq.trabajo_de(s, mundo["emp"], "CA-2")
        t2.actualizado_en = ahora - timedelta(days=400)
        s.commit()
        assert rq.purgar(s, ahora) > 0
        s.commit()
        assert s.execute(
            select(func.count()).select_from(CaRequisito).where(CaRequisito.ca_codigo == "CA-2")
        ).scalar_one() == 0
    assert any(p.nombre == "requisitos" for p in retencion.PURGAS)


# ---------------------------------------------------------------------------
# Cotización formal
# ---------------------------------------------------------------------------


def test_correlativo_por_empresa_y_estable_por_oferta(engine, mundo):
    with Session(engine) as s:
        o1 = _oferta(s, mundo, "CA-1")
        o2 = _oferta(s, mundo, "CA-2", linea="l3", cantidad=2.0)
        otra = OfertaPropia(ca_codigo="CA-1", empresa_id=mundo["otra"], lineas=[], total_neto=0,
                            version=1)  # fmt: skip
        s.add(otra)
        s.flush()
        n1 = cot.asignar_numero(s, mundo["emp"], o1)
        n2 = cot.asignar_numero(s, mundo["emp"], o2)
        s.commit()
        anio = ahora_utc().year
        assert n1 == f"COT-{anio}-1" and n2 == f"COT-{anio}-2"
        assert cot.asignar_numero(s, mundo["emp"], o1) == n1  # no gasta otro correlativo
        assert s.get(Empresa, mundo["emp"]).ultimo_correlativo == 2
        assert cot.asignar_numero(s, mundo["otra"], otra) == f"COT-{anio}-1"  # contador propio


def test_docx_tiene_las_mismas_filas_que_el_xlsx_y_excluye_lineas_reemplazadas(engine, mundo):
    with Session(engine) as s:
        o = _oferta(s, mundo, "CA-1")
        # una línea guardada que luego quedó `reemplazada` no aparece ni en el XLSX ni en el DOCX
        o.lineas = [*o.lineas, {"linea_id": mundo["l2"], "precio_unitario": 5.0,
                                "proveedor_producto_id": None}]  # fmt: skip
        s.commit()
        ca = s.get(CompraAgil, "CA-1")
        lineas = of.armar(s, mundo["emp"], ca, None, o)
        cot.asignar_numero(s, mundo["emp"], o)
        contenido = cot.docx(s.get(Empresa, mundo["emp"]), ca, o, lineas)
        planilla = of.xlsx(ca, lineas, o.plazo_entrega_dias)

    doc = Document(io.BytesIO(contenido))
    filas_docx = [[c.text for c in r.cells] for r in doc.tables[0].rows][1:]
    ws = load_workbook(io.BytesIO(planilla)).active
    filas_xlsx = [
        r for r in ws.iter_rows(min_row=5, values_only=True) if isinstance(r[0], int)
    ]
    assert len(filas_docx) == len(filas_xlsx) == 1
    n, producto, cantidad, unitario, total = filas_xlsx[0]
    d = filas_docx[0]
    assert d[0] == str(n) and d[1] == producto
    assert d[3] == "4" and cantidad == 4.0
    assert d[4] == cot._clp(unitario) == "$24.000" and d[5] == cot._clp(total) == "$96.000"
    assert d[2] == "saco 25 kg"  # el formato pedido


def test_docx_trae_membrete_totales_con_iva_condiciones_y_fuente(engine, mundo):
    with Session(engine) as s:
        o = _oferta(s, mundo, "CA-1")
        cot.configurar(o, "45", None)
        ca = s.get(CompraAgil, "CA-1")
        emp = s.get(Empresa, mundo["emp"])
        cot.asignar_numero(s, mundo["emp"], o)
        contenido = cot.docx(emp, ca, o, of.armar(s, mundo["emp"], ca, None, o))
        numero = o.numero_cotizacion
    texto = "\n".join(p.text for p in Document(io.BytesIO(contenido)).paragraphs)
    for esperado in (
        "Distribuidora Ñuble", _RUT, "Av. Libertad 100", "ventas@dn.cl", "Abarrotes",
        f"COTIZACIÓN N° {numero}", "Validez de la oferta: 45 días", "Liceo de Chillán",
        "Compra Ágil CA-1", "Total neto: $96.000", "IVA 19 %: $18.240", "Total bruto: $114.240",
        "Plazo de entrega: 5 días corridos", "Pago a 30 días.",
        "Fuente de los datos de la Compra Ágil: Dirección ChileCompra",
    ):  # fmt: skip
        assert esperado in texto, esperado
    assert cot.totales(of.filas_cotizacion([])) == (0, 0, 0)


def test_configurar_valida_validez_y_condiciones(engine, mundo):
    with Session(engine) as s:
        o = _oferta(s, mundo)
        for malo in ("0", "366", "abc"):
            with pytest.raises(cot.ErrorCotizacion):
                cot.configurar(o, malo, None)
        with pytest.raises(cot.ErrorCotizacion):
            cot.configurar(o, None, "x" * (cot.CONDICIONES_MAX + 1))
        cot.configurar(o, "", "  Entrega en bodega  ")
        assert o.validez_dias == 30 and o.condiciones == "Entrega en bodega"
        assert cot.condiciones_de(o, s.get(Empresa, mundo["emp"])) == "Entrega en bodega"
        cot.configurar(o, None, "")  # vuelve al default de la empresa
        assert cot.condiciones_de(o, s.get(Empresa, mundo["emp"])) == "Pago a 30 días."


def test_generar_el_documento_no_marca_cumplido_el_requisito(client, settings, engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
        _estado(s, mundo, "CA-1", eq.PRIORIZADA)
        _oferta(s, mundo)
        s.commit()
    cookies, hdr, _ = _auth(settings, mundo["boris"])
    client.cookies.update(cookies)
    r = client.get("/mesa/ca/CA-1/cotizacion.docx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert r.headers["x-numero-cotizacion"].startswith("COT-")
    assert Document(io.BytesIO(r.content)).tables
    with Session(engine) as s:
        assert "Cotización formal adjunta" in rq.pendientes(s, mundo["emp"], "CA-1")
        assert s.execute(select(OfertaPropia.documento_generado_en)).scalar_one() is not None
    # la segunda descarga conserva el número
    assert client.get("/mesa/ca/CA-1/cotizacion.docx").headers["x-numero-cotizacion"] == (
        r.headers["x-numero-cotizacion"]
    )


# ---------------------------------------------------------------------------
# Ventas
# ---------------------------------------------------------------------------


def _ganar(s, mundo, codigo="CA-1"):
    """Una CA enviada, ya cerrada y con nuestra oferta seleccionada → `generar_alarmas` la gana."""
    ahora = ahora_utc()
    ca = s.get(CompraAgil, codigo)
    ca.fecha_cierre = ahora - timedelta(days=1)
    ca.estado = "cerrada"
    ca.cotizaciones_obtenidas_en = ahora - timedelta(hours=1)
    _estado(s, mundo, codigo, eq.ENVIADA)
    s.add(CaCotizacion(ca_codigo=codigo, llamado=1, rut_proveedor=_RUT, seleccionado=True,
                       valor_neto=96000.0, id_cotizacion=f"{codigo}-n"))  # fmt: skip
    s.add(CaCotizacion(ca_codigo=codigo, llamado=1, rut_proveedor=_RUT_OTRO, seleccionado=False,
                       valor_neto=99000.0, id_cotizacion=f"{codigo}-o"))  # fmt: skip
    s.commit()
    return ahora


def test_ganar_abre_la_venta_una_sola_vez_y_ca_trabajo_sigue_enviada(engine, mundo):
    with Session(engine) as s:
        _oferta(s, mundo)
        ahora = _ganar(s, mundo)
        assert eq.generar_alarmas(s, mundo["emp"], ahora)["resultado"] == 1
        s.commit()
        v = s.execute(select(Venta)).scalar_one()
        assert (v.estado, v.ca_codigo, v.empresa_id) == ("adjudicada", "CA-1", mundo["emp"])
        assert v.entrega_direccion == "Liceo 123, Chillán"  # prellenada con la de la CA
        assert v.plazo_pago_dias == 30 and v.oferta_id is not None and v.version == 1
        t = eq.trabajo_de(s, mundo["emp"], "CA-1")
        assert (t.estado, t.resultado) == (eq.ENVIADA, eq.GANADA)
        eq.generar_alarmas(s, mundo["emp"], ahora)
        s.commit()
        assert s.execute(select(func.count()).select_from(Venta)).scalar_one() == 1
        assert vt.crear_desde_ganada(s, mundo["emp"], "CA-1").id == v.id
        ev = s.execute(select(CaEvento).where(CaEvento.tipo == "venta")).scalar_one()
        assert ev.usuario_id is None and ev.datos["estado"] == "adjudicada"


def test_registrar_venta_a_mano_solo_si_la_ca_esta_enviada(engine, mundo):
    with Session(engine) as s:
        with pytest.raises(vt.ErrorVenta):
            vt.crear_manual(s, mundo["emp"], _u(s, mundo), "CA-1")
        _estado(s, mundo, "CA-1", eq.ENVIADA)
        v = vt.crear_manual(s, mundo["emp"], _u(s, mundo, "gonzalo"), "CA-1")
        s.commit()
        assert vt.crear_manual(s, mundo["emp"], _u(s, mundo), "CA-1").id == v.id
        ev = s.execute(select(CaEvento).where(CaEvento.tipo == "venta")).scalar_one()
        assert ev.usuario_id == mundo["gonzalo"]


def test_oc_fija_la_fecha_comprometida_con_el_plazo_de_la_oferta(engine, mundo):
    with Session(engine) as s:
        _oferta(s, mundo)
        v = _venta(s, mundo)
        hoy = hoy_chile()
        vt.registrar_oc(s, mundo["emp"], _u(s, mundo), v.id, v.version, numero="OC-77",
                        fecha=hoy.isoformat(), monto="96.000")  # fmt: skip
        s.commit()
        assert v.estado == "oc_recibida" and v.oc_numero == "OC-77" and v.oc_monto == 96000.0
        assert v.entrega_fecha_comprometida == hoy + timedelta(days=5) and v.version == 2
        # se puede corregir sin cambiar de estado
        vt.registrar_oc(s, mundo["emp"], _u(s, mundo), v.id, v.version, numero="OC-78",
                        fecha=(hoy - timedelta(days=1)).isoformat())  # fmt: skip
        assert v.estado == "oc_recibida" and v.entrega_fecha_comprometida == hoy + timedelta(days=4)
        with pytest.raises(vt.ErrorVenta):
            vt.registrar_oc(s, mundo["emp"], _u(s, mundo), v.id, v.version, numero="", fecha="2026-11-01")
        with pytest.raises(vt.ErrorVenta):
            vt.registrar_oc(s, mundo["emp"], _u(s, mundo), v.id, v.version, numero="X", fecha="mañana")


def test_conflicto_de_version_y_ownership(engine, mundo):
    with Session(engine) as s:
        v = _venta(s, mundo)
        vt.guardar_entrega_datos(s, mundo["emp"], _u(s, mundo), v.id, v.version, direccion="Otra 1",
                                 horario="8 a 12", recibe="Marta")  # fmt: skip
        s.commit()
        with pytest.raises(vt.ConflictoVenta) as exc:
            vt.registrar_oc(s, mundo["emp"], _u(s, mundo), v.id, 1, numero="A", fecha="2026-11-01")
        assert exc.value.quien == "boris"
        with pytest.raises(vt.VentaAjena):
            vt.obtener(s, mundo["otra"], v.id)
        with pytest.raises(vt.VentaAjena):
            vt.registrar_pago(s, mundo["otra"], _u(s, mundo, "ana"), v.id, None)
        assert vt.listar(s, mundo["otra"]) == []


def test_compra_escribe_costo_historial_suma_stock_y_calcula_costo_real(engine, mundo):
    with Session(engine) as s:
        _oferta(s, mundo, stock=False)
        v = _venta(s, mundo)
        hoy = hoy_chile()
        c = vt.registrar_compra(s, mundo["emp"], _u(s, mundo), v.id, v.version,
                                proveedor_producto_id=mundo["pp"], cantidad="4", precio_neto="21000",
                                fecha=hoy.isoformat())  # fmt: skip
        s.commit()
        assert v.estado == "comprando" and v.costo_real == 84000.0
        assert c.pagar_en == hoy + timedelta(days=15)  # el plazo de pago del proveedor
        h = s.execute(select(CostoHistorial).where(CostoHistorial.fuente == "compra")).scalar_one()
        assert h.precio_neto == 21000.0 and h.usuario_id == mundo["boris"]
        ep = s.get(EmpresaProducto, mundo["ep"])
        assert ep.stock == 14  # 10 + 4 sacos
        mov = s.execute(select(EmpresaProductoMovimiento).where(
            EmpresaProductoMovimiento.motivo == "compra")).scalar_one()
        assert mov.referencia == f"venta:{v.id}" and mov.delta == 4
        # el precio del proveedor quedó actualizado por la compra
        vt.registrar_compra(s, mundo["emp"], _u(s, mundo), v.id, v.version,
                            proveedor_producto_id=mundo["pp"], cantidad="1", precio_neto="21000")  # fmt: skip
        assert v.costo_real == 105000.0
        with pytest.raises(vt.VentaAjena):  # la venta es de otra empresa
            vt.registrar_compra(s, mundo["otra"], _u(s, mundo, "ana"), v.id, None,
                                proveedor_producto_id=mundo["pp"], cantidad="1", precio_neto="1")  # fmt: skip
        ajeno = Proveedor(empresa_id=mundo["otra"], nombre="Ajeno", rubros=[], activo=True)
        s.add(ajeno)
        s.flush()
        pp_ajeno, _ = guardar_producto(s, ajeno, producto_texto="Arroz", precio_neto=1.0)
        with pytest.raises(of.ProveedorAjeno):  # el producto del proveedor es de otra empresa
            vt.registrar_compra(s, mundo["emp"], _u(s, mundo), v.id, v.version,
                                proveedor_producto_id=pp_ajeno.id, cantidad="1", precio_neto="1")  # fmt: skip
        with pytest.raises(vt.ErrorVenta):
            vt.registrar_compra(s, mundo["emp"], _u(s, mundo), v.id, v.version,
                                proveedor_producto_id=mundo["pp"], cantidad="0", precio_neto="5")  # fmt: skip


def test_margen_realizado_con_costo_real(engine, mundo):
    with Session(engine) as s:
        o = _oferta(s, mundo, stock=False)
        o.margen_realizado = 111.0  # el estimado de F-plat-5
        v = _venta(s, mundo)
        vt.registrar_oc(s, mundo["emp"], _u(s, mundo), v.id, v.version, numero="OC-1",
                        fecha=hoy_chile().isoformat(), monto="96000")  # fmt: skip
        assert o.margen_realizado == 111.0  # sin compras sigue el estimado
        vt.registrar_compra(s, mundo["emp"], _u(s, mundo), v.id, v.version,
                            proveedor_producto_id=mundo["pp"], cantidad="4", precio_neto="20000")  # fmt: skip
        s.commit()
        assert o.margen_realizado == 16000.0  # 96.000 de la OC − 80.000 de compras
        vista = vt.vista(s, mundo["emp"], v.id)
        assert vista.margen_realizado == 16000.0 and vista.ingreso == 96000.0
        assert vt.tiene_costo_real(s, mundo["emp"], "CA-1")
        assert not vt.tiene_costo_real(s, mundo["otra"], "CA-1")
        # la factura corrige el ingreso
        vt.registrar_entrega(s, mundo["emp"], _u(s, mundo), v.id, v.version)
        vt.registrar_factura(s, mundo["emp"], _u(s, mundo), v.id, v.version, numero="F-1",
                             fecha=hoy_chile().isoformat(), monto="100000")  # fmt: skip
        assert o.margen_realizado == 20000.0


def test_sombra_no_pisa_el_margen_con_costo_real(engine, mundo):
    from app.mercado import sombra

    with Session(engine) as s:
        o = _oferta(s, mundo, stock=False)
        v = _venta(s, mundo)
        vt.registrar_compra(s, mundo["emp"], _u(s, mundo), v.id, v.version,
                            proveedor_producto_id=mundo["pp"], cantidad="4", precio_neto="20000")  # fmt: skip
        o.margen_realizado = 16000.0
        ahora = _ganar(s, mundo)
        s.commit()
        sombra.cerrar_ofertas(s, ahora)
        s.refresh(o)
        assert o.precio_ganador_real == 96000.0
        assert o.margen_realizado == 16000.0  # no lo pisó la estimación


def _con_lote_ofertado(s, mundo):
    """CA-1 con oferta de stock, en un lote `ofertado` (reserva 4 sacos de los 10)."""
    _oferta(s, mundo)
    lote = lt.crear(s, mundo["emp"], _u(s, mundo), ["CA-1"])
    s.commit()
    _estado(s, mundo, "CA-1", eq.LISTA)
    s.commit()
    lt.cambiar_estado(s, mundo["emp"], _u(s, mundo), lote.id, lt.OFERTADO, None)
    s.commit()
    return lote


def test_entrega_convierte_la_reserva_en_movimiento_entrega(engine, mundo):
    with Session(engine) as s:
        lote = _con_lote_ofertado(s, mundo)
        ep = s.get(EmpresaProducto, mundo["ep"])
        assert (ep.stock, ep.reservado) == (10, 4)
        v = _venta(s, mundo)
        assert v.lote_id == lote.id
        v, avisos = vt.registrar_entrega(s, mundo["emp"], _u(s, mundo), v.id, v.version,
                                         guia="G-9", recibe="Marta 555")  # fmt: skip
        s.commit()
        s.refresh(ep)
        assert avisos == [] and v.estado == "entregada" and v.guia_numero == "G-9"
        assert (ep.stock, ep.reservado) == (6, 0)  # descontó 4 y liberó SU parte de la reserva
        movs = s.execute(
            select(EmpresaProductoMovimiento.motivo, EmpresaProductoMovimiento.delta,
                   EmpresaProductoMovimiento.referencia)
            .order_by(EmpresaProductoMovimiento.id)
        ).all()  # fmt: skip
        assert ("reserva", 4, f"lote:{lote.id}") in movs
        assert ("liberacion", -4, f"lote:{lote.id}") in movs
        assert ("entrega", -4, f"venta:{v.id}") in movs
        # cerrar el lote ya no libera nada más (la reserva se consumió) y el stock queda igual
        lt.cambiar_estado(s, mundo["emp"], _u(s, mundo), lote.id, lt.CERRADO, None)
        s.commit()
        s.refresh(ep)
        assert (ep.stock, ep.reservado) == (6, 0)
        with pytest.raises(vt.ErrorVenta):  # una sola vez
            vt.registrar_entrega(s, mundo["emp"], _u(s, mundo), v.id, v.version)


def test_entrega_con_otra_reserva_ajena_no_deja_reservado_sobre_stock(engine, mundo):
    with Session(engine) as s:
        _con_lote_ofertado(s, mundo)
        ep = s.get(EmpresaProducto, mundo["ep"])
        ep.reservado = 8.0  # 4 del lote + 4 de otro lote
        s.commit()
        v = _venta(s, mundo)
        vt.registrar_entrega(s, mundo["emp"], _u(s, mundo), v.id, v.version)
        s.commit()
        s.refresh(ep)
        assert (ep.stock, ep.reservado) == (6, 4) and ep.reservado <= ep.stock


def test_entrega_sin_stock_suficiente_avisa_y_descuenta_lo_que_hay(engine, mundo):
    with Session(engine) as s:
        _oferta(s, mundo)  # stock propio
        ep = s.get(EmpresaProducto, mundo["ep"])
        ep.stock = 3.0  # alguien lo movió: faltan 1 saco
        s.commit()
        v = _venta(s, mundo)
        v, avisos = vt.registrar_entrega(s, mundo["emp"], _u(s, mundo), v.id, v.version)
        s.commit()
        s.refresh(ep)
        assert ep.stock == 0 and v.estado == "entregada"
        assert len(avisos) == 1 and "Arroz grado 1" in avisos[0]


def test_entrega_saca_del_stock_lo_comprado_y_no_cuenta_lineas_reemplazadas(engine, mundo):
    with Session(engine) as s:
        o = _oferta(s, mundo, stock=False)
        # una línea de stock guardada que luego quedó reemplazada no se descuenta
        o.lineas = [*o.lineas, {"linea_id": mundo["l2"], "precio_unitario": 1.0,
                                "proveedor_producto_id": None, "origen": "stock",
                                "empresa_producto_id": mundo["ep"]}]  # fmt: skip
        v = _venta(s, mundo)
        vt.registrar_compra(s, mundo["emp"], _u(s, mundo), v.id, v.version,
                            proveedor_producto_id=mundo["pp"], cantidad="4", precio_neto="20000")  # fmt: skip
        s.commit()
        assert s.get(EmpresaProducto, mundo["ep"]).stock == 14
        vt.registrar_entrega(s, mundo["emp"], _u(s, mundo), v.id, v.version)
        s.commit()
        assert s.get(EmpresaProducto, mundo["ep"]).stock == 10  # +4 de la compra, −4 de la entrega


def test_factura_pago_y_transiciones_invalidas(engine, mundo):
    with Session(engine) as s:
        _oferta(s, mundo)
        v = _venta(s, mundo)
        u = _u(s, mundo)
        with pytest.raises(vt.ErrorVenta):  # no se factura lo que no se entregó
            vt.registrar_factura(s, mundo["emp"], u, v.id, v.version, numero="F", fecha="2026-11-01")
        with pytest.raises(vt.ErrorVenta):
            vt.registrar_pago(s, mundo["emp"], u, v.id, v.version)
        vt.registrar_entrega(s, mundo["emp"], u, v.id, v.version)
        with pytest.raises(vt.ErrorVenta):  # entregada ya no se anula ni se vuelve a comprar
            vt.anular(s, mundo["emp"], u, v.id, v.version, "x")
        d = hoy_chile()
        vt.registrar_factura(s, mundo["emp"], u, v.id, v.version, numero="F-5", fecha=d.isoformat(),
                             plazo_pago_dias="45")  # fmt: skip
        assert v.estado == "facturada" and v.pago_esperado_en == d + timedelta(days=45)
        assert v.factura_monto == 96000.0  # sin monto: el de la oferta
        vt.registrar_pago(s, mundo["emp"], u, v.id, v.version, fecha=d.isoformat())
        s.commit()
        assert v.estado == "pagada" and v.pago_recibido_en == d
        estados = [e.datos["estado"] for e in s.execute(
            select(CaEvento).where(CaEvento.tipo == "venta").order_by(CaEvento.id)).scalars()]
        assert estados == ["adjudicada", "entregada", "facturada", "pagada"]


def test_plazo_de_pago_por_defecto_es_el_de_la_empresa(engine, mundo):
    with Session(engine) as s:
        s.get(Empresa, mundo["emp"]).plazo_pago_comprador_dias = 60
        v = _venta(s, mundo)
        assert v.plazo_pago_dias == 60
        vt.registrar_entrega(s, mundo["emp"], _u(s, mundo), v.id, v.version)
        d = hoy_chile()
        vt.registrar_factura(s, mundo["emp"], _u(s, mundo), v.id, v.version, numero="F",
                             fecha=d.isoformat(), monto="1000")  # fmt: skip
        assert v.pago_esperado_en == d + timedelta(days=60)


def test_anular_pide_motivo(engine, mundo):
    with Session(engine) as s:
        v = _venta(s, mundo)
        with pytest.raises(vt.ErrorVenta):
            vt.anular(s, mundo["emp"], _u(s, mundo), v.id, v.version, "  ")
        vt.anular(s, mundo["emp"], _u(s, mundo), v.id, v.version, "El comprador desistió")
        assert v.estado == "anulada" and v.motivo_anulacion == "El comprador desistió"


# ---------------------------------------------------------------------------
# Alarmas
# ---------------------------------------------------------------------------


def _kinds(s, mundo):
    return {e.tipo: e for e in s.execute(select(CaEvento).where(CaEvento.usuario_id.is_(None))).scalars()}


def test_alarma_requisitos_pendientes_con_cierre_en_menos_de_24h(engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
        _estado(s, mundo, "CA-1", eq.COTIZANDO)
        _estado(s, mundo, "CA-2", eq.COTIZANDO)
        s.get(CompraAgil, "CA-1").fecha_cierre = ahora_utc() + timedelta(hours=10)
        s.commit()  # CA-2 cierra en 4 días: no alarma
        ahora = ahora_utc()
        assert vt.generar_alarmas_seguimiento(s, mundo["emp"], ahora)["req_pendientes"] == 1
        s.commit()
        assert vt.generar_alarmas_seguimiento(s, mundo["emp"], ahora)["req_pendientes"] == 0
        ev = s.execute(select(CaEvento).where(CaEvento.tipo == "req_pendientes")).scalar_one()
        assert ev.ca_codigo == "CA-1"
        # una CA sin obligatorios pendientes no alarma
        _estado(s, mundo, "CA-2", eq.LISTA)
        for r in rq.listar(s, mundo["emp"], "CA-2"):
            rq.marcar(s, mundo["emp"], mundo["boris"], r.id, True)
        s.get(CompraAgil, "CA-2").fecha_cierre = ahora_utc() + timedelta(hours=5)
        s.commit()
        assert vt.generar_alarmas_seguimiento(s, mundo["emp"], ahora)["req_pendientes"] == 0
        # ni una enviada
        _estado(s, mundo, "CA-1", eq.PRIORIZADA)
        assert s.execute(select(func.count()).select_from(CaEvento)
                         .where(CaEvento.tipo == "req_pendientes")).scalar_one() == 1


def test_alarmas_de_ventas_cada_una_e_idempotentes(engine, mundo):
    with Session(engine) as s:
        ahora = ahora_utc()
        hoy = vt.hoy_de(ahora)
        # CA-1: adjudicada hace 4 días, sin OC
        v1 = _venta(s, mundo, "CA-1", creado_en=ahora - timedelta(days=4))
        # CA-2: con entrega comprometida en 2 días
        v2 = _venta(s, mundo, "CA-2", estado="oc_recibida",
                    entrega_fecha_comprometida=hoy + timedelta(days=2))  # fmt: skip
        r = vt.generar_alarmas_seguimiento(s, mundo["emp"], ahora)
        s.commit()
        assert r["oc_sin_registrar"] == 1 and r["entrega_vence"] == 1
        assert r["factura_pendiente"] == r["pago_atrasado"] == r["req_pendientes"] == 0
        assert vt.generar_alarmas_seguimiento(s, mundo["emp"], ahora) == dict.fromkeys(r, 0)
        # entregada hace 6 días sin factura; y una facturada con pago vencido
        v2.estado, v2.entrega_fecha_real = "entregada", hoy - timedelta(days=6)
        v1.estado, v1.pago_esperado_en = "facturada", hoy - timedelta(days=1)
        s.commit()
        r2 = vt.generar_alarmas_seguimiento(s, mundo["emp"], ahora)
        s.commit()
        assert r2["factura_pendiente"] == 1 and r2["pago_atrasado"] == 1
        assert vt.generar_alarmas_seguimiento(s, mundo["emp"], ahora) == dict.fromkeys(r, 0)
        tipos = {e.tipo for e in s.execute(select(CaEvento).where(CaEvento.usuario_id.is_(None))).scalars()}
        assert {"oc_sin_registrar", "entrega_vence", "factura_pendiente", "pago_atrasado"} <= tipos
        # nada de esto alarma a la otra empresa
        assert vt.generar_alarmas_seguimiento(s, mundo["otra"], ahora) == dict.fromkeys(r, 0)


def test_alarmas_entran_a_la_campana_y_se_describen(engine, mundo):
    with Session(engine) as s:
        ahora = ahora_utc()
        _venta(s, mundo, "CA-1", creado_en=ahora - timedelta(days=4))
        vt.generar_alarmas_seguimiento(s, mundo["emp"], ahora)
        s.commit()
        gon = _u(s, mundo, "gonzalo")
        gon.actividad_leida_hasta = ahora - timedelta(days=1)
        eventos = list(s.execute(eq.consulta_no_leidos(s, gon, mundo["emp"])).scalars())
        assert {e.tipo for e in eventos} >= {"oc_sin_registrar", "venta"}
        out = eq.describir_eventos(s, eventos, gon.id, ahora)
        textos = {o["tipo"]: o for o in out}
        assert textos["oc_sin_registrar"]["url"].startswith("/ventas/")
        assert "orden de compra" in textos["oc_sin_registrar"]["antes"]
        assert "Adjudicada" in textos["venta"]["antes"]


def test_generar_alarmas_todas_incluye_seguimiento(engine, mundo):
    with Session(engine) as s:
        _venta(s, mundo, "CA-1", creado_en=ahora_utc() - timedelta(days=4))
        total = eq.generar_alarmas_todas(s, ahora_utc())
        assert total["oc_sin_registrar"] == 1 and "resultado" in total


# ---------------------------------------------------------------------------
# Caja, «Hoy» y listados
# ---------------------------------------------------------------------------


def test_caja_tres_numeros_y_neto_a_30_y_60_dias(engine, mundo):
    with Session(engine) as s:
        _oferta(s, mundo, "CA-1")
        _oferta(s, mundo, "CA-2", linea="l3", cantidad=2.0, precio=30000.0)
        hoy = hoy_chile()
        # CA-1: facturada, se cobra en 10 días (factura 100.000)
        a = _venta(s, mundo, "CA-1", estado="facturada", factura_numero="F-1", factura_fecha=hoy,
                   factura_monto=100000.0, pago_esperado_en=hoy + timedelta(days=10))  # fmt: skip
        # CA-2: adjudicada sin facturar (OC 60.000), entrega comprometida hoy → cobro a 30 días
        b = _venta(s, mundo, "CA-2", estado="oc_recibida", oc_monto=60000.0,
                   entrega_fecha_comprometida=hoy)  # fmt: skip
        s.add(VentaCompra(venta_id=b.id, proveedor_id=mundo["molino"], descripcion="Arroz", cantidad=2,
                          precio_neto=10000.0, fecha=hoy, pagada=False, pagar_en=hoy + timedelta(days=5)))
        s.add(VentaCompra(venta_id=b.id, proveedor_id=mundo["molino"], descripcion="Otro", cantidad=1,
                          precio_neto=70000.0, fecha=hoy, pagada=False, pagar_en=hoy + timedelta(days=45)))
        s.add(VentaCompra(venta_id=a.id, proveedor_id=mundo["molino"], descripcion="Pagada", cantidad=1,
                          precio_neto=999999.0, fecha=hoy, pagada=True))
        s.commit()
        c = vt.caja(s, mundo["emp"], hoy)
        # F-plat-impuestos: la caja va en BRUTO (sin impuestos específicos aquí: solo IVA 19 %);
        # cada movimiento conserva su neto.
        assert c.por_cobrar_total == pytest.approx(119000.0)
        assert c.por_cobrar[0].neto == 100000.0
        assert c.comprometido_total == pytest.approx(107100.0)  # (2 × 10.000 + 70.000) × 1,19
        assert c.sin_facturar_total == pytest.approx(71400.0)
        # a 30 días: cobros 100.000 + 60.000 (hoy + 30) − pagos 20.000 (el de 45 días no entra)
        assert c.neto[30] == pytest.approx(140000.0 * 1.19)
        # a 60 días: cobros 160.000 − pagos 90.000 (ahora sí entra el de 45 días)
        assert c.neto[60] == pytest.approx(70000.0 * 1.19)
        assert [m.venta_id for m in c.por_cobrar] == [a.id]
        assert vt.caja(s, mundo["otra"], hoy).por_cobrar_total == 0.0


def test_caja_marca_vencidos(engine, mundo):
    with Session(engine) as s:
        hoy = hoy_chile()
        _venta(s, mundo, "CA-1", estado="facturada", factura_monto=1000.0,
               pago_esperado_en=hoy - timedelta(days=3))  # fmt: skip
        c = vt.caja(s, mundo["emp"], hoy)
        assert c.por_cobrar[0].vencido and c.neto[30] == pytest.approx(1190.0)  # vencido: de inmediato, en bruto


def test_resumen_de_hoy(engine, mundo):
    with Session(engine) as s:
        hoy = hoy_chile()
        _venta(s, mundo, "CA-1", estado="facturada", factura_monto=5000.0,
               pago_esperado_en=hoy + timedelta(days=20))  # fmt: skip
        _venta(s, mundo, "CA-2", estado="oc_recibida", entrega_fecha_comprometida=hoy + timedelta(days=3))
        r = vt.resumen_hoy(s, mundo["emp"], hoy)
        assert r == {"en_curso": 2, "por_cobrar": 5000.0, "entregas_semana": 1}
        assert vt.resumen_hoy(s, mundo["otra"], hoy) == {"en_curso": 0, "por_cobrar": 0.0,
                                                          "entregas_semana": 0}  # fmt: skip


def test_listado_marca_entrega_vencida_y_filtra_por_estado(engine, mundo):
    with Session(engine) as s:
        hoy = hoy_chile()
        _venta(s, mundo, "CA-1", estado="oc_recibida", entrega_fecha_comprometida=hoy - timedelta(days=1))
        _venta(s, mundo, "CA-2", estado="entregada")
        filas = vt.listar(s, mundo["emp"], hoy=hoy)
        assert {f.venta.ca_codigo: f.atrasada for f in filas} == {"CA-1": True, "CA-2": False}
        assert [f.venta.ca_codigo for f in vt.listar(s, mundo["emp"], "entregada")] == ["CA-2"]
        assert vt.conteo_por_estado(s, mundo["emp"]) == {"oc_recibida": 1, "entregada": 1}


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------


def test_rutas_requisitos_bloque_marcar_y_422_de_enviada(client, settings, engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
        _estado(s, mundo, "CA-1", eq.PRIORIZADA)
        s.commit()
    cookies, hdr, csrf = _auth(settings, mundo["gonzalo"])
    client.cookies.update(cookies)
    r = client.get("/requisitos/ca/CA-1")
    assert r.status_code == 200 and "data-requisitos" in r.text and "Cotización formal adjunta" in r.text
    assert 'data-req-pendientes="3"' in r.text
    with Session(engine) as s:
        rid = rq.listar(s, mundo["emp"], "CA-1")[0].id
    assert client.post(f"/requisitos/ca/CA-1/marcar/{rid}", data={"cumplido": "1"}).status_code == 403
    r = client.post(f"/requisitos/ca/CA-1/marcar/{rid}", data={"cumplido": "1", "csrf_token": csrf},
                    headers=hdr)  # fmt: skip
    assert r.status_code == 200 and 'data-req-pendientes="2"' in r.text
    r = client.post("/requisitos/ca/CA-1/agregar", data={"texto": "Foto del local", "csrf_token": csrf},
                    headers=hdr)  # fmt: skip
    assert r.status_code == 200 and "Foto del local" in r.text
    r = client.post("/requisitos/ca/CA-1/agregar", data={"texto": "Foto del local", "csrf_token": csrf},
                    headers=hdr)  # fmt: skip
    assert r.status_code == 422 and "Ya existe" in r.text
    # «Enviada» con pendientes: 422 con la lista; el usuario normal no ve «forzar»
    with Session(engine) as s:
        version = eq.trabajo_de(s, mundo["emp"], "CA-1").version
    r = client.post("/equipo/ca/CA-1/estado",
                    data={"estado": "enviada", "version": str(version), "origen": "ficha",
                          "csrf_token": csrf},
                    headers=hdr)  # fmt: skip
    assert r.status_code == 422 and "Faltan requisitos obligatorios" in r.text
    assert "forzar_motivo" not in r.text


def test_rutas_admin_fuerza_el_envio_con_motivo(client, settings, engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
        _estado(s, mundo, "CA-1", eq.PRIORIZADA)
        s.commit()
        version = eq.trabajo_de(s, mundo["emp"], "CA-1").version
    cookies, hdr, csrf = _auth(settings, mundo["boris"])
    client.cookies.update(cookies)
    base = {"estado": "enviada", "version": str(version), "origen": "ficha", "csrf_token": csrf}
    r = client.post("/equipo/ca/CA-1/estado", data=base, headers=hdr)
    assert r.status_code == 422 and "forzar_motivo" in r.text  # el admin ve el formulario
    r = client.post("/equipo/ca/CA-1/estado", data={**base, "forzar_motivo": "lo pidió Mario"}, headers=hdr)
    assert r.status_code == 200
    with Session(engine) as s:
        assert eq.trabajo_de(s, mundo["emp"], "CA-1").estado == eq.ENVIADA


def test_ruta_mesa_enviar_muestra_los_requisitos_pendientes(client, settings, engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
        _oferta(s, mundo)
        s.commit()
    cookies, hdr, csrf = _auth(settings, mundo["gonzalo"])
    client.cookies.update(cookies)
    r = client.post("/mesa/ca/CA-1/enviar", data={"csrf_token": csrf}, headers=hdr)
    assert r.status_code == 422 and "Faltan requisitos obligatorios" in r.text


def test_rutas_plantilla_de_la_empresa(client, settings, engine, mundo):
    with Session(engine) as s:
        _plantilla(s, mundo)
    cookies, hdr, csrf = _auth(settings, mundo["gonzalo"])
    client.cookies.update(cookies)
    r = client.get("/requisitos")
    assert r.status_code == 200 and "Cotización formal adjunta" in r.text
    r = client.post("/requisitos/plantilla/agregar", data={"texto": "Seguro vigente", "obligatorio": "1",
                                                          "csrf_token": csrf})  # fmt: skip
    assert r.status_code == 200 and "Seguro vigente" in r.text  # sigue la redirección
    with Session(engine) as s:
        ajeno = rq.plantilla(s, mundo["emp"])[0].id
    cookies_a, _, csrf_a = _auth(settings, mundo["ana"])
    client.cookies.update(cookies_a)
    assert client.post(f"/requisitos/plantilla/{ajeno}", data={"texto": "x", "csrf_token": csrf_a}).status_code == 404
    assert "Cotización formal adjunta" not in client.get("/requisitos").text


def test_rutas_ventas_flujo_y_ownership(client, settings, engine, mundo):
    with Session(engine) as s:
        _oferta(s, mundo, stock=False)
        v = _venta(s, mundo)
        vid, version = v.id, v.version
    cookies, hdr, csrf = _auth(settings, mundo["gonzalo"])
    client.cookies.update(cookies)
    assert client.get("/ventas").status_code == 200
    r = client.get(f"/ventas/{vid}")
    assert r.status_code == 200 and "Registrar OC" in r.text and "Arroz CA-1" in r.text
    assert client.post(f"/ventas/{vid}/oc", data={"oc_numero": "A", "oc_fecha": "2026-11-01"}).status_code == 403
    hoy = hoy_chile().isoformat()
    r = client.post(f"/ventas/{vid}/oc", data={"oc_numero": "OC-9", "oc_fecha": hoy, "oc_monto": "96000",
                                              "version": str(version), "csrf_token": csrf})  # fmt: skip
    assert r.status_code == 200 and "OC-9" in r.text and "Orden de compra registrada" in r.text
    # versión vieja → 409 con la pantalla
    r = client.post(f"/ventas/{vid}/oc", data={"oc_numero": "OC-10", "oc_fecha": hoy,
                                              "version": str(version), "csrf_token": csrf},
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 409 and "cambió esta venta" in r.text
    with Session(engine) as s:
        version = s.get(Venta, vid).version
    r = client.post(f"/ventas/{vid}/compras",
                    data={"proveedor_producto_id": str(mundo["pp"]), "cantidad": "4", "precio_neto": "20000",
                          "version": str(version), "csrf_token": csrf})  # fmt: skip
    assert r.status_code == 200 and "Compra registrada" in r.text and "80.000" in r.text
    r = client.post(f"/ventas/{vid}/compras", data={"cantidad": "1", "precio_neto": "1", "csrf_token": csrf})
    assert r.status_code == 422 and "Elige el producto" in r.text
    # factura sin entrega: 422
    with Session(engine) as s:
        version = s.get(Venta, vid).version
    r = client.post(f"/ventas/{vid}/factura", data={"factura_numero": "F", "factura_fecha": hoy,
                                                   "version": str(version), "csrf_token": csrf})  # fmt: skip
    assert r.status_code == 422 and "No se puede facturar" in r.text
    r = client.post(f"/ventas/{vid}/entrega", data={"version": str(version), "csrf_token": csrf})
    assert r.status_code == 200 and "Entrega registrada" in r.text
    # otra empresa: 404 en todo
    cookies_a, _, csrf_a = _auth(settings, mundo["ana"])
    client.cookies.update(cookies_a)
    assert client.get(f"/ventas/{vid}").status_code == 404
    assert client.post(f"/ventas/{vid}/pago", data={"csrf_token": csrf_a}).status_code == 404
    assert client.post(f"/ventas/{vid}/anular", data={"motivo": "x", "csrf_token": csrf_a}).status_code == 404
    assert "Arroz CA-1" not in client.get("/ventas").text and client.get("/caja").status_code == 200


def test_ruta_registrar_venta_a_mano_y_enlace_en_el_bloque(client, settings, engine, mundo):
    with Session(engine) as s:
        _estado(s, mundo, "CA-1", eq.ENVIADA)
        s.commit()
    cookies, hdr, csrf = _auth(settings, mundo["gonzalo"])
    client.cookies.update(cookies)
    assert "venta-registrar" in client.get("/requisitos/ca/CA-1").text
    r = client.post("/ventas/nueva", data={"codigo": "CA-1", "csrf_token": csrf})
    assert r.status_code == 200 and "Orden de compra" in r.text  # siguió al detalle
    assert "data-venta-enlace" in client.get("/requisitos/ca/CA-1").text
    assert client.post("/ventas/nueva", data={"codigo": "CA-2", "csrf_token": csrf}).status_code == 422


def test_ruta_caja_y_hoy(client, settings, engine, mundo):
    hoy = hoy_chile()
    with Session(engine) as s:
        _venta(s, mundo, "CA-1", estado="facturada", factura_monto=100000.0, factura_numero="F-1",
               pago_esperado_en=hoy + timedelta(days=10))  # fmt: skip
    cookies, _, _ = _auth(settings, mundo["gonzalo"])
    client.cookies.update(cookies)
    r = client.get("/caja")
    assert r.status_code == 200
    assert 'data-caja-total="por_cobrar"' in r.text and "$100.000" in r.text
    assert 'data-caja-neto="30"' in r.text and 'data-caja-neto="60"' in r.text
    r = client.get("/")
    assert 'data-hoy="ventas-en-curso"' in r.text and 'data-hoy="ventas-por-cobrar"' in r.text
    assert "$100.000" in r.text


def test_lote_enlaza_a_la_venta_de_la_ca_ganada(client, settings, engine, mundo):
    with Session(engine) as s:
        lote = _con_lote_ofertado(s, mundo)
        v = _venta(s, mundo)
        lote_id, venta_id = lote.id, v.id
    cookies, _, _ = _auth(settings, mundo["gonzalo"])
    client.cookies.update(cookies)
    r = client.get(f"/lotes/{lote_id}")
    assert r.status_code == 200 and f'href="/ventas/{venta_id}"' in r.text


def test_admin_edita_membrete_y_plazo_de_pago(client, settings, engine, mundo):
    cookies, _, csrf = _auth(settings, mundo["boris"])
    client.cookies.update(cookies)
    datos = {"nombre": "Distribuidora Ñuble", "rut": _RUT, "comuna_base": "Chillán", "regiones": ["16"],
             "comunas": "", "activa": "1", "dias_vigencia_precio": "15", "margen_objetivo": "15",
             "direccion": "Calle Nueva 5", "contacto": "Marta 555", "giro": "Alimentos",
             "condiciones_cotizacion": "Pago contra factura.", "plazo_pago_comprador_dias": "45",
             "csrf_token": csrf}  # fmt: skip
    r = client.post(f"/admin/empresas/{mundo['emp']}", data=datos)
    assert r.status_code == 200
    with Session(engine) as s:
        e = s.get(Empresa, mundo["emp"])
        assert (e.direccion, e.contacto, e.giro) == ("Calle Nueva 5", "Marta 555", "Alimentos")
        assert e.condiciones_cotizacion == "Pago contra factura." and e.plazo_pago_comprador_dias == 45
    r = client.post(f"/admin/empresas/{mundo['emp']}", data={**datos, "plazo_pago_comprador_dias": "999"})
    assert r.status_code == 422 and "De 0 a 365" in r.text


def test_crear_empresa_siembra_la_plantilla(client, settings, engine, mundo):
    cookies, _, csrf = _auth(settings, mundo["boris"])
    client.cookies.update(cookies)
    r = client.post("/admin/empresas/nueva", data={"nombre": "Nueva SpA", "rut": "", "comuna_base": "",
                                                  "regiones": ["16"], "comunas": "", "csrf_token": csrf})
    assert r.status_code == 200
    with Session(engine) as s:
        nueva = s.execute(select(Empresa).where(Empresa.nombre == "Nueva SpA")).scalar_one()
        assert [i.texto for i in rq.plantilla(s, nueva.id)] == [t for t, _ in rq.PLANTILLA_INICIAL]


def test_salud_cuenta_las_tablas_nuevas(engine, mundo):
    from app.api.salud_data import _tablas_mercado_salud

    with Session(engine) as s:
        nombres = {t["tabla"] for t in _tablas_mercado_salud(s)}
    assert {"empresa_requisitos", "ca_requisitos", "ventas", "venta_compras"} <= nombres


def test_ca_trabajo_sin_estado_adjudicada(engine, mundo):
    """Decisión de la fase: el estado de la venta vive en `ventas`; `ca_trabajo` no suma estado."""
    assert "adjudicada" not in eq.ESTADOS
    with Session(engine) as s:
        v = _venta(s, mundo)
        assert s.execute(select(CaTrabajo)).first() is None
        assert v.estado == "adjudicada"
