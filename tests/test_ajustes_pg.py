"""Tests F-ajustes (Postgres): una sola vigencia en el matching y en detalles-match,
orden del tope de candidatos, "Deshacer exclusión" en segundo plano y exclusiones
que chocan con las keywords del perfil.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.db import normalizar_url_driver
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.core.vigencia import condicion_lic_vigente, es_vigente
from app.ingest.orchestrator import _candidatas_detalles_match
from app.matching import engine as eng
from app.matching.engine import exclusiones_que_chocan, limpiar_matches_perfil
from app.matching.perfiles import (
    PerfilInvalido,
    actualizar_perfil,
    crear_perfil,
    excluir_palabras,
    palabras_sugeridas,
)
from app.matching.text import build_tsquery
from app.models.enums import RolUsuario
from app.models.tables import (
    CompraAgil,
    Licitacion,
    MatchFeedback,
    OportunidadMatch,
    PerfilBusqueda,
    Usuario,
)

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_POSTGRES = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")
pytestmark = pytest.mark.skipif(
    not _TIENE_POSTGRES,
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)

_EMAIL = "ajustes-a@test.cl"
_EMAIL_B = "ajustes-b@test.cl"
_PW = "contraseña-segura-test"


@pytest.fixture(scope="module")
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    yield e
    e.dispose()


def _limpiar(engine) -> None:
    with Session(engine) as s:
        s.execute(delete(Usuario).where(Usuario.email.in_([_EMAIL, _EMAIL_B])))
        s.execute(delete(Licitacion).where(Licitacion.codigo.like("AJUS-%")))
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like("AJUS-%")))
        s.commit()


@pytest.fixture()
def limpio(pg_engine):
    _limpiar(pg_engine)
    yield pg_engine
    _limpiar(pg_engine)


@pytest.fixture()
def settings():
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url=_DB_URL,
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
    )


def _usuario(s: Session, email: str = _EMAIL) -> int:
    u = Usuario(email=email, password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
    s.add(u)
    s.flush()
    return u.id


def _perfil(s: Session, owner_id: int, **campos: Any) -> PerfilBusqueda:
    base: dict[str, Any] = {
        "owner_id": owner_id,
        "nombre": "Perfil ajustes",
        "keywords": [],
        "keywords_excluir": [],
        "regiones": [],
        "categorias_unspsc": [],
        "organismos_seguidos": [],
        "fuentes": ["licitaciones", "compras_agiles"],
        "activo": True,
    }
    base.update(campos)
    p = PerfilBusqueda(**base)
    s.add(p)
    s.flush()
    return p


def _ca(s: Session, codigo: str, nombre: str, **campos: Any) -> None:
    ahora = ahora_utc()
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": nombre,
        "descripcion": "",
        "estado": "publicada",
        "region": 13,
        "total_ofertas": 0,
        "monto_disponible_clp": 500_000.0,
        "organismo_nombre": "AJUS ORGANISMO",
        "fecha_publicacion": ahora - timedelta(days=1),
        "fecha_cierre": ahora + timedelta(days=10),
    }
    base.update(campos)
    s.add(CompraAgil(**base))


def _match(s: Session, perfil_id: int, fuente: str, codigo: str) -> None:
    s.add(
        OportunidadMatch(
            perfil_id=perfil_id, fuente=fuente, codigo_oportunidad=codigo, score=50, razones={}
        )
    )
    s.flush()


def _codigos_match(s: Session, perfil_id: int) -> set[str]:
    return set(
        s.execute(
            select(OportunidadMatch.codigo_oportunidad).where(OportunidadMatch.perfil_id == perfil_id)
        ).scalars()
    )


def _sesion(settings: Settings, user_id: int) -> tuple[dict[str, str], dict[str, str]]:
    token = create_session_token(settings.secret_key, user_id)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    _, nonce = decoded
    return {COOKIE_NAME: token}, {"X-CSRF-Token": generate_csrf_token(settings.secret_key, nonce)}


# ---------------------------------------------------------------------------
# 1. Una sola vigencia
# ---------------------------------------------------------------------------


def test_condicion_lic_vigente_coincide_con_es_vigente(limpio):
    ahora = ahora_utc()
    estados = ["publicada", "PUBLICADA ", "desconocido", "estado-raro", "cerrada", "adjudicada", "suspendida"]
    cierres = [ahora + timedelta(days=1), ahora - timedelta(days=1), None]
    esperado: dict[str, bool] = {}
    with Session(limpio) as s:
        n = 0
        for estado in estados:
            for cierre in cierres:
                n += 1
                codigo = f"AJUS-V{n:03d}"
                s.add(Licitacion(codigo=codigo, nombre=codigo, estado=estado, fecha_cierre=cierre))
                esperado[codigo] = es_vigente(estado, cierre, "licitaciones", ahora)
        s.commit()
        en_sql = set(
            s.execute(
                select(Licitacion.codigo).where(
                    Licitacion.codigo.like("AJUS-V%"), condicion_lic_vigente(ahora)
                )
            ).scalars()
        )
    assert en_sql == {c for c, v in esperado.items() if v}
    assert any(esperado.values()) and not all(esperado.values())


def test_candidatos_ca_excluye_sin_cierre_antigua(limpio):
    ahora = ahora_utc()
    with Session(limpio) as s:
        _ca(s, "AJUS-C-VIEJA", "zzajusaseo vieja", fecha_cierre=None, fecha_publicacion=ahora - timedelta(days=10))
        _ca(s, "AJUS-C-NUEVA", "zzajusaseo nueva", fecha_cierre=None, fecha_publicacion=ahora - timedelta(days=2))
        _ca(s, "AJUS-C-CIERRE", "zzajusaseo con cierre")
        s.commit()
        q = build_tsquery(["zzajusaseo"])
        codigos = {c.codigo for c in eng._candidatos_ca(s, ahora, q, None)}
    assert codigos == {"AJUS-C-NUEVA", "AJUS-C-CIERRE"}


def test_limpieza_no_borra_match_de_ca_fuera_de_alcance(limpio):
    ahora = ahora_utc()
    with Session(limpio) as s:
        uid = _usuario(s)
        p = _perfil(s, uid, keywords=["zzajusotra"], fuentes=["compras_agiles"])
        _ca(s, "AJUS-L-VIEJA", "otra cosa", fecha_cierre=None, fecha_publicacion=ahora - timedelta(days=10))
        _ca(s, "AJUS-L-NUEVA", "otra cosa", fecha_cierre=None, fecha_publicacion=ahora - timedelta(days=2))
        _match(s, p.id, "compras_agiles", "AJUS-L-VIEJA")
        _match(s, p.id, "compras_agiles", "AJUS-L-NUEVA")
        s.commit()
        borrados = limpiar_matches_perfil(s, p, ahora)
        s.commit()
        assert borrados == 1
        assert _codigos_match(s, p.id) == {"AJUS-L-VIEJA"}


def test_detalles_match_deja_fuera_ca_sin_cierre_antigua(limpio):
    ahora = ahora_utc()
    with Session(limpio) as s:
        uid = _usuario(s)
        p = _perfil(s, uid, keywords=["zzajusaseo"])
        _ca(s, "AJUS-D-VIEJA", "x", fecha_cierre=None, fecha_publicacion=ahora - timedelta(days=10))
        _ca(s, "AJUS-D-NUEVA", "x", fecha_cierre=None, fecha_publicacion=ahora - timedelta(days=2))
        s.add(Licitacion(codigo="AJUS-D-LIC", nombre="x", estado="publicada", fecha_cierre=ahora + timedelta(days=3)))
        s.flush()
        for fuente, codigo in [
            ("compras_agiles", "AJUS-D-VIEJA"),
            ("compras_agiles", "AJUS-D-NUEVA"),
            ("licitaciones", "AJUS-D-LIC"),
        ]:
            _match(s, p.id, fuente, codigo)
        s.commit()
        candidatas = {c for _, c, _, _ in _candidatas_detalles_match(s, ahora) if c.startswith("AJUS-")}
    assert candidatas == {"AJUS-D-NUEVA", "AJUS-D-LIC"}


def test_tope_de_candidatos_deja_las_de_cierre_mas_proximo(limpio):
    ahora = ahora_utc()
    with Session(limpio) as s:
        _ca(s, "AJUS-O-3", "zzajusorden tres", fecha_cierre=ahora + timedelta(days=3))
        _ca(s, "AJUS-O-1", "zzajusorden uno", fecha_cierre=ahora + timedelta(days=1))
        _ca(s, "AJUS-O-2", "zzajusorden dos", fecha_cierre=ahora + timedelta(days=2))
        s.commit()
        q = build_tsquery(["zzajusorden"])
        with patch.object(eng, "_MAX_CANDIDATOS", 2):
            codigos = [c.codigo for c in eng._candidatos_ca(s, ahora, q, None)]
    assert codigos == ["AJUS-O-1", "AJUS-O-2"]


# ---------------------------------------------------------------------------
# 2. Deshacer exclusión en segundo plano
# ---------------------------------------------------------------------------


def test_deshacer_exclusion_agenda_el_matching_en_segundo_plano(limpio, settings):
    with Session(limpio) as s:
        uid = _usuario(s)
        otro = _usuario(s, _EMAIL_B)
        p = _perfil(s, uid, keywords=["zzajusaseo"], keywords_excluir=["zzajusrata", "otra"])
        s.commit()
        pid = p.id
    client = TestClient(create_app(settings, limpio))
    cookies, headers = _sesion(settings, uid)

    with (
        patch("app.api.routes.pages.match_perfil") as en_peticion,
        patch("app.api.routes.pages._match_perfil_background", MagicMock()) as tarea,
    ):
        r = client.post(
            f"/perfiles/{pid}/deshacer-exclusion",
            data={"palabras": ["zzajusrata"], "next": "/"},
            cookies=cookies,
            headers=headers,
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/?restaurado=1"
        en_peticion.assert_not_called()
        tarea.assert_called_once()
        assert tarea.call_args.args[1] == pid
        html = client.get(r.headers["location"], cookies=cookies).text
        assert "Restauramos la exclusión" in html

        # Ownership: otro usuario no puede quitar exclusiones ajenas.
        c2, h2 = _sesion(settings, otro)
        r = client.post(
            f"/perfiles/{pid}/deshacer-exclusion",
            data={"palabras": ["otra"], "next": "/"},
            cookies=c2,
            headers=h2,
            follow_redirects=False,
        )
        assert r.status_code == 404
        tarea.assert_called_once()

    with Session(limpio) as s:
        perfil = s.get(PerfilBusqueda, pid)
        assert perfil is not None and list(perfil.keywords_excluir or []) == ["otra"]


# ---------------------------------------------------------------------------
# 3. Exclusiones que chocan con lo que el perfil busca
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keyword", "palabra", "choca"),
    [
        ("salud", "saludable", True),
        ("salud", "salud mental", False),
        ("eléctrico", "eléctricos", True),
        ("construcción", "ferretería", False),
    ],
)
def test_exclusiones_que_chocan_ejemplos(limpio, keyword, palabra, choca):
    with Session(limpio) as s:
        assert exclusiones_que_chocan(s, [keyword], [palabra]) == ([palabra] if choca else [])


def test_exclusiones_que_chocan_mantiene_orden_y_vacios(limpio):
    with Session(limpio) as s:
        assert exclusiones_que_chocan(s, ["salud", "aseo"], ["hospital", "aseos", "saludable"]) == [
            "aseos",
            "saludable",
        ]
        assert exclusiones_que_chocan(s, [], ["saludable"]) == []
        assert exclusiones_que_chocan(s, ["salud"], []) == []


def test_palabras_sugeridas_no_ofrece_la_misma_raiz(limpio):
    with Session(limpio) as s:
        sugeridas = palabras_sugeridas("Servicio saludable de hospital", ["salud"], s)
    assert sugeridas == ["hospital"]


def test_excluir_palabras_que_choca_no_modifica_el_perfil(limpio):
    with Session(limpio) as s:
        uid = _usuario(s)
        p = _perfil(s, uid, keywords=["salud"])
        s.commit()
        with pytest.raises(PerfilInvalido, match="saludable"):
            excluir_palabras(s, uid, p.id, ["saludable", "hospital"])
        s.rollback()
        perfil = s.get(PerfilBusqueda, p.id)
        assert perfil is not None and list(perfil.keywords_excluir or []) == []


def test_vista_previa_y_descartar_con_choque(limpio, settings):
    with Session(limpio) as s:
        uid = _usuario(s)
        p = _perfil(s, uid, keywords=["salud"], fuentes=["compras_agiles"])
        _ca(s, "AJUS-X-1", "Servicio saludable")
        _match(s, p.id, "compras_agiles", "AJUS-X-1")
        s.commit()
        pid = p.id
    client = TestClient(create_app(settings, limpio))
    cookies, headers = _sesion(settings, uid)

    r = client.get(
        f"/oportunidad/compras_agiles/AJUS-X-1/excluir-vista-previa?perfil_id={pid}&palabras=saludable",
        cookies=cookies,
    )
    assert r.status_code == 200
    assert "sacaría" in r.text and "salen" not in r.text

    r = client.post(
        "/oportunidad/compras_agiles/AJUS-X-1/descartar-y-excluir",
        data={"perfil_id": str(pid), "palabras": ["saludable"], "palabra_nueva": ""},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "elige otra palabra" in r.json()["detail"]
    with Session(limpio) as s:
        perfil = s.get(PerfilBusqueda, pid)
        assert perfil is not None and list(perfil.keywords_excluir or []) == []
        # No descartó: sin exclusión no hay descarte.
        assert s.execute(
            select(MatchFeedback).where(MatchFeedback.codigo_oportunidad == "AJUS-X-1")
        ).first() is None
        assert _codigos_match(s, pid) == {"AJUS-X-1"}


def test_formulario_de_perfil_rechaza_exclusion_que_choca(limpio, settings):
    with Session(limpio) as s:
        uid = _usuario(s)
        p = _perfil(s, uid, nombre="Existente", keywords=["salud"])
        s.commit()
        pid = p.id
    client = TestClient(create_app(settings, limpio))
    cookies, headers = _sesion(settings, uid)

    r = client.post(
        "/perfiles/nuevo",
        data={"nombre": "Nuevo", "keywords": "salud", "excluir": "saludable", "monto_min_clp": "5.000.000"},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    # F-perfiles-1: 422 con el formulario re-renderizado (lo escrito + el mensaje junto al campo).
    assert r.status_code == 422 and "sacaría todo lo que trae" in r.text
    assert 'value="salud"' in r.text and 'value="saludable"' in r.text and 'value="5.000.000"' in r.text
    assert 'id="nuevo_excluir_error"' in r.text
    r = client.post(
        f"/perfiles/{pid}/editar",
        data={"nombre": "Existente", "keywords": "salud", "excluir": "saludable"},
        cookies=cookies,
        headers={**headers, "HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 422 and f'id="p{pid}_excluir_error"' in r.text and "<html" not in r.text
    assert 'value="saludable"' in r.text
    with Session(limpio) as s:
        perfiles = list(s.execute(select(PerfilBusqueda).where(PerfilBusqueda.owner_id == uid)).scalars())
        assert [x.nombre for x in perfiles] == ["Existente"]
        assert list(perfiles[0].keywords_excluir or []) == []


def test_crear_y_actualizar_perfil_validan_choques(limpio):
    with Session(limpio) as s:
        uid = _usuario(s)
        with pytest.raises(PerfilInvalido):
            crear_perfil(s, uid, "x", keywords=["salud"], keywords_excluir=["saludable"])
        p = crear_perfil(s, uid, "ok", keywords=["salud"], keywords_excluir=["hospital"])
        with pytest.raises(PerfilInvalido):
            actualizar_perfil(s, p.id, uid, keywords_excluir=["hospital", "saludable"])
        # Lo ya guardado no se revisa: volver a guardar la misma exclusión no falla.
        p.keywords_excluir = ["saludable"]  # type: ignore[assignment]
        s.flush()
        assert actualizar_perfil(s, p.id, uid, keywords_excluir=["saludable"]) is not None
        s.rollback()
