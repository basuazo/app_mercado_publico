"""Tests F-guardar contra Postgres real (DATABASE_URL de DEV, migración aplicada):
limpieza de matches que ya no calzan, vista previa = lo que se borra, "Descartar y
excluir" de punta a punta, la migración de datos `c9e4b2f7a1d8` y el Texto del
explorador que busca también en `ca_productos`.

Todo dato de prueba lleva el prefijo `GUARD-` (o el correo `guardar-*@test.cl`) y
se borra al terminar. Las keywords son inventadas (`zzguard…`) para que el recall
no toque oportunidades reales de dev. Los tests de la migración corren dentro de
UNA transacción que se revierte al final (Postgres revierte también el DDL).
"""

from __future__ import annotations

import importlib.util
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.orm import Session

from app.alerts.detector import detectar_cambio_estado_seguidas
from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.db import normalizar_url_driver
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.explorador_ca import FiltrosExplorador, buscar
from app.matching import engine as eng
from app.matching.engine import (
    contar_limpieza,
    criterio_perfil,
    limpiar_matches_perfil,
    match_perfil,
)
from app.matching.perfiles import excluir_palabras
from app.models.enums import RolUsuario, ValorFeedback
from app.models.tables import (
    Alerta,
    CaProducto,
    CompraAgil,
    Licitacion,
    MatchFeedback,
    OportunidadMatch,
    OportunidadSeguida,
    PerfilBusqueda,
    Usuario,
)

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_POSTGRES = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")
needs_postgres = pytest.mark.skipif(
    not _TIENE_POSTGRES,
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
pytestmark = needs_postgres

_EMAIL = "guardar-a@test.cl"
_EMAIL_B = "guardar-b@test.cl"
_PW = "contraseña-segura-test"
_ORG = "GUARD ORGANISMO DE PRUEBA"


@pytest.fixture(scope="module")
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    yield e
    e.dispose()


def _limpiar(engine) -> None:
    with Session(engine) as s:
        s.execute(delete(Usuario).where(Usuario.email.in_([_EMAIL, _EMAIL_B])))
        s.execute(delete(Licitacion).where(Licitacion.codigo.like("GUARD-%")))
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like("GUARD-%")))
        s.commit()


@pytest.fixture()
def limpio(pg_engine):
    _limpiar(pg_engine)
    yield pg_engine
    _limpiar(pg_engine)


def _usuario(s: Session, email: str = _EMAIL) -> int:
    u = Usuario(email=email, password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
    s.add(u)
    s.flush()
    return u.id


def _perfil(s: Session, owner_id: int, **campos: Any) -> PerfilBusqueda:
    base: dict[str, Any] = {
        "owner_id": owner_id,
        "nombre": "Perfil guardar",
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


def _lic(s: Session, codigo: str, nombre: str, **campos: Any) -> None:
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": nombre,
        "descripcion": "",
        "estado": "publicada",
        "fecha_cierre": ahora_utc() + timedelta(days=10),
    }
    base.update(campos)
    s.add(Licitacion(**base))


def _ca(s: Session, codigo: str, nombre: str, **campos: Any) -> None:
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": nombre,
        "descripcion": "",
        "estado": "publicada",
        "region": 13,
        "total_ofertas": 0,
        "monto_disponible_clp": 500_000.0,
        "organismo_nombre": _ORG,
        "fecha_publicacion": ahora_utc() - timedelta(days=1),
        "fecha_cierre": ahora_utc() + timedelta(days=10),
    }
    base.update(campos)
    s.add(CompraAgil(**base))


def _match(s: Session, perfil_id: int, fuente: str, codigo: str) -> OportunidadMatch:
    m = OportunidadMatch(
        perfil_id=perfil_id, fuente=fuente, codigo_oportunidad=codigo, score=50, razones={}
    )
    s.add(m)
    s.flush()
    return m


