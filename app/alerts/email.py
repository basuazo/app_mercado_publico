"""Envío de correos de Mercado Público.

Reglas críticas:
- Tope diario persistido en Postgres (SyncState fuente='alerts_email').
- Solo las oportunidades seguidas generan correos inmediatos.
- Los matches no seguidos se notifican por resumen consolidado por usuario.
- Pie de TODAS las plantillas: "Fuente: Dirección ChileCompra" (regla 8).
- smtp_host vacío → log warning, no error (entorno sin SMTP configurado).
"""

from __future__ import annotations

import smtplib
from datetime import UTC, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.logging import get_logger
from app.core.settings import Settings
from app.models.enums import EstadoAlerta
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

_log = get_logger(__name__)
_TZ_CHILE = ZoneInfo("America/Santiago")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
)


class EmailCounter:
    """Contador diario de correos persistido en Postgres."""

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


def _fmt_monto(monto: float | None) -> str:
    if monto is None:
        return "No informado"
    return f"${monto:,.0f} CLP"


def _fmt_fecha(dt: datetime | None) -> str:
    if dt is None:
        return "Sin fecha"
    return dt.strftime("%d/%m/%Y %H:%M")


def _datos_oportunidad(session: Session, fuente: str, codigo: str) -> dict[str, Any]:
    if fuente == "licitaciones":
        lic = session.get(Licitacion, codigo)
        if lic is None:
            return {
                "nombre": codigo,
                "organismo": "",
                "region": None,
                "monto": None,
                "fecha_cierre": None,
                "estado": "",
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
            "nombre": codigo,
            "organismo": "",
            "region": None,
            "monto": None,
            "fecha_cierre": None,
            "estado": "",
        }
    return {
        "nombre": ca.nombre,
        "organismo": ca.organismo_nombre or "",
        "region": ca.region,
        "monto": ca.monto_disponible_clp,
        "fecha_cierre": ca.fecha_cierre,
        "estado": ca.estado,
    }


_MENSAJES_SEGUIMIENTO: dict[str, str] = {
    "adjudicada": "Se adjudicó. Te recomendamos hacer pronto un análisis de competencia.",
    "cerrada": "El proceso cerró su etapa de recepción de ofertas.",
    "desierta": "El proceso quedó desierto.",
    "revocada": "El proceso fue revocado.",
}


def _mensaje_seguimiento(alerta: Alerta, estado: str) -> str:
    if alerta.tipo == "seguimiento_cierre":
        return "Esta oportunidad seguida cierra dentro de las próximas 48 horas."
    return _MENSAJES_SEGUIMIENTO.get(estado, "Cambió de estado.")


def _url_ficha_app(settings: Settings, fuente: str, codigo: str) -> str:
    """Enlace a la ficha de la app. Sin APP_BASE_URL degrada a ruta relativa."""
    ruta = f"/oportunidad/{fuente}/{codigo}"
    base = settings.app_base_url.strip().rstrip("/")
    return f"{base}{ruta}" if base else ruta


def _ctx_alerta_seguimiento(alerta: Alerta, session: Session, settings: Settings) -> dict[str, Any]:
    seguimiento = alerta.seguimiento
    assert seguimiento is not None, "alerta de seguimiento sin seguimiento_id"
    op = _datos_oportunidad(session, seguimiento.fuente, seguimiento.codigo_oportunidad)
    return {
        "tipo_alerta": alerta.tipo,
        "nombre": op["nombre"],
        "organismo": op["organismo"],
        "estado": op["estado"],
        "fecha_cierre": _fmt_fecha(op["fecha_cierre"]),
        "mensaje": _mensaje_seguimiento(alerta, op["estado"]),
        "url": _url_ficha_app(settings, seguimiento.fuente, seguimiento.codigo_oportunidad),
        "owner_email": seguimiento.owner.email,
    }


def _ctx_resumen_item(match: OportunidadMatch, session: Session, settings: Settings) -> dict[str, Any]:
    op = _datos_oportunidad(session, match.fuente, match.codigo_oportunidad)
    return {
        "perfil_nombre": match.perfil.nombre,
        "nombre": op["nombre"],
        "organismo": op["organismo"],
        "monto": _fmt_monto(op["monto"]),
        "fecha_cierre": _fmt_fecha(op["fecha_cierre"]),
        "estado": op["estado"],
        "score": match.score,
        "url": _url_ficha_app(settings, match.fuente, match.codigo_oportunidad),
    }


_BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"


def _smtp_send(
    settings: Settings,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str,
) -> None:
    if settings.brevo_api_key:
        _brevo_send(settings, to_email, subject, body_text, body_html)
    elif settings.smtp_host:
        _smtp_send_raw(settings, to_email, subject, body_text, body_html)
    else:
        _log.warning("Sin proveedor de correo configurado — correo a %s descartado", to_email)


