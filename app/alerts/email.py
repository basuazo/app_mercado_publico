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
from collections.abc import Iterable
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, NamedTuple, cast

import httpx
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, defer, selectinload

from app.api.presentacion import fecha_cierre_legible
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.core.vigencia import es_vigente
from app.models.enums import EstadoAlerta, ValorFeedback
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

_log = get_logger(__name__)

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
        return datetime.now(TZ_CHILE).date().isoformat()

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


def _fmt_cierre(dt: datetime | None, fuente: str) -> str:
    """El cierre del correo lo decide la MISMA función que el badge de la app.

    Antes esto era un `strftime("%d/%m/%Y %H:%M")` para todo, así que el correo
    de una licitación publicaba una medianoche que la fuente nunca entregó
    (F-coherencia). `fecha_cierre_legible` muestra la hora solo en Compra Ágil.
    """
    return fecha_cierre_legible(dt, fuente)


_DATOS_VACIOS: dict[str, Any] = {
    "organismo": "",
    "region": None,
    "monto": None,
    "fecha_cierre": None,
    "fecha_publicacion": None,
    "estado": "",
}


def _datos_de(op: Licitacion | CompraAgil) -> dict[str, Any]:
    """Lo que el correo necesita de una oportunidad ya cargada."""
    if isinstance(op, Licitacion):
        return {
            "nombre": op.nombre,
            "organismo": op.organismo_nombre or op.codigo_organismo or "",
            "region": op.region,
            "monto": op.monto_clp,
            "fecha_cierre": op.fecha_cierre,
            "fecha_publicacion": op.fecha_publicacion,
            "estado": op.estado,
        }
    return {
        "nombre": op.nombre,
        "organismo": op.organismo_nombre or "",
        "region": op.region,
        "monto": op.monto_disponible_clp,
        "fecha_cierre": op.fecha_cierre,
        "fecha_publicacion": op.fecha_publicacion,
        "estado": op.estado,
    }


def _datos_oportunidad(session: Session, fuente: str, codigo: str) -> dict[str, Any]:
    op: Licitacion | CompraAgil | None
    op = session.get(Licitacion, codigo) if fuente == "licitaciones" else session.get(CompraAgil, codigo)
    if op is None:
        return {"nombre": codigo, **_DATOS_VACIOS}
    return _datos_de(op)


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
        "fecha_cierre": _fmt_cierre(op["fecha_cierre"], seguimiento.fuente),
        "mensaje": _mensaje_seguimiento(alerta, op["estado"]),
        "url": _url_ficha_app(settings, seguimiento.fuente, seguimiento.codigo_oportunidad),
        "owner_email": seguimiento.owner.email,
    }


