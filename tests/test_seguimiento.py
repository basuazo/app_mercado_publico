"""Tests F-seguir — seguir/archivar oportunidades, detección de avance y alertas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.alerts.detector import detectar_cambio_estado_seguidas
from app.alerts.email import (
    _ctx_alerta_seguimiento,
    _jinja,
    _url_ficha_app,
    enviar_pendientes_inmediatas,
)
from app.ingest.lifecycle import refresh_estados
from app.matching.seguimiento import (
    archivar_seguimiento,
    dejar_de_seguir,
    listar_seguidas,
    obtener_seguimiento,
    seguir_oportunidad,
)
from app.models.tables import Alerta, CompraAgil, Licitacion, OportunidadSeguida, Usuario

_PW_HASH = "$2b$12$fakehashforteststhatislong.enough.xyz"


@pytest.fixture()
def engine():
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


def _user(session: Session, email: str = "user@test.com") -> Usuario:
    u = Usuario(email=email, password_hash=_PW_HASH, activo=True)
    session.add(u)
    session.flush()
    return u


def _lic(session: Session, codigo: str = "LIC-001", estado: str = "publicada") -> Licitacion:
    lic = Licitacion(codigo=codigo, nombre=f"Licitación {codigo}", descripcion="", estado=estado)
    session.add(lic)
    session.flush()
    return lic


def _ca(session: Session, codigo: str = "CA-001", estado: str = "publicada") -> CompraAgil:
    c = CompraAgil(codigo=codigo, nombre=f"CA {codigo}", descripcion="", estado=estado)
    session.add(c)
    session.flush()
    return c


def _fake_settings(app_base_url: str = "", limit: int = 250) -> Any:
    s = MagicMock()
    s.email_daily_limit = limit
    s.brevo_api_key = ""
    s.smtp_host = "smtp.test.local"
    s.smtp_port = 587
    s.smtp_user = "user"
    s.smtp_password = "pw"
    s.smtp_from = "from@test.com"
    s.app_base_url = app_base_url
    s.mp_ticket = "TICKET_SECRETO"
    s.secret_key = "SECRET_KEY_SECRETO"
    s.jobs_token = "JOBS_TOKEN_SECRETO"
    return s


# ---------------------------------------------------------------------------
# 1. CRUD + ownership
# ---------------------------------------------------------------------------


class TestSeguirCRUD:
    def test_seguir_crea_seguimiento(self, session: Session):
        u = _user(session)
        _lic(session, "LIC-001", estado="publicada")
        s = seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()
        assert s.id is not None
        assert s.estado_visto == "publicada"
        assert s.archivada is False

    def test_seguir_no_duplica(self, session: Session):
        u = _user(session)
        _lic(session)
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()
        filas = list(
            session.execute(
                select(OportunidadSeguida).where(
                    OportunidadSeguida.owner_id == u.id, OportunidadSeguida.codigo_oportunidad == "LIC-001"
                )
            ).scalars()
        )
        assert len(filas) == 1

    def test_seguir_reactiva_si_estaba_archivada(self, session: Session):
        u = _user(session)
        _lic(session)
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()
        archivar_seguimiento(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", archivada=True)
        session.commit()
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()
        s = obtener_seguimiento(session, u.id, "licitaciones", "LIC-001")
        assert s is not None
        assert s.archivada is False

    def test_archivar_y_desarchivar(self, session: Session):
        u = _user(session)
        _lic(session)
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()

        assert archivar_seguimiento(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", archivada=True)
        session.commit()
        s = obtener_seguimiento(session, u.id, "licitaciones", "LIC-001")
        assert s is not None and s.archivada is True

        assert archivar_seguimiento(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", archivada=False)
        session.commit()
        s = obtener_seguimiento(session, u.id, "licitaciones", "LIC-001")
        assert s is not None and s.archivada is False

    def test_archivar_inexistente_retorna_false(self, session: Session):
        u = _user(session)
        assert archivar_seguimiento(session, owner_id=u.id, fuente="licitaciones", codigo="NOPE", archivada=True) is False

    def test_dejar_de_seguir_elimina(self, session: Session):
        u = _user(session)
        _lic(session)
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()
        assert dejar_de_seguir(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001") is True
        session.commit()
        assert obtener_seguimiento(session, u.id, "licitaciones", "LIC-001") is None

    def test_dejar_de_seguir_inexistente_retorna_false(self, session: Session):
        u = _user(session)
        assert dejar_de_seguir(session, owner_id=u.id, fuente="licitaciones", codigo="NOPE") is False

    def test_ownership_un_usuario_no_toca_seguidas_de_otro(self, session: Session):
        u1 = _user(session, "u1@test.com")
        u2 = _user(session, "u2@test.com")
        _lic(session)
        seguir_oportunidad(session, owner_id=u1.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()

        # u2 no ve el seguimiento de u1
        assert obtener_seguimiento(session, u2.id, "licitaciones", "LIC-001") is None
        # u2 no puede archivar ni eliminar el seguimiento de u1
        assert archivar_seguimiento(session, owner_id=u2.id, fuente="licitaciones", codigo="LIC-001", archivada=True) is False
        assert dejar_de_seguir(session, owner_id=u2.id, fuente="licitaciones", codigo="LIC-001") is False

        # el seguimiento de u1 sigue intacto
        s1 = obtener_seguimiento(session, u1.id, "licitaciones", "LIC-001")
        assert s1 is not None and s1.archivada is False

    def test_listar_seguidas_excluye_archivadas_por_defecto(self, session: Session):
        u = _user(session)
        _lic(session, "LIC-A")
        _lic(session, "LIC-B")
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-A", estado_actual="publicada")
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-B", estado_actual="publicada")
        session.commit()
        archivar_seguimiento(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-B", archivada=True)
        session.commit()

        activas = listar_seguidas(session, u.id)
        assert {s.codigo_oportunidad for s in activas} == {"LIC-A"}

        todas = listar_seguidas(session, u.id, incluir_archivadas=True)
        assert {s.codigo_oportunidad for s in todas} == {"LIC-A", "LIC-B"}


# ---------------------------------------------------------------------------
# 2. Detección de cambio de estado
# ---------------------------------------------------------------------------


class TestDetectarCambioEstadoSeguidas:
    def test_cambio_de_estado_genera_una_alerta(self, session: Session):
        u = _user(session)
        _lic(session, "LIC-001", estado="cerrada")
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()

        creados = detectar_cambio_estado_seguidas(session)
        assert creados == 1
        alertas = list(session.execute(select(Alerta)).scalars())
        assert len(alertas) == 1
        assert alertas[0].tipo == "seguimiento_estado:cerrada"

        s = obtener_seguimiento(session, u.id, "licitaciones", "LIC-001")
        assert s is not None and s.estado_visto == "cerrada"

    def test_no_re_alerta_sin_cambio_adicional(self, session: Session):
        u = _user(session)
        _lic(session, "LIC-001", estado="cerrada")
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()

        detectar_cambio_estado_seguidas(session)
        session.commit()
        creados2 = detectar_cambio_estado_seguidas(session)
        assert creados2 == 0
        alertas = list(session.execute(select(Alerta)).scalars())
        assert len(alertas) == 1

    def test_transicion_a_adjudicada(self, session: Session):
        u = _user(session)
        _lic(session, "LIC-ADJ", estado="adjudicada")
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-ADJ", estado_actual="publicada")
        session.commit()

        creados = detectar_cambio_estado_seguidas(session)
        assert creados == 1
        a = session.execute(select(Alerta)).scalar_one()
        assert a.tipo == "seguimiento_estado:adjudicada"

    def test_no_detecta_sin_cambio_de_estado(self, session: Session):
        u = _user(session)
        _lic(session, "LIC-001", estado="publicada")
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()
        assert detectar_cambio_estado_seguidas(session) == 0

    def test_no_detecta_seguimiento_archivado(self, session: Session):
        u = _user(session)
        _lic(session, "LIC-001", estado="cerrada")
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()
        archivar_seguimiento(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", archivada=True)
        session.commit()
        assert detectar_cambio_estado_seguidas(session) == 0

    def test_compra_agil_cambio_de_estado(self, session: Session):
        u = _user(session)
        _ca(session, "CA-001", estado="desierta")
        seguir_oportunidad(session, owner_id=u.id, fuente="compras_agiles", codigo="CA-001", estado_actual="publicada")
        session.commit()
        creados = detectar_cambio_estado_seguidas(session)
        assert creados == 1
        a = session.execute(select(Alerta)).scalar_one()
        assert a.tipo == "seguimiento_estado:desierta"


# ---------------------------------------------------------------------------
# 3. Lifecycle: refresh_estados incluye seguidas aunque no sean match
# ---------------------------------------------------------------------------


class TestRefreshEstadosIncluyeSeguidas:
    def test_seguida_sin_match_y_fuera_de_ventana_entra_al_refresh(self, session: Session):
        from app.clients.types import LicitacionDetalle

        u = _user(session)
        # fecha_cierre muy lejana → fuera de la ventana ±7/+3 días
        lic = Licitacion(
            codigo="LIC-LEJOS",
            nombre="Lic lejos",
            descripcion="",
            estado="publicada",
            fecha_cierre=datetime(2030, 1, 1),
        )
        session.add(lic)
        session.commit()
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-LEJOS", estado_actual="publicada")
        session.commit()

        v1 = MagicMock()
        v1.licitacion_detalle.return_value = LicitacionDetalle(
            codigo="LIC-LEJOS",
            nombre="Lic lejos",
            estado=6,
            fecha_publicacion=None,
            fecha_cierre=None,
            tipo=None,
            codigo_organismo=None,
            descripcion="actualizada",
        )
        v2 = MagicMock()
        settings = MagicMock()

        result = refresh_estados(session, v1, v2, settings, max_requests=10)
        assert result["actualizadas_licitaciones"] == 1
        v1.licitacion_detalle.assert_called_once_with("LIC-LEJOS")

    def test_compra_agil_seguida_fuera_de_ventana_entra_al_refresh(self, session: Session):
        from app.clients.types import CompraAgilDetalle

        u = _user(session)
        ca = CompraAgil(
            codigo="CA-LEJOS",
            nombre="CA lejos",
            descripcion="",
            estado="publicada",
            fecha_cierre=datetime(2030, 1, 1),
        )
        session.add(ca)
        session.commit()
        seguir_oportunidad(session, owner_id=u.id, fuente="compras_agiles", codigo="CA-LEJOS", estado_actual="publicada")
        session.commit()

        v1 = MagicMock()
        v2 = MagicMock()
        v2.detalle_compra_agil.return_value = CompraAgilDetalle(
            codigo="CA-LEJOS",
            nombre="CA lejos",
            estado="cerrada",
            fecha_publicacion=None,
            fecha_cierre=None,
            fecha_ultimo_cambio=None,
            monto_clp=None,
            region=None,
            organismo_nombre=None,
            organismo_rut=None,
            total_ofertas=0,
            descripcion="",
            productos=[],
            id_orden_compra=None,
        )
        settings = MagicMock()

        result = refresh_estados(session, v1, v2, settings, max_requests=10)
        assert result["actualizadas_ca"] == 1
        v2.detalle_compra_agil.assert_called_once_with("CA-LEJOS")

    def test_seguida_archivada_no_entra_al_refresh(self, session: Session):
        u = _user(session)
        lic = Licitacion(
            codigo="LIC-ARCH",
            nombre="Lic archivada",
            descripcion="",
            estado="publicada",
            fecha_cierre=datetime(2030, 1, 1),
        )
        session.add(lic)
        session.commit()
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-ARCH", estado_actual="publicada")
        session.commit()
        archivar_seguimiento(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-ARCH", archivada=True)
        session.commit()

        v1 = MagicMock()
        v2 = MagicMock()
        settings = MagicMock()
        result = refresh_estados(session, v1, v2, settings, max_requests=10)
        assert result["actualizadas_licitaciones"] == 0
        v1.licitacion_detalle.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Envío de alertas de seguimiento
# ---------------------------------------------------------------------------


class TestEnvioAlertaSeguimiento:
    def _setup_pendiente(self, session: Session, estado: str = "adjudicada") -> Alerta:
        u = _user(session)
        _lic(session, "LIC-001", estado=estado)
        seguir_oportunidad(session, owner_id=u.id, fuente="licitaciones", codigo="LIC-001", estado_actual="publicada")
        session.commit()
        detectar_cambio_estado_seguidas(session)
        session.commit()
        return session.execute(select(Alerta)).scalar_one()

    def test_envia_y_marca_enviada(self, session: Session, monkeypatch):
        self._setup_pendiente(session)
        enviados: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "app.alerts.email._smtp_send",
            lambda settings, to, subj, txt, html: enviados.append((to, subj, txt)),
        )
        result = enviar_pendientes_inmediatas(session, _fake_settings())
        assert result["enviados"] == 1
        assert len(enviados) == 1
        assert enviados[0][0] == "user@test.com"
        assert "adjudicó" in enviados[0][2] or "adjudic" in enviados[0][2].lower()

        a = session.execute(select(Alerta)).scalar_one()
        assert a.estado == "enviada"

    def test_link_usa_app_base_url_si_configurado(self, session: Session):
        a = self._setup_pendiente(session)
        ctx = _ctx_alerta_seguimiento(a, session, _fake_settings(app_base_url="https://app.test"))
        assert ctx["url"] == "https://app.test/oportunidad/licitaciones/LIC-001"

    def test_link_relativo_si_no_hay_app_base_url(self, session: Session):
        url = _url_ficha_app(_fake_settings(app_base_url=""), "licitaciones", "LIC-001")
        assert url == "/oportunidad/licitaciones/LIC-001"

    def test_render_sin_secretos_y_con_pie_fuente(self, session: Session):
        a = self._setup_pendiente(session)
        settings = _fake_settings()
        ctx = _ctx_alerta_seguimiento(a, session, settings)
        html = _jinja.get_template("alerta_seguimiento.html").render(**ctx)
        txt = _jinja.get_template("alerta_seguimiento.txt").render(**ctx)
        for secreto in (settings.mp_ticket, settings.secret_key, settings.jobs_token):
            assert secreto not in html
            assert secreto not in txt
        assert "Fuente: Dirección ChileCompra" in html
        assert "Fuente: Dirección ChileCompra" in txt

    def test_no_re_envia_si_no_pendiente(self, session: Session, monkeypatch):
        self._setup_pendiente(session)
        monkeypatch.setattr("app.alerts.email._smtp_send", lambda *a: None)
        enviar_pendientes_inmediatas(session, _fake_settings())
        # segunda corrida: la alerta ya está 'enviada', no debe reenviarse
        enviados: list[str] = []
        monkeypatch.setattr("app.alerts.email._smtp_send", lambda s, to, *a: enviados.append(to))
        enviar_pendientes_inmediatas(session, _fake_settings())
        assert enviados == []
