"""F-plat-3: empresa con territorio priorizado, proveedores y costos con historial."""

from __future__ import annotations

import base64
import io
from datetime import date, timedelta

import pytest
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
from app.mercado import importar
from app.mercado.proveedores import (
    asegurar_tarifas_iniciales,
    guardar_producto,
    mejor_costo,
    variacion,
)
from app.mercado.territorio import regiones_activas
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaProducto,
    CompraAgil,
    CostoHistorial,
    Empresa,
    Proveedor,
    ProveedorProducto,
    TarifaLogistica,
    Usuario,
)

_PW = "contraseña-segura-test"


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
    return TestClient(create_app(settings, engine), raise_server_exceptions=True)


def _empresa(session: Session, nombre: str, regiones=None, activa=True) -> Empresa:
    e = Empresa(
        nombre=nombre,
        regiones_prioritarias=regiones if regiones is not None else [16],
        comunas_prioritarias=[],
        activa=activa,
    )
    session.add(e)
    session.flush()
    return e


def _usuario(session: Session, email: str, empresa_id=None, rol=RolUsuario.USUARIO) -> int:
    u = Usuario(
        email=email, password_hash=hash_password(_PW), rol=rol, activo=True, empresa_id=empresa_id
    )
    session.add(u)
    session.flush()
    return u.id


def _sesion(settings: Settings, user_id: int) -> tuple[dict[str, str], dict[str, str]]:
    token = create_session_token(settings.secret_key, user_id)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    return (
        {COOKIE_NAME: token},
        {"X-CSRF-Token": generate_csrf_token(settings.secret_key, decoded[1])},
    )


def _proveedor(session: Session, empresa_id: int, nombre="Dist Norte", **kw) -> Proveedor:
    p = Proveedor(empresa_id=empresa_id, nombre=nombre, rubros=[], despacha=False, activo=True, **kw)
    session.add(p)
    session.flush()
    return p


# ---------------------------------------------------------------------------
# Territorio
# ---------------------------------------------------------------------------


def test_regiones_activas_union_ordenada(engine, settings):
    with Session(engine) as s:
        _empresa(s, "A", [16])
        _empresa(s, "B", [16, 8])
        _empresa(s, "C inactiva", [5], activa=False)
        s.commit()
        assert regiones_activas(s, settings) == [8, 16]


def test_regiones_activas_sin_empresas_o_todas_inactivas_usa_el_setting(engine, settings):
    with Session(engine) as s:
        assert regiones_activas(s, settings) == [16]
        _empresa(s, "Inactiva", [5], activa=False)
        s.commit()
        assert regiones_activas(s, settings) == [16]


def test_backfill_usa_las_regiones_de_las_empresas(engine, settings):
    from app.ingest.mercado import Backfill

    with Session(engine) as s:
        _empresa(s, "B", [16, 8])
        s.commit()
    assert Backfill(settings, engine, object()).regiones == [8, 16]  # type: ignore[arg-type]
    assert Backfill(settings, engine, object(), regiones=[3]).regiones == [3]  # type: ignore[arg-type]


def test_retencion_no_purga_ca_de_region_agregada_por_una_empresa(engine, settings):
    from app.ingest.orchestrator import run_retencion

    vieja = ahora_utc() - timedelta(days=200)
    with Session(engine) as s:
        _empresa(s, "B", [16, 8])
        for codigo, region in (("R8", 8), ("R13", 13)):
            s.add(
                CompraAgil(
                    codigo=codigo, nombre=codigo, estado="desierta", region=region,
                    raw_json={"x": 1}, actualizado_en=vieja,
                )
            )  # fmt: skip
            s.add(CaProducto(ca_codigo=codigo, codigo_producto="50131700", nombre="Leche"))
        s.commit()
    run_retencion(engine, settings)
    with Session(engine) as s:
        assert s.get(CompraAgil, "R8").raw_json == {"x": 1}  # type: ignore[union-attr]
        assert s.get(CompraAgil, "R13") is None  # fuera del territorio: se borra (F-plat-purga-legado)


# ---------------------------------------------------------------------------
# Costos con historial
# ---------------------------------------------------------------------------


def test_historial_un_cambio_una_fila_y_precio_igual_no_agrega(engine):
    with Session(engine) as s:
        e = _empresa(s, "A")
        p = _proveedor(s, e.id)
        kw = dict(producto_texto="Arroz grado 1", formato_texto="saco 25 kg", factor_base=25,
                  unidad_base="kg", fuente="manual")  # fmt: skip
        pp, est = guardar_producto(s, p, precio_neto=27000, vigente_desde=date(2026, 1, 1), **kw)
        assert est == "nuevo"
        _, est = guardar_producto(s, p, precio_neto=27000, vigente_desde=date(2026, 2, 1), **kw)
        assert est == "igual"
        _, est = guardar_producto(s, p, precio_neto=27500, vigente_desde=date(2026, 6, 1), **kw)
        assert est == "cambiado"
        _, est = guardar_producto(s, p, precio_neto=30250, vigente_desde=date(2026, 10, 1), **kw)
        assert est == "cambiado"
        s.commit()
        filas = list(
            s.execute(
                select(CostoHistorial).order_by(CostoHistorial.vigente_desde)
            ).scalars()
        )
        assert [f.precio_neto for f in filas] == [27000, 27500, 30250]
        assert pp.precio_neto_actual == 30250
        v = variacion(filas, pp.precio_neto_actual, hoy=date(2026, 10, 15))
        assert v.anterior == 27500
        assert v.pct_vs_anterior == 10.0
        assert v.hace_90 == 27500  # último precio con vigencia <= 17-jul-2026
        assert v.pct_vs_90 == 10.0