def _ctx_resumen_item(item: _ItemResumen, settings: Settings) -> dict[str, Any]:
    op = item.datos
    match = item.match
    return {
        "perfil_nombre": ", ".join(item.perfiles),
        "nombre": op["nombre"],
        "organismo": op["organismo"],
        "monto": _fmt_monto(op["monto"]),
        "fecha_cierre": _fmt_cierre(op["fecha_cierre"], match.fuente),
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
        alerta.enviada_en = ahora_utc()
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


# Resumen (F-match-1): top de lo nuevo y, aparte, lo que cierra pronto.
_TOP_RESUMEN = 5
_MAX_CIERRA_PRONTO = 5
_HORAS_CIERRA_PRONTO = 48
_RELEVANCIA_CIERRA_PRONTO = 60


class _ItemResumen(NamedTuple):
    """El mejor match del usuario con una oportunidad, sus datos y los perfiles que la traen."""

    match: OportunidadMatch
    datos: dict[str, Any]
    perfiles: list[str]


def _cargar_ops_lote(session: Session, fuente: str, codigos: list[str]) -> dict[str, Licitacion | CompraAgil]:
    """Las oportunidades de esos códigos en UNA query (sin `raw_json`, que el correo no usa)."""
    if not codigos:
        return {}
    modelo: type[Licitacion] | type[CompraAgil] = Licitacion if fuente == "licitaciones" else CompraAgil
    filas = session.execute(
        select(modelo).options(defer(modelo.raw_json)).where(modelo.codigo.in_(codigos))
    ).scalars()
    return {op.codigo: op for op in cast(Iterable[Licitacion | CompraAgil], filas)}


def _items_resumen(
    session: Session,
    usuario: Usuario,
    ahora: datetime,
    *,
    min_score: float,
    nuevos_desde: datetime | None = None,
    cierra_hasta: datetime | None = None,
) -> list[_ItemResumen]:
    """Oportunidades vigentes del usuario, UNA por (fuente, código), ordenadas por
    relevancia descendente y cierre más próximo.

    Todo el filtrado posible va en SQL: perfiles activos del usuario, piso de
    relevancia, matches nuevos desde el último resumen, y sin las que el usuario
    descartó o ya guardó (`NOT EXISTS`). Las oportunidades se cargan en lote
    (una query por fuente), no una por match. `cierra_hasta` acota a las que
    cierran entre `ahora` y ese instante.
    """
    guardada = exists().where(
        OportunidadSeguida.owner_id == usuario.id,
        OportunidadSeguida.fuente == OportunidadMatch.fuente,
        OportunidadSeguida.codigo_oportunidad == OportunidadMatch.codigo_oportunidad,
    )
    descartada = exists().where(
        MatchFeedback.usuario_id == usuario.id,
        MatchFeedback.fuente == OportunidadMatch.fuente,
        MatchFeedback.codigo_oportunidad == OportunidadMatch.codigo_oportunidad,
        MatchFeedback.valor == ValorFeedback.DESCARTE.value,
    )
    stmt = (
        select(OportunidadMatch)
        .join(PerfilBusqueda, OportunidadMatch.perfil_id == PerfilBusqueda.id)
        .where(
            PerfilBusqueda.owner_id == usuario.id,
            PerfilBusqueda.activo.is_(True),
            OportunidadMatch.score >= min_score,
            ~guardada,
            ~descartada,
        )
        .options(selectinload(OportunidadMatch.perfil))
        .order_by(OportunidadMatch.score.desc(), OportunidadMatch.id)
    )
    if nuevos_desde is not None:
        stmt = stmt.where(OportunidadMatch.fecha_match > nuevos_desde)
    if cierra_hasta is not None:
        stmt = stmt.where(
            or_(
                and_(
                    OportunidadMatch.fuente == "licitaciones",
                    OportunidadMatch.codigo_oportunidad.in_(
                        select(Licitacion.codigo).where(
                            Licitacion.fecha_cierre > ahora, Licitacion.fecha_cierre <= cierra_hasta
                        )
                    ),
                ),
                and_(
                    OportunidadMatch.fuente == "compras_agiles",
                    OportunidadMatch.codigo_oportunidad.in_(
                        select(CompraAgil.codigo).where(
                            CompraAgil.fecha_cierre > ahora, CompraAgil.fecha_cierre <= cierra_hasta
                        )
                    ),
                ),
            )
        )

    # Un solo item por oportunidad: el primero (mayor relevancia) manda y los
    # demás matches solo suman el nombre de su perfil.
    mejores: dict[tuple[str, str], OportunidadMatch] = {}
    perfiles: dict[tuple[str, str], list[str]] = {}
    for m in session.execute(stmt).scalars():
        clave = (m.fuente, m.codigo_oportunidad)
        mejores.setdefault(clave, m)
        nombres = perfiles.setdefault(clave, [])
        if m.perfil.nombre not in nombres:
            nombres.append(m.perfil.nombre)

    ops: dict[str, dict[str, Licitacion | CompraAgil]] = {
        fuente: _cargar_ops_lote(session, fuente, [c for f, c in mejores if f == fuente])
        for fuente in ("licitaciones", "compras_agiles")
    }
    items: list[_ItemResumen] = []
    for clave, m in mejores.items():
        op = ops[m.fuente].get(m.codigo_oportunidad)
        if op is None:
            continue
        datos = _datos_de(op)
        # F-vigencia: una oportunidad que cerró entre el match y el envío del
        # resumen no se anuncia (misma definición de "vigente" que el feed).
        if es_vigente(datos["estado"], datos["fecha_cierre"], m.fuente, ahora, datos["fecha_publicacion"]):
            items.append(_ItemResumen(m, datos, perfiles[clave]))
    items.sort(
        key=lambda i: (
            -i.match.score,
            i.datos["fecha_cierre"] is None,
            i.datos["fecha_cierre"] or datetime.max,
        )
    )
    return items


def _matches_nuevos_usuario(
    session: Session, usuario: Usuario, ahora: datetime, min_score: float
) -> list[_ItemResumen]:
    """Lo nuevo desde el último resumen que pasa el piso de relevancia del feed."""
    return _items_resumen(
        session, usuario, ahora, min_score=min_score, nuevos_desde=usuario.ultimo_resumen_en
    )


def _cierra_pronto_usuario(
    session: Session, usuario: Usuario, ahora: datetime, ya_incluidas: list[_ItemResumen]
) -> list[_ItemResumen]:
    """Hasta 5 oportunidades de relevancia alta que cierran en las próximas 48 h y
    no están ya en el top del correo (el cierre más próximo primero)."""
    en_top = {(i.match.fuente, i.match.codigo_oportunidad) for i in ya_incluidas}
    candidatas = _items_resumen(
        session,
        usuario,
        ahora,
        min_score=_RELEVANCIA_CIERRA_PRONTO,
        cierra_hasta=ahora + timedelta(hours=_HORAS_CIERRA_PRONTO),
    )
    pendientes = [i for i in candidatas if (i.match.fuente, i.match.codigo_oportunidad) not in en_top]
    pendientes.sort(key=lambda i: (i.datos["fecha_cierre"], -i.match.score))
    return pendientes[:_MAX_CIERRA_PRONTO]


def enviar_resumen(session: Session, settings: Settings, ahora: datetime | None = None) -> dict[str, int]:
    """Envía un resumen consolidado por usuario elegible si tiene matches nuevos."""
    if ahora is None:
        ahora = ahora_utc()
    counter = EmailCounter(session, settings.email_daily_limit)
    usuarios = list(session.execute(select(Usuario).where(Usuario.activo.is_(True))).scalars())

    enviados = sin_nuevos = no_elegibles = pospuestos = errores = 0
    for usuario in usuarios:
        if not _usuario_elegible_resumen(usuario, ahora):
            no_elegibles += 1
            continue
        matches = _matches_nuevos_usuario(session, usuario, ahora, settings.feed_min_score_default)
        if not matches:
            sin_nuevos += 1
            continue
        if counter.remaining() <= 0:
            pospuestos += 1
            continue

        top = matches[:_TOP_RESUMEN]
        items = [_ctx_resumen_item(i, settings) for i in top]
        cierra_pronto = [
            _ctx_resumen_item(i, settings) for i in _cierra_pronto_usuario(session, usuario, ahora, top)
        ]
        total = len(matches)
        subject = f"[MP] Encontramos {total} oportunidades para tu perfil en Mercado Público"
        ctx = {
            "total": total,
            "items": items,
            "cierra_pronto": cierra_pronto,
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
