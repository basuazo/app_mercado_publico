"""Envío de alertas por correo electrónico.

Reglas críticas:
- Tope diario persistido en Postgres (SyncState fuente='alerts_email').
- Proceso desechable: el contador vive en BD, no en memoria.
- Correo va ÚNICAMENTE al dueño del perfil (nunca mezclar usuarios).
- Pie de TODAS las plantillas: "Fuente: Dirección ChileCompra" (regla 8).
- smtp_host vacío → log warning, no error (entorno sin SMTP configurado).
"""

from __future__ import annotations

import smtplib
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.logging import get_logger
from app.core.settings import Settings
from app.models.enums import FrecuenciaAlerta
from app.models.tables import (
    Alerta,
    CompraAgil,
    Licitacion,
    OportunidadMatch,
    PerfilBusqueda,
    SyncState,
)

_log = get_logger(__name__)
_TZ_CHILE = ZoneInfo("America/Santiago")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
)


# ---------------------------------------------------------------------------
# Contador de correos (persistido en Postgres via SyncState)
# ---------------------------------------------------------------------------


class EmailCounter:
    """Lee y escribe el contador de correos diarios en Postgres.

    Usa SyncState(fuente='alerts_email'). Si la fecha del contador no es
    hoy en Chile, resetea a 0. Cada consume() hace commit inmediato.
    """

    _FUENTE = "alerts_email"

    def __init__(self, session: Session, limit: int) -> None:
        self._session = session
        self._limit = limit
        self._state = self._load()

    def _today_chile(self) -> str:
        return datetime.now(_TZ_CHILE).date().isoformat()

    def _load(self) -> SyncState:
        s = self._session.get(SyncState, self._FUENTE)
        if s is None:
            s = SyncState(
                fuente=self._FUENTE,
                requests_usadas_hoy=0,
                fecha_contador=self._today_chile(),
            )
            self._session.add(s)
            self._session.flush()
        elif s.fecha_contador != self._today_chile():
            s.requests_usadas_hoy = 0
            s.fecha_contador = self._today_chile()
            self._session.flush()
        return s

    def remaining(self) -> int:
        return max(0, self._limit - self._state.requests_usadas_hoy)

    def consume(self) -> None:
        self._state.requests_usadas_hoy += 1
        self._session.commit()

    def mark_tope_alcanzado(self) -> None:
        hoy = self._today_chile()
        self._state.notas = f"Tope diario ({self._limit}) alcanzado el {hoy}"
        self._session.commit()


# ---------------------------------------------------------------------------
# Helpers de formato y contexto
# ---------------------------------------------------------------------------


def _fmt_monto(monto: float | None) -> str:
    if monto is None:
        return "No informado"
    return f"${monto:,.0f} CLP"


def _fmt_fecha(dt: datetime | None) -> str:
    if dt is None:
        return "Sin fecha"
    return dt.strftime("%d/%m/%Y %H:%M")


def _url_ficha(fuente: str, codigo: str) -> str:
    if fuente == "licitaciones":
        return (
            "https://www.mercadopublico.cl/Procurement/Modules/RFB/"
            f"DetailsAcquisition.aspx?qs={codigo}"
        )
    return (
        "https://www.mercadopublico.cl/cmr/www/public/"
        f"oportContratacion_detalle.aspx?qs={codigo}"
    )


def _datos_oportunidad(session: Session, fuente: str, codigo: str) -> dict[str, Any]:
    if fuente == "licitaciones":
        lic = session.get(Licitacion, codigo)
        if lic is None:
            return {
                "nombre": codigo, "organismo": "", "region": None,
                "monto": None, "fecha_cierre": None, "estado": "",
            }
        return {
            "nombre": lic.nombre,
            "organismo": lic.codigo_organismo or "",
            "region": None,
            "monto": lic.monto_clp,
            "fecha_cierre": lic.fecha_cierre,
            "estado": lic.estado,
        }
    ca = session.get(CompraAgil, codigo)
    if ca is None:
        return {
            "nombre": codigo, "organismo": "", "region": None,
            "monto": None, "fecha_cierre": None, "estado": "",
        }
    return {
        "nombre": ca.nombre,
        "organismo": ca.organismo_nombre or "",
        "region": ca.region,
        "monto": ca.monto_disponible_clp,
        "fecha_cierre": ca.fecha_cierre,
        "estado": ca.estado,
    }


def _ctx_alerta(alerta: Alerta, session: Session) -> dict[str, Any]:
    """Construye el contexto Jinja2 para una alerta individual."""
    match = alerta.match
    perfil = match.perfil
    op = _datos_oportunidad(session, match.fuente, match.codigo_oportunidad)
    return {
        "perfil_nombre": perfil.nombre,
        "tipo_alerta": alerta.tipo,
        "nombre": op["nombre"],
        "organismo": op["organismo"],
        "region": op["region"],
        "monto": _fmt_monto(op["monto"]),
        "fecha_cierre": _fmt_fecha(op["fecha_cierre"]),
        "estado": op["estado"],
        "score": match.score,
        "razones": match.razones,
        "url": _url_ficha(match.fuente, match.codigo_oportunidad),
        "owner_email": perfil.owner.email,
    }