def test_guardar_producto_valida(engine):
    with Session(engine) as s:
        p = _proveedor(s, _empresa(s, "A").id)
        for kw in (
            {"precio_neto": 0},
            {"precio_neto": 10, "factor_base": 2},  # factor sin unidad
            {"precio_neto": 10, "factor_base": 2, "unidad_base": "m"},
        ):
            with pytest.raises(ValueError):
                guardar_producto(s, p, producto_texto="X", **kw)


def test_mejor_costo_saco_25kg_gana_a_bolsa_1kg(engine):
    with Session(engine) as s:
        e = _empresa(s, "A")
        otra = _empresa(s, "B")
        mayorista = _proveedor(s, e.id, "Mayorista")
        minorista = _proveedor(s, e.id, "Minorista")
        inactivo = _proveedor(s, e.id, "Inactivo")
        inactivo.activo = False
        ajeno = _proveedor(s, otra.id, "Ajeno")
        kw = dict(producto_texto="Arroz grado 1", unidad_base="kg")
        guardar_producto(s, mayorista, formato_texto="saco 25 kg", factor_base=25, precio_neto=27500, **kw)
        guardar_producto(s, minorista, formato_texto="bolsa 1 kg", factor_base=1, precio_neto=1300, **kw)
        guardar_producto(s, inactivo, formato_texto="saco", factor_base=25, precio_neto=1000, **kw)
        guardar_producto(s, ajeno, formato_texto="saco", factor_base=25, precio_neto=500, **kw)
        # No compiten: sin factor/unidad y no disponible.
        guardar_producto(s, minorista, producto_texto="ARROZ  grado 1", formato_texto="a granel", precio_neto=1)
        guardar_producto(s, mayorista, formato_texto="saco 50 kg", factor_base=50, precio_neto=1000,
                         disponible=False, **kw)  # fmt: skip
        s.commit()
        r = mejor_costo(s, e.id, producto_texto="arroz GRADO 1")
        assert r is not None
        assert r.unidad_base == "kg"
        assert r.ganador.proveedor == "Mayorista"
        assert r.ganador.precio_por_unidad_base == 1100
        assert [c.proveedor for c in r.candidatos] == ["Mayorista", "Minorista"]
        assert mejor_costo(s, e.id, producto_texto="café") is None
        assert mejor_costo(s, e.id, producto_texto="Arroz grado 1", unidad_base="L") is None


# ---------------------------------------------------------------------------
# Importación
# ---------------------------------------------------------------------------