def _brevo_send(
    settings: Settings,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str,
) -> None:
    payload = {
        "sender": {"email": settings.smtp_from},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": body_html,
        "textContent": body_text,
    }
    response = httpx.post(
        _BREVO_ENDPOINT,
        headers={"api-key": settings.brevo_api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=15.0,
    )
    _log.info("Brevo response status=%d to=%s", response.status_code, to_email)
    if not response.is_success:
        _log.error("Brevo error status=%d body=%s", response.status_code, response.text)
        response.raise_for_status()


def _smtp_send_raw(
    settings: Settings,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str,
) -> None:
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


def _load_alertas_seguimiento_pendientes(session: Session) -> list[Alerta]:
    return list(
        session.execute(
            select(Alerta)
            .join(OportunidadSeguida, Alerta.seguimiento_id == OportunidadSeguida.id)
            .join(Usuario, OportunidadSeguida.owner_id == Usuario.id)
            .where(
                Alerta.estado == EstadoAlerta.PENDIENTE.value,
                Usuario.activo.is_(True),
            )
            .options(selectinload(Alerta.seguimiento).selectinload(OportunidadSeguida.owner))
        ).scalars()
    )


def _enviar_una(
    session: Session,
    settings: Settings,
    counter: EmailCounter,
    alerta: Alerta,
    to_email: str,
    subject: str,
    template_base: str,
    ctx: dict[str, Any],
) -> bool:
    try:
        body_text = _jinja.get_template(f"{template_base}.txt").render(**ctx)
        body_html = _jinja.get_template(f"{template_base}.html").render(**ctx)
        _smtp_send(settings, to_email, subject, body_text, body_html)
        alerta.estado = EstadoAlerta.ENVIADA.value
        alerta.enviada_en = _now_utc()
        session.commit()
        counter.consume()
        return True
    except Exception:
        _log.error("Error enviando alerta id=%d a %s", alerta.id, to_email, exc_info=True)
        session.rollback()
        alerta.intentos_envio += 1
        if alerta.intentos_envio >= alerta.max_intentos:
            alerta.estado = EstadoAlerta.FALLIDA.value
            _log.warning(
                "Alerta id=%d marcada fallida tras %d intentos", alerta.id, alerta.intentos_envio
            )
        session.commit()
        return False


def enviar_pendientes_inmediatas(session: Session, settings: Settings) -> dict[str, int]:
    """Envía solo alertas pendientes de oportunidades seguidas."""
    counter = EmailCounter(session, settings.email_daily_limit)
    alertas = _load_alertas_seguimiento_pendientes(session)

    enviados = pospuestos = errores = 0
    for alerta in alertas:
        if counter.remaining() <= 0:
            pospuestos += 1
            continue
        ctx = _ctx_alerta_seguimiento(alerta, session, settings)
        subject = f"[MP] Seguimiento: {ctx['nombre'][:50]}"
        if _enviar_una(session, settings, counter, alerta, ctx["owner_email"], subject, "alerta_seguimiento", ctx):
            enviados += 1
        else:
            errores += 1

    if pospuestos > 0:
        counter.mark_tope_alcanzado()

    _log.info("enviar_inmediatas: enviados=%d pospuestos=%d errores=%d", enviados, pospuestos, errores)
    return {"enviados": enviados, "pospuestos": pospuestos, "errores": errores}


def _usuario_elegible_resumen(usuario: Usuario, ahora: datetime) -> bool:
    if not usuario.activo or usuario.dias_resumen <= 0:
        return False
    return usuario.ultimo_resumen_en is None or ahora - usuario.ultimo_resumen_en >= timedelta(
        days=usuario.dias_resumen
    )


def _matches_nuevos_usuario(session: Session, usuario: Usuario) -> list[OportunidadMatch]:
    stmt = (
        select(OportunidadMatch)
        .join(PerfilBusqueda, OportunidadMatch.perfil_id == PerfilBusqueda.id)
        .where(
            PerfilBusqueda.owner_id == usuario.id,
            PerfilBusqueda.activo.is_(True),
        )
        .options(selectinload(OportunidadMatch.perfil))
        .order_by(OportunidadMatch.score.desc(), OportunidadMatch.fecha_match.desc())
    )
    if usuario.ultimo_resumen_en is not None:
        stmt = stmt.where(OportunidadMatch.fecha_match > usuario.ultimo_resumen_en)
    return list(session.execute(stmt).scalars())


def enviar_resumen(session: Session, settings: Settings, ahora: datetime | None = None) -> dict[str, int]:
    """Envía un resumen consolidado por usuario elegible si tiene matches nuevos."""
    if ahora is None:
        ahora = _now_utc()
    counter = EmailCounter(session, settings.email_daily_limit)
    usuarios = list(session.execute(select(Usuario).where(Usuario.activo.is_(True))).scalars())

    enviados = sin_nuevos = no_elegibles = pospuestos = errores = 0
    for usuario in usuarios:
        if not _usuario_elegible_resumen(usuario, ahora):
            no_elegibles += 1
            continue
        matches = _matches_nuevos_usuario(session, usuario)
        if not matches:
            sin_nuevos += 1
            continue
        if counter.remaining() <= 0:
            pospuestos += 1
            continue

        items = [_ctx_resumen_item(m, session, settings) for m in matches[:5]]
        total = len(matches)
        subject = f"[MP] Encontramos {total} oportunidades para tu perfil en Mercado Público"
        ctx = {
            "total": total,
            "items": items,
            "url_app": settings.app_base_url.strip().rstrip("/") or "/",
        }
        try:
            body_text = _jinja.get_template("resumen.txt").render(**ctx)
            body_html = _jinja.get_template("resumen.html").render(**ctx)
            _smtp_send(settings, usuario.email, subject, body_text, body_html)
            usuario.ultimo_resumen_en = ahora
            session.commit()
            counter.consume()
            enviados += 1
        except Exception:
            _log.error("Error enviando resumen a %s", usuario.email, exc_info=True)
            session.rollback()
            errores += 1

    if pospuestos > 0:
        counter.mark_tope_alcanzado()

    _log.info(
        "enviar_resumen: enviados=%d sin_nuevos=%d no_elegibles=%d pospuestos=%d errores=%d",
        enviados,
        sin_nuevos,
        no_elegibles,
        pospuestos,
        errores,
    )
    return {
        "resumenes_enviados": enviados,
        "resumenes_sin_nuevos": sin_nuevos,
        "resumenes_no_elegibles": no_elegibles,
        "resumenes_pospuestos": pospuestos,
        "errores_resumen": errores,
    }
