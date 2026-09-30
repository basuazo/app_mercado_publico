"""Tests F-ca-explorar sobre SQLite: la regla de vigencia en SQL, las rutas del
explorador (universo, filtros que no usan FTS, paginación, conteo), los rubros
favoritos y que ninguna ruta llame a la red.

Lo que necesita Postgres (vocabulario, "posible" por texto, websearch) está en
tests/test_ca_explorar_pg.py.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.core.vigencia import condicion_ca_vigente, es_vigente
from app.explorador_ca import (
    FiltrosExplorador,
    agregar_favorito,
    buscar,
    contar,
    listar_favoritos,
    prefijos_validos,
    quitar_favorito,
)
from app.models.base import Base
from app.models.enums import RolUsuario, ValorFeedback
from app.models.tables import (
    CaProducto,
    CompraAgil,
    MatchFeedback,
    RubroFavorito,
    RubroVocabulario,
    SyncState,
    Usuario,
)

_PW = "contraseña-segura-test"


@pytest.fixture()
def engine():
    import app.models.tables  # noqa: F401

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


def _usuario(engine, email: str) -> int:
    with Session(engine) as s:
        u = Usuario(email=email, password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


@pytest.fixture()
def usuario(engine):
    return _usuario(engine, "user@test.cl")


@pytest.fixture()
def otro_usuario(engine):
    return _usuario(engine, "otro@test.cl")


def _sesion(settings: Settings, user_id: int) -> tuple[dict[str, str], dict[str, str]]:
    token = create_session_token(settings.secret_key, user_id)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    _, nonce = decoded
    return {COOKIE_NAME: token}, {"X-CSRF-Token": generate_csrf_token(settings.secret_key, nonce)}


def _ca(engine, codigo: str, **campos: Any) -> None:
    ahora = ahora_utc()
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": f"Compra {codigo}",
        "estado": "publicada",
        "fecha_publicacion": ahora - timedelta(days=1),
        "fecha_cierre": ahora + timedelta(days=2),
        "monto_disponible_clp": 1_000_000.0,
        "organismo_nombre": "ORG EXP",
        "region": 13,
    }
    base.update(campos)
    with Session(engine) as s:
        s.add(CompraAgil(**base))
        s.commit()


def _codigos(html: str, prefijo: str = "EXP-") -> set[str]:
    import re

    return set(re.findall(rf"{prefijo}[A-Z0-9-]+", html))


# ---------------------------------------------------------------------------
# La regla SQL de vigencia coincide con `es_vigente`
# ---------------------------------------------------------------------------


def test_condicion_sql_de_vigencia_coincide_con_es_vigente(engine):
    ahora = ahora_utc()
    estados = ["publicada", "PUBLICADA ", "desconocido", "estado-raro", "cerrada", "adjudicada", "suspendida"]
    cierres = [ahora + timedelta(days=1), ahora - timedelta(days=1), None]
    publicaciones = [ahora - timedelta(days=1), ahora - timedelta(days=8), None]
    esperado: dict[str, bool] = {}
    with Session(engine) as s:
        n = 0
        for estado in estados:
            for cierre in cierres:
                for publicacion in publicaciones:
                    n += 1
                    codigo = f"M{n:03d}"
                    s.add(
                        CompraAgil(
                            codigo=codigo,
                            estado=estado,
                            fecha_cierre=cierre,
                            fecha_publicacion=publicacion,
                        )
                    )
                    esperado[codigo] = es_vigente(estado, cierre, "compras_agiles", ahora, publicacion)
        s.commit()
        en_sql = set(s.execute(select(CompraAgil.codigo).where(condicion_ca_vigente(ahora))).scalars())
    assert en_sql == {c for c, v in esperado.items() if v}
    assert any(esperado.values()) and not all(esperado.values())


# ---------------------------------------------------------------------------
# Explorador: universo y filtros (sin FTS)
# ---------------------------------------------------------------------------


def _pagina(client, settings, user_id, **params) -> Any:
    # `sin_favoritos=1`: sin rubros y sin favoritos preseleccionados.
    params.setdefault("sin_favoritos", "1")
    r = client.get("/compras-agiles", params=params, cookies=_sesion(settings, user_id)[0])
    assert r.status_code == 200
    return r


def test_universo_solo_vigentes(client, engine, settings, usuario):
    ahora = ahora_utc()
    _ca(engine, "EXP-VIGENTE")
    _ca(engine, "EXP-VENCIDA", fecha_cierre=ahora - timedelta(hours=1))
    _ca(engine, "EXP-SINCIERRE-RECIENTE", fecha_cierre=None, fecha_publicacion=ahora - timedelta(days=2))
    _ca(engine, "EXP-SINCIERRE-VIEJA", fecha_cierre=None, fecha_publicacion=ahora - timedelta(days=8))
    _ca(engine, "EXP-CERRADA", estado="cerrada")
    html = _pagina(client, settings, usuario).text
    assert _codigos(html) == {"EXP-VIGENTE", "EXP-SINCIERRE-RECIENTE"}
    assert "Fuente: Dirección ChileCompra" in html


def test_descartadas_del_usuario_no_aparecen_y_las_de_otro_si(client, engine, settings, usuario, otro_usuario):
    _ca(engine, "EXP-A")
    _ca(engine, "EXP-B")
    with Session(engine) as s:
        s.add(
            MatchFeedback(
                usuario_id=usuario,
                fuente="compras_agiles",
                codigo_oportunidad="EXP-A",
                valor=ValorFeedback.DESCARTE.value,
            )
        )
        s.add(
            MatchFeedback(
                usuario_id=otro_usuario,
                fuente="compras_agiles",
                codigo_oportunidad="EXP-B",
                valor=ValorFeedback.DESCARTE.value,
            )
        )
        s.commit()
    assert _codigos(_pagina(client, settings, usuario).text) == {"EXP-B"}
    assert _codigos(_pagina(client, settings, otro_usuario).text) == {"EXP-A"}


def test_filtros_region_monto_cierre_y_organismo_combinados(client, engine, settings, usuario):
    ahora = ahora_utc()
    _ca(engine, "EXP-OK", region=13, monto_disponible_clp=2_000_000.0, fecha_cierre=ahora + timedelta(hours=30))
    _ca(engine, "EXP-OTRA-REGION", region=5, monto_disponible_clp=2_000_000.0)
    _ca(engine, "EXP-BARATA", region=13, monto_disponible_clp=10_000.0)
    _ca(engine, "EXP-LEJANA", region=13, monto_disponible_clp=2_000_000.0, fecha_cierre=ahora + timedelta(days=20))
    _ca(engine, "EXP-SINMONTO", region=13, monto_disponible_clp=None)
    html = _pagina(
        client, settings, usuario, region="13", monto_min="1.000.000", cierre="3d", organismo="org"
    ).text
    # "Sin monto" entra por defecto; el rango deja fuera a la barata y la lejana.
    assert _codigos(html) == {"EXP-OK", "EXP-SINMONTO"}
    # Sin la casilla (envío del panel), "sin monto" sale.
    html = _pagina(
        client, settings, usuario, panel="1", region="13", monto_min="1000000", cierre="3d"
    ).text
    assert _codigos(html) == {"EXP-OK"}


def test_organismo_con_comodines_se_trata_como_texto(client, engine, settings, usuario):
    _ca(engine, "EXP-PCT", organismo_nombre="MUNI 100% AL DIA")
    _ca(engine, "EXP-NUM", organismo_nombre="MUNI 1000 AL DIA")
    assert _codigos(_pagina(client, settings, usuario, organismo="100%").text) == {"EXP-PCT"}
    assert _codigos(_pagina(client, settings, usuario, organismo="1_0").text) == set()


def test_orden_por_monto_y_por_cierre(engine, usuario):
    ahora = ahora_utc()
    _ca(engine, "EXP-1", monto_disponible_clp=5.0, fecha_cierre=ahora + timedelta(days=3))
    _ca(engine, "EXP-2", monto_disponible_clp=50.0, fecha_cierre=ahora + timedelta(days=1))
    _ca(engine, "EXP-3", monto_disponible_clp=None, fecha_cierre=ahora + timedelta(days=2))
    with Session(engine) as s:
        por_cierre = buscar(s, FiltrosExplorador(usuario_id=usuario, orden="cierre")).items
        por_monto = buscar(s, FiltrosExplorador(usuario_id=usuario, orden="monto")).items
    assert [i["codigo"] for i in por_cierre] == ["EXP-2", "EXP-3", "EXP-1"]
    assert [i["codigo"] for i in por_monto] == ["EXP-2", "EXP-1", "EXP-3"]


# ---------------------------------------------------------------------------
# Paginación y conteo
# ---------------------------------------------------------------------------


def test_conteo_coincide_con_la_suma_de_paginas_y_pagina_fuera_de_rango_es_vacia(engine, usuario):
    with Session(engine) as s:
        for i in range(123):
            s.add(CompraAgil(codigo=f"P{i:03d}", estado="publicada", fecha_cierre=ahora_utc() + timedelta(days=1 + i % 5)))
        s.commit()
        filtros = FiltrosExplorador(usuario_id=usuario)
        total = contar(s, filtros)
        paginas = [buscar(s, filtros, pagina=n) for n in (1, 2, 3)]
        fuera = buscar(s, filtros, pagina=99)
        vista = buscar(s, filtros, pagina=0)
    assert total == 123
    assert [len(p.items) for p in paginas] == [50, 50, 23]
    assert all(p.total == 123 and p.total_paginas == 3 for p in paginas)
    codigos = [i["codigo"] for p in paginas for i in p.items]
    assert len(codigos) == len(set(codigos)) == 123
    assert fuera.items == [] and fuera.total == 123
    assert vista.pagina == 1 and len(vista.items) == 50


def test_ruta_pagina_fuera_de_rango_responde_200_con_estado_vacio(client, engine, settings, usuario):
    _ca(engine, "EXP-UNICA")
    html = _pagina(client, settings, usuario, pagina="40").text
    assert "EXP-UNICA" not in html


def test_estado_vacio_honesto_y_conteo_en_vivo(client, engine, settings, usuario):
    html = _pagina(client, settings, usuario).text
    assert "Sin CA vigentes con estos filtros." in html
    _ca(engine, "EXP-X")
    _ca(engine, "EXP-Y", region=5)
    cookies, _ = _sesion(settings, usuario)
    r = client.get("/compras-agiles/conteo", params={"sin_favoritos": "1"}, cookies=cookies)
    assert r.status_code == 200
    assert "<strong" in r.text and ">2<" in r.text
    r = client.get("/compras-agiles/conteo", params={"sin_favoritos": "1", "region": "5"}, cookies=cookies)
    assert ">1<" in r.text
    # Solo números: nunca filas.
    assert "EXP-Y" not in r.text


# ---------------------------------------------------------------------------
# Rubro confirmado (sin FTS) y favoritos
# ---------------------------------------------------------------------------


def _con_producto(engine, codigo: str, unspsc: str) -> None:
    with Session(engine) as s:
        s.add(CaProducto(ca_codigo=codigo, codigo_producto=unspsc, nombre="prod"))
        s.commit()


def test_confirmado_por_prefijo_de_producto_y_etiqueta(client, engine, settings, usuario):
    _ca(engine, "EXP-CONF")
    _ca(engine, "EXP-OTRO")
    _con_producto(engine, "EXP-CONF", "43211503")
    _con_producto(engine, "EXP-OTRO", "50101500")
    html = _pagina(client, settings, usuario, categorias_unspsc="4321").text
    assert _codigos(html) == {"EXP-CONF"}
    assert "Confirmado" in html
    # Un segmento (2 dígitos) también sirve, y "solo confirmados" no cambia el resultado.
    html = _pagina(client, settings, usuario, categorias_unspsc="43", solo_confirmados="1").text
    assert _codigos(html) == {"EXP-CONF"}


def test_favoritos_vienen_preseleccionados_al_entrar(client, engine, settings, usuario, otro_usuario):
    _ca(engine, "EXP-FAV")
    _ca(engine, "EXP-NOFAV")
    _con_producto(engine, "EXP-FAV", "43211503")
    with Session(engine) as s:
        agregar_favorito(s, usuario, "4321")
        agregar_favorito(s, otro_usuario, "5010")
        s.commit()
    cookies, _ = _sesion(settings, usuario)
    r = client.get("/compras-agiles", cookies=cookies)
    assert _codigos(r.text) == {"EXP-FAV"}
    assert 'value="4321"' in r.text and "checked" in r.text
    # Quitar todos los rubros no hace reaparecer los favoritos.
    r = client.get("/compras-agiles", params={"sin_favoritos": "1"}, cookies=cookies)
    assert _codigos(r.text) == {"EXP-FAV", "EXP-NOFAV"}


def test_prefijos_validos_descarta_basura_y_duplicados():
    assert prefijos_validos(["4321,43", "43", "1", "123456789", "abc", "4321 ", "'; DROP--"]) == ["4321", "43"]


def test_favoritos_agregar_quitar_y_unico_por_usuario(engine, usuario, otro_usuario):
    with Session(engine) as s:
        assert agregar_favorito(s, usuario, "4321") is True
        assert agregar_favorito(s, usuario, "4321") is False
        assert agregar_favorito(s, otro_usuario, "4321") is True
        s.commit()
        assert listar_favoritos(s, usuario) == ["4321"]
        assert quitar_favorito(s, usuario, "4321") is True
        assert quitar_favorito(s, usuario, "4321") is False
        s.commit()
        assert listar_favoritos(s, usuario) == []
        assert listar_favoritos(s, otro_usuario) == ["4321"]
        with pytest.raises(ValueError):
            agregar_favorito(s, usuario, "43x")


def test_rutas_de_favoritos_exigen_csrf(client, engine, settings, usuario):
    cookies, _ = _sesion(settings, usuario)
    r = client.post("/rubros-favoritos/agregar", data={"prefijo": "4321", "csrf_token": "malo"}, cookies=cookies)
    assert r.status_code == 403
    with Session(engine) as s:
        assert listar_favoritos(s, usuario) == []


def test_rutas_de_favoritos_solo_tocan_los_del_dueno(client, engine, settings, usuario, otro_usuario):
    cookies, headers = _sesion(settings, usuario)
    r = client.post(
        "/rubros-favoritos/agregar",
        data={"prefijo": "4321", "next": "/compras-agiles?sin_favoritos=1"},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/compras-agiles?sin_favoritos=1"
    with Session(engine) as s:
        agregar_favorito(s, otro_usuario, "5010")
        s.commit()
    # Quitar el rubro AJENO desde la cuenta del usuario no hace nada.
    client.post("/rubros-favoritos/quitar", data={"prefijo": "5010"}, cookies=cookies, headers=headers)
    with Session(engine) as s:
        assert listar_favoritos(s, otro_usuario) == ["5010"]
        assert listar_favoritos(s, usuario) == ["4321"]
    # Un prefijo inválido es 400, no un error de base de datos.
    r = client.post("/rubros-favoritos/agregar", data={"prefijo": "no-es"}, cookies=cookies, headers=headers)
    assert r.status_code == 400
    # No se sigue un "next" externo.
    r = client.post(
        "/rubros-favoritos/quitar",
        data={"prefijo": "4321", "next": "https://malo.example/"},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.headers["location"] == "/perfiles"


def test_perfiles_muestra_los_favoritos_solo_del_usuario(client, engine, settings, usuario, otro_usuario):
    with Session(engine) as s:
        agregar_favorito(s, usuario, "4321")
        agregar_favorito(s, otro_usuario, "5010")
        s.commit()
    cookies, _ = _sesion(settings, usuario)
    # Catálogo de organismos "fresco": /perfiles no sale a la red a sincronizarlo.
    ahora = ahora_utc()
    with Session(engine) as s:
        for fuente in ("plan_compra_instituciones", "plan_compra_sectores"):
            s.add(SyncState(fuente=fuente, ultima_ejecucion=ahora, ultimo_ok=ahora))
        s.commit()
    with respx.mock:
        r = client.get("/perfiles", cookies=cookies)
    assert r.status_code == 200
    assert "Rubros favoritos" in r.text
    assert "★</span> 4321" in r.text
    insignias = r.text.split('id="rubros-favoritos"', 1)[1].split("</ul>", 1)[0]
    assert "4321" in insignias and "5010" not in insignias


def test_el_modelo_declara_cascada_y_claves(engine, usuario):
    # SQLite no aplica ON DELETE CASCADE sin PRAGMA; el contrato está en la migración
    # y en el modelo. Acá basta con que el modelo lo declare.
    fk = next(iter(RubroFavorito.__table__.c.owner_id.foreign_keys))
    assert fk.ondelete == "CASCADE"
    assert {c.name for c in RubroFavorito.__table__.primary_key.columns} == {"id"}
    assert {c.name for c in RubroVocabulario.__table__.primary_key.columns} == {"prefijo", "lexema"}


# ---------------------------------------------------------------------------
# Ficha sin match y ninguna llamada a la red
# ---------------------------------------------------------------------------


def test_ficha_de_ca_sin_match_se_puede_abrir_sin_acciones(client, engine, settings, usuario):
    _ca(engine, "EXP-SINMATCH")
    cookies, _ = _sesion(settings, usuario)
    r = client.get(
        "/oportunidad/compras_agiles/EXP-SINMATCH",
        cookies=cookies,
        headers={"referer": "http://testserver/compras-agiles?region=13&sin_favoritos=1"},
    )
    assert r.status_code == 200
    assert "Compra EXP-SINMATCH" in r.text
    assert "no coincide con ninguno de tus perfiles" in r.text
    assert "/seguir" not in r.text and "/descartar" not in r.text
    assert 'href="/compras-agiles?region=13&amp;sin_favoritos=1"' in r.text
    # Las licitaciones siguen exigiendo match (regla 17).
    assert client.get("/oportunidad/licitaciones/NO-ES-MIA", cookies=cookies).status_code == 404


def test_las_rutas_del_explorador_no_llaman_a_la_red(client, engine, settings, usuario):
    _ca(engine, "EXP-RED")
    cookies, headers = _sesion(settings, usuario)
    # respx sin rutas: cualquier request saliente de httpx hace fallar el test.
    with respx.mock(assert_all_called=False) as router:
        assert client.get("/compras-agiles", cookies=cookies).status_code == 200
        assert client.get("/compras-agiles/conteo", cookies=cookies).status_code == 200
        client.post("/rubros-favoritos/agregar", data={"prefijo": "4321"}, cookies=cookies, headers=headers)
        assert client.get("/oportunidad/compras_agiles/EXP-RED", cookies=cookies).status_code == 200
        assert not router.calls


def test_explorador_exige_sesion(client):
    r = client.get("/compras-agiles", follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401)
    assert "EXP" not in r.text