def _xlsx(precios, condiciones=None, logistica=None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "precios"
    ws.append(importar.COLUMNAS_PRECIOS)
    for f in precios:
        ws.append(f)
    if condiciones is not None:
        ws = wb.create_sheet("condiciones")
        ws.append(importar.COLUMNAS_CONDICIONES)
        for f in condiciones:
            ws.append(f)
    if logistica is not None:
        ws = wb.create_sheet("logistica")
        ws.append(importar.COLUMNAS_LOGISTICA)
        for f in logistica:
            ws.append(f)
    salida = io.BytesIO()
    wb.save(salida)
    return salida.getvalue()


_F1 = ["Dist Norte", "76.123.456-7", "Chillán", "Ana", "+56 9 1", "Arroz grado 1", "saco 25 kg",
       25, "kg", 27500, "2026-11-03"]  # fmt: skip
_F2 = ["Dist Norte", "76123456-7", "Chillán", "Ana", "+56 9 1", "Leche entera", "caja 1 L",
       1, "L", 890, "2026-11-03"]  # fmt: skip


def test_importar_nueva_repetida_cambiada_e_invalida(engine):
    with Session(engine) as s:
        e = _empresa(s, "A")
        s.commit()
        eid = e.id

    archivo = _xlsx(
        [
            _F1,
            _F2,
            ["EJEMPLO Dist X", "", "", "", "", "Leche", "caja", 1, "L", 100, None],
            ["Dist Norte", "", "", "", "", "", "caja", 1, "L", 100, None],  # sin producto
            ["Dist Norte", "", "", "", "", "Sal", "saco", 25, None, 100, None],  # factor sin unidad
            ["Dist Norte", "", "", "", "", "Sal", "saco", None, None, "abc", None],  # precio malo
        ],
        condiciones=[
            ["Dist Norte", "v@x.cl", "Av. 1", "lácteos, abarrotes", 30, 50000, "si", "notas"],
            ["Fantasma", None, None, None, None, None, None, None],
        ],
        logistica=[
            ["provincia", "Diguillín", 15000, 12, 2, None],
            ["provincia", "Itata", None, None, None, "solo nota"],  # sin valores: se ignora
            ["comuna", "(opcional, ej. Cobquecura)", None, None, None, None],
        ],
    )
    planilla = importar.parsear(archivo, "p.xlsx")
    assert planilla.ejemplos == ["EJEMPLO Dist X"]
    assert len(planilla.precios) == 2
    assert len(planilla.errores) == 3
    assert len(planilla.logistica) == 1

    with Session(engine) as s:
        r = importar.aplicar(s, eid, planilla, usuario_id=None)
        s.commit()
    assert (r.proveedores_nuevos, r.productos_nuevos, r.precios_cambiados) == (1, 2, 0)
    assert r.condiciones_actualizadas == 1
    assert r.tarifas_nuevas == 1
    assert any("Fantasma" in err for err in r.errores)

    # La misma planilla otra vez: nada nuevo, sin historial extra.
    with Session(engine) as s:
        r2 = importar.aplicar(s, eid, planilla)
        s.commit()
        assert (r2.proveedores_nuevos, r2.productos_nuevos, r2.precios_cambiados) == (0, 0, 0)
        assert r2.sin_cambios == 2 and r2.condiciones_actualizadas == 0
        assert r2.tarifas_nuevas == 0 and r2.tarifas_actualizadas == 0
        assert s.scalar(select(func.count()).select_from(CostoHistorial)) == 2
        prov = s.scalars(select(Proveedor)).one()
        assert prov.rut == "76123456-7" and prov.rubros == ["lácteos", "abarrotes"]
        assert prov.despacha is True and prov.minimo_compra_clp == 50000

    # Precio cambiado (el mismo proveedor, ahora sin RUT en la fila: por nombre normalizado).
    f1b = [*_F1]
    f1b[1], f1b[9] = "", 28000
    p3 = importar.parsear(_xlsx([f1b]), "p.xlsx")
    with Session(engine) as s:
        r3 = importar.aplicar(s, eid, p3)
        s.commit()
        assert (r3.proveedores_nuevos, r3.precios_cambiados) == (0, 1)
        assert s.scalar(select(func.count()).select_from(Proveedor)) == 1
        assert s.scalar(select(func.count()).select_from(CostoHistorial)) == 3


def test_importar_csv_con_punto_y_coma_y_precios_a_la_chilena(engine):
    csv = (
        "proveedor;rut;comuna;contacto;telefono;producto;formato;factor_base;unidad_base;"
        "precio_neto;vigente_desde\n"
        "Dist Sur;;Bulnes;;;Aceite;botella 1 L;1;lt;2.490;03/11/2026\n"
    ).encode("cp1252")
    p = importar.parsear(csv, "p.csv")
    assert not p.errores
    assert p.precios[0].precio_neto == 2490
    assert p.precios[0].unidad_base == "L"
    assert p.precios[0].vigente_desde == date(2026, 11, 3)


def test_importar_rechaza_formato_y_hoja_faltante():
    with pytest.raises(importar.PlanillaInvalida):
        importar.parsear(b"x", "p.txt")
    with pytest.raises(importar.PlanillaInvalida):
        importar.parsear(b"no es un zip", "p.xlsx")
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "otra"
    b = io.BytesIO()
    wb.save(b)
    with pytest.raises(importar.PlanillaInvalida, match="precios"):
        importar.parsear(b.getvalue(), "p.xlsx")


def test_plantilla_descargable_se_lee_y_sus_ejemplos_se_ignoran():
    p = importar.parsear(importar.generar_plantilla_xlsx(), "plantilla.xlsx")
    assert p.ejemplos == ["EJEMPLO Distribuidora X (borrar)"]
    assert not p.precios and not p.condiciones and not p.logistica and not p.errores


@pytest.mark.datos_locales
def test_plantilla_real_de_boris_se_lee():
    from pathlib import Path

    ruta = Path(__file__).resolve().parent.parent / "data" / "mercado" / "proveedores_plantilla.xlsx"
    if not ruta.exists():
        pytest.skip("plantilla de data/mercado no disponible")  # pragma: no cover
    p = importar.parsear(ruta.read_bytes(), "proveedores_plantilla.xlsx")
    assert not p.precios and not p.errores and p.ejemplos


# ---------------------------------------------------------------------------
# Rutas: empresas (admin)
# ---------------------------------------------------------------------------


def test_admin_empresas_crear_editar_asignar_y_quitar(client, engine, settings):
    with Session(engine) as s:
        admin = _usuario(s, "admin@t.cl", rol=RolUsuario.ADMIN)
        libre = _usuario(s, "libre@t.cl")
        s.commit()
    ck, h = _sesion(settings, admin)

    r = client.post("/admin/empresas/nueva", cookies=ck, headers=h, follow_redirects=False,
                    data={"nombre": "Mi Empresa", "rut": "76.111.222-3", "comuna_base": "Chillán",
                          "regiones": ["16", "8"], "comunas": "Chillán, Chillán Viejo, chillán"})  # fmt: skip
    assert r.status_code == 303
    with Session(engine) as s:
        e = s.scalars(select(Empresa)).one()
        assert e.regiones_prioritarias == [8, 16]
        assert e.comunas_prioritarias == ["Chillán", "Chillán Viejo"]
        assert e.rut == "76111222-3" and e.activa
        eid = e.id
        provincias = {t.nombre for t in s.scalars(select(TarifaLogistica))}
        assert provincias == {"Diguillín", "Itata", "Punilla"}

    # Validaciones: sin región, RUT repetido, RUT inválido.
    for datos in (
        {"nombre": "X", "regiones": []},
        {"nombre": "X", "rut": "76111222-3", "regiones": ["16"]},
        {"nombre": "X", "rut": "abc", "regiones": ["16"]},
        {"nombre": " ", "regiones": ["16"]},
        {"nombre": "X", "regiones": ["99"]},
    ):
        assert client.post("/admin/empresas/nueva", cookies=ck, headers=h, data=datos).status_code == 422

    # Editar y desactivar.
    r = client.post(f"/admin/empresas/{eid}", cookies=ck, headers=h, follow_redirects=False,
                    data={"nombre": "Mi Empresa SpA", "regiones": ["16"], "comunas": "", "activa": ""})  # fmt: skip
    assert r.status_code == 303
    with Session(engine) as s:
        e = s.get(Empresa, eid)
        assert e.nombre == "Mi Empresa SpA" and e.activa is False and e.regiones_prioritarias == [16]
    client.post(f"/admin/empresas/{eid}", cookies=ck, headers=h,
                data={"nombre": "Mi Empresa SpA", "regiones": ["16"], "activa": "1"})  # fmt: skip

    # Asignar y quitar.
    r = client.post(f"/admin/empresas/{eid}/usuarios/asignar", cookies=ck, headers=h,
                    data={"usuario_id": libre}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(Usuario, libre).empresa_id == eid
    assert client.get(f"/admin/empresas/{eid}", cookies=ck).status_code == 200
    assert "libre@t.cl" in client.get(f"/admin/empresas/{eid}", cookies=ck).text
    client.post(f"/admin/empresas/{eid}/usuarios/{libre}/quitar", cookies=ck, headers=h)
    with Session(engine) as s:
        assert s.get(Usuario, libre).empresa_id is None
    assert client.get("/admin/empresas", cookies=ck).status_code == 200
    assert client.get("/admin/empresas/999", cookies=ck).status_code == 404


def test_un_usuario_pertenece_a_una_sola_empresa(client, engine, settings):
    with Session(engine) as s:
        admin = _usuario(s, "admin@t.cl", rol=RolUsuario.ADMIN)
        a, b = _empresa(s, "A"), _empresa(s, "B")
        u = _usuario(s, "u@t.cl", a.id)
        s.commit()
        bid = b.id
    ck, h = _sesion(settings, admin)
    r = client.post(f"/admin/empresas/{bid}/usuarios/asignar", cookies=ck, headers=h,
                    data={"usuario_id": u}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and "error=" in r.headers["location"]
    with Session(engine) as s:
        assert s.get(Usuario, u).empresa_id != bid


def test_admin_empresas_solo_admin_y_csrf(client, engine, settings):
    with Session(engine) as s:
        u = _usuario(s, "u@t.cl")
        admin = _usuario(s, "admin@t.cl", rol=RolUsuario.ADMIN)
        s.commit()
    ck, h = _sesion(settings, u)
    assert client.get("/admin/empresas", cookies=ck).status_code == 403
    assert client.post("/admin/empresas/nueva", cookies=ck, headers=h,
                       data={"nombre": "X", "regiones": ["16"]}).status_code == 403  # fmt: skip
    cka, _ = _sesion(settings, admin)
    sin_csrf = client.post("/admin/empresas/nueva", cookies=cka,
                           data={"nombre": "X", "regiones": ["16"]})  # fmt: skip
    assert sin_csrf.status_code == 403


def test_crear_usuario_con_empresa(client, engine, settings):
    with Session(engine) as s:
        admin = _usuario(s, "admin@t.cl", rol=RolUsuario.ADMIN)
        e = _empresa(s, "A")
        s.commit()
        eid = e.id
    ck, h = _sesion(settings, admin)
    datos = {"email": "nuevo@t.cl", "password": "12345678", "rol": "usuario"}
    for extra in ({"empresa_id": str(eid)}, {"empresa_id": ""}):
        email = f"{len(extra['empresa_id'])}{datos['email']}"
        client.post("/admin/usuarios/nuevo", cookies=ck, headers=h, data={**datos, **extra, "email": email})
    with Session(engine) as s:
        con = s.scalars(select(Usuario).where(Usuario.email == f"{len(str(eid))}nuevo@t.cl")).one()
        sin = s.scalars(select(Usuario).where(Usuario.email == "0nuevo@t.cl")).one()
        assert con.empresa_id == eid and sin.empresa_id is None
    r = client.post("/admin/usuarios/nuevo", cookies=ck, headers=h, follow_redirects=False,
                    data={**datos, "email": "x@t.cl", "empresa_id": "999"})  # fmt: skip
    assert "error=" in r.headers["location"]
    assert client.get("/admin/usuarios", cookies=ck).status_code == 200


# ---------------------------------------------------------------------------
# Rutas: proveedores y ownership (regla 17/26)
# ---------------------------------------------------------------------------


@pytest.fixture()
def dos_empresas(engine, settings):
    with Session(engine) as s:
        a, b = _empresa(s, "A"), _empresa(s, "B")
        ua = _usuario(s, "a@t.cl", a.id)
        ub = _usuario(s, "b@t.cl", b.id)
        admin_sin = _usuario(s, "admin@t.cl", rol=RolUsuario.ADMIN)
        admin_con = _usuario(s, "boris@t.cl", a.id, rol=RolUsuario.ADMIN)
        sin = _usuario(s, "sin@t.cl")
        pa = _proveedor(s, a.id, "Prov de A")
        pb = _proveedor(s, b.id, "Prov de B")
        ppb, _ = guardar_producto(s, pb, producto_texto="Sal", formato_texto="saco", precio_neto=500)
        s.commit()
        return {
            "ua": ua, "ub": ub, "admin_sin": admin_sin, "admin_con": admin_con, "sin": sin,
            "pa": pa.id, "pb": pb.id, "ppb": ppb.id, "a": a.id, "b": b.id,
        }  # fmt: skip


def test_usuario_de_a_no_ve_ni_edita_proveedor_de_b(client, settings, engine, dos_empresas):
    d = dos_empresas
    ck, h = _sesion(settings, d["ua"])
    pb, ppb = d["pb"], d["ppb"]
    assert client.get(f"/proveedores/{pb}", cookies=ck).status_code == 404
    assert client.get(f"/proveedores/{pb}/form", cookies=ck).status_code == 404
    assert client.get(f"/proveedores/{pb}/productos/form", cookies=ck).status_code == 404
    assert client.get(f"/proveedores/{pb}/productos/{ppb}/form", cookies=ck).status_code == 404
    assert client.get(f"/proveedores/{pb}/productos/{ppb}/historial", cookies=ck).status_code == 404
    datos = {"nombre": "Hackeado"}
    assert client.post(f"/proveedores/{pb}", cookies=ck, headers=h, data=datos).status_code == 404
    assert client.post(f"/proveedores/{pb}/activo", cookies=ck, headers=h).status_code == 404
    assert client.post(f"/proveedores/{pb}/productos", cookies=ck, headers=h,
                       data={"producto_texto": "X", "precio_neto": "1"}).status_code == 404  # fmt: skip
    assert client.post(f"/proveedores/{pb}/productos/{ppb}", cookies=ck, headers=h,
                       data={"precio_neto": "1"}).status_code == 404  # fmt: skip
    # Un producto de B colgado de un proveedor de A tampoco existe.
    assert client.get(f"/proveedores/{d['pa']}/productos/{ppb}/form", cookies=ck).status_code == 404
    with Session(engine) as s:
        assert s.get(Proveedor, pb).nombre == "Prov de B" and s.get(Proveedor, pb).activo
        assert s.get(ProveedorProducto, ppb).precio_neto_actual == 500
    # La lista solo trae lo propio.
    lista = client.get("/proveedores", cookies=ck).text
    assert "Prov de A" in lista and "Prov de B" not in lista


def test_admin_sin_empresa_no_ve_ni_edita_proveedores_admin_con_empresa_solo_los_suyos(
    client, settings, dos_empresas
):
    d = dos_empresas
    ck, h = _sesion(settings, d["admin_sin"])
    aviso = client.get("/proveedores", cookies=ck)
    assert aviso.status_code == 200 and "asigne a una empresa" in aviso.text
    for pid in (d["pa"], d["pb"]):
        assert client.get(f"/proveedores/{pid}", cookies=ck).status_code == 403
        assert client.post(f"/proveedores/{pid}", cookies=ck, headers=h,
                           data={"nombre": "X"}).status_code == 403  # fmt: skip
    assert client.post("/proveedores/nuevo", cookies=ck, headers=h, data={"nombre": "X"}).status_code == 403

    ck, h = _sesion(settings, d["admin_con"])
    assert client.get(f"/proveedores/{d['pa']}", cookies=ck).status_code == 200
    assert client.get(f"/proveedores/{d['pb']}", cookies=ck).status_code == 404


def test_usuario_sin_empresa_o_con_empresa_inactiva_ve_el_aviso(client, settings, engine, dos_empresas):
    d = dos_empresas
    ck, _ = _sesion(settings, d["sin"])
    assert "asigne a una empresa" in client.get("/proveedores", cookies=ck).text
    assert client.get("/proveedores/importar", cookies=ck).status_code == 403
    assert client.get("/proveedores/logistica", cookies=ck).status_code == 403
    with Session(engine) as s:
        s.get(Empresa, d["a"]).activa = False
        s.commit()
    ck, _ = _sesion(settings, d["ua"])
    assert "asigne a una empresa" in client.get("/proveedores", cookies=ck).text
    assert client.get(f"/proveedores/{d['pa']}", cookies=ck).status_code == 403


def test_proveedores_requieren_login(client):
    for ruta in ("/proveedores", "/proveedores/plantilla.xlsx", "/proveedores/importar", "/proveedores/logistica"):
        r = client.get(ruta, follow_redirects=False)
        assert r.status_code in (302, 303, 307), ruta
        assert "/login" in r.headers["location"]


def test_mutaciones_exigen_csrf(client, settings, engine, dos_empresas):
    d = dos_empresas
    ck, _ = _sesion(settings, d["ua"])
    assert client.post("/proveedores/nuevo", cookies=ck, data={"nombre": "X"}).status_code == 403
    assert client.post(f"/proveedores/{d['pa']}", cookies=ck, data={"nombre": "X"}).status_code == 403
    assert client.post(f"/proveedores/{d['pa']}/activo", cookies=ck).status_code == 403
    assert client.post(f"/proveedores/{d['pa']}/productos", cookies=ck,
                       data={"producto_texto": "X", "precio_neto": "1"}).status_code == 403  # fmt: skip
    assert client.post("/proveedores/logistica", cookies=ck, data={}).status_code == 403
    assert client.post("/proveedores/importar/confirmar", cookies=ck, data={}).status_code == 403
    with Session(engine) as s:
        assert s.scalars(select(Proveedor).where(Proveedor.empresa_id == d["a"])).one().nombre == "Prov de A"


def test_crud_proveedor_y_producto_con_historial_por_htmx(client, settings, engine, dos_empresas):
    d = dos_empresas
    ck, h = _sesion(settings, d["ua"])
    hx = {**h, "HX-Request": "true"}

    assert client.get("/proveedores/nuevo/form", cookies=ck).status_code == 200
    # Error de validación: 422 con el formulario y el mensaje.
    r = client.post("/proveedores/nuevo", cookies=ck, headers=hx, data={"nombre": "", "rut": "mal"})
    assert r.status_code == 422 and "obligatorio" in r.text and "RUT inválido" in r.text
    r = client.post("/proveedores/nuevo", cookies=ck, headers=hx,
                    data={"nombre": "Dist Sur", "rut": "76.555.444-3", "rubros": "lácteos, abarrotes",
                          "plazo_pago_dias": "30", "minimo_compra_clp": "50.000", "despacha": "1"})  # fmt: skip
    assert r.status_code == 200 and r.headers["HX-Redirect"].startswith("/proveedores/")
    pid = int(r.headers["HX-Redirect"].rsplit("/", 1)[1])
    # Duplicado por nombre.
    dup = client.post("/proveedores/nuevo", cookies=ck, headers=hx, data={"nombre": "dist  sur"})
    assert dup.status_code == 422 and "Ya tienes" in dup.text

    # Productos: nuevo, mismo producto y formato, precio cambiado.
    nuevo = {"producto_texto": "Arroz", "formato_texto": "saco 25 kg", "precio_neto": "27.500",
             "factor_base": "25", "unidad_base": "kg", "vigente_desde": "2026-01-01", "disponible": "1"}  # fmt: skip
    assert client.post(f"/proveedores/{pid}/productos", cookies=ck, headers=hx, data=nuevo).status_code == 200
    assert client.post(f"/proveedores/{pid}/productos", cookies=ck, headers=hx, data=nuevo).status_code == 422
    malo = client.post(f"/proveedores/{pid}/productos", cookies=ck, headers=hx,
                       data={**nuevo, "producto_texto": "Otro", "factor_base": "", "precio_neto": "0"})  # fmt: skip
    assert malo.status_code == 422
    with Session(engine) as s:
        ppid = s.scalars(select(ProveedorProducto).where(ProveedorProducto.proveedor_id == pid)).one().id
    assert client.get(f"/proveedores/{pid}/productos/{ppid}/form", cookies=ck).status_code == 200
    cambio = {"producto_texto": "ignorado", "formato_texto": "ignorado", "precio_neto": "30250",
              "factor_base": "25", "unidad_base": "kg", "vigente_desde": "2026-10-01", "disponible": "1"}  # fmt: skip
    assert client.post(f"/proveedores/{pid}/productos/{ppid}", cookies=ck, headers=hx, data=cambio).status_code == 200
    # Mismo precio otra vez: sin fila nueva.
    client.post(f"/proveedores/{pid}/productos/{ppid}", cookies=ck, headers=hx, data=cambio)
    with Session(engine) as s:
        pp = s.get(ProveedorProducto, ppid)
        assert pp.producto_texto == "Arroz" and pp.precio_neto_actual == 30250
        precios = [
            x.precio_neto
            for x in s.scalars(
                select(CostoHistorial)
                .where(CostoHistorial.proveedor_producto_id == ppid)
                .order_by(CostoHistorial.id)
            )
        ]
        assert precios == [27500, 30250]
        assert s.get(Proveedor, pid).minimo_compra_clp == 50000

    ficha = client.get(f"/proveedores/{pid}", cookies=ck)
    assert ficha.status_code == 200 and "30.250" in ficha.text and "1.210" in ficha.text and "10.0%" in ficha.text
    hist = client.get(f"/proveedores/{pid}/productos/{ppid}/historial", cookies=ck)
    assert hist.status_code == 200 and "27.500" in hist.text

    # Búsqueda por nombre, rubro y comuna; desactivar.
    assert "Dist Sur" in client.get("/proveedores?q=LACTEOS", cookies=ck).text
    assert "Dist Sur" not in client.get("/proveedores?q=zzz", cookies=ck).text
    client.post(f"/proveedores/{pid}/activo", cookies=ck, headers=h)
    with Session(engine) as s:
        assert s.get(Proveedor, pid).activo is False
    # Editar proveedor.
    r = client.post(f"/proveedores/{pid}", cookies=ck, headers=hx,
                    data={"nombre": "Dist Sur SpA", "rut": "76555444-3"})  # fmt: skip
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(Proveedor, pid).nombre == "Dist Sur SpA"


def test_importar_vista_previa_no_escribe_y_confirmar_es_idempotente(client, settings, engine, dos_empresas):
    d = dos_empresas
    ck, h = _sesion(settings, d["ua"])
    assert client.get("/proveedores/plantilla.xlsx", cookies=ck).headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )
    assert client.get("/proveedores/importar", cookies=ck).status_code == 200
    archivo = _xlsx([_F1, _F2])
    fx = {"archivo": ("p.xlsx", archivo, "application/octet-stream")}

    previa = client.post("/proveedores/importar", cookies=ck, headers=h, files=fx, data={"csrf_token": h["X-CSRF-Token"]})
    assert previa.status_code == 200 and "Vista previa" in previa.text
    with Session(engine) as s:
        # Solo existe el historial del producto de B que trae el fixture.
        assert s.scalar(select(func.count()).select_from(CostoHistorial)) == 1
        assert s.scalar(select(func.count()).select_from(Proveedor).where(Proveedor.nombre == "Dist Norte")) == 0

    datos = {"nombre_archivo": "p.xlsx", "archivo_b64": base64.b64encode(archivo).decode(),
             "csrf_token": h["X-CSRF-Token"]}  # fmt: skip
    ok = client.post("/proveedores/importar/confirmar", cookies=ck, headers=h, data=datos)
    assert ok.status_code == 200 and "Importación aplicada" in ok.text
    client.post("/proveedores/importar/confirmar", cookies=ck, headers=h, data=datos)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(ProveedorProducto)) == 3  # los 2 importados + el "Sal" de B
        propios = s.scalars(select(Proveedor).where(Proveedor.empresa_id == d["a"], Proveedor.nombre == "Dist Norte")).all()
        assert len(propios) == 1
    # Otra empresa no ve lo importado.
    ckb, _ = _sesion(settings, d["ub"])
    assert "Dist Norte" not in client.get("/proveedores", cookies=ckb).text

    # Archivo inválido: 422 con mensaje.
    mal = client.post("/proveedores/importar", cookies=ck, headers=h, data={"csrf_token": h["X-CSRF-Token"]},
                      files={"archivo": ("p.txt", b"hola", "text/plain")})  # fmt: skip
    assert mal.status_code == 422 and ".xlsx" in mal.text
    mal = client.post("/proveedores/importar/confirmar", cookies=ck, headers=h,
                      data={"nombre_archivo": "p.xlsx", "archivo_b64": "!!", "csrf_token": h["X-CSRF-Token"]})  # fmt: skip
    assert mal.status_code == 422


def test_logistica_se_edita_por_empresa(client, settings, engine, dos_empresas):
    d = dos_empresas
    with Session(engine) as s:
        asegurar_tarifas_iniciales(s, d["a"])
        asegurar_tarifas_iniciales(s, d["b"])
        assert asegurar_tarifas_iniciales(s, d["a"]) == 0  # idempotente
        s.commit()
        ids_a = [t.id for t in s.scalars(select(TarifaLogistica).where(TarifaLogistica.empresa_id == d["a"]))]
        id_b = s.scalars(select(TarifaLogistica).where(TarifaLogistica.empresa_id == d["b"])).first().id
    ck, h = _sesion(settings, d["ua"])
    assert client.get("/proveedores/logistica", cookies=ck).status_code == 200
    datos = {"csrf_token": h["X-CSRF-Token"], f"fijo_{ids_a[0]}": "15.000", f"kg_{ids_a[0]}": "12,5",
             f"dias_{ids_a[0]}": "2", f"notas_{ids_a[0]}": "camión",
             f"fijo_{id_b}": "999",  # id de otra empresa: se ignora
             "nueva_ambito": "comuna", "nueva_nombre": "Cobquecura", "nueva_fijo": "40000"}  # fmt: skip
    r = client.post("/proveedores/logistica", cookies=ck, headers=h, data=datos, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        t = s.get(TarifaLogistica, ids_a[0])
        assert (t.costo_fijo_clp, t.costo_por_kg_clp, t.dias, t.notas) == (15000, 12.5, 2, "camión")
        assert s.get(TarifaLogistica, id_b).costo_fijo_clp == 0
        assert s.scalars(select(TarifaLogistica).where(TarifaLogistica.nombre == "Cobquecura")).one().empresa_id == d["a"]
    malo = client.post("/proveedores/logistica", cookies=ck, headers=h, data={**datos, f"fijo_{ids_a[0]}": "abc"})
    assert malo.status_code == 422


def test_menu_mesa_solo_con_empresa(client, settings, dos_empresas):
    d = dos_empresas
    ck, _ = _sesion(settings, d["ua"])
    assert 'href="/proveedores"' in client.get("/cuenta", cookies=ck).text
    ck, _ = _sesion(settings, d["sin"])
    assert 'href="/proveedores"' not in client.get("/cuenta", cookies=ck).text
    ck, _ = _sesion(settings, d["admin_sin"])
    assert 'href="/admin/empresas"' in client.get("/cuenta", cookies=ck).text


# ---------------------------------------------------------------------------
# F-plat-3-fix
# ---------------------------------------------------------------------------


def _csv_de(n_bytes: int) -> bytes:
    """CSV válido de exactamente `n_bytes`: filas con una columna de relleno que se ignora."""
    cab = b"proveedor;producto;formato;precio_neto;relleno\n"
    fila = b"Dist Relleno;Arroz;saco;1000;" + b"x" * 100_000 + b"\n"
    cuerpo = fila * ((n_bytes - len(cab)) // len(fila))
    resto = n_bytes - len(cab) - len(cuerpo)
    ultima = b"Dist Relleno;Arroz;saco;1000;"
    return cab + cuerpo + ultima + b"x" * (resto - len(ultima) - 1) + b"\n"


def test_importar_tope_700kb_en_vista_previa_y_confirmacion(client, settings, engine, dos_empresas):
    ck, h = _sesion(settings, dos_empresas["ua"])
    tok = {"csrf_token": h["X-CSRF-Token"]}

    grande = _csv_de(701 * 1024)
    r = client.post("/proveedores/importar", cookies=ck, headers=h, data=tok,
                    files={"archivo": ("p.csv", grande, "text/csv")})  # fmt: skip
    assert r.status_code == 422 and "700 KB" in r.text

    ok = _csv_de(690 * 1024)
    r = client.post("/proveedores/importar", cookies=ck, headers=h, data=tok,
                    files={"archivo": ("p.csv", ok, "text/csv")})  # fmt: skip
    assert r.status_code == 200 and "Vista previa" in r.text
    datos = {**tok, "nombre_archivo": "p.csv", "archivo_b64": base64.b64encode(ok).decode()}
    r = client.post("/proveedores/importar/confirmar", cookies=ck, headers=h, data=datos)
    assert r.status_code == 200 and "Importación aplicada" in r.text
    with Session(engine) as s:
        assert s.scalars(select(Proveedor).where(Proveedor.nombre == "Dist Relleno")).one()


def test_retencion_protege_el_setting_aunque_las_empresas_no_incluyan_la_region(engine, settings):
    from app.ingest.orchestrator import run_retencion

    vieja = ahora_utc() - timedelta(days=200)
    with Session(engine) as s:
        _empresa(s, "Solo Biobío", [8])
        for codigo, region in (("R16", 16), ("R8", 8), ("R13", 13)):
            s.add(CompraAgil(codigo=codigo, nombre=codigo, estado="desierta", region=region,
                             raw_json={"x": 1}, actualizado_en=vieja))  # fmt: skip
        s.commit()
    run_retencion(engine, settings)
    with Session(engine) as s:
        assert s.get(CompraAgil, "R16").raw_json == {"x": 1}  # type: ignore[union-attr]
        assert s.get(CompraAgil, "R8").raw_json == {"x": 1}  # type: ignore[union-attr]
        assert s.get(CompraAgil, "R13") is None  # fuera del territorio: se borra (F-plat-purga-legado)


def test_precio_con_fecha_anterior_no_pisa_el_actual(engine):
    with Session(engine) as s:
        p = _proveedor(s, _empresa(s, "A").id)
        kw = dict(producto_texto="Sal", formato_texto="saco")
        pp, _ = guardar_producto(s, p, precio_neto=1000, vigente_desde=date(2026, 11, 1), **kw)
        pp, est = guardar_producto(s, p, precio_neto=900, vigente_desde=date(2026, 9, 1), **kw)
        assert est == "historico"
        _, est = guardar_producto(s, p, precio_neto=900, vigente_desde=date(2026, 9, 1), **kw)
        assert est == "igual"  # repetir la lista vieja no duplica
        s.commit()
        assert pp.precio_neto_actual == 1000 and pp.vigente_desde == date(2026, 11, 1)
        filas = list(s.scalars(select(CostoHistorial).where(CostoHistorial.proveedor_producto_id == pp.id)))
        assert len(filas) == 2
        v = variacion(filas, pp.precio_neto_actual, hoy=date(2026, 11, 15))
        assert v.anterior == 900 and v.pct_vs_anterior == 11.1

        # Por importación: se cuenta aparte.
        pl = importar.parsear(
            _xlsx([["Prov", "", "", "", "", "Sal", "saco", None, None, 800, "2026-08-01"]]), "p.xlsx"
        )
        r = importar.aplicar(s, pp.proveedor.empresa_id, pl)
        assert r.precios_historicos == 0 and r.productos_nuevos == 1  # otro proveedor: es nuevo
        pl = importar.parsear(
            _xlsx([[p.nombre, "", "", "", "", "Sal", "saco", None, None, 800, "2026-08-01"]]), "p.xlsx"
        )
        r = importar.aplicar(s, p.empresa_id, pl)
        assert (r.precios_historicos, r.precios_cambiados, r.productos_nuevos) == (1, 0, 0)
        assert r.hay_cambios


def test_menu_mesa_oculto_con_empresa_inactiva(client, settings, engine, dos_empresas):
    d = dos_empresas
    ck, _ = _sesion(settings, d["ua"])
    assert 'href="/proveedores"' in client.get("/cuenta", cookies=ck).text
    with Session(engine) as s:
        s.get(Empresa, d["a"]).activa = False
        s.commit()
    assert 'href="/proveedores"' not in client.get("/cuenta", cookies=ck).text