# ---------------------------------------------------------------------------
# Envío SMTP (mockeable en tests)
# ---------------------------------------------------------------------------


def _smtp_send(
    settings: Settings,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str,
) -> None:
    """Envía un correo vía SMTP con STARTTLS. Stub-able en tests."""
    if not settings.smtp_host:
        _log.warning("SMTP no configurado — correo a %s descartado", to_email)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to_email
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.sendmail(settings.smtp_from, [to_email], msg.as_string())


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _load_alertas_pendientes(session: Session, frecuencia: FrecuenciaAlerta) -> list[Alerta]:
    return list(
        session.execute(
            select(Alerta)
            .join(OportunidadMatch, Alerta.match_id == OportunidadMatch.id)
            .join(PerfilBusqueda, OportunidadMatch.perfil_id == PerfilBusqueda.id)
            .where(
                Alerta.estado == "pendiente",
                PerfilBusqueda.frecuencia_alerta == frecuencia.value,
            )
            .options(
                selectinload(Alerta.match)
                .selectinload(OportunidadMatch.perfil)
                .selectinload(PerfilBusqueda.owner)
            )
        ).scalars()
    )


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def enviar_pendientes_inmediatas(session: Session, settings: Settings) -> dict[str, int]:
    """Envía todas las alertas pendientes de perfiles con frecuencia 'inmediata'.

    Respeta el tope diario. Si se alcanza, las restantes quedan en 'pendiente'
    y se registra en SyncState.notas.
    """
    counter = EmailCounter(session, settings.email_daily_limit)
    alertas = _load_alertas_pendientes(session, FrecuenciaAlerta.INMEDIATA)

    enviados = pospuestos = errores = 0
    for alerta in alertas:
        if counter.remaining() <= 0:
            pospuestos += 1
            continue

        ctx = _ctx_alerta(alerta, session)
        subject = f"[MP] {ctx['tipo_alerta']}: {ctx['nombre'][:50]}"
        try:
            body_text = _jinja.get_template("alerta_inmediata.txt").render(**ctx)
            body_html = _jinja.get_template("alerta_inmediata.html").render(**ctx)
            _smtp_send(settings, ctx["owner_email"], subject, body_text, body_html)
            alerta.estado = "enviada"
            alerta.enviada_en = _now_utc()
            session.commit()
            counter.consume()
            enviados += 1
        except Exception:
            _log.error("Error enviando inmediata id=%d a %s", alerta.id, ctx["owner_email"], exc_info=True)
            session.rollback()
            errores += 1

    if pospuestos > 0:
        counter.mark_tope_alcanzado()

    _log.info("enviar_inmediatas: enviados=%d pospuestos=%d errores=%d", enviados, pospuestos, errores)
    return {"enviados": enviados, "pospuestos": pospuestos, "errores": errores}


def enviar_digest(session: Session, settings: Settings) -> dict[str, int]:
    """Agrupa alertas pendientes de perfiles 'digest' por usuario y envía un correo por usuario.

    Respeta el tope diario. Un usuario nunca recibe alertas de perfiles ajenos.
    """
    counter = EmailCounter(session, settings.email_daily_limit)
    alertas = _load_alertas_pendientes(session, FrecuenciaAlerta.DIGEST)

    # Agrupar por owner_id — NUNCA mezclar usuarios
    by_owner: dict[int, tuple[str, list[Alerta]]] = {}
    for a in alertas:
        uid = a.match.perfil.owner_id
        email = a.match.perfil.owner.email
        if uid not in by_owner:
            by_owner[uid] = (email, [])
        by_owner[uid][1].append(a)

    enviados = pospuestos = errores = 0
    for _uid, (owner_email, user_alertas) in by_owner.items():
        if counter.remaining() <= 0:
            pospuestos += len(user_alertas)
            continue

        items = [_ctx_alerta(a, session) for a in user_alertas]
        n = len(items)
        subject = f"[MP] Resumen: {n} oportunidad{'es' if n != 1 else ''}"
        try:
            body_text = _jinja.get_template("digest.txt").render(items=items)
            body_html = _jinja.get_template("digest.html").render(items=items)
            _smtp_send(settings, owner_email, subject, body_text, body_html)
            ahora = _now_utc()
            for a in user_alertas:
                a.estado = "enviada"
                a.enviada_en = ahora
            session.commit()
            counter.consume()
            enviados += 1
        except Exception:
            _log.error("Error enviando digest a %s", owner_email, exc_info=True)
            session.rollback()
            errores += 1

    if pospuestos > 0:
        counter.mark_tope_alcanzado()

    _log.info("enviar_digest: enviados=%d pospuestos=%d errores=%d", enviados, pospuestos, errores)
    return {"digests_enviados": enviados, "digests_pospuestos": pospuestos, "errores_digest": errores}
