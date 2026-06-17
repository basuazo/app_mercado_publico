"""Tests F5 — alertas: detector, email, deduplicación, digest, tope diario."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.alerts.detector import (
    detectar_cambio_estado,
    detectar_nuevo_match,
    detectar_recordatorios,
)
from app.alerts.email import (
    EmailCounter,
    _ctx_alerta,
    _jinja,
    enviar_digest,
    enviar_pendientes_inmediatas,
)
from app.models.enums import FrecuenciaAlerta
from app.models.tables import (
    Alerta,
    CompraAgil,
    Licitacion,
    OportunidadMatch,
    PerfilBusqueda,
    SyncState,
    Usuario,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


def _user(session: Session, email: str = "user@test.com") -> Usuario:
    u = Usuario(email=email, password_hash=_PW_HASH, activo=True)
    session.add(u)
    session.flush()
    return u


def _perfil(
    session: Session,
    owner: Usuario,
    frecuencia: FrecuenciaAlerta = FrecuenciaAlerta.INMEDIATA,
) -> PerfilBusqueda:
    p = PerfilBusqueda(
        owner_id=owner.id,
        nombre="Test Perfil",
        keywords=["cable"],
        keywords_excluir=[],
        regiones=[],
        fuentes=["licitaciones"],
        frecuencia_alerta=frecuencia.value,
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
    fuente: str = "licitaciones",
) -> OportunidadMatch:
    m = OportunidadMatch(
        perfil_id=perfil.id,
        fuente=fuente,
        codigo_oportunidad=codigo,
        score=75.0,
        razones={"keywords_hit": ["cable"], "campo_hit": "nombre"},
        fecha_match=_AHORA,
    )
    session.add(m)
    session.flush()
    return m


def _fake_settings(limit: int = 250) -> Any:
    s = MagicMock()
    s.email_daily_limit = limit
    s.brevo_api_key = ""  # desactivado por defecto; tests Brevo lo sobreescriben
    s.smtp_host = "smtp.test.local"
    s.smtp_port = 587
    s.smtp_user = "user"
    s.smtp_password = "pw"
    s.smtp_from = "from@test.com"
    s.mp_ticket = "TICKET_SECRETO"
    s.secret_key = "SECRET_KEY_SECRETO"
    s.jobs_token = "JOBS_TOKEN_SECRETO"
    return s


# ---------------------------------------------------------------------------
# 1. Tests de detector — nuevo_match
# ---------------------------------------------------------------------------


class TestDetectarNuevoMatch:
    def test_crea_alerta_para_match_nuevo(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session)
        m = _match(session, p, "LIC-001")
        creados = detectar_nuevo_match(session)
        assert creados == 1
        alertas = list(session.execute(
            select(Alerta).where(Alerta.match_id == m.id, Alerta.tipo == "nuevo_match")
        ).scalars())
        assert len(alertas) == 1
        assert alertas[0].estado == "pendiente"

    def test_no_duplica_si_ya_existe_pendiente(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session)
        m = _match(session, p, "LIC-001")
        # Primera detección
        detectar_nuevo_match(session)
        # Segunda detección — no debe crear otra alerta
        creados2 = detectar_nuevo_match(session)
        assert creados2 == 0
        alertas = list(session.execute(
            select(Alerta).where(Alerta.match_id == m.id, Alerta.tipo == "nuevo_match")
        ).scalars())
        assert len(alertas) == 1

    def test_no_duplica_despues_de_enviada(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session)
        m = _match(session, p, "LIC-001")
        # Simular alerta ya enviada
        session.add(Alerta(match_id=m.id, tipo="nuevo_match", estado="enviada"))
        session.flush()
        creados = detectar_nuevo_match(session)
        assert creados == 0

    def test_multiples_matches_distintos(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, "LIC-A")
        _lic(session, "LIC-B")
        _match(session, p, "LIC-A")
        _match(session, p, "LIC-B")
        creados = detectar_nuevo_match(session)
        assert creados == 2


# ---------------------------------------------------------------------------
# 2. Tests de detector — cambio_estado
# ---------------------------------------------------------------------------


class TestDetectarCambioEstado:
    def test_detecta_licitacion_cerrada(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, "LIC-CERR", estado="cerrada")
        m = _match(session, p, "LIC-CERR")
        creados = detectar_cambio_estado(session)
        assert creados == 1
        a = session.execute(
            select(Alerta).where(Alerta.match_id == m.id)
        ).scalar_one()
        assert a.tipo == "cambio_estado:cerrada"

    def test_no_detecta_publicada(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, estado="publicada")
        _match(session, p, "LIC-001")
        assert detectar_cambio_estado(session) == 0

    def test_no_duplica_cambio_mismo_estado(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, "LIC-ADJ", estado="adjudicada")
        _match(session, p, "LIC-ADJ")
        detectar_cambio_estado(session)
        creados2 = detectar_cambio_estado(session)
        assert creados2 == 0

    def test_detecta_ca_cancelada(self, session: Session):
        u = _user(session)
        p = PerfilBusqueda(
            owner_id=u.id, nombre="CA Test",
            keywords=["cable"], keywords_excluir=[], regiones=[],
            fuentes=["compras_agiles"],
            frecuencia_alerta=FrecuenciaAlerta.INMEDIATA.value, activo=True,
        )
        session.add(p)
        session.flush()
        _ca(session, "CA-CANC", estado="cancelada")
        m = _match(session, p, "CA-CANC", fuente="compras_agiles")
        creados = detectar_cambio_estado(session)
        assert creados == 1
        a = session.execute(
            select(Alerta).where(Alerta.match_id == m.id)
        ).scalar_one()
        assert a.tipo == "cambio_estado:cancelada"


# ---------------------------------------------------------------------------
# 3. Tests de detector — recordatorio cierre
# ---------------------------------------------------------------------------


class TestDetectarRecordatorios:
    def test_crea_recordatorio_en_30h(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        # Cierre en 30 horas → dentro de ventana 48h
        _lic(session, "LIC-CLOSE", dias=30.0 / 24.0)
        m = _match(session, p, "LIC-CLOSE")
        creados = detectar_recordatorios(session, ahora=_AHORA)
        assert creados == 1
        a = session.execute(
            select(Alerta).where(Alerta.match_id == m.id)
        ).scalar_one()
        assert a.tipo == "recordatorio_cierre"

    def test_no_crea_si_cierre_en_72h(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, "LIC-LEJOS", dias=3.0)
        _match(session, p, "LIC-LEJOS")
        creados = detectar_recordatorios(session, ahora=_AHORA)
        assert creados == 0

    def test_no_duplica_recordatorio(self, session: Session):
        u = _user(session)
        p = _perfil(session, u)
        _lic(session, "LIC-CLOSE2", dias=30.0 / 24.0)
        m = _match(session, p, "LIC-CLOSE2")
        detectar_recordatorios(session, ahora=_AHORA)
        creados2 = detectar_recordatorios(session, ahora=_AHORA)
        assert creados2 == 0
        alertas = list(session.execute(
            select(Alerta).where(Alerta.match_id == m.id, Alerta.tipo == "recordatorio_cierre")
        ).scalars())
        assert len(alertas) == 1

    def test_recordatorio_ca_en_48h(self, session: Session):
        u = _user(session)
        p = PerfilBusqueda(
            owner_id=u.id, nombre="P CA",
            keywords=["cable"], keywords_excluir=[], regiones=[],
            fuentes=["compras_agiles"],
            frecuencia_alerta=FrecuenciaAlerta.INMEDIATA.value, activo=True,
        )
        session.add(p)
        session.flush()
        _ca(session, "CA-CLOSE", dias=1.0)
        m = _match(session, p, "CA-CLOSE", fuente="compras_agiles")
        creados = detectar_recordatorios(session, ahora=_AHORA)
        assert creados == 1
        a = session.execute(
            select(Alerta).where(Alerta.match_id == m.id)
        ).scalar_one()
        assert a.tipo == "recordatorio_cierre"


# ---------------------------------------------------------------------------
# 4. Tests de envío — inmediatas
# ---------------------------------------------------------------------------


class TestEnviarInmediatas:
    def _setup_alerta_inmediata(self, session: Session) -> tuple[OportunidadMatch, Alerta]:
        u = _user(session)
        p = _perfil(session, u, FrecuenciaAlerta.INMEDIATA)
        _lic(session)
        m = _match(session, p, "LIC-001")
        a = Alerta(match_id=m.id, tipo="nuevo_match", estado="pendiente")
        session.add(a)
        session.commit()
        return m, a

    def test_envia_y_marca_enviada(self, session: Session, monkeypatch):
        m, a = self._setup_alerta_inmediata(session)
        enviados_a: list[tuple[str, str, str]] = []

        def fake_smtp(settings, to, subj, txt, html):
            enviados_a.append((to, subj, txt))

        monkeypatch.setattr("app.alerts.email._smtp_send", fake_smtp)
        result = enviar_pendientes_inmediatas(session, _fake_settings())

        assert result["enviados"] == 1
        assert result["pospuestos"] == 0
        assert len(enviados_a) == 1
        assert enviados_a[0][0] == "user@test.com"
        session.expire_all()
        alerta_db = session.get(Alerta, a.id)
        assert alerta_db is not None
        assert alerta_db.estado == "enviada"

    def test_usuario_inactivo_no_recibe_inmediata(self, session: Session, monkeypatch):
        u = _user(session)
        p = _perfil(session, u, FrecuenciaAlerta.INMEDIATA)
        _lic(session)
        m = _match(session, p, "LIC-001")
        session.add(Alerta(match_id=m.id, tipo="nuevo_match", estado="pendiente"))
        u.activo = False  # desactivar usuario
        session.commit()

        enviados_a: list[str] = []
        monkeypatch.setattr("app.alerts.email._smtp_send", lambda s, to, *a: enviados_a.append(to))
        result = enviar_pendientes_inmediatas(session, _fake_settings())
        assert result["enviados"] == 0
        assert enviados_a == []

    def test_digest_no_se_envia_en_inmediatas(self, session: Session, monkeypatch):
        u = _user(session)
        p = _perfil(session, u, FrecuenciaAlerta.DIGEST)  # digest, no inmediata
        _lic(session)
        m = _match(session, p, "LIC-001")
        session.add(Alerta(match_id=m.id, tipo="nuevo_match", estado="pendiente"))
        session.commit()

        enviados_a: list[str] = []
        monkeypatch.setattr("app.alerts.email._smtp_send", lambda *a: enviados_a.append(a[1]))
        result = enviar_pendientes_inmediatas(session, _fake_settings())
        assert result["enviados"] == 0
        assert enviados_a == []

    def test_tope_diario_pospone_resto(self, session: Session, monkeypatch):
        u = _user(session)
        p = _perfil(session, u, FrecuenciaAlerta.INMEDIATA)
        for i in range(3):
            _lic(session, f"LIC-{i:03d}")
            m = _match(session, p, f"LIC-{i:03d}")
            session.add(Alerta(match_id=m.id, tipo="nuevo_match", estado="pendiente"))
        session.commit()

        enviados_a: list[str] = []
        monkeypatch.setattr("app.alerts.email._smtp_send", lambda *a: enviados_a.append(a[1]))
        result = enviar_pendientes_inmediatas(session, _fake_settings(limit=2))

        assert result["enviados"] == 2
        assert result["pospuestos"] == 1
        assert len(enviados_a) == 2
        # SyncState debe reflejar tope
        s = session.get(SyncState, "alerts_email")
        assert s is not None
        assert s.notas is not None
        assert "Tope" in s.notas

    def test_alertas_pendientes_quedan_pendientes_tras_tope(self, session: Session, monkeypatch):
        u = _user(session)
        p = _perfil(session, u, FrecuenciaAlerta.INMEDIATA)
        ids_alertas = []
        for i in range(3):
            _lic(session, f"LIC-X{i}")
            m = _match(session, p, f"LIC-X{i}")
            a = Alerta(match_id=m.id, tipo="nuevo_match", estado="pendiente")
            session.add(a)
            session.flush()
            ids_alertas.append(a.id)
        session.commit()

        monkeypatch.setattr("app.alerts.email._smtp_send", lambda *a: None)
        enviar_pendientes_inmediatas(session, _fake_settings(limit=1))

        session.expire_all()
        estados = [session.get(Alerta, aid).estado for aid in ids_alertas]  # type: ignore[union-attr]
        assert estados.count("enviada") == 1
        assert estados.count("pendiente") == 2

    def test_fallo_incrementa_intentos(self, session: Session, monkeypatch):
        """Un fallo de envío incrementa intentos_envio y deja la alerta pendiente."""
        _, a = self._setup_alerta_inmediata(session)
        monkeypatch.setattr(
            "app.alerts.email._smtp_send", lambda *args: (_ for _ in ()).throw(RuntimeError("smtp fail"))
        )
        enviar_pendientes_inmediatas(session, _fake_settings())
        session.expire_all()
        alerta_db = session.get(Alerta, a.id)
        assert alerta_db is not None
        assert alerta_db.intentos_envio == 1
        assert alerta_db.estado == "pendiente"

    def test_tres_fallos_marca_fallida(self, session: Session, monkeypatch):
        """Tras max_intentos fallos consecutivos la alerta pasa a estado 'fallida'."""
        _, a = self._setup_alerta_inmediata(session)
        a.max_intentos = 3
        session.commit()

        monkeypatch.setattr(
            "app.alerts.email._smtp_send", lambda *args: (_ for _ in ()).throw(RuntimeError("smtp fail"))
        )
        # Tres ciclos de envío simulando re-ejecuciones del job
        for _ in range(3):
            enviar_pendientes_inmediatas(session, _fake_settings())
            session.expire_all()

        alerta_db = session.get(Alerta, a.id)
        assert alerta_db is not None
        assert alerta_db.estado == "fallida"
        assert alerta_db.intentos_envio == 3

    def test_alerta_fallida_no_se_reintenta(self, session: Session, monkeypatch):
        """Una alerta en estado 'fallida' no se carga en el siguiente ciclo."""
        _, a = self._setup_alerta_inmediata(session)
        a.estado = "fallida"
        a.intentos_envio = 3
        session.commit()

        llamadas: list[str] = []
        monkeypatch.setattr("app.alerts.email._smtp_send", lambda s, to, *args: llamadas.append(to))
        enviar_pendientes_inmediatas(session, _fake_settings())
        assert llamadas == []


# ---------------------------------------------------------------------------
# 5. Tests de envío — digest
# ---------------------------------------------------------------------------


class TestEnviarDigest:
    def _setup_digest(self, session: Session) -> tuple[Usuario, Usuario]:
        ua = _user(session, "user_a@test.com")
        ub = _user(session, "user_b@test.com")
        pa = _perfil(session, ua, FrecuenciaAlerta.DIGEST)
        pb = _perfil(session, ub, FrecuenciaAlerta.DIGEST)

        # 3 alertas para usuario A
        for i in range(3):
            _lic(session, f"LIC-A{i}")
            m = _match(session, pa, f"LIC-A{i}")
            session.add(Alerta(match_id=m.id, tipo="nuevo_match", estado="pendiente"))

        # 2 alertas para usuario B
        for i in range(2):
            _lic(session, f"LIC-B{i}")
            m = _match(session, pb, f"LIC-B{i}")
            session.add(Alerta(match_id=m.id, tipo="nuevo_match", estado="pendiente"))

        session.commit()
        return ua, ub

    def test_agrupa_por_usuario_dos_correos(self, session: Session, monkeypatch):
        self._setup_digest(session)
        enviados: list[str] = []
        monkeypatch.setattr("app.alerts.email._smtp_send", lambda s, to, *a: enviados.append(to))
        result = enviar_digest(session, _fake_settings())

        assert result["digests_enviados"] == 2
        assert set(enviados) == {"user_a@test.com", "user_b@test.com"}

    def test_ownership_isolation_destinatario(self, session: Session, monkeypatch):
        self._setup_digest(session)
        sends: list[tuple[str, str]] = []

        def fake_smtp(settings, to, subj, txt, html):
            sends.append((to, txt))

        monkeypatch.setattr("app.alerts.email._smtp_send", fake_smtp)
        enviar_digest(session, _fake_settings())

        # Verificar que cada correo solo menciona oportunidades del destinatario
        for to_email, body in sends:
            user_prefix = "A" if "user_a" in to_email else "B"
            other_prefix = "B" if user_prefix == "A" else "A"
            # El body debe tener LIC-{prefix}X y no LIC-{other_prefix}X
            assert f"LIC-{user_prefix}" in body
            assert f"LIC-{other_prefix}" not in body

    def test_tope_pospone_digests(self, session: Session, monkeypatch):
        self._setup_digest(session)
        enviados: list[str] = []
        monkeypatch.setattr("app.alerts.email._smtp_send", lambda s, to, *a: enviados.append(to))
        result = enviar_digest(session, _fake_settings(limit=1))

        assert result["digests_enviados"] == 1
        assert result["digests_pospuestos"] > 0

        # SyncState debe registrar tope
        s = session.get(SyncState, "alerts_email")
        assert s is not None
        assert s.notas is not None

    def test_usuario_inactivo_no_recibe_digest(self, session: Session, monkeypatch):
        ua, ub = self._setup_digest(session)
        ua.activo = False  # desactivar usuario A
        session.commit()

        enviados: list[str] = []
        monkeypatch.setattr("app.alerts.email._smtp_send", lambda s, to, *a: enviados.append(to))
        result = enviar_digest(session, _fake_settings())

        assert result["digests_enviados"] == 1  # solo usuario B
        assert enviados == ["user_b@test.com"]

    def test_alertas_pendientes_tras_digest_se_marcan_enviadas(self, session: Session, monkeypatch):
        ua, _ = self._setup_digest(session)
        monkeypatch.setattr("app.alerts.email._smtp_send", lambda *a: None)
        enviar_digest(session, _fake_settings())

        # Todas las alertas de user A deben estar enviadas
        pa = session.execute(
            select(PerfilBusqueda).where(PerfilBusqueda.owner_id == ua.id)
        ).scalar_one()
        for m in pa.matches:
            for a in m.alertas:
                assert a.estado == "enviada"


# ---------------------------------------------------------------------------
# Tests Brevo REST API
# ---------------------------------------------------------------------------


class TestBrevoSend:
    """Verifica que _brevo_send llama al endpoint correcto con el header api-key."""

    def _settings_brevo(self, limit: int = 250) -> Any:
        s = _fake_settings(limit)
        s.brevo_api_key = "test-brevo-key-abc123"
        s.smtp_from = "from@test.com"
        return s

    @respx.mock
    def test_brevo_llama_endpoint_correcto(self):
        """Verifica endpoint, header api-key y payload de Brevo."""
        from app.alerts.email import _brevo_send

        route = respx.post("https://api.brevo.com/v3/smtp/email").mock(
            return_value=httpx.Response(201, json={"messageId": "<abc@brevo>"})
        )
        s = self._settings_brevo()
        _brevo_send(s, "dest@ejemplo.cl", "Asunto", "texto", "<p>html</p>")

        assert route.called
        req = route.calls.last.request
        assert req.headers["api-key"] == "test-brevo-key-abc123"
        import json
        body = json.loads(req.content)
        assert body["to"] == [{"email": "dest@ejemplo.cl"}]
        assert body["sender"] == {"email": "from@test.com"}
        assert body["subject"] == "Asunto"
        assert body["htmlContent"] == "<p>html</p>"
        assert body["textContent"] == "texto"

    @respx.mock
    def test_brevo_error_5xx_levanta_excepcion(self):
        """Un 5xx de Brevo debe propagarse como excepción."""
        from app.alerts.email import _brevo_send

        respx.post("https://api.brevo.com/v3/smtp/email").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        s = self._settings_brevo()
        with pytest.raises(httpx.HTTPStatusError):
            _brevo_send(s, "dest@ejemplo.cl", "Asunto", "texto", "<p>html</p>")

    @respx.mock
    def test_smtp_send_usa_brevo_cuando_api_key_configurada(self):
        """_smtp_send delega a Brevo si brevo_api_key está presente."""
        from app.alerts.email import _smtp_send as smtp_send_fn

        route = respx.post("https://api.brevo.com/v3/smtp/email").mock(
            return_value=httpx.Response(201, json={"messageId": "<x>"})
        )
        s = self._settings_brevo()
        smtp_send_fn(s, "dest@ejemplo.cl", "Asunto", "texto", "<p>html</p>")
        assert route.called

    def test_smtp_send_fallback_sin_brevo(self, monkeypatch):
        """Sin brevo_api_key, _smtp_send usa la ruta SMTP."""
        from app.alerts.email import _smtp_send as smtp_send_fn

        llamadas: list[str] = []
        monkeypatch.setattr(
            "app.alerts.email._smtp_send_raw",
            lambda s, to, *a: llamadas.append(to),
        )
        s = _fake_settings()
        s.brevo_api_key = ""
        smtp_send_fn(s, "dest@ejemplo.cl", "Asunto", "texto", "<p>html</p>")
        assert llamadas == ["dest@ejemplo.cl"]


# ---------------------------------------------------------------------------
# 6. Test de plantilla sin secretos
# ---------------------------------------------------------------------------


class TestPlantillaSinSecretos:
    def test_inmediata_sin_secretos(self, session: Session):
        u = _user(session)
        p = _perfil(session, u, FrecuenciaAlerta.INMEDIATA)
        _lic(session)
        m = _match(session, p, "LIC-001")
        a = Alerta(match_id=m.id, tipo="nuevo_match", estado="pendiente")
        session.add(a)
        session.flush()

        # Recargar con relaciones
        session.expire_all()
        from sqlalchemy.orm import selectinload

        a_loaded = session.execute(
            select(Alerta)
            .where(Alerta.id == a.id)
            .options(
                selectinload(Alerta.match)
                .selectinload(OportunidadMatch.perfil)
                .selectinload(PerfilBusqueda.owner)
            )
        ).scalar_one()

        settings = _fake_settings()
        ctx = _ctx_alerta(a_loaded, session)

        html = _jinja.get_template("alerta_inmediata.html").render(**ctx)
        txt = _jinja.get_template("alerta_inmediata.txt").render(**ctx)

        for secreto in (settings.mp_ticket, settings.secret_key, settings.jobs_token):
            assert secreto not in html
            assert secreto not in txt

    def test_digest_sin_secretos(self, session: Session):
        u = _user(session)
        p = _perfil(session, u, FrecuenciaAlerta.DIGEST)
        _lic(session)
        m = _match(session, p, "LIC-001")
        a = Alerta(match_id=m.id, tipo="nuevo_match", estado="pendiente")
        session.add(a)
        session.flush()

        session.expire_all()
        from sqlalchemy.orm import selectinload

        a_loaded = session.execute(
            select(Alerta)
            .where(Alerta.id == a.id)
            .options(
                selectinload(Alerta.match)
                .selectinload(OportunidadMatch.perfil)
                .selectinload(PerfilBusqueda.owner)
            )
        ).scalar_one()

        settings = _fake_settings()
        items = [_ctx_alerta(a_loaded, session)]
        html = _jinja.get_template("digest.html").render(items=items)
        txt = _jinja.get_template("digest.txt").render(items=items)

        for secreto in (settings.mp_ticket, settings.secret_key, settings.jobs_token):
            assert secreto not in html
            assert secreto not in txt

    def test_pie_fuente_presente(self, session: Session):
        u = _user(session)
        p = _perfil(session, u, FrecuenciaAlerta.INMEDIATA)
        _lic(session)
        m = _match(session, p, "LIC-001")
        a = Alerta(match_id=m.id, tipo="nuevo_match", estado="pendiente")
        session.add(a)
        session.flush()

        session.expire_all()
        from sqlalchemy.orm import selectinload

        a_loaded = session.execute(
            select(Alerta)
            .where(Alerta.id == a.id)
            .options(
                selectinload(Alerta.match)
                .selectinload(OportunidadMatch.perfil)
                .selectinload(PerfilBusqueda.owner)
            )
        ).scalar_one()

        ctx = _ctx_alerta(a_loaded, session)
        for tpl in ("alerta_inmediata.html", "alerta_inmediata.txt", "digest.html", "digest.txt"):
            if "digest" in tpl:
                rendered = _jinja.get_template(tpl).render(items=[ctx])
            else:
                rendered = _jinja.get_template(tpl).render(**ctx)
            assert "Fuente: Dirección ChileCompra" in rendered


# ---------------------------------------------------------------------------
# 7. Test contador email
# ---------------------------------------------------------------------------


class TestEmailCounter:
    def test_contador_reset_por_dia(self, session: Session):
        from freezegun import freeze_time

        with freeze_time("2026-06-13 10:00:00"):
            c = EmailCounter(session, 250)
            c.consume()  # hoy = 1

        with freeze_time("2026-06-14 10:00:00"):
            c2 = EmailCounter(session, 250)
            assert c2.remaining() == 250  # nuevo día → reseteo

    def test_remaining_decrementa(self, session: Session):
        c = EmailCounter(session, 5)
        assert c.remaining() == 5
        c.consume()
        c2 = EmailCounter(session, 5)
        assert c2.remaining() == 4