def _codigos_match(s: Session, perfil_id: int) -> set[str]:
    return set(
        s.execute(
            select(OportunidadMatch.codigo_oportunidad).where(OportunidadMatch.perfil_id == perfil_id)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# B. Limpieza de matches que ya no calzan
# ---------------------------------------------------------------------------


class TestLimpieza:
    def test_quitar_keyword_borra_lo_que_no_calza_y_deja_lo_que_si(self, limpio):
        with Session(limpio) as s:
            uid = _usuario(s)
            _lic(s, "GUARD-L-A", "Servicio zzguardalfa de mantención")
            _lic(s, "GUARD-L-B", "Compra zzguardbeta de insumos")
            _ca(s, "GUARD-C-B", "Compra zzguardbeta menor")
            p = _perfil(s, uid, keywords=["zzguardalfa", "zzguardbeta"])
            s.commit()
            match_perfil(p, s)
            assert _codigos_match(s, p.id) == {"GUARD-L-A", "GUARD-L-B", "GUARD-C-B"}

            p.keywords = ["zzguardalfa"]  # type: ignore[assignment]
            s.flush()
            assert limpiar_matches_perfil(s, p) == 2
            s.commit()
            assert _codigos_match(s, p.id) == {"GUARD-L-A"}

    def test_exclusion_con_tilde_borra_en_nombre_y_en_producto(self, limpio):
        with Session(limpio) as s:
            uid = _usuario(s)
            _ca(s, "GUARD-C-1", "Servicio zzguardplaga desratización")
            _ca(s, "GUARD-C-2", "Servicio zzguardplaga integral")
            _ca(s, "GUARD-C-3", "Servicio zzguardplaga fumigación")
            s.flush()
            # El término solo está en el producto, y escrito sin tilde.
            s.add(
                CaProducto(
                    ca_codigo="GUARD-C-2", codigo_producto="", nombre="servicio de desratizacion",
                    descripcion="",
                )
            )
            p = _perfil(s, uid, keywords=["zzguardplaga"], fuentes=["compras_agiles"])
            s.commit()
            match_perfil(p, s)
            assert _codigos_match(s, p.id) == {"GUARD-C-1", "GUARD-C-2", "GUARD-C-3"}

            resultado = excluir_palabras(s, uid, p.id, ["desratización"])
            s.commit()
            assert resultado == (["desratización"], 2)
            assert _codigos_match(s, p.id) == {"GUARD-C-3"}
            assert list(p.keywords_excluir) == ["desratización"]

    def test_no_toca_terminales_otro_perfil_otro_usuario_seguidas_ni_feedback(self, limpio):
        with Session(limpio) as s:
            uid = _usuario(s)
            uid_b = _usuario(s, _EMAIL_B)
            _lic(s, "GUARD-L-VIG", "Compra zzguardotra cosa")
            _lic(s, "GUARD-L-TERM", "Compra zzguardotra cosa", estado="adjudicada")
            _lic(s, "GUARD-L-VENC", "Compra zzguardotra cosa", fecha_cierre=ahora_utc() - timedelta(days=3))
            p = _perfil(s, uid, keywords=["zzguardnada"])
            p_otro = _perfil(s, uid, keywords=["zzguardnada2"], nombre="Otro perfil")
            p_b = _perfil(s, uid_b, keywords=["zzguardnada3"])
            for codigo in ("GUARD-L-VIG", "GUARD-L-TERM", "GUARD-L-VENC"):
                _match(s, p.id, "licitaciones", codigo)
            _match(s, p_otro.id, "licitaciones", "GUARD-L-VIG")
            _match(s, p_b.id, "licitaciones", "GUARD-L-VIG")
            s.add(OportunidadSeguida(owner_id=uid, fuente="licitaciones", codigo_oportunidad="GUARD-L-VIG"))
            s.add(
                MatchFeedback(
                    usuario_id=uid, fuente="licitaciones", codigo_oportunidad="GUARD-L-VENC",
                    valor=ValorFeedback.DESCARTE.value,
                )
            )
            s.commit()

            assert limpiar_matches_perfil(s, p) == 1
            s.commit()
            assert _codigos_match(s, p.id) == {"GUARD-L-TERM", "GUARD-L-VENC"}
            assert _codigos_match(s, p_otro.id) == {"GUARD-L-VIG"}
            assert _codigos_match(s, p_b.id) == {"GUARD-L-VIG"}
            assert s.execute(
                select(func.count()).select_from(OportunidadSeguida).where(OportunidadSeguida.owner_id == uid)
            ).scalar_one() == 1
            assert s.execute(
                select(func.count()).select_from(MatchFeedback).where(MatchFeedback.usuario_id == uid)
            ).scalar_one() == 1

    def test_perfil_sin_keywords_rubros_ni_organismos_no_borra(self, limpio):
        with Session(limpio) as s:
            uid = _usuario(s)
            _lic(s, "GUARD-L-SR", "Compra zzguardcualquiera")
            _ca(s, "GUARD-C-SR", "Compra zzguardcualquiera", region=13)
            p = _perfil(s, uid, regiones=[13])
            _match(s, p.id, "licitaciones", "GUARD-L-SR")
            _match(s, p.id, "compras_agiles", "GUARD-C-SR")
            s.commit()
            assert limpiar_matches_perfil(s, p) == 0

    def test_region_y_monto_no_informado(self, limpio):
        with Session(limpio) as s:
            uid = _usuario(s)
            _ca(s, "GUARD-C-SINMONTO", "Compra zzguardmonto", monto_disponible_clp=None)
            _ca(s, "GUARD-C-CHICA", "Compra zzguardmonto", monto_disponible_clp=10.0)
            _ca(s, "GUARD-C-OK", "Compra zzguardmonto", monto_disponible_clp=5_000_000.0)
            _ca(s, "GUARD-C-REGION", "Compra zzguardmonto", monto_disponible_clp=5_000_000.0, region=5)
            p = _perfil(
                s, uid, keywords=["zzguardmonto"], monto_min_clp=1_000_000.0, regiones=[13],
                fuentes=["compras_agiles"],
            )
            for codigo in ("GUARD-C-SINMONTO", "GUARD-C-CHICA", "GUARD-C-OK", "GUARD-C-REGION"):
                _match(s, p.id, "compras_agiles", codigo)
            s.commit()
            assert limpiar_matches_perfil(s, p) == 2
            s.commit()
            assert _codigos_match(s, p.id) == {"GUARD-C-SINMONTO", "GUARD-C-OK"}

    def test_fuente_quitada_borra_los_vigentes_de_esa_fuente(self, limpio):
        with Session(limpio) as s:
            uid = _usuario(s)
            _lic(s, "GUARD-L-F", "Compra zzguardfuente")
            _ca(s, "GUARD-C-F", "Compra zzguardfuente")
            p = _perfil(s, uid, keywords=["zzguardfuente"], fuentes=["compras_agiles"])
            _match(s, p.id, "licitaciones", "GUARD-L-F")
            _match(s, p.id, "compras_agiles", "GUARD-C-F")
            s.commit()
            assert limpiar_matches_perfil(s, p) == 1
            assert _codigos_match(s, p.id) == {"GUARD-C-F"}

    def test_candidatos_sobre_el_tope_no_se_borran(self, limpio):
        with Session(limpio) as s:
            uid = _usuario(s)
            for i in range(5):
                _lic(s, f"GUARD-L-TOPE{i}", f"Compra zzguardtope numero {i}")
            p = _perfil(s, uid, keywords=["zzguardtope"], fuentes=["licitaciones"])
            s.commit()
            with patch.object(eng, "_MAX_CANDIDATOS", 2):
                r = match_perfil(p, s)
            assert r["nuevos"] == 2
            # Los que el tope dejó fuera del recall se siembran a mano: calzan igual.
            for i in range(5):
                codigo = f"GUARD-L-TOPE{i}"
                if codigo not in _codigos_match(s, p.id):
                    _match(s, p.id, "licitaciones", codigo)
            s.commit()
            with patch.object(eng, "_MAX_CANDIDATOS", 2):
                assert limpiar_matches_perfil(s, p) == 0
            assert len(_codigos_match(s, p.id)) == 5

    def test_alertas_del_match_borrado_se_van_por_cascade(self, limpio):
        with Session(limpio) as s:
            uid = _usuario(s)
            _lic(s, "GUARD-L-AL", "Compra zzguardalerta")
            p = _perfil(s, uid, keywords=["zzguardotracosa"])
            m = _match(s, p.id, "licitaciones", "GUARD-L-AL")
            s.add(Alerta(match_id=m.id, tipo="nuevo_match"))
            s.commit()
            match_id = m.id
            assert limpiar_matches_perfil(s, p) == 1
            s.commit()
            assert s.execute(
                select(func.count()).select_from(Alerta).where(Alerta.match_id == match_id)
            ).scalar_one() == 0

    def test_match_perfil_devuelve_borrados(self, limpio):
        with Session(limpio) as s:
            uid = _usuario(s)
            _lic(s, "GUARD-L-MP", "Compra zzguardmp")
            p = _perfil(s, uid, keywords=["zzguardmp"], fuentes=["licitaciones"])
            _lic(s, "GUARD-L-MP2", "Compra sin la palabra")
            _match(s, p.id, "licitaciones", "GUARD-L-MP2")
            s.commit()
            r = match_perfil(p, s)
            assert r["borrados"] == 1
            assert _codigos_match(s, p.id) == {"GUARD-L-MP"}


# ---------------------------------------------------------------------------
# E. Vista previa = lo que realmente se borra; Descartar y excluir de punta a punta
# ---------------------------------------------------------------------------


def _sesion(settings: Settings, user_id: int) -> tuple[dict[str, str], dict[str, str]]:
    token = create_session_token(settings.secret_key, user_id)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    _, nonce = decoded
    return {COOKIE_NAME: token}, {"X-CSRF-Token": generate_csrf_token(settings.secret_key, nonce)}


@pytest.fixture()
def settings():
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url=_DB_URL,
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
    )


def _sembrar_feed(s: Session) -> tuple[int, int]:
    uid = _usuario(s)
    _ca(s, "GUARD-E-1", "Servicio zzguardaseo hospital zzguardrata")
    _ca(s, "GUARD-E-2", "Servicio zzguardaseo zzguardrata bodega")
    _ca(s, "GUARD-E-3", "Servicio zzguardaseo oficinas")
    p = _perfil(s, uid, keywords=["zzguardaseo"], fuentes=["compras_agiles"])
    s.commit()
    match_perfil(p, s)
    return uid, p.id


def test_vista_previa_es_lo_que_se_borra(limpio, settings):
    with Session(limpio) as s:
        uid, pid = _sembrar_feed(s)
        perfil = s.get(PerfilBusqueda, pid)
        assert perfil is not None
        previsto = contar_limpieza(s, criterio_perfil(perfil, ["zzguardrata"]))

        client = TestClient(create_app(settings, limpio))
        cookies, _ = _sesion(settings, uid)
        r = client.get(
            f"/oportunidad/compras_agiles/GUARD-E-1/excluir-vista-previa?perfil_id={pid}&palabras=zzguardrata",
            cookies=cookies,
        )
        assert r.status_code == 200
        assert f"salen {previsto} oportunidades" in r.text

        resultado = excluir_palabras(s, uid, pid, ["zzguardrata"])
        s.commit()
        assert resultado is not None
        assert resultado[1] == previsto == 2


def test_descartar_y_excluir_y_deshacer(limpio, settings):
    with Session(limpio) as s:
        uid, pid = _sembrar_feed(s)
    client = TestClient(create_app(settings, limpio))
    cookies, headers = _sesion(settings, uid)

    r = client.post(
        "/oportunidad/compras_agiles/GUARD-E-1/descartar-y-excluir",
        data={"perfil_id": str(pid), "palabras": ["zzguardrata"], "palabra_nueva": ""},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "excluidos_n=2" in r.headers["location"]
    with Session(limpio) as s:
        assert _codigos_match(s, pid) == {"GUARD-E-3"}
        fb = s.execute(
            select(MatchFeedback).where(
                MatchFeedback.usuario_id == uid, MatchFeedback.codigo_oportunidad == "GUARD-E-1"
            )
        ).scalar_one()
        assert fb.valor == ValorFeedback.DESCARTE.value

    # El aviso del feed ofrece deshacer.
    html = client.get(r.headers["location"], cookies=cookies).text
    assert "Deshacer exclusión" in html
    assert re.search(r"salieron\s+2\s+oportunidades", html)

    r = client.post(
        f"/perfiles/{pid}/deshacer-exclusion",
        data={"palabras": ["zzguardrata"], "next": "/"},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(limpio) as s:
        perfil = s.get(PerfilBusqueda, pid)
        assert perfil is not None and list(perfil.keywords_excluir or []) == []
        # match_perfil recreó los matches (la descartada sigue descartada).
        assert _codigos_match(s, pid) == {"GUARD-E-1", "GUARD-E-2", "GUARD-E-3"}


# ---------------------------------------------------------------------------
# D. Texto del explorador también en productos
# ---------------------------------------------------------------------------


def test_texto_del_explorador_encuentra_termino_solo_en_productos(limpio):
    with Session(limpio) as s:
        uid = _usuario(s)
        _ca(s, "GUARD-X-1", "Insumos varios zzguardx")
        _ca(s, "GUARD-X-2", "Otros insumos zzguardx")
        s.flush()
        s.add(
            CaProducto(
                ca_codigo="GUARD-X-1", codigo_producto="", nombre="Reparación de techumbre", descripcion=""
            )
        )
        s.commit()
        filtros = FiltrosExplorador(usuario_id=uid, texto="reparación", organismo=_ORG)
        assert {i["codigo"] for i in buscar(s, filtros).items} == {"GUARD-X-1"}
        # Sin tilde también.
        filtros = FiltrosExplorador(usuario_id=uid, texto="reparacion", organismo=_ORG)
        assert {i["codigo"] for i in buscar(s, filtros).items} == {"GUARD-X-1"}


# ---------------------------------------------------------------------------
# C. Migración de datos
# ---------------------------------------------------------------------------


def _modulo_migracion() -> Any:
    ruta = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "c9e4b2f7a1d8_guardar_unificado.py"
    spec = importlib.util.spec_from_file_location("mig_guardar", ruta)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migracion_sirve_a_guardadas(pg_engine):
    mig = _modulo_migracion()
    ahora = ahora_utc()
    with pg_engine.connect() as conn:
        trans = conn.begin()
        try:
            s = Session(bind=conn)
            uid = _usuario(s, "guardar-mig@test.cl")
            _lic(s, "GUARD-M-ADJ", "Lic adjudicada", estado="adjudicada", fecha_cierre=ahora - timedelta(days=30))
            _lic(s, "GUARD-M-PRONTO", "Lic que cierra pronto", fecha_cierre=ahora + timedelta(hours=20))
            _lic(s, "GUARD-M-ARCH", "Lic ya archivada")
            _ca(s, "GUARD-M-ACT", "CA ya guardada")
            s.flush()
            s.add(OportunidadSeguida(
                owner_id=uid, fuente="licitaciones", codigo_oportunidad="GUARD-M-ARCH",
                estado_visto="publicada", archivada=True,
            ))
            s.add(OportunidadSeguida(
                owner_id=uid, fuente="compras_agiles", codigo_oportunidad="GUARD-M-ACT",
                estado_visto="publicada",
            ))
            for fuente, codigo in [
                ("licitaciones", "GUARD-M-ADJ"),
                ("licitaciones", "GUARD-M-PRONTO"),
                ("licitaciones", "GUARD-M-ARCH"),
                ("compras_agiles", "GUARD-M-ACT"),
                ("licitaciones", "GUARD-M-NOEXISTE"),
            ]:
                s.add(MatchFeedback(usuario_id=uid, fuente=fuente, codigo_oportunidad=codigo, valor="sirve"))
            s.add(MatchFeedback(
                usuario_id=uid, fuente="compras_agiles", codigo_oportunidad="GUARD-M-DESC", valor="descarte"
            ))
            s.flush()

            def seguidas() -> dict[str, OportunidadSeguida]:
                s.expire_all()
                return {
                    x.codigo_oportunidad: x
                    for x in s.execute(select(OportunidadSeguida).where(OportunidadSeguida.owner_id == uid)).scalars()
                }

            def feedback() -> dict[str, str]:
                return {
                    f.codigo_oportunidad: f.valor
                    for f in s.execute(select(MatchFeedback).where(MatchFeedback.usuario_id == uid)).scalars()
                }

            mig.migrar_sirve(conn)

            seg = seguidas()
            assert set(seg) == {"GUARD-M-ADJ", "GUARD-M-PRONTO", "GUARD-M-ARCH", "GUARD-M-ACT", "GUARD-M-NOEXISTE"}
            assert seg["GUARD-M-ADJ"].estado_visto == "adjudicada" and not seg["GUARD-M-ADJ"].archivada
            assert seg["GUARD-M-NOEXISTE"].estado_visto == ""
            assert seg["GUARD-M-ARCH"].archivada  # con seguida archivada: sigue archivada
            assert feedback() == {"GUARD-M-DESC": "descarte"}  # las `sirve` se borraron

            # Cierre < 48 h: alerta ya "enviada", no pendiente (no se manda correo).
            alertas = list(s.execute(
                select(Alerta).where(Alerta.seguimiento_id == seg["GUARD-M-PRONTO"].id)
            ).scalars())
            assert [(a.tipo, a.estado) for a in alertas] == [("seguimiento_cierre", "enviada")]

            # Con estado_visto = estado actual, la detección no alerta por las migradas.
            ids = [x.id for x in seg.values()]
            detectar_cambio_estado_seguidas(s)
            s.flush()
            assert s.execute(
                select(func.count()).select_from(Alerta).where(
                    Alerta.seguimiento_id.in_(ids), Alerta.tipo.like("seguimiento_estado:%")
                )
            ).scalar_one() == 0

            # Upgrade dos veces: no duplica nada.
            n_seg = len(seguidas())
            n_reg = conn.execute(text("SELECT count(*) FROM _mig_guardar_creadas")).scalar_one()
            mig.migrar_sirve(conn)
            assert len(seguidas()) == n_seg
            assert conn.execute(text("SELECT count(*) FROM _mig_guardar_creadas")).scalar_one() == n_reg

            # Downgrade: vuelven las 5 `sirve` y se borran solo las seguidas creadas.
            mig.revertir_sirve(conn)
            assert feedback() == {
                "GUARD-M-ADJ": "sirve",
                "GUARD-M-PRONTO": "sirve",
                "GUARD-M-ARCH": "sirve",
                "GUARD-M-ACT": "sirve",
                "GUARD-M-NOEXISTE": "sirve",
                "GUARD-M-DESC": "descarte",
            }
            seg = seguidas()
            assert set(seg) == {"GUARD-M-ARCH", "GUARD-M-ACT"}
            assert seg["GUARD-M-ARCH"].archivada
            assert conn.execute(text("SELECT to_regclass('_mig_guardar_creadas')")).scalar() is None
            s.close()
        finally:
            trans.rollback()
