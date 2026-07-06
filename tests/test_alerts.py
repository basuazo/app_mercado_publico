"""Tests F-notificaciones — resumen consolidado + inmediatas solo para seguidas."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.alerts.detector import (
    detectar_cambio_estado_seguidas,
    detectar_recordatorio_cierre_seguidas,
)
from app.alerts.email import EmailCounter, _jinja, enviar_pendientes_inmediatas, enviar_resumen
from app.models.tables import (
    Alerta,
    CompraAgil,
    Licitacion,
    OportunidadMatch,
    OportunidadSeguida,
    PerfilBusqueda,
    SyncState,
    Usuario,
)

_PW_HASH = "$2b$12$fakehashforteststhatislong.enough.xyz"
_AHORA = datetime(2026, 6, 13, 12, 0, 0)


@pytest.fixture()
def sqlite_engine():
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def session(sqlite_engine):
    with Session(sqlite_engine) as s:
        yield s


def _user(
    session: Session,
    email: str = "user@test.com",
    *,
    dias_resumen: int = 3,
    ultimo_resumen_en: datetime | None = None,
    activo: bool = True,
) -> Usuario:
    u = Usuario(
        email=email,
        password_hash=_PW_HASH,
        activo=activo,
        dias_resumen=dias_resumen,
        ultimo_resumen_en=ultimo_resumen_en,
    )
    session.add(u)
    session.flush()
    return u


def _perfil(session: Session, owner: Usuario) -> PerfilBusqueda:
    p = PerfilBusqueda(
        owner_id=owner.id,
        nombre="Test Perfil",
        keywords=["cable"],
        keywords_excluir=[],
        regiones=[],
        fuentes=["licitaciones"],
        activo=True,
    )
    session.add(p)
    session.flush()
    return p


def _lic(
    session: Session,
    codigo: str = "LIC-001",
    estado: str = "publicada",
    dias: float = 5.0,
) -> Licitacion:
    lic = Licitacion(
        codigo=codigo,
        nombre=f"Licitación {codigo}",
        descripcion="",
        estado=estado,
        fecha_cierre=_AHORA + timedelta(days=dias),
        monto_clp=200_000.0,
        codigo_organismo="ORG-1",
    )
    session.add(lic)
    session.flush()
    return lic


def _ca(
    session: Session,
    codigo: str = "CA-001",
    estado: str = "publicada",
    dias: float = 5.0,
) -> CompraAgil:
    c = CompraAgil(
        codigo=codigo,
        nombre=f"Compra Ágil {codigo}",
        descripcion="",
        estado=estado,
        region=13,
        total_ofertas=2,
        monto_disponible_clp=150_000.0,
        fecha_cierre=_AHORA + timedelta(days=dias),
    )
    session.add(c)
    session.flush()
    return c


def _match(
    session: Session,
    perfil: PerfilBusqueda,
    codigo: str,
    *,
    fuente: str = "licitaciones",
    score: float = 75.0,
    fecha_match: datetime = _AHORA,
) -> OportunidadMatch:
    m = OportunidadMatch(
        perfil_id=perfil.id,
        fuente=fuente,
        codigo_oportunidad=codigo,
        score=score,
        razones={"keywords_hit": ["cable"], "campo_hit": "nombre"},
        fecha_match=fecha_match,
    )
    session.add(m)
    session.flush()
    return m


def _seguida(
    session: Session,
    owner: Usuario,
    codigo: str,
    *,
    fuente: str = "licitaciones",
    estado_visto: str = "publicada",
    archivada: bool = False,
) -> OportunidadSeguida:
    s = OportunidadSeguida(
        owner_id=owner.id,
        fuente=fuente,
        codigo_oportunidad=codigo,
        estado_visto=estado_visto,
        archivada=archivada,
    )
    session.add(s)
    session.flush()
    return s


def _fake_settings(limit: int = 250, app_base_url: str = "") -> Any:
    s = MagicMock()
    s.email_daily_limit = limit
    s.brevo_api_key = ""
    s.smtp_host = "smtp.test.local"
    s.smtp_port = 587
    s.smtp_user = "user"
    s.smtp_password = "pw"
    s.smtp_from = "from@test.com"
    s.app_base_url = app_base_url
    return s


class _MailSpy:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []

    def __call__(
        self,
        settings: Any,
        to_email: str,
        subject: str,
        body_text: str,
        body_html: str,
    ) -> None:
        self.sent.append((to_email, subject, body_text, body_html))


class TestResumenConsolidado:
    def test_no_envia_con_cero_nuevos_y_no_toca_ultimo_resumen(self, session: Session, monkeypatch):
        spy = _MailSpy()
        monkeypatch.setattr("app.alerts.email._smtp_send", spy)
        ultimo = _AHORA - timedelta(days=4)
        u = _user(session, ultimo_resumen_en=ultimo)

        result = enviar_resumen(session, _fake_settings(), ahora=_AHORA)

        assert result["resumenes_enviados"] == 0
        assert result["resumenes_sin_nuevos"] == 1
        assert spy.sent == []
        assert session.get(Usuario, u.id).ultimo_resumen_en == ultimo

    def test_envia_con_nuevos_y_actualiza_ultimo_resumen(self, session: Session, monkeypatch):
        spy = _MailSpy()
        monkeypatch.setattr("app.alerts.email._smtp_send", spy)
        u = _user(session, ultimo_resumen_en=_AHORA - timedelta(days=4))
        p = _perfil(session, u)
        _lic(session, "LIC-NEW")
        _match(session, p, "LIC-NEW", score=90, fecha_match=_AHORA - timedelta(hours=1))

        result = enviar_resumen(session, _fake_settings(app_base_url="https://app.test"), ahora=_AHORA)

        assert result["resumenes_enviados"] == 1
        assert session.get(Usuario, u.id).ultimo_resumen_en == _AHORA
        assert len(spy.sent) == 1
        to_email, subject, body_text, body_html = spy.sent[0]
        assert to_email == "user@test.com"
        assert "Encontramos 1 oportunidades" in subject
        assert "LIC-NEW" in body_text
        assert "https://app.test/oportunidad/licitaciones/LIC-NEW" in body_text
        assert "Fuente: Dirección ChileCompra" in body_html

    def test_top_5_por_score(self, session: Session, monkeypatch):
        spy = _MailSpy()
        monkeypatch.setattr("app.alerts.email._smtp_send", spy)
        u = _user(session, ultimo_resumen_en=None)
        p = _perfil(session, u)
        for idx, score in enumerate([10, 90, 50, 80, 70, 60], start=1):
            codigo = f"LIC-{idx}"
            _lic(session, codigo)
            _match(session, p, codigo, score=score)

        result = enviar_resumen(session, _fake_settings(), ahora=_AHORA)

        assert result["resumenes_enviados"] == 1
        body = spy.sent[0][2]
        assert "LIC-2" in body
        assert "LIC-4" in body
        assert "LIC-5" in body
        assert "LIC-6" in body
        assert "LIC-3" in body
        assert "LIC-1" not in body
        assert body.index("LIC-2") < body.index("LIC-4") < body.index("LIC-5")

    def test_dias_resumen_cero_nunca_envia(self, session: Session, monkeypatch):
        spy = _MailSpy()
        monkeypatch.setattr("app.alerts.email._smtp_send", spy)
        u = _user(session, dias_resumen=0)
        p = _perfil(session, u)
        _lic(session)
        _match(session, p, "LIC-001")

        result = enviar_resumen(session, _fake_settings(), ahora=_AHORA)

        assert result["resumenes_enviados"] == 0
        assert result["resumenes_no_elegibles"] == 1
        assert spy.sent == []

    def test_no_envia_si_aun_no_cumple_cadencia(self, session: Session, monkeypatch):
        spy = _MailSpy()
        monkeypatch.setattr("app.alerts.email._smtp_send", spy)
        u = _user(session, dias_resumen=7, ultimo_resumen_en=_AHORA - timedelta(days=3))
        p = _perfil(session, u)
        _lic(session)
        _match(session, p, "LIC-001")

        result = enviar_resumen(session, _fake_settings(), ahora=_AHORA)

        assert result["resumenes_enviados"] == 0
        assert result["resumenes_no_elegibles"] == 1


class TestInmediatasSoloSeguidas:
    def test_match_no_seguido_no_genera_alerta_de_correo(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, "LIC-SPAM", estado="cerrada")
        _match(session, p, "LIC-SPAM")

        assert detectar_cambio_estado_seguidas(session) == 0
        assert detectar_recordatorio_cierre_seguidas(session, ahora=_AHORA) == 0
        assert session.execute(select(Alerta)).scalars().all() == []

    def test_seguida_cambio_estado_genera_y_envia_inmediata(self, session: Session, monkeypatch):
        spy = _MailSpy()
        monkeypatch.setattr("app.alerts.email._smtp_send", spy)
        u = _user(session)
        _lic(session, "LIC-SEG", estado="adjudicada")
        _seguida(session, u, "LIC-SEG", estado_visto="publicada")

        assert detectar_cambio_estado_seguidas(session) == 1
        session.commit()
        result = enviar_pendientes_inmediatas(session, _fake_settings())

        assert result["enviados"] == 1
        alerta = session.execute(select(Alerta)).scalar_one()
        assert alerta.estado == "enviada"
        assert "LIC-SEG" in spy.sent[0][2]

    def test_seguida_cierre_48h_genera_alerta_idempotente(self, session: Session):
        u = _user(session)
        _lic(session, "LIC-CIERRE", dias=1)
        seguimiento = _seguida(session, u, "LIC-CIERRE")

        assert detectar_recordatorio_cierre_seguidas(session, ahora=_AHORA) == 1
        assert detectar_recordatorio_cierre_seguidas(session, ahora=_AHORA) == 0
        alertas = session.execute(
            select(Alerta).where(
                Alerta.seguimiento_id == seguimiento.id,
                Alerta.tipo == "seguimiento_cierre",
            )
        ).scalars().all()
        assert len(alertas) == 1

    def test_seguida_archivada_no_recibe_recordatorio(self, session: Session):
        u = _user(session)
        _ca(session, "CA-CIERRE", dias=1)
        _seguida(session, u, "CA-CIERRE", fuente="compras_agiles", archivada=True)

        assert detectar_recordatorio_cierre_seguidas(session, ahora=_AHORA) == 0


class TestEmailCounter:
    def test_resetea_contador_si_cambia_fecha(self, session: Session):
        old = SyncState(fuente="alerts_email", requests_usadas_hoy=7, fecha_contador="2000-01-01")
        session.add(old)
        session.commit()

        counter = EmailCounter(session, limit=10)

        assert counter.remaining() == 10
        state = session.get(SyncState, "alerts_email")
        assert state is not None
        assert state.requests_usadas_hoy == 0


class TestPlantillas:
    def test_resumen_sin_secretos_y_con_fuente(self):
        html = _jinja.get_template("resumen.html").render(
            total=1,
            url_app="/",
            items=[
                {
                    "url": "/oportunidad/licitaciones/LIC-1",
                    "nombre": "LIC-1",
                    "perfil_nombre": "Perfil",
                    "score": 80,
                    "organismo": "ORG",
                    "monto": "$1 CLP",
                    "fecha_cierre": "Sin fecha",
                }
            ],
        )
        txt = _jinja.get_template("resumen.txt").render(
            total=1,
            url_app="/",
            items=[
                {
                    "url": "/oportunidad/licitaciones/LIC-1",
                    "nombre": "LIC-1",
                    "perfil_nombre": "Perfil",
                    "score": 80,
                    "organismo": "ORG",
                    "monto": "$1 CLP",
                    "fecha_cierre": "Sin fecha",
                }
            ],
        )
        assert "Fuente: Dirección ChileCompra" in html
        assert "Fuente: Dirección ChileCompra" in txt
        for secreto in ("TICKET_SECRETO", "SECRET_KEY_SECRETO", "JOBS_TOKEN_SECRETO"):
            assert secreto not in html
            assert secreto not in txt
