"""F-perfiles-1: /perfiles liviano y legible, formulario único bajo demanda, pausar, /cuenta.

Corre sobre SQLite en memoria (como test_api.py); lo que necesita Postgres (la
exclusión que choca con una keyword) se prueba en test_ajustes_pg.py.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.query import conteo_vigentes_por_perfil
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.core.vigencia import es_vigente
from app.matching.engine import match_todos
from app.matching.perfiles import crear_perfil, listar_perfiles
from app.models.base import Base
from app.models.enums import EstadoOportunidad, RolUsuario
from app.models.tables import (
    CompraAgil,
    InstitucionPAC,
    Licitacion,
    OportunidadMatch,
    PerfilBusqueda,
    Usuario,
)

_PW = "contraseña-segura-test"


@pytest.fixture()
def engine():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    return e


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


@pytest.fixture()
def usuario(engine):
    with Session(engine) as s:
        u = Usuario(email="user@test.cl", password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        s.add(u)
        s.commit()
        return u.id


def _cookie(settings: Settings, user_id: int) -> dict[str, str]:
    return {COOKIE_NAME: create_session_token(settings.secret_key, user_id)}


def _session(settings: Settings, user_id: int) -> tuple[dict[str, str], dict[str, str]]:
    """Cookie de sesión + header X-CSRF-Token coherentes (mismo nonce)."""
    token = create_session_token(settings.secret_key, user_id)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    return {COOKIE_NAME: token}, {"X-CSRF-Token": generate_csrf_token(settings.secret_key, decoded[1])}


def _ahora() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _otro_usuario(engine) -> int:
    from app.auth.password import hash_password

    with Session(engine) as s:
        u = Usuario(email="otro@test.cl", password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        s.add(u)
        s.commit()
        return u.id


def _perfil(engine, uid: int, nombre: str = "P", **kw) -> int:
    kw.setdefault("keywords", ["luz"])
    with Session(engine) as s:
        p = crear_perfil(s, owner_id=uid, nombre=nombre, **kw)
        s.commit()
        return p.id


def _sembrar_ca(engine, codigo: str, *, region: int = 13, cierre_dias: int | None = 5) -> None:
    with Session(engine) as s:
        s.add(
            CompraAgil(
                codigo=codigo,
                nombre="Compra ágil",
                estado=EstadoOportunidad.PUBLICADA.value,
                fecha_cierre=None if cierre_dias is None else _ahora() + timedelta(days=cierre_dias),
                fecha_publicacion=_ahora() - timedelta(days=1),
                region=region,
            )
        )
        s.commit()


def _sembrar_lic(engine, codigo: str, *, cierre_dias: int = 5) -> None:
    with Session(engine) as s:
        s.add(
            Licitacion(
                codigo=codigo,
                nombre="Licitación",
                estado=EstadoOportunidad.PUBLICADA.value,
                fecha_cierre=_ahora() + timedelta(days=cierre_dias),
            )
        )
        s.commit()


def _match(engine, perfil_id: int, fuente: str, codigo: str, dias_atras: int = 0) -> None:
    with Session(engine) as s:
        s.add(
            OportunidadMatch(
                perfil_id=perfil_id,
                fuente=fuente,
                codigo_oportunidad=codigo,
                score=50,
                razones={},
                fecha_match=_ahora() - timedelta(days=dias_atras),
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# GET /perfiles: liviana y legible
# ---------------------------------------------------------------------------


def test_perfiles_no_trae_widgets_ni_catalogo_y_pesa_poco(engine, client, usuario, settings):
    with Session(engine) as s:
        s.add(InstitucionPAC(codigo_entidad=224060, razon_social="MINISTERIO  PUBLICO", sector="Legislativo"))
        s.commit()
    ids = [
        _perfil(
            engine,
            usuario,
            f"Perfil {n}",
            keywords=["software", "desarrollo"],
            keywords_excluir=["aseo"],
            regiones=[13, 5],
            monto_min_clp=5_000_000,
            categorias_unspsc=["4321", "8111"],
            organismos_seguidos=["224060", "ORG-RARO"],
        )
        for n in range(4)
    ]
    _sembrar_ca(engine, "CA-1")
    _sembrar_lic(engine, "LIC-1")
    _match(engine, ids[0], "compras_agiles", "CA-1")
    _match(engine, ids[0], "licitaciones", "LIC-1")

    r = client.get("/perfiles", cookies=_cookie(settings, usuario))

    assert r.status_code == 200
    assert "js-rubros-widget" not in r.text
    assert "js-org-widget" not in r.text
    assert "MP_ORGANISMOS_CATALOGO" not in r.text
    assert "categorias_unspsc" not in r.text
    assert len(r.content) < 200_000
    # Resumen legible: montos, regiones, rubros y organismos por nombre; no códigos.
    assert "$5.000.000 – sin máximo" in r.text
    assert "Tarapacá" not in r.text and "Metropolitana de Santiago" in r.text
    assert "MINISTERIO PUBLICO" in r.text  # razón social resuelta (espacios normalizados)
    assert "ORG-RARO" in r.text  # sin nombre en el catálogo: el código
    assert "Licitaciones · Compras Ágiles" in r.text
    assert "<strong>2</strong> oportunidades vigentes" in r.text
    assert "1 licitaciones · 1 compras ágiles" in r.text
    assert "último match nuevo" in r.text


def test_perfiles_get_no_sincroniza_el_catalogo_por_red(client, usuario, settings):
    import respx

    with respx.mock(assert_all_called=False) as mock:
        ruta = mock.get(settings.plan_compra_kpi_url)
        r = client.get("/perfiles", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert not ruta.called


def test_perfiles_sin_perfiles_y_tarjeta_de_favoritos_plegable(client, usuario, settings):
    r = client.get("/perfiles", cookies=_cookie(settings, usuario))
    assert "Aún no tienes perfiles" in r.text
    assert 'id="rubros-favoritos"' in r.text
    assert "/rubros-favoritos/form" in r.text
    assert "<optgroup" not in r.text  # el <select> de rubros llega al desplegar
    r2 = client.get("/rubros-favoritos/form", cookies=_cookie(settings, usuario))
    assert r2.status_code == 200 and "<optgroup" in r2.text


def test_nav_tiene_cuenta_y_cuenta_tiene_los_ajustes(client, usuario, settings):
    html = client.get("/perfiles", cookies=_cookie(settings, usuario)).text
    assert 'href="/cuenta"' in html
    assert "RUT de proveedor" not in html and "dias_resumen" not in html
    cuenta = client.get("/cuenta", cookies=_cookie(settings, usuario))
    assert cuenta.status_code == 200
    for texto in ("RUT de proveedor", "dias_resumen", "password_actual"):
        assert texto in cuenta.text
    assert client.get("/cuenta", follow_redirects=False).status_code in (302, 303, 307)


def test_post_de_cuenta_redirige_a_cuenta(client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post("/cuenta/resumen", data={"dias_resumen": "7"}, cookies=cookies, headers=headers, follow_redirects=False)
    assert r.headers["location"].startswith("/cuenta?mensaje=")
    r = client.post("/cuenta/resumen", data={"dias_resumen": "x"}, cookies=cookies, headers=headers, follow_redirects=False)
    assert r.headers["location"].startswith("/cuenta?error=")
    r = client.post("/perfiles/rut-proveedor", data={"rut_proveedor": "1-9"}, cookies=cookies, headers=headers, follow_redirects=False)
    assert r.headers["location"].startswith("/cuenta?mensaje=")


# ---------------------------------------------------------------------------
# Formulario bajo demanda: ownership
# ---------------------------------------------------------------------------


def test_form_editar_dueno_200_y_ajeno_404(engine, client, usuario, settings):
    pid = _perfil(engine, usuario, "Mío", keywords=["luz"], monto_min_clp=5_000_000, categorias_unspsc=["4321", "43211500"])
    otro = _otro_usuario(engine)

    r = client.get(f"/perfiles/{pid}/form", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert 'value="Mío"' in r.text
    assert 'value="5.000.000"' in r.text  # monto con miles
    assert 'name="categorias_unspsc_extra"' in r.text and 'value="43211500"' in r.text
    assert f'for="p{pid}_nombre"' in r.text

    assert client.get(f"/perfiles/{pid}/form", cookies=_cookie(settings, otro)).status_code == 404
    assert client.get("/perfiles/999999/form", cookies=_cookie(settings, usuario)).status_code == 404


def test_form_nuevo_trae_widgets_con_ids_unicos(client, usuario, settings):
    r = client.get("/perfiles/nuevo/form", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "js-rubros-widget" in r.text and "js-org-widget" in r.text
    assert 'name="categorias_unspsc_extra"' in r.text
    # Un solo input con name="categorias_unspsc" por familia (checkbox); el de prefijos finos ya no lo comparte.
    assert 'type="text" name="categorias_unspsc"' not in r.text
    assert 'for="nuevo_nombre"' in r.text and 'id="nuevo_nombre"' in r.text


def test_activo_dueno_ok_y_ajeno_404(engine, client, usuario, settings):
    pid = _perfil(engine, usuario)
    otro = _otro_usuario(engine)
    c2, h2 = _session(settings, otro)
    r = client.post(f"/perfiles/{pid}/activo", data={"activo": ""}, cookies=c2, headers=h2)
    assert r.status_code == 404
    c, h = _session(settings, usuario)
    sin_csrf = client.post(f"/perfiles/{pid}/activo", data={"activo": "", "csrf_token": "malo"}, cookies=_cookie(settings, usuario))
    assert sin_csrf.status_code == 403
    r = client.post(f"/perfiles/{pid}/activo", data={"activo": ""}, cookies=c, headers=h, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(PerfilBusqueda, pid).activo is False


# ---------------------------------------------------------------------------
# Pausar y reactivar
# ---------------------------------------------------------------------------


def test_pausar_deja_el_perfil_visible_pero_fuera_del_matching(engine, client, usuario, settings):
    _sembrar_ca(engine, "CA-PAUSA", region=13)
    pid = _perfil(engine, usuario, "Pausable", keywords=[], regiones=[13], fuentes=["compras_agiles"])
    cookies, headers = _session(settings, usuario)

    client.post(f"/perfiles/{pid}/activo", data={"activo": ""}, cookies=cookies, headers=headers)

    with Session(engine) as s:
        assert s.get(PerfilBusqueda, pid).activo is False
        assert listar_perfiles(s, usuario) == []  # el feed y el resumen usan solo activos
        assert [p.id for p in listar_perfiles(s, usuario, incluir_pausados=True)] == [pid]
        match_todos(s)
        s.commit()
        assert s.execute(select(OportunidadMatch).where(OportunidadMatch.perfil_id == pid)).first() is None
    html = client.get("/perfiles", cookies=cookies).text
    assert "Pausable" in html and "Pausado" in html


def test_pausado_muestra_el_ultimo_conteo_con_nota(engine, client, usuario, settings):
    _sembrar_ca(engine, "CA-P1")
    pid = _perfil(engine, usuario, "Con conteo", keywords=[], regiones=[13])
    _match(engine, pid, "compras_agiles", "CA-P1")
    cookies, headers = _session(settings, usuario)
    client.post(f"/perfiles/{pid}/activo", data={"activo": ""}, cookies=cookies, headers=headers)
    html = client.get("/perfiles", cookies=cookies).text
    assert "(pausado)" in html and "<strong>1</strong> oportunidad vigente" in html


def test_reactivar_dispara_el_match_en_segundo_plano(engine, client, usuario, settings):
    pid = _perfil(engine, usuario)
    with Session(engine) as s:
        s.get(PerfilBusqueda, pid).activo = False
        s.commit()
    cookies, headers = _session(settings, usuario)
    with patch("app.api.routes.pages._match_perfil_background") as match:
        r = client.post(f"/perfiles/{pid}/activo", data={"activo": "1"}, cookies=cookies, headers=headers)
        assert r.status_code == 200 or r.status_code == 303
        match.assert_called_once()
        assert match.call_args.args[1] == pid
        # Ya activo: repetir no vuelve a lanzarlo.
        client.post(f"/perfiles/{pid}/activo", data={"activo": "1"}, cookies=cookies, headers=headers)
        match.assert_called_once()
    with Session(engine) as s:
        assert s.get(PerfilBusqueda, pid).activo is True


def test_activo_con_htmx_devuelve_la_tarjeta(engine, client, usuario, settings):
    pid = _perfil(engine, usuario, "Tarjeta")
    cookies, headers = _session(settings, usuario)
    r = client.post(
        f"/perfiles/{pid}/activo", data={"activo": ""}, cookies=cookies, headers={**headers, "HX-Request": "true"}
    )
    assert r.status_code == 200
    assert f'id="perfil-{pid}"' in r.text and "<html" not in r.text and "Pausado" in r.text


# ---------------------------------------------------------------------------
# Errores sin perder lo escrito
# ---------------------------------------------------------------------------


_ESCRITO = {
    "nombre": "",
    "keywords": "energía, luz",
    "excluir": "aseo",
    "fuentes": ["compras_agiles"],
    "regiones": ["5", "13"],
    "monto_min_clp": "9.000.000",
    "monto_max_clp": "1.000",
    "categorias_unspsc": ["4321"],
    "categorias_unspsc_extra": "43211500",
    "organismos_seguidos": "224060,ORG-X",
}


def _assert_conserva_lo_escrito(html: str, form_id: str) -> None:
    assert 'value="energía, luz"' in html
    assert 'value="aseo"' in html
    assert 'value="9.000.000"' in html and 'value="1.000"' in html
    for region in (5, 13):
        assert re.search(rf'<input[^>]*id="{form_id}_reg_{region}"[^>]*checked', html), region
    assert not re.search(rf'<input[^>]*id="{form_id}_reg_1"[^>]*checked', html)
    assert 'value="4321"' in html and "checked" in html.split('value="4321"')[1].split(">")[0]
    assert 'value="43211500"' in html
    assert 'data-preseleccion="224060,ORG-X"' in html
    assert f'id="{form_id}_f_lic"' in html


def test_crear_con_nombre_vacio_responde_422_con_lo_escrito(engine, client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post("/perfiles/nuevo", data=_ESCRITO, cookies=cookies, headers=headers)
    assert r.status_code == 422
    assert "Escribe un nombre para el perfil" in r.text
    assert "El monto mínimo no puede ser mayor al monto máximo" in r.text
    assert "<html" in r.text  # sin JS: página completa con el formulario abierto
    _assert_conserva_lo_escrito(r.text, "nuevo")
    with Session(engine) as s:
        assert s.execute(select(PerfilBusqueda)).first() is None


def test_crear_con_htmx_responde_solo_el_formulario(engine, client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post("/perfiles/nuevo", data=_ESCRITO, cookies=cookies, headers={**headers, "HX-Request": "true"})
    assert r.status_code == 422
    assert "<html" not in r.text and "data-perfil-form" in r.text
    assert 'aria-describedby="nuevo_nombre_error"' in r.text
    _assert_conserva_lo_escrito(r.text, "nuevo")


def test_editar_con_error_no_modifica_nada_y_conserva_lo_escrito(engine, client, usuario, settings):
    pid = _perfil(engine, usuario, "Original", keywords=["luz"], monto_min_clp=100)
    cookies, headers = _session(settings, usuario)
    r = client.post(
        f"/perfiles/{pid}/editar",
        data={**_ESCRITO, "nombre": "Cambiado"},
        cookies=cookies,
        headers={**headers, "HX-Request": "true"},
    )
    assert r.status_code == 422
    assert 'value="Cambiado"' in r.text and f'id="formwrap-p{pid}"' in r.text
    _assert_conserva_lo_escrito(r.text, f"p{pid}")
    with Session(engine) as s:
        s.expire_all()
        p = s.get(PerfilBusqueda, pid)
        assert (p.nombre, p.keywords, p.monto_min_clp, p.regiones) == ("Original", ["luz"], 100, [])


def test_editar_sin_js_re_renderiza_la_pagina_con_el_formulario_en_su_tarjeta(engine, client, usuario, settings):
    pid = _perfil(engine, usuario, "Original")
    cookies, headers = _session(settings, usuario)
    r = client.post(f"/perfiles/{pid}/editar", data={**_ESCRITO, "nombre": "X"}, cookies=cookies, headers=headers)
    assert r.status_code == 422
    assert f'id="slot-{pid}"' in r.text
    assert r.text.index(f'id="slot-{pid}"') < r.text.index(f'id="formwrap-p{pid}"')


def test_perfil_sin_criterios_error_junto_al_campo_keywords(client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post("/perfiles/nuevo", data={"nombre": "Vacío"}, cookies=cookies, headers={**headers, "HX-Request": "true"})
    assert r.status_code == 422
    assert 'id="nuevo_keywords_error"' in r.text


def test_guardar_ok_redirige_con_aviso_y_la_tarjeta_dice_actualizando(engine, client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post("/perfiles/nuevo", data={"nombre": "Ok", "keywords": "luz"}, cookies=cookies, headers=headers, follow_redirects=False)
    assert r.status_code == 303
    destino = r.headers["location"]
    assert "Perfil+guardado" in destino and "reciente=" in destino
    html = client.get(destino, cookies=cookies).text
    assert "Perfil guardado. Buscando oportunidades…" in html
    assert "Actualizando…" in html and "hx-trigger=\"load delay:5s\"" in html

    # Con HTMX no se navega: HX-Redirect.
    r = client.post(
        "/perfiles/nuevo",
        data={"nombre": "Ok2", "keywords": "luz"},
        cookies=cookies,
        headers={**headers, "HX-Request": "true"},
    )
    assert r.status_code == 200 and r.headers["HX-Redirect"].startswith("/perfiles?mensaje=")


def test_tarjeta_sigue_actualizando_hasta_que_cambie_o_se_agoten_los_intentos(engine, client, usuario, settings):
    pid = _perfil(engine, usuario)
    cookies, _ = _session(settings, usuario)
    r = client.get(f"/perfiles/{pid}/tarjeta?intento=1&antes=0", cookies=cookies)
    assert "Actualizando…" in r.text and "intento=2" in r.text
    r = client.get(f"/perfiles/{pid}/tarjeta?intento=2&antes=0", cookies=cookies)
    assert "Actualizando…" not in r.text
    _sembrar_ca(engine, "CA-T")
    _match(engine, pid, "compras_agiles", "CA-T")
    r = client.get(f"/perfiles/{pid}/tarjeta?intento=1&antes=0", cookies=cookies)
    assert "Actualizando…" not in r.text and "<strong>1</strong>" in r.text
    assert client.get(f"/perfiles/{pid}/tarjeta", cookies=_cookie(settings, _otro_usuario(engine))).status_code == 404


# ---------------------------------------------------------------------------
# Montos y rubros
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("texto", ["5.000.000", "$5.000.000", "5000000", " $ 5.000.000 "])
def test_montos_con_miles_se_guardan_como_numero(engine, client, usuario, settings, texto):
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/perfiles/nuevo",
        data={"nombre": "Monto", "keywords": "luz", "monto_min_clp": texto},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.execute(select(PerfilBusqueda.monto_min_clp)).scalar_one() == 5_000_000


def test_rubros_avanzados_se_unen_con_los_checkboxes(engine, client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    client.post(
        "/perfiles/nuevo",
        data={
            "nombre": "Rubros",
            "categorias_unspsc": ["4321", "8111"],
            "categorias_unspsc_extra": "43211500, 432115, xx",
        },
        cookies=cookies,
        headers=headers,
    )
    with Session(engine) as s:
        assert s.execute(select(PerfilBusqueda.categorias_unspsc)).scalar_one() == ["4321", "8111", "43211500", "432115"]


def test_el_explorador_de_ca_mantiene_su_campo_de_prefijos(client, usuario, settings):
    html = client.get("/compras-agiles", cookies=_cookie(settings, usuario)).text
    assert 'name="categorias_unspsc"' in html
    assert "categorias_unspsc_extra" not in html


# ---------------------------------------------------------------------------
# Conteo agrupado
# ---------------------------------------------------------------------------


def test_conteo_agrupado_coincide_con_contar_perfil_por_perfil(engine, usuario):
    ahora = _ahora()
    with Session(engine) as s:
        s.add_all(
            [
                Licitacion(codigo="L-VIG", nombre="v", estado="publicada", fecha_cierre=ahora + timedelta(days=3)),
                Licitacion(codigo="L-VENC", nombre="v", estado="publicada", fecha_cierre=ahora - timedelta(days=1)),
                Licitacion(codigo="L-ADJ", nombre="v", estado="adjudicada", fecha_cierre=ahora + timedelta(days=3)),
                CompraAgil(codigo="C-VIG", nombre="v", estado="publicada", fecha_cierre=ahora + timedelta(days=1)),
                CompraAgil(codigo="C-VENC", nombre="v", estado="publicada", fecha_cierre=ahora - timedelta(hours=1)),
                CompraAgil(codigo="C-SINCIERRE", nombre="v", estado="publicada", fecha_publicacion=ahora - timedelta(days=2)),
                CompraAgil(codigo="C-SINCIERRE-VIEJA", nombre="v", estado="publicada", fecha_publicacion=ahora - timedelta(days=30)),
            ]
        )
        s.commit()
    a = _perfil(engine, usuario, "A")
    b = _perfil(engine, usuario, "B")
    vacio = _perfil(engine, usuario, "Vacío")
    for pid, fuente, codigo, dias in [
        (a, "licitaciones", "L-VIG", 3),
        (a, "licitaciones", "L-VENC", 1),
        (a, "licitaciones", "L-ADJ", 2),
        (a, "compras_agiles", "C-VIG", 0),
        (a, "compras_agiles", "C-SINCIERRE", 5),
        (b, "licitaciones", "L-VIG", 7),
        (b, "compras_agiles", "C-VENC", 0),
        (b, "compras_agiles", "C-SINCIERRE-VIEJA", 0),
        (b, "compras_agiles", "C-FANTASMA", 0),  # match sin fila de la oportunidad
    ]:
        _match(engine, pid, fuente, codigo, dias)

    with Session(engine) as s:
        agrupado = conteo_vigentes_por_perfil(s, [a, b, vacio], ahora)
        esperado: dict[int, tuple[int, int]] = {}
        for pid in (a, b, vacio):
            n = {"licitaciones": 0, "compras_agiles": 0}
            for m in s.execute(select(OportunidadMatch).where(OportunidadMatch.perfil_id == pid)).scalars():
                modelo = Licitacion if m.fuente == "licitaciones" else CompraAgil
                op = s.get(modelo, m.codigo_oportunidad)
                if op is not None and es_vigente(op.estado, op.fecha_cierre, m.fuente, ahora, op.fecha_publicacion):
                    n[m.fuente] += 1
            esperado[pid] = (n["licitaciones"], n["compras_agiles"])

    assert esperado == {a: (1, 2), b: (1, 0), vacio: (0, 0)}
    for pid, (n_lic, n_ca) in esperado.items():
        c = agrupado.get(pid)
        obtenido = (c.licitaciones, c.compras_agiles) if c else (0, 0)
        assert obtenido == (n_lic, n_ca)
    assert agrupado[a].ultimo_match is not None and vacio not in agrupado


def test_perfiles_get_usa_una_consulta_de_conteo_para_todos_los_perfiles(engine, client, usuario, settings):
    for n in range(5):
        _perfil(engine, usuario, f"P{n}")
    with patch("app.api.routes.pages.conteo_vigentes_por_perfil", wraps=conteo_vigentes_por_perfil) as conteo:
        client.get("/perfiles", cookies=_cookie(settings, usuario))
    assert conteo.call_count == 1
    assert len(conteo.call_args.args[1]) == 5
