"""F-mesa-v0: la mesa del día (CA vigentes del territorio), sin predicción."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.mercado import adjudicacion
from app.mercado.comprador import clave_unidad, historial_comprador
from app.mercado.mesa_dia import (
    FiltrosMesa,
    buscar_codigos,
    comuna_en_direccion,
    contar_vigentes,
    cuenta_regresiva,
)
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaCotizacion,
    CaLinea,
    CaProducto,
    CompraAgil,
    Empresa,
    Usuario,
)

_PW = "contraseña-segura-test"
LECHE = "50131700"  # Lácteos y huevos
ASEO = "47131800"  # Aseo


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


def _usuario(engine, email, comunas=(), con_empresa=True, regiones=(16,)) -> int:
    with Session(engine) as s:
        empresa_id = None
        if con_empresa:
            e = Empresa(
                nombre="Emp " + email,
                regiones_prioritarias=list(regiones),
                comunas_prioritarias=list(comunas),
            )
            s.add(e)
            s.flush()
            empresa_id = e.id
        u = Usuario(
            email=email,
            password_hash=hash_password(_PW),
            rol=RolUsuario.USUARIO,
            activo=True,
            empresa_id=empresa_id,
        )
        s.add(u)
        s.commit()
        return u.id


def _cookies(settings, uid):
    token = create_session_token(settings.secret_key, uid)
    nonce = decode_session_token(settings.secret_key, token)[1]
    generate_csrf_token(settings.secret_key, nonce)
    return {COOKIE_NAME: token}


def _ca(engine, codigo, *, cierre=None, estado="publicada", region=16, productos=(), **campos):
    with Session(engine) as s:
        s.add(
            CompraAgil(
                codigo=codigo,
                nombre=campos.pop("nombre", f"Compra {codigo}"),
                estado=estado,
                region=region,
                fecha_cierre=cierre if cierre is not None else ahora_utc() + timedelta(days=2),
                **campos,
            )
        )
        for cod in productos:
            s.add(CaProducto(ca_codigo=codigo, codigo_producto=cod, nombre="p"))
        s.commit()


def _buscar(engine, filtros=FiltrosMesa(), comunas=(), ahora=None, regiones=(16,)):
    with Session(engine) as s:
        return buscar_codigos(s, list(regiones), list(comunas), filtros, ahora or ahora_utc())


# --- Ownership y territorio ------------------------------------------------------------------


def test_sin_empresa_ve_el_aviso_y_los_parciales_dan_403(client, engine, settings):
    uid = _usuario(engine, "u@t.cl", con_empresa=False)
    _ca(engine, "CA-1")
    c = _cookies(settings, uid)
    r = client.get("/mesa", cookies=c)
    assert r.status_code == 200
    assert "todavía no tiene una empresa" in r.text
    assert "CA-1" not in r.text
    assert client.get("/mesa/conteo", cookies=c).status_code == 403
    assert client.get("/mesa/ca/CA-1/comprador", cookies=c).status_code == 403


def test_mesa_exige_login(client):
    r = client.get("/mesa", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers["location"]


def test_region_fuera_del_territorio_no_aparece_ni_su_historial(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    _ca(engine, "CA-NUBLE", region=16)
    _ca(engine, "CA-OTRA", region=5)
    c = _cookies(settings, uid)
    r = client.get("/mesa", cookies=c)
    assert "CA-NUBLE" in r.text
    assert "CA-OTRA" not in r.text
    assert client.get("/mesa/ca/CA-OTRA/comprador", cookies=c).status_code == 404
    assert client.get("/mesa/ca/NO-EXISTE/comprador", cookies=c).status_code == 404
    assert client.get("/mesa/ca/CA-NUBLE/comprador", cookies=c).status_code == 200


def test_mesa_con_empresa_muestra_menu_fuente_y_tarjeta(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    _ca(
        engine, "CA-1", organismo_nombre="Municipalidad X", unidad_compra="Salud",
        monto_disponible_clp=1500000, n_documentos=2, plazo_entrega_dias=5,
        detalle_lineas="completo",
    )  # fmt: skip
    with Session(engine) as s:
        s.add(CaLinea(ca_codigo="CA-1", n=1, texto_origen="x", estado="ok"))
        s.commit()
    r = client.get("/mesa", cookies=_cookies(settings, uid))
    assert r.status_code == 200
    assert "Fuente: Dirección ChileCompra" in r.text
    assert 'href="/mesa"' in r.text  # entrada del menú
    for esperado in ("Municipalidad X", "Salud", "$1.500.000", "2 anexos", "5 días",
                     "1 línea · detalle completo", "/oportunidad/compras_agiles/CA-1",
                     "mercadopublico.cl", "Historial del comprador"):  # fmt: skip
        assert esperado in r.text


# --- Solo vigentes, en hora de Chile ----------------------------------------------------------


def test_solo_aparecen_las_vigentes(engine):
    ahora = ahora_utc()
    _ca(engine, "VIGENTE", cierre=ahora + timedelta(hours=3))
    _ca(engine, "CERRADA", estado="cerrada", cierre=ahora + timedelta(hours=3))
    _ca(engine, "DESIERTA", estado="desierta", cierre=ahora + timedelta(hours=3))
    _ca(engine, "VENCIDA", cierre=ahora - timedelta(minutes=1))
    assert _buscar(engine, ahora=ahora) == ["VIGENTE"]
    with Session(engine) as s:
        assert contar_vigentes(s, [16], ahora)[0] == 1


def test_cierra_hoy_y_manana_son_dias_de_chile_no_de_utc(engine):
    # 15-jun-2026 16:00 en Chile (UTC-4 en invierno) = 20:00 UTC.
    ahora = datetime(2026, 6, 15, 20, 0)
    # 22:30 del 15 en Chile = 02:30 UTC del 16: es HOY aunque en UTC ya sea otro día.
    _ca(engine, "HOY", cierre=datetime(2026, 6, 16, 2, 30))
    # 00:30 del 16 en Chile = 04:30 UTC del 16: es MAÑANA.
    _ca(engine, "MANANA", cierre=datetime(2026, 6, 16, 4, 30))
    # 20-jun (sábado) 12:00 Chile: misma semana (15-jun es lunes). 22-jun: semana siguiente.
    _ca(engine, "SABADO", cierre=datetime(2026, 6, 20, 16, 0))
    _ca(engine, "LUNES_SIG", cierre=datetime(2026, 6, 22, 16, 0))
    assert _buscar(engine, FiltrosMesa(cierre="hoy"), ahora=ahora) == ["HOY"]
    assert _buscar(engine, FiltrosMesa(cierre="manana"), ahora=ahora) == ["MANANA"]
    assert _buscar(engine, FiltrosMesa(cierre="semana"), ahora=ahora) == ["HOY", "MANANA", "SABADO"]
    assert _buscar(engine, ahora=ahora) == ["HOY", "MANANA", "SABADO", "LUNES_SIG"]  # por cierre
    with Session(engine) as s:
        assert contar_vigentes(s, [16], ahora) == (4, 1)


def test_cuenta_regresiva():
    ahora = datetime(2026, 6, 15, 12, 0)
    assert cuenta_regresiva(ahora + timedelta(days=2, hours=4, minutes=5), ahora) == "Cierra en 2 d 4 h"
    assert cuenta_regresiva(ahora + timedelta(hours=3, minutes=20), ahora) == "Cierra en 3 h 20 min"
    assert cuenta_regresiva(ahora + timedelta(minutes=25), ahora) == "Cierra en 25 min"
    assert cuenta_regresiva(ahora - timedelta(minutes=1), ahora) == "Cerrada"


def test_la_tarjeta_muestra_el_cierre_en_hora_de_chile(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    futuro = (ahora_utc() + timedelta(days=3)).replace(hour=15, minute=0, second=0, microsecond=0)
    _ca(engine, "CA-1", cierre=futuro)
    esperado = futuro.replace(tzinfo=UTC).astimezone(TZ_CHILE).strftime("%d/%m/%Y %H:%M")
    r = client.get("/mesa", cookies=_cookies(settings, uid))
    assert esperado in r.text
    assert "(hora de Chile)" in r.text


# --- Filtros y conteo --------------------------------------------------------------------------


def test_filtros_familia_detalle_anexos_monto_y_texto(engine):
    ahora = ahora_utc()
    _ca(engine, "LECHE", productos=[LECHE], detalle_lineas="completo", n_documentos=1,
        monto_disponible_clp=500000, nombre="Leche en polvo")  # fmt: skip
    _ca(engine, "ASEO", productos=[ASEO], detalle_lineas="sin_detalle", n_documentos=0,
        monto_disponible_clp=100000, nombre="Productos de aseo 100%")  # fmt: skip
    _ca(engine, "VARIAS", productos=[LECHE, ASEO], detalle_lineas="incompleto",
        monto_disponible_clp=None, nombre="Mix")  # fmt: skip
    assert set(_buscar(engine, FiltrosMesa(familia="Aseo"), ahora=ahora)) == {"ASEO", "VARIAS"}
    assert set(_buscar(engine, FiltrosMesa(detalle="incompleto"), ahora=ahora)) == {"VARIAS"}
    assert set(_buscar(engine, FiltrosMesa(detalle="sin_detalle"), ahora=ahora)) == {"ASEO"}
    assert set(_buscar(engine, FiltrosMesa(con_anexos=True), ahora=ahora)) == {"LECHE"}
    assert set(_buscar(engine, FiltrosMesa(monto_min=200000), ahora=ahora)) == {"LECHE"}
    assert set(_buscar(engine, FiltrosMesa(q="leche"), ahora=ahora)) == {"LECHE"}
    # `%` del usuario es literal, no comodín.
    assert set(_buscar(engine, FiltrosMesa(q="100%"), ahora=ahora)) == {"ASEO"}
    assert _buscar(engine, FiltrosMesa(q="%"), ahora=ahora) == ["ASEO"]
    assert _buscar(engine, FiltrosMesa(q="_"), ahora=ahora) == []


def test_conteo_usa_los_mismos_filtros_y_no_expone_filas(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    _ca(engine, "LECHE", productos=[LECHE], nombre="Leche")
    _ca(engine, "ASEO", productos=[ASEO], nombre="Aseo")
    c = _cookies(settings, uid)
    r = client.get("/mesa/conteo", params={"familia": "Aseo"}, cookies=c)
    assert r.status_code == 200
    assert ">1<" in r.text and "Aseo" not in r.text and "ASEO" not in r.text
    assert ">2<" in client.get("/mesa/conteo", cookies=c).text


def test_parametros_invalidos_no_rompen(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    _ca(engine, "CA-1")
    r = client.get(
        "/mesa",
        params={"cierre": "x", "detalle": "y", "monto_min": "abc", "pagina": "-4"},
        cookies=_cookies(settings, uid),
    )
    assert r.status_code == 200 and "CA-1" in r.text


def test_pagina_de_50(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    base = ahora_utc() + timedelta(days=1)
    for i in range(55):
        _ca(engine, f"CA-{i:03d}", cierre=base + timedelta(minutes=i))
    c = _cookies(settings, uid)
    r1 = client.get("/mesa", cookies=c)
    assert "CA-049" in r1.text and "CA-050" not in r1.text and "Página 1 de 2" in r1.text
    r2 = client.get("/mesa", params={"pagina": 2}, cookies=c)
    assert "CA-050" in r2.text and "CA-049" not in r2.text


# --- Orden por comunas prioritarias ------------------------------------------------------------


def test_comuna_en_direccion_sin_tildes_y_por_palabra():
    assert comuna_en_direccion("Av. Libertad 123, CHILLAN", ["Chillán"]) == "Chillán"
    assert comuna_en_direccion("Bulnes s/n", ["Chillán", "Bulnes"]) == "Bulnes"
    assert comuna_en_direccion("Camino a Chillanejo", ["Chillán"]) is None
    assert comuna_en_direccion(None, ["Chillán"]) is None
    assert comuna_en_direccion("Chillán", []) is None


def test_orden_primero_comunas_prioritarias_luego_cierre(engine, client, settings):
    ahora = ahora_utc()
    _ca(engine, "A-SIN", cierre=ahora + timedelta(hours=1), direccion_entrega="Los Ángeles 1")
    _ca(engine, "B-CHI", cierre=ahora + timedelta(hours=5), direccion_entrega="Calle 1, Chillán")
    _ca(engine, "C-SIN", cierre=ahora + timedelta(hours=2), direccion_entrega=None)
    _ca(engine, "D-CHI", cierre=ahora + timedelta(hours=3), direccion_entrega="CHILLAN VIEJO s/n")
    assert _buscar(engine, ahora=ahora) == ["A-SIN", "C-SIN", "D-CHI", "B-CHI"]
    # «Chillán Viejo» también nombra la palabra «Chillán»: por eso la detección es [I] (texto libre).
    assert _buscar(engine, comunas=["Chillán"], ahora=ahora) == ["D-CHI", "B-CHI", "A-SIN", "C-SIN"]
    uid = _usuario(engine, "u@t.cl", comunas=["Chillán"])
    r = client.get("/mesa", cookies=_cookies(settings, uid))
    assert r.text.index("B-CHI") < r.text.index("A-SIN") < r.text.index("C-SIN")
    assert "Comuna detectada en la dirección: Chillán" in r.text
    assert "Inferida" in r.text  # marcada como [I]


# --- Sin raw_json ------------------------------------------------------------------------------


def test_la_mesa_no_lee_raw_json_ni_descripcion(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    _ca(engine, "CA-1", productos=[LECHE])
    sentencias: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _captura(conn, cursor, statement, parameters, context, executemany):
        sentencias.append(statement)

    c = _cookies(settings, uid)
    for url in ("/mesa", "/mesa?familia=Aseo", "/mesa/conteo", "/mesa/ca/CA-1/comprador"):
        assert client.get(url, cookies=c).status_code == 200
    de_ca = [s for s in sentencias if "compras_agiles" in s and "FROM compras_agiles" in s]
    assert de_ca
    assert not [s for s in de_ca if "raw_json" in s], "la mesa no debe pedir raw_json"


# --- Clave de comprador e historial ------------------------------------------------------------


def test_clave_unidad_una_sola_definicion_y_normalizada():
    assert adjudicacion.clave_unidad is clave_unidad  # una sola definición
    assert clave_unidad("69.150.800-1", "Dirección de Salud") == clave_unidad(
        " 69150800-1 ", "DIRECCION DE SALUD"
    )
    assert clave_unidad("69150800-k", None) == "69150800-K|"
    assert clave_unidad("69150800-1", "A") != clave_unidad("69150800-1", "B")
    assert clave_unidad(None, None) == "|"


def _ca_cerrada(s, codigo, *, org="69.150.800-1", unidad="Salud", dias=30, leida=True, **kw):
    cierre = ahora_utc() - timedelta(days=dias)
    s.add(
        CompraAgil(
            codigo=codigo, nombre=codigo, estado="adjudicada", region=16, organismo_rut=org,
            unidad_compra=unidad, fecha_cierre=cierre,
            cotizaciones_obtenidas_en=cierre if leida else None, **kw,
        )
    )  # fmt: skip


def _oferta(s, ca, rut, valor, *, sel=None, id_cot=None, nombre=None):
    s.add(
        CaCotizacion(
            ca_codigo=ca, llamado=1, rut_proveedor=rut, razon_social=nombre or f"Prov {rut}",
            seleccionado=sel, valor_neto=valor, id_cotizacion=id_cot or f"{ca}-{rut}-{valor}",
        )
    )  # fmt: skip


def test_historial_del_comprador(engine):
    with Session(engine) as s:
        # CA1: A gana con 1000; B manda DOS ofertas (cuenta como UN oferente) → 2 oferentes.
        _ca_cerrada(s, "CA1")
        _oferta(s, "CA1", "1-9", 1000, sel=True, nombre="Alfa")
        _oferta(s, "CA1", "2-7", 1200, sel=False)
        _oferta(s, "CA1", "2-7", 1100, sel=False)
        # CA2: mismo comprador con RUT/unidad escritos distinto; A gana con 500; 3 oferentes.
        _ca_cerrada(s, "CA2", org="69150800-1", unidad="SALUD", dias=60)
        _oferta(s, "CA2", "1-9", 500, sel=True, nombre="Alfa")
        _oferta(s, "CA2", "3-5", 520, sel=False)
        _oferta(s, "CA2", "4-3", 530, sel=False)
        # CA3: mismo comprador, gana C con 200; 1 oferente.
        _ca_cerrada(s, "CA3", dias=90)
        _oferta(s, "CA3", "5-1", 200, sel=True, nombre="Gamma")
        # Otra unidad del mismo organismo y otro organismo: NO cuentan.
        _ca_cerrada(s, "OTRA-U", unidad="Educación")
        _oferta(s, "OTRA-U", "9-9", 99999, sel=True)
        _ca_cerrada(s, "OTRO-ORG", org="61.000.000-0")
        _oferta(s, "OTRO-ORG", "9-9", 99999, sel=True)
        # Fuera de los 12 meses: no cuenta.
        _ca_cerrada(s, "VIEJA", dias=400)
        _oferta(s, "VIEJA", "9-9", 99999, sel=True)
        # Cerrada sin ofertas leídas: cuenta como CA pero no entra a la mediana.
        _ca_cerrada(s, "SIN-LEER", dias=10, leida=False)
        s.commit()
        h = historial_comprador(s, "69.150.800-1", "Salud", ahora=ahora_utc())
    assert h.n_ca == 4  # CA1, CA2, CA3, SIN-LEER
    assert h.n_con_ofertas == 3
    assert h.mediana_oferentes == 2  # [2, 3, 1] → 2 (RUT distintos, no filas: CA1 tiene 3 filas)
    assert h.monto_adjudicado == 1700
    assert h.top_proveedores == [("1-9", "Alfa", 2), ("5-1", "Gamma", 1)]


def test_historial_sin_unidad_y_sin_datos(engine):
    with Session(engine) as s:
        _ca_cerrada(s, "CA1", unidad=None)
        _oferta(s, "CA1", "1-9", 10, sel=True)
        s.commit()
        h = historial_comprador(s, "69.150.800-1", None, ahora=ahora_utc())
        vacio = historial_comprador(s, "11-1", "X", ahora=ahora_utc())
    assert h.n_ca == 1 and h.monto_adjudicado == 10
    assert vacio.n_ca == 0 and vacio.mediana_oferentes is None and vacio.top_proveedores == []


def test_historial_por_http(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    with Session(engine) as s:
        s.add(
            CompraAgil(
                codigo="VIG", nombre="v", estado="publicada", region=16,
                organismo_rut="69.150.800-1", unidad_compra="Salud",
                fecha_cierre=ahora_utc() + timedelta(days=1),
            )
        )  # fmt: skip
        _ca_cerrada(s, "CA1")
        _oferta(s, "CA1", "1-9", 1000, sel=True, nombre="Alfa")
        s.commit()
    r = client.get("/mesa/ca/VIG/comprador", cookies=_cookies(settings, uid))
    assert r.status_code == 200
    assert "Alfa" in r.text and "$1.000" in r.text
    assert "Fuente: Dirección ChileCompra" in r.text


# --- Portada «Hoy» -----------------------------------------------------------------------------


def test_hoy_muestra_ca_vigentes_y_enlaza_a_la_mesa(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    _ca(engine, "CA-1", cierre=ahora_utc() + timedelta(days=3))
    r = client.get("/", cookies=_cookies(settings, uid))
    assert "CA vigentes" in r.text
    assert 'data-hoy="ca-abiertas">1<' in r.text
    assert 'data-hoy="ca-cierran-hoy">' in r.text
    assert 'href="/mesa"' in r.text
