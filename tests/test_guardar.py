"""Tests F-guardar sobre SQLite: Guardar = seguir (también para CA sin match),
acceso con `puede_actuar`, exclusión mutua guardar/descartar, alias deprecated,
botones del explorador, señales B.4 (rezagadas y detalles-match) y retención.

Lo que necesita Postgres (limpieza de matches con FTS, vista previa, migración,
Texto del explorador en productos) está en tests/test_guardar_pg.py.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.retencion import purgar_terminales
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.ingest.lifecycle import _rezagadas
from app.ingest.orchestrator import _cola_detalles_match
from app.matching.feedback import obtener_feedback
from app.matching.perfiles import PerfilInvalido, normalizar_palabras_excluir, palabras_sugeridas
from app.matching.seguimiento import esta_guardada, obtener_seguimiento
from app.models.base import Base
from app.models.enums import RolUsuario, ValorFeedback
from app.models.tables import (
    CaProducto,
    CompraAgil,
    Licitacion,
    LicitacionItem,
    MatchFeedback,
    OportunidadMatch,
    OportunidadSeguida,
    PerfilBusqueda,
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
def otro(engine):
    return _usuario(engine, "otro@test.cl")


def _sesion(settings: Settings, user_id: int) -> tuple[dict[str, str], dict[str, str]]:
    token = create_session_token(settings.secret_key, user_id)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    _, nonce = decoded
    return {COOKIE_NAME: token}, {"X-CSRF-Token": generate_csrf_token(settings.secret_key, nonce)}


def _lic(engine, codigo: str, **campos: Any) -> None:
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": f"Licitación {codigo}",
        "descripcion": "",
        "estado": "publicada",
        "fecha_cierre": ahora_utc() + timedelta(days=5),
    }
    base.update(campos)
    with Session(engine) as s:
        s.add(Licitacion(**base))
        s.commit()


def _ca(engine, codigo: str, **campos: Any) -> None:
    ahora = ahora_utc()
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": f"Compra {codigo}",
        "estado": "publicada",
        "fecha_publicacion": ahora - timedelta(days=1),
        "fecha_cierre": ahora + timedelta(days=2),
        "monto_disponible_clp": 1_000_000.0,
        "organismo_nombre": "ORG GUARDAR",
        "region": 13,
        "total_ofertas": 0,
    }
    base.update(campos)
    with Session(engine) as s:
        s.add(CompraAgil(**base))
        s.commit()


def _match(engine, owner_id: int, fuente: str, codigo: str, razones: dict | None = None) -> int:
    with Session(engine) as s:
        perfil = PerfilBusqueda(
            owner_id=owner_id,
            nombre="Perfil salud",
            keywords=["salud"],
            keywords_excluir=[],
            regiones=[],
            fuentes=["licitaciones", "compras_agiles"],
            activo=True,
        )
        s.add(perfil)
        s.flush()
        m = OportunidadMatch(
            perfil_id=perfil.id,
            fuente=fuente,
            codigo_oportunidad=codigo,
            score=70,
            razones=razones or {"keywords_hit": ["salud"]},
        )
        s.add(m)
        s.commit()
        return m.id


def _post(client, url: str, cookies, headers, **data: str):
    return client.post(url, data=data, cookies=cookies, headers=headers, follow_redirects=False)


# ---------------------------------------------------------------------------
# Acceso (puede_actuar) y ownership
# ---------------------------------------------------------------------------


def test_usuario_b_no_guarda_ni_descarta_la_licitacion_de_a(client, engine, settings, usuario, otro):
    _lic(engine, "LIC-A")
    _match(engine, usuario, "licitaciones", "LIC-A")
    cookies_b, headers_b = _sesion(settings, otro)
    for ruta in ("guardar", "descartar", "me-sirve", "seguir"):
        r = _post(client, f"/oportunidad/licitaciones/LIC-A/{ruta}", cookies_b, headers_b)
        assert r.status_code == 404, ruta
    with Session(engine) as s:
        assert obtener_seguimiento(s, otro, "licitaciones", "LIC-A") is None
        assert obtener_feedback(s, otro, "licitaciones", "LIC-A") is None


def test_guardar_de_b_no_toca_lo_guardado_por_a(client, engine, settings, usuario, otro):
    _ca(engine, "CA-COMPARTIDA")
    cookies_a, headers_a = _sesion(settings, usuario)
    cookies_b, headers_b = _sesion(settings, otro)
    _post(client, "/oportunidad/compras_agiles/CA-COMPARTIDA/guardar", cookies_a, headers_a)
    # B guarda y luego quita: solo su propio registro.
    _post(client, "/oportunidad/compras_agiles/CA-COMPARTIDA/guardar", cookies_b, headers_b)
    _post(client, "/oportunidad/compras_agiles/CA-COMPARTIDA/guardar", cookies_b, headers_b)
    r = _post(client, "/oportunidad/compras_agiles/CA-COMPARTIDA/dejar-de-seguir", cookies_b, headers_b)
    assert r.status_code == 404  # B ya no la tenía
    with Session(engine) as s:
        assert esta_guardada(s, usuario, "compras_agiles", "CA-COMPARTIDA")
        assert not esta_guardada(s, otro, "compras_agiles", "CA-COMPARTIDA")


def test_ca_sin_match_se_puede_guardar_y_descartar(client, engine, settings, usuario):
    _ca(engine, "CA-SINMATCH")
    _ca(engine, "CA-SINMATCH-2")
    cookies, headers = _sesion(settings, usuario)
    r = _post(client, "/oportunidad/compras_agiles/CA-SINMATCH/guardar", cookies, headers)
    assert r.status_code == 303
    r = _post(client, "/oportunidad/compras_agiles/CA-SINMATCH-2/descartar", cookies, headers)
    assert r.status_code == 303
    with Session(engine) as s:
        seg = obtener_seguimiento(s, usuario, "compras_agiles", "CA-SINMATCH")
        assert seg is not None and not seg.archivada
        assert seg.estado_visto == "publicada"
        fb = obtener_feedback(s, usuario, "compras_agiles", "CA-SINMATCH-2")
        assert fb is not None and fb.valor == ValorFeedback.DESCARTE.value


def test_licitacion_sin_match_ni_guardada_404(client, engine, settings, usuario):
    _lic(engine, "LIC-SUELTA")
    cookies, headers = _sesion(settings, usuario)
    assert client.get("/oportunidad/licitaciones/LIC-SUELTA", cookies=cookies).status_code == 404
    assert _post(client, "/oportunidad/licitaciones/LIC-SUELTA/guardar", cookies, headers).status_code == 404
    assert _post(client, "/oportunidad/licitaciones/LIC-SUELTA/descartar", cookies, headers).status_code == 404


def test_licitacion_guardada_cuyo_match_se_borro_abre_sin_razones(client, engine, settings, usuario):
    _lic(engine, "LIC-GUARD")
    match_id = _match(engine, usuario, "licitaciones", "LIC-GUARD", {"keywords_hit": ["salud"]})
    cookies, headers = _sesion(settings, usuario)
    assert _post(client, "/oportunidad/licitaciones/LIC-GUARD/guardar", cookies, headers).status_code == 303
    with Session(engine) as s:
        s.execute(delete(OportunidadMatch).where(OportunidadMatch.id == match_id))
        s.commit()

    r = client.get("/oportunidad/licitaciones/LIC-GUARD", cookies=cookies)
    assert r.status_code == 200
    assert 'data-accion="guardar" aria-pressed="true"' in r.text
    assert "visually-hidden\"> — alta relevancia" not in r.text
    assert "fw-normal\">Palabras" not in r.text
    # Sin match no hay modal: Descartar es directo.
    assert "descartar-opciones" not in r.text
    # Y se puede quitar de guardadas aunque ya no haya match.
    r = _post(client, "/oportunidad/licitaciones/LIC-GUARD/guardar", cookies, headers)
    assert r.status_code == 303
    with Session(engine) as s:
        assert obtener_seguimiento(s, usuario, "licitaciones", "LIC-GUARD") is None


def test_fuente_invalida_404(client, engine, settings, usuario):
    _ca(engine, "CA-FUENTE")
    cookies, headers = _sesion(settings, usuario)
    assert client.get("/oportunidad/ordenes/CA-FUENTE", cookies=cookies).status_code == 404
    for ruta in ("guardar", "descartar", "me-sirve", "seguir"):
        r = _post(client, f"/oportunidad/ordenes/CA-FUENTE/{ruta}", cookies, headers)
        assert r.status_code == 404, ruta
    with Session(engine) as s:
        assert list(s.execute(select(OportunidadSeguida)).scalars()) == []


@pytest.mark.parametrize(
    "ruta",
    [
        "/oportunidad/compras_agiles/CA-CSRF/guardar",
        "/oportunidad/compras_agiles/CA-CSRF/me-sirve",
        "/oportunidad/compras_agiles/CA-CSRF/seguir",
        "/oportunidad/compras_agiles/CA-CSRF/dejar-de-seguir",
        "/oportunidad/compras_agiles/CA-CSRF/descartar",
        "/oportunidad/compras_agiles/CA-CSRF/descartar-y-excluir",
        "/perfiles/1/deshacer-exclusion",
    ],
)
def test_csrf_en_rutas_nuevas_y_alias(client, engine, settings, usuario, ruta):
    _ca(engine, "CA-CSRF")
    cookies, _ = _sesion(settings, usuario)
    r = client.post(ruta, data={"csrf_token": "invalido"}, cookies=cookies, follow_redirects=False)
    assert r.status_code == 403
    with Session(engine) as s:
        assert list(s.execute(select(OportunidadSeguida)).scalars()) == []
        assert list(s.execute(select(MatchFeedback)).scalars()) == []


# ---------------------------------------------------------------------------
# Exclusión mutua y alias
# ---------------------------------------------------------------------------


def test_guardar_una_descartada_borra_el_descarte(client, engine, settings, usuario):
    _ca(engine, "CA-MUTUA")
    cookies, headers = _sesion(settings, usuario)
    _post(client, "/oportunidad/compras_agiles/CA-MUTUA/descartar", cookies, headers)
    _post(client, "/oportunidad/compras_agiles/CA-MUTUA/guardar", cookies, headers)
    with Session(engine) as s:
        assert esta_guardada(s, usuario, "compras_agiles", "CA-MUTUA")
        assert obtener_feedback(s, usuario, "compras_agiles", "CA-MUTUA") is None


def test_descartar_una_guardada_la_quita_de_guardadas(client, engine, settings, usuario):
    _ca(engine, "CA-MUTUA2")
    cookies, headers = _sesion(settings, usuario)
    _post(client, "/oportunidad/compras_agiles/CA-MUTUA2/guardar", cookies, headers)
    _post(client, "/oportunidad/compras_agiles/CA-MUTUA2/descartar", cookies, headers)
    with Session(engine) as s:
        # Se quita (dejar_de_seguir), no se archiva.
        assert obtener_seguimiento(s, usuario, "compras_agiles", "CA-MUTUA2") is None
        fb = obtener_feedback(s, usuario, "compras_agiles", "CA-MUTUA2")
        assert fb is not None and fb.valor == ValorFeedback.DESCARTE.value


def test_alias_hacen_lo_mismo_que_guardar(client, engine, settings, usuario):
    for codigo in ("CA-AL-1", "CA-AL-2"):
        _ca(engine, codigo)
    cookies, headers = _sesion(settings, usuario)
    # /guardar y /me-sirve: el mismo toggle.
    _post(client, "/oportunidad/compras_agiles/CA-AL-1/guardar", cookies, headers)
    _post(client, "/oportunidad/compras_agiles/CA-AL-2/me-sirve", cookies, headers)
    with Session(engine) as s:
        assert esta_guardada(s, usuario, "compras_agiles", "CA-AL-1")
        assert esta_guardada(s, usuario, "compras_agiles", "CA-AL-2")
    _post(client, "/oportunidad/compras_agiles/CA-AL-1/guardar", cookies, headers)
    _post(client, "/oportunidad/compras_agiles/CA-AL-2/me-sirve", cookies, headers)
    with Session(engine) as s:
        assert not esta_guardada(s, usuario, "compras_agiles", "CA-AL-1")
        assert not esta_guardada(s, usuario, "compras_agiles", "CA-AL-2")
    # /seguir guarda (idempotente) y /dejar-de-seguir quita.
    for _ in range(2):
        _post(client, "/oportunidad/compras_agiles/CA-AL-1/seguir", cookies, headers)
    with Session(engine) as s:
        assert esta_guardada(s, usuario, "compras_agiles", "CA-AL-1")
    _post(client, "/oportunidad/compras_agiles/CA-AL-1/dejar-de-seguir", cookies, headers)
    with Session(engine) as s:
        assert obtener_seguimiento(s, usuario, "compras_agiles", "CA-AL-1") is None
    # Ninguno escribe "sirve".
    with Session(engine) as s:
        valores = {f.valor for f in s.execute(select(MatchFeedback)).scalars()}
        assert ValorFeedback.SIRVE.value not in valores


def test_guardar_una_archivada_la_reactiva(client, engine, settings, usuario):
    _ca(engine, "CA-ARCH")
    cookies, headers = _sesion(settings, usuario)
    _post(client, "/oportunidad/compras_agiles/CA-ARCH/guardar", cookies, headers)
    _post(client, "/oportunidad/compras_agiles/CA-ARCH/archivar", cookies, headers)
    _post(client, "/oportunidad/compras_agiles/CA-ARCH/guardar", cookies, headers)
    with Session(engine) as s:
        seg = obtener_seguimiento(s, usuario, "compras_agiles", "CA-ARCH")
        assert seg is not None and not seg.archivada


# ---------------------------------------------------------------------------
# Tarjeta, ficha y modal
# ---------------------------------------------------------------------------


def test_tarjeta_guardada_lleva_chip_con_texto(client, engine, settings, usuario):
    _lic(engine, "LIC-CHIP")
    _match(engine, usuario, "licitaciones", "LIC-CHIP")
    cookies, headers = _sesion(settings, usuario)
    r = _post(
        client, "/oportunidad/licitaciones/LIC-CHIP/guardar", cookies, {**headers, "HX-Request": "true"}
    )
    assert r.status_code == 200
    assert 'data-accion="guardar" aria-pressed="true"' in r.text
    assert "Guardada: Licitación LIC-CHIP" in r.text  # data-anuncio
    html = client.get("/", cookies=cookies).text
    # (El changelog del modal de novedades sí nombra los botones viejos.)
    assert "Me sirve</button>" not in html and "Activar alertas</button>" not in html
    assert ">Mi registro</a>" in html


def test_modal_descartar_muestra_que_la_trajo(client, engine, settings, usuario, otro):
    _lic(engine, "LIC-MODAL", nombre="Servicio de desratización hospital")
    _match(engine, usuario, "licitaciones", "LIC-MODAL", {"keywords_hit": ["salud"]})
    cookies, _ = _sesion(settings, usuario)
    r = client.get("/oportunidad/licitaciones/LIC-MODAL/descartar-opciones", cookies=cookies)
    assert r.status_code == 200
    assert "Llegó por <em>salud</em> en el perfil <em>Perfil salud</em>" in r.text
    assert 'value="desratización"' in r.text
    assert "descartar-y-excluir" in r.text
    # Otro usuario: 404 (regla 17).
    cookies_b, _ = _sesion(settings, otro)
    assert client.get("/oportunidad/licitaciones/LIC-MODAL/descartar-opciones", cookies=cookies_b).status_code == 404


def test_modal_descartar_404_sin_match(client, engine, settings, usuario):
    _ca(engine, "CA-NOMODAL")
    cookies, _ = _sesion(settings, usuario)
    r = client.get("/oportunidad/compras_agiles/CA-NOMODAL/descartar-opciones", cookies=cookies)
    assert r.status_code == 404


def test_vista_previa_con_perfil_ajeno_404(client, engine, settings, usuario, otro):
    _lic(engine, "LIC-VP")
    _match(engine, usuario, "licitaciones", "LIC-VP")
    with Session(engine) as s:
        pid = s.execute(select(PerfilBusqueda.id).where(PerfilBusqueda.owner_id == usuario)).scalar_one()
    cookies_b, _ = _sesion(settings, otro)
    r = client.get(
        f"/oportunidad/licitaciones/LIC-VP/excluir-vista-previa?perfil_id={pid}&palabras=hospital",
        cookies=cookies_b,
    )
    assert r.status_code == 404


def test_descartar_y_excluir_con_perfil_ajeno_404(client, engine, settings, usuario, otro):
    _ca(engine, "CA-EXC")
    _match(engine, usuario, "compras_agiles", "CA-EXC")
    with Session(engine) as s:
        pid = s.execute(select(PerfilBusqueda.id).where(PerfilBusqueda.owner_id == usuario)).scalar_one()
    cookies_b, headers_b = _sesion(settings, otro)
    r = client.post(
        "/oportunidad/compras_agiles/CA-EXC/descartar-y-excluir",
        data={"perfil_id": str(pid), "palabras": "hospital"},
        cookies=cookies_b,
        headers=headers_b,
        follow_redirects=False,
    )
    assert r.status_code == 404
    with Session(engine) as s:
        perfil = s.get(PerfilBusqueda, pid)
        assert perfil is not None and list(perfil.keywords_excluir or []) == []
        assert obtener_feedback(s, otro, "compras_agiles", "CA-EXC") is None


def test_palabras_sugeridas_sin_stopwords_ni_keywords():
    assert palabras_sugeridas(
        "Adquisición de servicio de desratización para Hospital de Salud", ["salud"]
    ) == ["desratización", "hospital"]


def test_normalizar_palabras_excluir_tope_y_duplicados():
    assert normalizar_palabras_excluir(["Hospital", "hospital, aseo ", "x"]) == ["Hospital", "aseo"]
    with pytest.raises(PerfilInvalido):
        normalizar_palabras_excluir(["uno, dos, tres, cuatro, cinco, seis"])


# ---------------------------------------------------------------------------
# Explorador de Compras Ágiles
# ---------------------------------------------------------------------------


def test_explorador_botones_con_aria_pressed(client, engine, settings, usuario):
    _ca(engine, "CA-EXP-1")
    _ca(engine, "CA-EXP-2")
    cookies, headers = _sesion(settings, usuario)
    _post(client, "/oportunidad/compras_agiles/CA-EXP-1/guardar", cookies, headers)
    html = client.get("/compras-agiles?sin_favoritos=1", cookies=cookies).text
    assert 'id="acciones-ca-CA-EXP-1"' in html and 'id="acciones-ca-CA-EXP-2"' in html
    bloque_1 = html.split('id="acciones-ca-CA-EXP-1"')[1].split("</div>")[0]
    bloque_2 = html.split('id="acciones-ca-CA-EXP-2"')[1].split("</div>")[0]
    assert 'data-accion="guardar" aria-pressed="true"' in bloque_1
    assert 'data-accion="guardar" aria-pressed="false"' in bloque_2


def test_explorador_guardar_htmx_repinta_solo_los_botones(client, engine, settings, usuario):
    _ca(engine, "CA-EXP-3")
    cookies, headers = _sesion(settings, usuario)
    r = client.post(
        "/oportunidad/compras_agiles/CA-EXP-3/guardar",
        data={"origen": "explorador"},
        cookies=cookies,
        headers={**headers, "HX-Request": "true"},
    )
    assert r.status_code == 200
    assert 'id="acciones-ca-CA-EXP-3"' in r.text
    assert 'aria-pressed="true"' in r.text
    assert "Guardada: Compra CA-EXP-3" in r.text


def test_explorador_descartar_saca_la_fila(client, engine, settings, usuario):
    _ca(engine, "CA-EXP-4")
    _ca(engine, "CA-EXP-5")
    cookies, headers = _sesion(settings, usuario)
    r = client.post(
        "/oportunidad/compras_agiles/CA-EXP-4/descartar",
        data={"origen": "explorador"},
        cookies=cookies,
        headers={**headers, "HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "hidden" in r.text and "Descartada: Compra CA-EXP-4" in r.text
    assert 'id="fila-ca-' not in r.text
    html = client.get("/compras-agiles?sin_favoritos=1", cookies=cookies).text
    assert "CA-EXP-4" not in html
    assert "CA-EXP-5" in html


# ---------------------------------------------------------------------------
# B.4: rezagadas y detalles-match ven las guardadas sin match
# ---------------------------------------------------------------------------


def test_licitacion_guardada_sin_match_entra_a_rezagadas_y_detalles(engine, usuario):
    ahora = ahora_utc()
    _lic(engine, "LIC-VIEJA", fecha_cierre=ahora - timedelta(days=10), estado="cerrada")
    _lic(engine, "LIC-NUEVA", fecha_cierre=ahora + timedelta(days=3))
    _lic(engine, "LIC-NADIE", fecha_cierre=ahora - timedelta(days=10), estado="cerrada")
    _ca(engine, "CA-GUARD-DET")
    with Session(engine) as s:
        for fuente, codigo in [
            ("licitaciones", "LIC-VIEJA"),
            ("licitaciones", "LIC-NUEVA"),
            ("compras_agiles", "CA-GUARD-DET"),
        ]:
            s.add(
                OportunidadSeguida(
                    owner_id=usuario, fuente=fuente, codigo_oportunidad=codigo, estado_visto="publicada"
                )
            )
        s.commit()

        assert _rezagadas(s, set()) == ["LIC-VIEJA"]
        cola, _ = _cola_detalles_match(s, ahora, fallos_max=3, espera_horas=3)
        assert ("licitaciones", "LIC-NUEVA") in cola
        assert ("compras_agiles", "CA-GUARD-DET") in cola

        # Archivada: ya no cuenta como guardada.
        for seg in s.execute(select(OportunidadSeguida)).scalars():
            seg.archivada = True
        s.commit()
        assert _rezagadas(s, set()) == []
        cola, _ = _cola_detalles_match(s, ahora, fallos_max=3, espera_horas=3)
        assert cola == []


# ---------------------------------------------------------------------------
# G. Retención
# ---------------------------------------------------------------------------


def test_retencion_protege_guardadas_y_archivadas(engine, usuario):
    viejo = ahora_utc() - timedelta(days=200)
    for codigo in ("LIC-RET-G", "LIC-RET-A", "LIC-RET-X"):
        _lic(engine, codigo, estado="adjudicada", raw_json={"x": 1}, fecha_cierre=viejo)
    for codigo in ("CA-RET-G", "CA-RET-X"):
        _ca(engine, codigo, estado="cancelada", raw_json={"x": 1}, fecha_cierre=viejo)
    with Session(engine) as s:
        for codigo in ("LIC-RET-G", "LIC-RET-A", "LIC-RET-X"):
            s.add(LicitacionItem(licitacion_codigo=codigo, codigo_producto="1", nombre="i"))
        for codigo in ("CA-RET-G", "CA-RET-X"):
            s.add(CaProducto(ca_codigo=codigo, codigo_producto="1", nombre="p", descripcion=""))
        s.add(OportunidadSeguida(owner_id=usuario, fuente="licitaciones", codigo_oportunidad="LIC-RET-G"))
        s.add(
            OportunidadSeguida(
                owner_id=usuario, fuente="licitaciones", codigo_oportunidad="LIC-RET-A", archivada=True
            )
        )
        s.add(OportunidadSeguida(owner_id=usuario, fuente="compras_agiles", codigo_oportunidad="CA-RET-G"))
        s.commit()
        # actualizado_en lo pone el ORM al crear: se envejece a mano.
        for modelo in (Licitacion, CompraAgil):
            for op in s.execute(select(modelo)).scalars():
                op.actualizado_en = viejo
        s.commit()

        purgar_terminales(s)
        s.commit()

        assert s.get(Licitacion, "LIC-RET-G").raw_json is not None  # type: ignore[union-attr]
        assert s.get(Licitacion, "LIC-RET-A").raw_json is not None  # type: ignore[union-attr]
        assert s.get(Licitacion, "LIC-RET-X").raw_json is None  # type: ignore[union-attr]
        assert s.get(CompraAgil, "CA-RET-G").raw_json is not None  # type: ignore[union-attr]
        assert s.get(CompraAgil, "CA-RET-X").raw_json is None  # type: ignore[union-attr]
        items = {i.licitacion_codigo for i in s.execute(select(LicitacionItem)).scalars()}
        assert items == {"LIC-RET-G", "LIC-RET-A"}
        prods = {p.ca_codigo for p in s.execute(select(CaProducto)).scalars()}
        assert prods == {"CA-RET-G"}
