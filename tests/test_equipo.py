"""F-equipo: estado compartido de las CA, comentarios, actividad del equipo y alarmas."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.retencion import purgar_terminales
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.mercado import equipo as eq
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaComentario,
    CaCotizacion,
    CaEvento,
    CaProducto,
    CaTrabajo,
    CompraAgil,
    Empresa,
    Usuario,
)

_PW = "contraseña-segura-test"
_RUT_NUESTRO = "76.123.456-0"
_RUT_OTRO = "77.777.777-7"


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


def _empresa(engine, nombre="Emp", rut: str | None = _RUT_NUESTRO, regiones=(16,)) -> int:
    with Session(engine) as s:
        e = Empresa(nombre=nombre, rut=rut, regiones_prioritarias=list(regiones))
        s.add(e)
        s.commit()
        return e.id


def _usuario(engine, email, empresa_id, nombre=None, activo=True, rol=RolUsuario.USUARIO) -> int:
    with Session(engine) as s:
        u = Usuario(
            email=email,
            nombre=nombre,
            password_hash=hash_password(_PW),
            rol=rol,
            activo=activo,
            empresa_id=empresa_id,
        )
        s.add(u)
        s.commit()
        return u.id


def _ca(engine, codigo, *, cierre=None, region=16, estado="publicada", **campos) -> None:
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
        s.commit()


def _auth(settings, uid):
    token = create_session_token(settings.secret_key, uid)
    nonce = decode_session_token(settings.secret_key, token)[1]
    csrf = generate_csrf_token(settings.secret_key, nonce)
    return {COOKIE_NAME: token}, {"X-CSRF-Token": csrf, "HX-Request": "true"}


@pytest.fixture()
def equipo(engine):
    """Empresa con Boris, Mario y Gonzalo; otra empresa con Ana; una CA vigente de Ñuble."""
    emp = _empresa(engine, "Nuestra")
    otra = _empresa(engine, "Otra", rut="11.111.111-1")
    ids = {
        "emp": emp,
        "otra": otra,
        "boris": _usuario(engine, "boris@t.cl", emp, nombre="Boris"),
        "mario": _usuario(engine, "mario@t.cl", emp, nombre="Mario Pérez"),
        "gonzalo": _usuario(engine, "gonzalo@t.cl", emp),  # sin nombre: «gonzalo»
        "ana": _usuario(engine, "ana@t.cl", otra, nombre="Ana"),
    }
    _ca(engine, "CA-1", nombre="Abarrotes Cesfam San Carlos")
    return ids


def _u(s, uid) -> Usuario:
    u = s.get(Usuario, uid)
    assert u is not None
    return u


def _estado(client, settings, uid, codigo, estado, version, motivo="", **kw):
    cookies, headers = _auth(settings, uid)
    data = {"estado": estado, "version": str(version), "motivo": motivo, "origen": "mesa"}
    return client.post(
        f"/equipo/ca/{codigo}/estado", data=data, cookies=cookies, headers=kw.get("h", headers)
    )


# --- Estado compartido y concurrencia -------------------------------------------------------


def test_priorizar_crea_trabajo_y_evento_en_la_misma_transaccion(engine, equipo):
    with Session(engine) as s:
        t = eq.cambiar_estado(s, equipo["emp"], _u(s, equipo["mario"]), "CA-1", eq.PRIORIZADA, 0)
        assert t.version == 1
        s.rollback()  # sin commit: no queda ni la fila ni el evento
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(CaTrabajo)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(CaEvento)).scalar_one() == 0
        eq.cambiar_estado(s, equipo["emp"], _u(s, equipo["mario"]), "CA-1", eq.PRIORIZADA, 0)
        s.commit()
        ev = s.execute(select(CaEvento)).scalar_one()
        assert (ev.tipo, ev.usuario_id, ev.datos) == (
            eq.EV_ESTADO, equipo["mario"], {"de": "nueva", "a": "priorizada"}
        )


def test_transiciones_libres_y_reabrir_descartada(engine, equipo):
    with Session(engine) as s:
        u = _u(s, equipo["boris"])
        eq.cambiar_estado(s, equipo["emp"], u, "CA-1", eq.ENVIADA, 0)
        eq.cambiar_estado(s, equipo["emp"], u, "CA-1", eq.DESCARTADA, 1, motivo="flete caro")
        t = eq.cambiar_estado(s, equipo["emp"], u, "CA-1", eq.COTIZANDO, 2)
        s.commit()
        assert (t.estado, t.version, t.motivo_descarte) == (eq.COTIZANDO, 3, None)
        datos = [e.datos for e in s.execute(select(CaEvento).order_by(CaEvento.id)).scalars()]
        assert datos[1] == {"de": "enviada", "a": "descartada", "motivo": "flete caro"}
        assert datos[2] == {"de": "descartada", "a": "cotizando"}


def test_descartar_sin_motivo_es_422(client, settings, equipo):
    r = _estado(client, settings, equipo["mario"], "CA-1", "descartada", 0)
    assert r.status_code == 422
    assert "motivo" in r.text
    r = _estado(client, settings, equipo["mario"], "CA-1", "descartada", 0, motivo="lejos")
    assert r.status_code == 200
    assert "Descartada" in r.text and "lejos" in r.text


def test_conflicto_de_version_no_pisa_e_informa_quien(client, engine, settings, equipo):
    # Mario y Gonzalo ven la CA en «nueva» (versión 0); Gonzalo cambia primero.
    assert _estado(client, settings, equipo["gonzalo"], "CA-1", "cotizando", 0).status_code == 200
    r = _estado(client, settings, equipo["mario"], "CA-1", "priorizada", 0)
    assert r.status_code == 409
    assert "gonzalo" in r.text and "Cotizando" in r.text
    assert "No se aplicó tu cambio" in r.text
    with Session(engine) as s:
        t = s.execute(select(CaTrabajo)).scalar_one()
        assert (t.estado, t.version, t.actualizado_por_id) == ("cotizando", 1, equipo["gonzalo"])
        assert s.execute(select(func.count()).select_from(CaEvento)).scalar_one() == 1


def test_conflicto_sin_htmx_es_409(client, settings, equipo):
    _estado(client, settings, equipo["gonzalo"], "CA-1", "cotizando", 0)
    cookies, headers = _auth(settings, equipo["mario"])
    r = client.post(
        "/equipo/ca/CA-1/estado",
        data={"estado": "lista", "version": "0"},
        cookies=cookies,
        headers={"X-CSRF-Token": headers["X-CSRF-Token"]},
    )
    assert r.status_code == 409


def test_tomar_y_asignar(client, engine, settings, equipo):
    cookies, headers = _auth(settings, equipo["mario"])
    r = client.post(
        "/equipo/ca/CA-1/asignar", data={"responsable_id": "yo", "version": "0"},
        cookies=cookies, headers=headers,
    )
    assert r.status_code == 200 and "MP" in r.text  # iniciales de «Mario Pérez»
    with Session(engine) as s:
        t = s.execute(select(CaTrabajo)).scalar_one()
        # Tomar una CA nueva la prioriza: tener responsable es empezar a trabajarla.
        assert (t.estado, t.responsable_id, t.version) == ("priorizada", equipo["mario"], 1)
    r = client.post(
        "/equipo/ca/CA-1/asignar",
        data={"responsable_id": str(equipo["gonzalo"]), "version": "1"},
        cookies=cookies, headers=headers,
    )
    assert r.status_code == 200
    with Session(engine) as s:
        ev = s.execute(
            select(CaEvento).where(CaEvento.tipo == "asignada").order_by(CaEvento.id.desc())
        ).scalars().first()
        assert ev is not None and ev.destinatario_id == equipo["gonzalo"]


def test_asignar_a_otra_empresa_o_inactivo_se_rechaza(client, engine, settings, equipo):
    inactivo = _usuario(engine, "ex@t.cl", equipo["emp"], activo=False)
    cookies, headers = _auth(settings, equipo["mario"])
    for destino in (equipo["ana"], inactivo, 999999):
        r = client.post(
            "/equipo/ca/CA-1/asignar",
            data={"responsable_id": str(destino), "version": "0"},
            cookies=cookies, headers=headers,
        )
        assert r.status_code == 422
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(CaTrabajo)).scalar_one() == 0


# --- Ownership y CSRF -----------------------------------------------------------------------


def test_ca_fuera_del_territorio_es_404(client, engine, settings, equipo):
    _ca(engine, "CA-RM", region=13)
    cookies, headers = _auth(settings, equipo["mario"])
    assert client.get("/equipo/ca/CA-RM/bloque", cookies=cookies).status_code == 404
    assert client.get("/equipo/ca/CA-RM/hilo", cookies=cookies).status_code == 404
    assert client.get("/equipo/ca/NO-EXISTE/hilo", cookies=cookies).status_code == 404
    assert _estado(client, settings, equipo["mario"], "CA-RM", "priorizada", 0).status_code == 404
    r = client.post(
        "/equipo/ca/CA-RM/comentarios", data={"texto": "hola"}, cookies=cookies, headers=headers
    )
    assert r.status_code == 404


def test_trabajo_de_otra_empresa_no_se_ve(client, engine, settings, equipo):
    _estado(client, settings, equipo["mario"], "CA-1", "cotizando", 0)
    cookies, _ = _auth(settings, equipo["ana"])
    r = client.get("/equipo/ca/CA-1/bloque", cookies=cookies)
    assert r.status_code == 200
    assert 'data-equipo-estado="nueva"' in r.text
    # Ana trabaja la misma CA para su empresa: filas separadas, sin conflicto.
    assert _estado(client, settings, equipo["ana"], "CA-1", "priorizada", 0).status_code == 200
    with Session(engine) as s:
        filas = {t.empresa_id: t.estado for t in s.execute(select(CaTrabajo)).scalars()}
    assert filas == {equipo["emp"]: "cotizando", equipo["otra"]: "priorizada"}


def test_sin_empresa_403(client, engine, settings):
    _ca(engine, "CA-1")
    uid = _usuario(engine, "solo@t.cl", None)
    cookies, headers = _auth(settings, uid)
    assert client.get("/actividad/contador", cookies=cookies).status_code == 403
    assert client.get("/equipo/ca/CA-1/hilo", cookies=cookies).status_code == 403


def test_csrf_en_cada_mutacion(client, engine, settings, equipo):
    with Session(engine) as s:
        c = eq.comentar(s, equipo["emp"], _u(s, equipo["mario"]), "CA-1", "hola")
        s.commit()
        cid = c.id
    cookies, _ = _auth(settings, equipo["mario"])
    sin = {"HX-Request": "true"}
    mutaciones = [
        ("/equipo/ca/CA-1/estado", {"estado": "priorizada", "version": "0"}),
        ("/equipo/ca/CA-1/asignar", {"responsable_id": "yo", "version": "0"}),
        ("/equipo/ca/CA-1/comentarios", {"texto": "x"}),
        (f"/equipo/comentarios/{cid}/editar", {"texto": "y"}),
        (f"/equipo/comentarios/{cid}/eliminar", {}),
        ("/actividad/leido", {}),
        ("/cuenta/alarmas", {"pref": "resultado"}),
    ]
    for url, data in mutaciones:
        r = client.post(url, data=data, cookies=cookies, headers=sin)
        assert r.status_code == 403, url
    admin = _usuario(engine, "adm@t.cl", equipo["emp"], rol=RolUsuario.ADMIN)
    cookies_a, _ = _auth(settings, admin)
    r = client.post(
        f"/admin/usuarios/{equipo['mario']}/nombre", data={"nombre": "X"}, cookies=cookies_a
    )
    assert r.status_code == 403


# --- Comentarios y menciones ----------------------------------------------------------------


def _comentar(client, settings, uid, texto, codigo="CA-1"):
    cookies, headers = _auth(settings, uid)
    return client.post(
        f"/equipo/ca/{codigo}/comentarios", data={"texto": texto}, cookies=cookies, headers=headers
    )


def test_comentar_no_crea_trabajo_y_escapa_html(client, engine, settings, equipo):
    r = _comentar(client, settings, equipo["mario"], "el flete a Coihueco sale caro\n<script>alert(1)</script>")
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "Fuente: Dirección ChileCompra" in r.text
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(CaTrabajo)).scalar_one() == 0
        ev = s.execute(select(CaEvento)).scalar_one()
        c = s.execute(select(CaComentario)).scalar_one()
        assert ev.tipo == "comentario" and ev.datos == {"comentario_id": c.id}


def test_limite_de_2000_y_vacio(client, settings, equipo):
    assert _comentar(client, settings, equipo["mario"], "x" * 2001).status_code == 422
    assert _comentar(client, settings, equipo["mario"], "   ").status_code == 422
    assert _comentar(client, settings, equipo["mario"], "x" * 2000).status_code == 200


def test_editar_y_eliminar_solo_el_autor(client, engine, settings, equipo):
    _comentar(client, settings, equipo["mario"], "primero")
    with Session(engine) as s:
        cid = s.execute(select(CaComentario.id)).scalar_one()
    ck_g, h_g = _auth(settings, equipo["gonzalo"])
    ck_a, h_a = _auth(settings, equipo["ana"])
    url_e, url_d = f"/equipo/comentarios/{cid}/editar", f"/equipo/comentarios/{cid}/eliminar"
    assert client.post(url_e, data={"texto": "no"}, cookies=ck_g, headers=h_g).status_code == 403
    assert client.post(url_d, cookies=ck_g, headers=h_g).status_code == 403
    assert client.post(url_e, data={"texto": "no"}, cookies=ck_a, headers=h_a).status_code == 404
    assert client.post(url_d, cookies=ck_a, headers=h_a).status_code == 404
    ck_m, h_m = _auth(settings, equipo["mario"])
    r = client.post(url_e, data={"texto": "corregido"}, cookies=ck_m, headers=h_m)
    assert r.status_code == 200 and "corregido" in r.text and "(editado)" in r.text
    r = client.post(url_d, cookies=ck_m, headers=h_m)
    assert r.status_code == 200 and "comentario eliminado" in r.text and "corregido" not in r.text
    with Session(engine) as s:
        c = s.get(CaComentario, cid)
        assert c is not None and c.eliminado  # lógico: el registro queda
        # Editar o eliminar no genera alarma.
        assert s.execute(select(func.count()).select_from(CaEvento)).scalar_one() == 1


def test_menciones_se_resuelven_contra_la_empresa(engine, equipo):
    with Session(engine) as s:
        _usuario(engine, "inactivo@t.cl", equipo["emp"], nombre="Pedro", activo=False)
        eq.comentar(
            s, equipo["emp"], _u(s, equipo["boris"]), "CA-1",
            "@Mario Pérez y @gonzalo revisen; @Ana no es de acá, @Pedro tampoco, @nadie",
        )
        s.commit()
        dest = {
            e.destinatario_id
            for e in s.execute(select(CaEvento).where(CaEvento.tipo == "mencion")).scalars()
        }
    assert dest == {equipo["mario"], equipo["gonzalo"]}


def test_resolver_menciones_palabra_completa():
    a = Usuario(id=1, email="mario@t.cl", nombre="Mario")
    b = Usuario(id=2, email="mariana@t.cl", nombre="Mariana")
    assert {u.id for u in eq.resolver_menciones("hola @mariana", [a, b])} == {2}
    assert {u.id for u in eq.resolver_menciones("correo x@mario.cl", [a, b])} == set()
    assert {u.id for u in eq.resolver_menciones("@MARIO ok", [a, b])} == {1}


# --- No leídos y preferencias ---------------------------------------------------------------


def _no_leidos(engine, uid, emp) -> list[str]:
    with Session(engine) as s:
        return [e.tipo for e in eq.listar_no_leidos(s, _u(s, uid), emp)]


def test_no_leidos_excluye_los_propios_y_marca_leido(engine, equipo):
    emp = equipo["emp"]
    with Session(engine) as s:
        eq.cambiar_estado(s, emp, _u(s, equipo["mario"]), "CA-1", eq.PRIORIZADA, 0)
        s.commit()
    assert _no_leidos(engine, equipo["mario"], emp) == []
    assert _no_leidos(engine, equipo["boris"], emp) == ["estado"]
    assert _no_leidos(engine, equipo["ana"], equipo["otra"]) == []
    with Session(engine) as s:
        eq.marcar_leido(s, _u(s, equipo["boris"]))
        s.commit()
    assert _no_leidos(engine, equipo["boris"], emp) == []


def test_sin_marca_de_lectura_solo_cuentan_los_ultimos_7_dias(client, engine, settings, equipo):
    emp, mario = equipo["emp"], equipo["mario"]
    ahora = ahora_utc()
    _ca(engine, "CA-2")
    with Session(engine) as s:
        assert _u(s, equipo["boris"]).actividad_leida_hasta is None
        for codigo, dias in (("CA-1", 8), ("CA-2", 6)):
            creado = ahora - timedelta(days=dias)
            s.add(CaEvento(empresa_id=emp, ca_codigo=codigo, usuario_id=mario, tipo="estado",
                           datos={"de": "nueva", "a": "priorizada"}, creado_en=creado))
            s.add(CaComentario(empresa_id=emp, ca_codigo=codigo, usuario_id=mario,
                               texto="hola", creado_en=creado))
        s.commit()
    with Session(engine) as s:
        boris = _u(s, equipo["boris"])
        assert [e.ca_codigo for e in eq.listar_no_leidos(s, boris, emp)] == ["CA-2"]
        assert eq.contar_no_leidos(s, boris, emp) == 1
        tarjetas = eq.equipo_de(s, emp, boris, ["CA-1", "CA-2"])
        assert tarjetas["CA-1"].n_comentarios == 1  # el hilo se ve entero
        assert tarjetas["CA-1"].comentarios_no_leidos is False  # de hace 8 días: sin punto
        assert tarjetas["CA-2"].comentarios_no_leidos is True
    cookies, _ = _auth(settings, equipo["boris"])
    assert 'data-no-leidos="1"' in client.get("/actividad/contador", cookies=cookies).text
    # Con marca explícita manda la marca, aunque sea más vieja que 7 días.
    with Session(engine) as s:
        eq.marcar_leido(s, _u(s, equipo["boris"]), ahora - timedelta(days=30))
        s.commit()
        boris = _u(s, equipo["boris"])
        assert eq.contar_no_leidos(s, boris, emp) == 2
        assert eq.equipo_de(s, emp, boris, ["CA-1"])["CA-1"].comentarios_no_leidos is True


def test_preferencias_y_asignada_a_mi(engine, equipo):
    emp = equipo["emp"]
    with Session(engine) as s:
        eq.guardar_preferencias(s, equipo["gonzalo"], {eq.PREF_ASIGNADA})  # equipo apagado
        mario = _u(s, equipo["mario"])
        eq.cambiar_estado(s, emp, mario, "CA-1", eq.PRIORIZADA, 0)
        eq.asignar(s, emp, mario, "CA-1", equipo["gonzalo"], 1)
        s.commit()
    assert _no_leidos(engine, equipo["gonzalo"], emp) == ["asignada"]
    # Boris (todo prendido) ve el cambio de estado y la asignación a otro.
    assert sorted(_no_leidos(engine, equipo["boris"], emp)) == ["asignada", "estado"]
    with Session(engine) as s:
        eq.guardar_preferencias(s, equipo["gonzalo"], set())
        s.commit()
    assert _no_leidos(engine, equipo["gonzalo"], emp) == []


def test_la_mencion_llega_con_todo_apagado_y_no_cuenta_doble(engine, equipo):
    emp = equipo["emp"]
    with Session(engine) as s:
        eq.guardar_preferencias(s, equipo["gonzalo"], set())
        eq.comentar(s, emp, _u(s, equipo["mario"]), "CA-1", "@gonzalo mira esto")
        s.commit()
    assert _no_leidos(engine, equipo["gonzalo"], emp) == ["mencion"]
    with Session(engine) as s:
        eq.guardar_preferencias(s, equipo["gonzalo"], set(eq.PREFERENCIAS))
        s.commit()
        assert eq.contar_no_leidos(s, _u(s, equipo["gonzalo"]), emp) == 1
        # Boris no fue mencionado: le llega como comentario.
        assert [e.tipo for e in eq.listar_no_leidos(s, _u(s, equipo["boris"]), emp)] == [
            "comentario"
        ]


def test_campana_contador_y_texto_humano(client, engine, settings, equipo):
    _estado(client, settings, equipo["mario"], "CA-1", "priorizada", 0)
    _comentar(client, settings, equipo["mario"], "@Boris el flete sale caro")
    cookies, headers = _auth(settings, equipo["boris"])
    r = client.get("/actividad/contador", cookies=cookies)
    assert r.status_code == 200 and 'data-no-leidos="2"' in r.text
    r = client.get("/actividad/campana", cookies=cookies)
    assert "Mario Pérez priorizó" in r.text
    assert "Abarrotes Cesfam San Carlos" in r.text
    assert "Mario Pérez te mencionó en" in r.text and "el flete sale caro" in r.text
    assert "Fuente: Dirección ChileCompra" in r.text
    r = client.post("/actividad/leido", cookies=cookies, headers=headers)
    assert r.status_code == 200 and "Estás al día" in r.text
    assert 'data-no-leidos="0"' in client.get("/actividad/contador", cookies=cookies).text


def test_pagina_actividad_con_filtros(client, settings, equipo):
    _estado(client, settings, equipo["mario"], "CA-1", "priorizada", 0)
    _comentar(client, settings, equipo["gonzalo"], "ok")
    cookies, _ = _auth(settings, equipo["boris"])
    r = client.get("/actividad", cookies=cookies)
    assert r.status_code == 200
    assert "Mario Pérez priorizó" in r.text and "gonzalo comentó en" in r.text
    assert "Fuente: Dirección ChileCompra" in r.text
    r = client.get(f"/actividad?persona={equipo['gonzalo']}", cookies=cookies)
    assert "priorizó" not in r.text and "comentó" in r.text
    r = client.get("/actividad?tipo=estado&ca=CA-1", cookies=cookies)
    assert "priorizó" in r.text and "comentó" not in r.text
    # Otra empresa no ve nada.
    ck_a, _ = _auth(settings, equipo["ana"])
    assert "priorizó" not in client.get("/actividad", cookies=ck_a).text


def test_preferencias_desde_cuenta(client, engine, settings, equipo):
    cookies, headers = _auth(settings, equipo["mario"])
    r = client.get("/cuenta", cookies=cookies)
    assert "Alarmas del equipo" in r.text
    r = client.post(
        "/cuenta/alarmas", data={"pref": ["resultado", "inventada"]}, cookies=cookies,
        headers=headers, follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(engine) as s:
        assert eq.preferencias(s, equipo["mario"]) == {
            "actividad_equipo": False, "asignada_a_mi": False, "cierre_cercano": False,
            "resultado": True,
        }


def test_admin_edita_nombre_visible(client, engine, settings, equipo):
    admin = _usuario(engine, "adm@t.cl", None, rol=RolUsuario.ADMIN)
    cookies, headers = _auth(settings, admin)
    r = client.post(
        f"/admin/usuarios/{equipo['gonzalo']}/nombre", data={"nombre": "  Gonzalo   Soto "},
        cookies=cookies, headers=headers, follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(engine) as s:
        assert _u(s, equipo["gonzalo"]).nombre == "Gonzalo Soto"
    assert "Gonzalo Soto" in client.get("/admin/usuarios", cookies=cookies).text
    ck_m, h_m = _auth(settings, equipo["mario"])
    r = client.post(
        f"/admin/usuarios/{equipo['gonzalo']}/nombre", data={"nombre": "X"}, cookies=ck_m,
        headers=h_m,
    )
    assert r.status_code == 403


def test_nombre_visible_e_iniciales():
    assert eq.nombre_visible(Usuario(email="gonzalo@t.cl")) == "gonzalo"
    assert eq.nombre_visible(Usuario(email="m@t.cl", nombre=" Mario ")) == "Mario"
    assert eq.iniciales("Mario Pérez") == "MP"
    assert eq.iniciales("gonzalo") == "GO"


# --- Alarmas de sistema ---------------------------------------------------------------------


def _trabajo(engine, emp, codigo, estado, uid, **campos):
    with Session(engine) as s:
        ahora = ahora_utc()
        s.add(
            CaTrabajo(
                empresa_id=emp, ca_codigo=codigo, estado=estado, version=1, creado_en=ahora,
                actualizado_en=ahora, actualizado_por_id=uid, **campos,
            )
        )
        s.commit()


def _sistema(engine, emp) -> list[tuple[str, str]]:
    with Session(engine) as s:
        return sorted(
            (e.ca_codigo, e.tipo)
            for e in s.execute(
                select(CaEvento).where(CaEvento.empresa_id == emp, CaEvento.usuario_id.is_(None))
            ).scalars()
        )


def test_alarmas_de_cierre_bordes_e_idempotencia(engine, equipo):
    emp, uid = equipo["emp"], equipo["mario"]
    ahora = ahora_utc().replace(microsecond=0)
    casos = {
        "C-24": ahora + timedelta(hours=24),  # borde: entra en 24 h
        "C-24M": ahora + timedelta(hours=24, seconds=1),  # fuera
        "C-4": ahora + timedelta(hours=4),  # borde: 4 h (solo el de 4 h)
        "C-10": ahora + timedelta(hours=10),
        "C-PAS": ahora - timedelta(minutes=1),  # ya cerró
    }
    for codigo, cierre in casos.items():
        _ca(engine, codigo, cierre=cierre)
        _trabajo(engine, emp, codigo, eq.COTIZANDO, uid)
    _ca(engine, "C-ENV", cierre=ahora + timedelta(hours=2))
    _trabajo(engine, emp, "C-ENV", eq.ENVIADA, uid)  # enviada: sin alarma de cierre
    with Session(engine) as s:
        assert eq.generar_alarmas(s, emp, ahora) == {"cierre_24h": 2, "cierre_4h": 1, "resultado": 0}
        s.commit()
    esperado = [("C-10", "cierre_24h"), ("C-24", "cierre_24h"), ("C-4", "cierre_4h")]
    assert _sistema(engine, emp) == esperado
    with Session(engine) as s:  # segunda pasada: mismos eventos
        assert eq.generar_alarmas(s, emp, ahora) == {"cierre_24h": 0, "cierre_4h": 0, "resultado": 0}
        s.commit()
    assert _sistema(engine, emp) == esperado
    # Pasan 7 h: C-10 entra en 4 h y suma su segundo aviso.
    with Session(engine) as s:
        eq.generar_alarmas(s, emp, ahora + timedelta(hours=7))
        s.commit()
    assert ("C-10", "cierre_4h") in _sistema(engine, emp)


def test_cierre_no_leido_si_la_ca_ya_no_esta_en_curso(engine, equipo):
    emp = equipo["emp"]
    ahora = ahora_utc()
    _ca(engine, "C-X", cierre=ahora + timedelta(hours=3), nombre="Leche")
    _trabajo(engine, emp, "C-X", eq.LISTA, equipo["mario"])
    with Session(engine) as s:
        eq.generar_alarmas(s, emp, ahora)
        s.commit()
        textos = eq.describir_eventos(
            s, eq.listar_no_leidos(s, _u(s, equipo["boris"]), emp), equipo["boris"], ahora
        )
    assert textos[0]["antes"] == "Cierra en 4 h y sigue en Lista para enviar:"
    with Session(engine) as s:
        t = s.execute(select(CaTrabajo).where(CaTrabajo.ca_codigo == "C-X")).scalar_one()
        t.estado = eq.ENVIADA
        s.commit()
    assert _no_leidos(engine, equipo["boris"], emp) == []


def _cot(s, codigo, rut, sel, valor=1000.0, id_cot=None):
    s.add(
        CaCotizacion(
            ca_codigo=codigo, llamado=1, rut_proveedor=rut, seleccionado=sel, valor_neto=valor,
            id_cotizacion=id_cot or f"{codigo}-{rut}-{valor}",
        )
    )


@pytest.mark.parametrize(
    ("ofertas", "estado_ca", "esperado"),
    [
        ([(_RUT_NUESTRO, True), (_RUT_OTRO, False)], "cerrada", "ganada"),
        ([(_RUT_NUESTRO, False), (_RUT_OTRO, True)], "cerrada", "perdida"),
        ([(_RUT_OTRO, True)], "cerrada", "no_registrada"),
        ([], "cerrada", "no_registrada"),
        ([(_RUT_NUESTRO, None), (_RUT_OTRO, None)], "cerrada", "sin_dato"),
        ([(_RUT_NUESTRO, None)], "desierta", "desierta"),
        ([], "cancelada", "desierta"),
    ],
)
def test_resultado_de_una_enviada(engine, equipo, ofertas, estado_ca, esperado):
    emp = equipo["emp"]
    ahora = ahora_utc()
    _ca(
        engine, "C-R", cierre=ahora - timedelta(days=1), estado=estado_ca,
        cotizaciones_obtenidas_en=ahora - timedelta(hours=1),
    )
    _trabajo(engine, emp, "C-R", eq.ENVIADA, equipo["mario"])
    with Session(engine) as s:
        for rut, sel in ofertas:
            _cot(s, "C-R", rut, sel)
        s.commit()
        assert eq.generar_alarmas(s, emp, ahora)["resultado"] == 1
        s.commit()
        t = s.execute(select(CaTrabajo)).scalar_one()
        assert t.resultado == esperado
        ev = s.execute(select(CaEvento).where(CaEvento.tipo == "resultado")).scalar_one()
        assert ev.datos == {"resultado": esperado}
        assert eq.generar_alarmas(s, emp, ahora)["resultado"] == 0  # idempotente


def test_resultado_usa_la_regla_de_eleccion_unica(engine, equipo):
    """Dos ofertas nuestras: una seleccionada → ganada aunque la otra no lo esté (F-merc-1c)."""
    emp, ahora = equipo["emp"], ahora_utc()
    _ca(engine, "C-R", cierre=ahora - timedelta(days=1), estado="cerrada",
        cotizaciones_obtenidas_en=ahora)
    _trabajo(engine, emp, "C-R", eq.ENVIADA, equipo["mario"])
    with Session(engine) as s:
        _cot(s, "C-R", "76123456-0", False, 900.0)
        _cot(s, "C-R", "76.123.456-0", True, 1000.0)
        s.commit()
        eq.generar_alarmas(s, emp, ahora)
        assert s.execute(select(CaTrabajo.resultado)).scalar_one() == "ganada"


def test_resultado_espera_ofertas_leidas_y_empresa_con_rut(client, engine, settings, equipo):
    ahora = ahora_utc()
    sin_rut = _empresa(engine, "Sin RUT", rut=None)
    uid = _usuario(engine, "x@t.cl", sin_rut)
    _ca(engine, "C-R", cierre=ahora - timedelta(days=1), estado="cerrada",
        cotizaciones_obtenidas_en=ahora)
    _ca(engine, "C-NL", cierre=ahora - timedelta(days=1), estado="cerrada")  # sin leer
    _trabajo(engine, sin_rut, "C-R", eq.ENVIADA, uid)
    _trabajo(engine, equipo["emp"], "C-NL", eq.ENVIADA, equipo["mario"])
    with Session(engine) as s:
        assert eq.generar_alarmas(s, sin_rut, ahora)["resultado"] == 0
        assert eq.generar_alarmas(s, equipo["emp"], ahora)["resultado"] == 0
    cookies, _ = _auth(settings, uid)
    r = client.get("/equipo/ca/C-R/bloque", cookies=cookies)
    assert "necesita su RUT" in r.text and "/admin/empresas" in r.text


def test_sondeo_limitado_a_una_vez_por_minuto(engine, equipo):
    emp = equipo["emp"]
    ahora = ahora_utc()
    with Session(engine) as s:
        assert eq.generar_si_toca(s, emp, ahora) is True
        s.commit()
        assert eq.generar_si_toca(s, emp, ahora + timedelta(seconds=30)) is False
        assert eq.generar_si_toca(s, emp, ahora + timedelta(seconds=61)) is True
        s.commit()
        # Otra empresa tiene su propia marca.
        assert eq.generar_si_toca(s, equipo["otra"], ahora + timedelta(seconds=30)) is True


def test_alarmas_al_final_de_mercado_ca(engine, equipo, monkeypatch):
    from app.ingest import orchestrator

    emp = equipo["emp"]
    _ca(engine, "C-J", cierre=ahora_utc() + timedelta(hours=2))
    _trabajo(engine, emp, "C-J", eq.PRIORIZADA, equipo["mario"])
    assert orchestrator._alarmas_equipo(engine) == {"cierre_24h": 0, "cierre_4h": 1, "resultado": 0}
    assert _sistema(engine, emp) == [("C-J", "cierre_4h")]

    def falla(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr("app.mercado.equipo.generar_alarmas_todas", falla)
    assert orchestrator._alarmas_equipo(engine) == "error: RuntimeError"


# --- Retención -------------------------------------------------------------------------------


def test_retencion_purga_eventos_viejos_y_protege_ca_trabajada(engine, equipo):
    emp = equipo["emp"]
    viejo = ahora_utc() - timedelta(days=400)
    _ca(engine, "C-OLD", estado="desierta", cierre=viejo)
    _ca(engine, "C-OLD2", estado="desierta", cierre=viejo)
    with Session(engine) as s:
        for c in ("C-OLD", "C-OLD2"):
            ca = s.get(CompraAgil, c)
            assert ca is not None
            ca.actualizado_en = viejo
            ca.raw_json = {"x": 1}
            s.add(CaProducto(ca_codigo=c, nombre="p"))
        s.add(CaEvento(empresa_id=emp, ca_codigo="C-OLD", usuario_id=equipo["mario"],
                       tipo="estado", creado_en=viejo))
        s.add(CaEvento(empresa_id=emp, ca_codigo="C-OLD", usuario_id=equipo["mario"],
                       tipo="comentario", creado_en=ahora_utc()))
        s.commit()
    _trabajo(engine, emp, "C-OLD", eq.DESCARTADA, equipo["mario"], motivo_descarte="m")
    with Session(engine) as s:
        res = purgar_terminales(s)
        s.commit()
        assert res["eventos_equipo_borrados"] == 1
        assert [e.tipo for e in s.execute(select(CaEvento)).scalars()] == ["comentario"]
        assert s.execute(select(func.count()).select_from(CaTrabajo)).scalar_one() == 1
        protegida, purgada = s.get(CompraAgil, "C-OLD"), s.get(CompraAgil, "C-OLD2")
        assert protegida is not None and protegida.raw_json == {"x": 1}
        assert purgada is not None and purgada.raw_json is None


# --- Pantallas -------------------------------------------------------------------------------


def test_mesa_filtro_equipo_y_descartadas_ocultas(client, engine, settings, equipo):
    for c in ("CA-2", "CA-3", "CA-4"):
        _ca(engine, c)
    _estado(client, settings, equipo["mario"], "CA-2", "priorizada", 0)
    _estado(client, settings, equipo["mario"], "CA-3", "descartada", 0, motivo="lejos")
    _estado(client, settings, equipo["gonzalo"], "CA-4", "cotizando", 0)
    cookies, headers = _auth(settings, equipo["mario"])
    r = client.get("/mesa", cookies=cookies)
    assert "mesa-ca-CA-1" in r.text and "mesa-ca-CA-3" not in r.text
    assert 'data-equipo-estado="priorizada"' in r.text
    assert "Fuente: Dirección ChileCompra" in r.text

    def codigos(qs):
        t = client.get("/mesa?" + qs, cookies=cookies).text
        return {c for c in ("CA-1", "CA-2", "CA-3", "CA-4") if f'id="mesa-ca-{c}"' in t}

    assert codigos("descartadas=1") == {"CA-1", "CA-2", "CA-3", "CA-4"}
    assert codigos("equipo=sin_trabajar") == {"CA-1"}
    assert codigos("equipo=priorizadas") == {"CA-2"}
    assert codigos("equipo=en_curso") == {"CA-4"}
    assert codigos("equipo=enviadas") == set()
    r = client.get("/mesa/conteo?equipo=priorizadas", cookies=cookies)
    assert ">1<" in r.text


def test_mesa_mias_y_punto_de_comentarios(client, engine, settings, equipo):
    cookies, headers = _auth(settings, equipo["mario"])
    client.post("/equipo/ca/CA-1/asignar", data={"responsable_id": "yo", "version": "0"},
                cookies=cookies, headers=headers)
    _comentar(client, settings, equipo["gonzalo"], "hola")
    r = client.get("/mesa?equipo=mias", cookies=cookies)
    assert 'id="mesa-ca-CA-1"' in r.text
    assert "punto-nuevo" in r.text  # comentario de otro, sin leer
    ck_g, _ = _auth(settings, equipo["gonzalo"])
    assert 'id="mesa-ca-CA-1"' not in client.get("/mesa?equipo=mias", cookies=ck_g).text


def test_ficha_y_base_traen_equipo_y_campana(client, engine, settings, equipo):
    cookies, _ = _auth(settings, equipo["mario"])
    r = client.get("/oportunidad/compras_agiles/CA-1", cookies=cookies)
    assert r.status_code == 200
    assert "/equipo/ca/CA-1/bloque?origen=ficha" in r.text and "/equipo/ca/CA-1/hilo" in r.text
    assert 'hx-get="/actividad/contador"' in r.text
    assert 'id="campana-n"' in r.text and 'id="campana-n-m"' in r.text
    r = client.get("/equipo/ca/CA-1/hilo", cookies=cookies)
    assert 'hx-trigger="every 60s"' in r.text
    # Ficha de una CA fuera del territorio: sin bloque de equipo.
    _ca(engine, "CA-RM", region=13)
    r = client.get("/oportunidad/compras_agiles/CA-RM", cookies=cookies)
    assert "/equipo/ca/CA-RM/bloque" not in r.text


def test_hilo_intercala_lineas_de_sistema(client, settings, equipo):
    _comentar(client, settings, equipo["mario"], "uno")
    _estado(client, settings, equipo["mario"], "CA-1", "cotizando", 0)
    cookies, _ = _auth(settings, equipo["boris"])
    t = client.get("/equipo/ca/CA-1/hilo", cookies=cookies).text
    assert t.index("uno") < t.index("Mario Pérez pasó a Cotizando")


def test_hoy_bloque_equipo(client, engine, settings, equipo):
    _ca(engine, "C-URG", cierre=ahora_utc() + timedelta(hours=5), nombre="Urgente")
    cookies, headers = _auth(settings, equipo["mario"])
    client.post("/equipo/ca/C-URG/asignar", data={"responsable_id": "yo", "version": "0"},
                cookies=cookies, headers=headers)
    r = client.get("/", cookies=cookies)
    assert r.status_code == 200
    assert 'data-hoy="mis-ca"' in r.text and "Urgente" in r.text
    assert "cierra hoy" in r.text or "cierra mañana" in r.text


def test_cuando_cierra_y_hace():
    from datetime import datetime

    ahora = datetime(2026, 11, 5, 15, 0)  # 12:00 en Chile (UTC-3)
    assert eq.cuando_cierra(datetime(2026, 11, 5, 18, 0), ahora) == "cierra hoy 15:00"
    assert eq.cuando_cierra(datetime(2026, 11, 6, 18, 0), ahora) == "cierra mañana 15:00"
    assert eq.cuando_cierra(datetime(2026, 11, 9, 18, 0), ahora) == "cierra el 09/11 15:00"
    assert eq.cuando_cierra(datetime(2026, 11, 5, 14, 0), ahora) == "ya cerró"
    assert eq.hace(ahora - timedelta(minutes=2), ahora) == "hace 2 min"
