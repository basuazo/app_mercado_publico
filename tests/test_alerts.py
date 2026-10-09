"""Tests F-notificaciones — resumen consolidado + inmediatas solo para seguidas."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.alerts.detector import (
    detectar_cambio_estado_seguidas,
    detectar_recordatorio_cierre_seguidas,
)
from app.alerts.email import (
    EmailCounter,
    _items_resumen,
    _jinja,
    enviar_pendientes_inmediatas,
    enviar_resumen,
)
from app.matching.engine import _upsert_match
from app.models.tables import (
    Alerta,
    CompraAgil,
    Licitacion,
    MatchFeedback,
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
    s.feed_min_score_default = 40
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

    def test_resumen_no_recuenta_match_viejo_rescoreado(self, session: Session, monkeypatch):
        spy = _MailSpy()
        monkeypatch.setattr("app.alerts.email._smtp_send", spy)
        ultimo = _AHORA - timedelta(days=4)
        fecha_original = _AHORA - timedelta(days=5)
        u = _user(session, ultimo_resumen_en=ultimo)
        p = _perfil(session, u)
        _lic(session, "LIC-VIEJA")
        _match(session, p, "LIC-VIEJA", score=40, fecha_match=fecha_original)

        es_nuevo = _upsert_match(
            session,
            p.id,
            "licitaciones",
            "LIC-VIEJA",
            95,
            {"rescore": True},
            _AHORA - timedelta(hours=1),
        )
        session.flush()

        result = enviar_resumen(session, _fake_settings(), ahora=_AHORA)
        match = session.execute(
            select(OportunidadMatch).where(OportunidadMatch.codigo_oportunidad == "LIC-VIEJA")
        ).scalar_one()

        assert es_nuevo is False
        assert match.score == 95
        assert match.fecha_match == fecha_original
        assert result["resumenes_enviados"] == 0
        assert result["resumenes_sin_nuevos"] == 1
        assert spy.sent == []

    def test_match_nuevo_pero_ya_vencido_no_entra_al_correo(self, session: Session, monkeypatch):
        """F-vigencia: una oportunidad que cerró entre el match y el envío del
        resumen no se anuncia — misma definición de "vigente" que el feed. Sin
        nada vigente, cuenta como sin_nuevos y NO se mueve `ultimo_resumen_en`
        (mismo comportamiento de hoy cuando no hay matches nuevos)."""
        spy = _MailSpy()
        monkeypatch.setattr("app.alerts.email._smtp_send", spy)
        ultimo = _AHORA - timedelta(days=4)
        u = _user(session, ultimo_resumen_en=ultimo)
        p = _perfil(session, u)
        _lic(session, "LIC-VENCIDA", dias=-1)
        _match(session, p, "LIC-VENCIDA", score=90, fecha_match=_AHORA - timedelta(hours=1))

        result = enviar_resumen(session, _fake_settings(), ahora=_AHORA)

        assert result["resumenes_enviados"] == 0
        assert result["resumenes_sin_nuevos"] == 1
        assert spy.sent == []
        assert session.get(Usuario, u.id).ultimo_resumen_en == ultimo


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


class TestResumenF_match1:
    """F-match-1: piso de relevancia, sin descartadas/guardadas, sin duplicados,
    sección "Cierra en ≤ 48 h" y carga en lote."""

    def _enviar(self, session: Session, monkeypatch) -> tuple[dict[str, int], _MailSpy]:
        spy = _MailSpy()
        monkeypatch.setattr("app.alerts.email._smtp_send", spy)
        return enviar_resumen(session, _fake_settings(), ahora=_AHORA), spy

    def test_respeta_el_piso_de_relevancia(self, session: Session, monkeypatch):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, "LIC-ALTA")
        _lic(session, "LIC-BAJA")
        _match(session, p, "LIC-ALTA", score=45)
        _match(session, p, "LIC-BAJA", score=39)

        result, spy = self._enviar(session, monkeypatch)

        assert result["resumenes_enviados"] == 1
        assert "LIC-ALTA" in spy.sent[0][2]
        assert "LIC-BAJA" not in spy.sent[0][2]
        assert "Encontramos 1 oportunidades" in spy.sent[0][1]

    def test_solo_bajo_el_piso_no_envia(self, session: Session, monkeypatch):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, "LIC-BAJA")
        _match(session, p, "LIC-BAJA", score=10)

        result, spy = self._enviar(session, monkeypatch)

        assert result["resumenes_sin_nuevos"] == 1
        assert spy.sent == []

    def test_excluye_descartadas_y_guardadas(self, session: Session, monkeypatch):
        u = _user(session)
        p = _perfil(session, u)
        for cod in ("LIC-OK", "LIC-DESC", "LIC-GUARD", "LIC-ARCH"):
            _lic(session, cod)
            _match(session, p, cod, score=70)
        session.add(
            MatchFeedback(usuario_id=u.id, fuente="licitaciones", codigo_oportunidad="LIC-DESC", valor="descarte")
        )
        _seguida(session, u, "LIC-GUARD")
        _seguida(session, u, "LIC-ARCH", archivada=True)
        session.flush()

        _, spy = self._enviar(session, monkeypatch)

        cuerpo = spy.sent[0][2]
        assert "LIC-OK" in cuerpo
        assert "LIC-DESC" not in cuerpo and "LIC-GUARD" not in cuerpo and "LIC-ARCH" not in cuerpo
        assert "Encontramos 1 oportunidades" in spy.sent[0][1]

    def test_una_oportunidad_que_calza_con_dos_perfiles_sale_una_vez(self, session: Session, monkeypatch):
        u = _user(session)
        p1 = _perfil(session, u)
        p2 = PerfilBusqueda(owner_id=u.id, nombre="Otro Perfil", keywords=["x"], fuentes=["licitaciones"], activo=True)
        session.add(p2)
        session.flush()
        _lic(session, "LIC-DUP")
        _match(session, p1, "LIC-DUP", score=60)
        _match(session, p2, "LIC-DUP", score=80)

        _, spy = self._enviar(session, monkeypatch)

        cuerpo = spy.sent[0][2]
        assert cuerpo.count("Ver ficha:") == 1
        assert "Match: 80" in cuerpo
        assert "Test Perfil" in cuerpo and "Otro Perfil" in cuerpo
        assert "Encontramos 1 oportunidades" in spy.sent[0][1]

    def test_orden_por_relevancia_y_luego_cierre_mas_proximo(self, session: Session, monkeypatch):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, "LIC-A", dias=20)
        _lic(session, "LIC-B-LEJOS", dias=9)
        _lic(session, "LIC-B-CERCA", dias=4)
        _match(session, p, "LIC-A", score=90)
        _match(session, p, "LIC-B-LEJOS", score=60)
        _match(session, p, "LIC-B-CERCA", score=60)

        _, spy = self._enviar(session, monkeypatch)

        cuerpo = spy.sent[0][2]
        assert cuerpo.index("LIC-A") < cuerpo.index("LIC-B-CERCA") < cuerpo.index("LIC-B-LEJOS")

    def test_seccion_cierra_en_48_horas(self, session: Session, monkeypatch):
        u = _user(session)
        p = _perfil(session, u)
        # Seis nuevas que llenan el top 5; la sexta (menor relevancia) cierra en 30 h.
        for i, score in enumerate([95, 94, 93, 92, 91], start=1):
            _lic(session, f"LIC-TOP{i}", dias=20)
            _match(session, p, f"LIC-TOP{i}", score=score)
        _lic(session, "LIC-PRONTO", dias=30 / 24)  # cierra en 30 h, relevancia 65
        _match(session, p, "LIC-PRONTO", score=65)
        _lic(session, "LIC-PRONTO-BAJA", dias=10 / 24)  # cierra pronto pero relevancia 55 < 60
        _match(session, p, "LIC-PRONTO-BAJA", score=55)
        _lic(session, "LIC-LEJOS", dias=3)  # cierra en 72 h: fuera de la ventana
        _match(session, p, "LIC-LEJOS", score=99)
        _lic(session, "LIC-GUARDADA", dias=1)
        _match(session, p, "LIC-GUARDADA", score=99)
        _seguida(session, u, "LIC-GUARDADA")

        _, spy = self._enviar(session, monkeypatch)

        cuerpo = spy.sent[0][2]
        assert "Cierra en ≤ 48 h" in cuerpo
        seccion = cuerpo[cuerpo.index("Cierra en ≤ 48 h"):]
        assert "LIC-PRONTO" in seccion
        assert "LIC-PRONTO-BAJA" not in cuerpo.split("Cierra en ≤ 48 h")[1]
        assert "LIC-LEJOS" not in seccion and "LIC-GUARDADA" not in cuerpo
        # El top ya incluye a TOP1-5, que no se repiten en la sección.
        assert not any(f"LIC-TOP{i}" in seccion for i in range(1, 6))

    def test_sin_oportunidades_que_cierren_pronto_no_hay_seccion(self, session: Session, monkeypatch):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, "LIC-1", dias=10)
        _match(session, p, "LIC-1", score=80)

        _, spy = self._enviar(session, monkeypatch)

        assert "Cierra en ≤ 48 h" not in spy.sent[0][2]
        assert "Cierra en ≤ 48 h" not in spy.sent[0][3]

    def test_no_hace_una_query_por_match(self, session: Session, sqlite_engine):
        u = _user(session)
        p = _perfil(session, u)
        for i in range(12):
            _lic(session, f"LIC-Q{i}")
            _match(session, p, f"LIC-Q{i}", score=70)
        for i in range(4):
            _ca(session, f"CA-Q{i}")
            _match(session, p, f"CA-Q{i}", fuente="compras_agiles", score=70)
        session.commit()
        sentencias: list[str] = []
        event.listen(sqlite_engine, "before_cursor_execute", lambda *a: sentencias.append(a[2]))

        items = _items_resumen(session, u, _AHORA, min_score=40)

        assert len(items) == 16
        # (releer el usuario tras el commit) + matches + perfiles + licitaciones + compras
        # ágiles: constante, no una por match (16).
        assert len(sentencias) <= 5
