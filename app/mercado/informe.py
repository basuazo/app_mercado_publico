"""Informe mensual por empresa (F-plat-7 §1.2): mismo contenido por correo y en `/mesa/informe/{mes}`.

Job `mercado-informe` (día 1, 08:40 Chile; sin API: no gasta cuota). Un informe por empresa activa
con regiones, enviado a cada usuario activo de la empresa con `dias_resumen > 0`. Reglas:

- Ownership (regla 26): todo se filtra por `empresa_id`; la web toma la empresa de la sesión.
- Idempotente (regla de jobs): cada envío queda en `sync_state`, fuente
  `mercado_informe:<empresa_id>:<AAAA-MM>`, con los ids de usuario ya atendidos en `cursor`.
  Re-disparar el job el mismo mes no repite el correo; un fallo a mitad se completa al re-correr.
- Tope diario (regla 14): el mismo contador `alerts_email` del resumen. Sin cupo, el resto queda
  pendiente para la próxima corrida (no se marca).
- Sin proveedor de correo (ni `BREVO_API_KEY` ni SMTP), el job NO falla: lo dice en `avisos`, no
  envía y no marca nada (el informe sigue visible en la web).

Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.presentacion import formato_clp, formato_pct
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.mercado import abastecimiento as ab
from app.mercado import adjudicacion as adj
from app.mercado import equipo as eq
from app.models.tables import (
    CaLinea,
    CaProducto,
    CaTrabajo,
    CompraAgil,
    Empresa,
    OfertaPropia,
    Prediccion,
    SyncState,
    Usuario,
)

_log = get_logger(__name__)

FUENTE_INFORME = "mercado_informe:{empresa_id}:{mes}"
_MES = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TOP = 5
PIE = "Fuente: Dirección ChileCompra"


def mes_valido(mes: str) -> bool:
    return bool(_MES.match(mes))


def mes_anterior(ahora: datetime) -> str:
    """El mes que cierra antes de `ahora` (día 1 → el mes que acaba de terminar), hora de Chile."""
    local = ahora.replace(tzinfo=UTC).astimezone(TZ_CHILE)
    a, m = (local.year - 1, 12) if local.month == 1 else (local.year, local.month - 1)
    return f"{a:04d}-{m:02d}"


def rango_mes_utc(mes: str) -> tuple[datetime, datetime]:
    """[inicio, fin) del mes de Chile, en UTC naive (como se guardan las fechas)."""
    a, m = (int(x) for x in mes.split("-"))
    fin = (a + 1, 1) if m == 12 else (a, m + 1)
    ini = datetime(a, m, 1, tzinfo=TZ_CHILE).astimezone(UTC).replace(tzinfo=None)
    return ini, datetime(*fin, 1, tzinfo=TZ_CHILE).astimezone(UTC).replace(tzinfo=None)


def _etiqueta_mes(mes: str) -> str:
    nombres = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    a, m = (int(x) for x in mes.split("-"))
    return f"{nombres[m - 1]} de {a}"


def _familias(session: Session, codigos: list[str]) -> dict[str, str]:
    """Familia (UNSPSC) predominante de cada CA, de a lotes."""
    productos: dict[str, list[tuple[str | None, str | None]]] = defaultdict(list)
    for i in range(0, len(codigos), 500):
        for ca, cod, nombre in session.execute(
            select(CaProducto.ca_codigo, CaProducto.codigo_producto, CaProducto.nombre).where(
                CaProducto.ca_codigo.in_(codigos[i : i + 500])
            )
        ):
            productos[ca].append((cod, nombre))
    return {c: adj.familia_de_ca(productos.get(c, [])) for c in codigos}


def construir(session: Session, empresa_id: int, mes: str, ahora: datetime | None = None) -> dict[str, Any]:
    """Todo el informe de una empresa y un mes, ya formateado para mostrar (correo y web)."""
    ahora = ahora or ahora_utc()
    ini, fin = rango_mes_utc(mes)
    empresa = session.get(Empresa, empresa_id)
    regiones = [int(r) for r in ((empresa.regiones_prioritarias if empresa else None) or [])
                if str(r).isdigit()]  # fmt: skip

    # 1. CA en alcance del mes (de las regiones de la empresa, publicadas en el mes).
    en_alcance = (CompraAgil.region.in_(regiones), CompraAgil.fecha_publicacion >= ini,
                  CompraAgil.fecha_publicacion < fin)  # fmt: skip
    n_ca, monto_ca = session.execute(
        select(func.count(), func.coalesce(func.sum(CompraAgil.monto_disponible_clp), 0.0)).where(
            *en_alcance
        )
    ).one()
    compradores = [
        {"nombre": nombre or "Sin organismo", "n": int(n), "monto": formato_clp(monto)}
        for nombre, n, monto in session.execute(
            select(
                CompraAgil.organismo_nombre,
                func.count().label("n"),
                func.coalesce(func.sum(CompraAgil.monto_disponible_clp), 0.0).label("monto"),
            )
            .where(*en_alcance)
            .group_by(CompraAgil.organismo_nombre)
            .order_by(func.count().desc(), func.sum(CompraAgil.monto_disponible_clp).desc())
            .limit(TOP)
        )
    ]

    # 2. Cotizadas por la empresa (enviadas, con cierre en el mes) y su resultado.
    resultados: dict[str | None, int] = {
        r: int(n)
        for r, n in session.execute(
            select(CaTrabajo.resultado, func.count())
            .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
            .where(
                CaTrabajo.empresa_id == empresa_id,
                CaTrabajo.estado == eq.ENVIADA,
                CompraAgil.fecha_cierre >= ini,
                CompraAgil.fecha_cierre < fin,
            )
            .group_by(CaTrabajo.resultado)
        ).all()
    }
    cotizadas = sum(resultados.values())
    ganadas = resultados.get(eq.GANADA, 0)
    perdidas = resultados.get(eq.PERDIDA, 0)

    # 3. Margen realizado (ofertas ganadas con cierre en el mes), total y por familia.
    ofertas = session.execute(
        select(OfertaPropia.ca_codigo, OfertaPropia.margen_realizado, OfertaPropia.precio_ganador_real)
        .join(CompraAgil, CompraAgil.codigo == OfertaPropia.ca_codigo)
        .where(
            OfertaPropia.empresa_id == empresa_id,
            OfertaPropia.margen_realizado.is_not(None),
            CompraAgil.fecha_cierre >= ini,
            CompraAgil.fecha_cierre < fin,
        )
    ).all()
    fam = _familias(session, [o[0] for o in ofertas])
    por_familia: dict[str, float] = defaultdict(float)
    for codigo, margen, _ in ofertas:
        por_familia[fam[codigo]] += float(margen)
    margen_total = sum(por_familia.values())
    vendido = sum(float(o[2] or 0.0) for o in ofertas)

    # 4. Acierto de las predicciones (CA del mes ya cerradas).
    preds = session.execute(
        select(Prediccion).where(
            Prediccion.empresa_id == empresa_id,
            Prediccion.cierre_ca >= ini,
            Prediccion.cierre_ca < fin,
            Prediccion.estado == "cerrada",
        )
    ).scalars().all()
    con_int = [p for p in preds if p.resultado_en_intervalo is not None]
    aciertos = sum(1 for p in con_int if p.resultado_en_intervalo)
    ofertamos = [
        (float(p.p_ganar_sugerido), p.resultado_propio == eq.GANADA)
        for p in preds
        if p.resultado_propio in (eq.GANADA, eq.PERDIDA) and p.p_ganar_sugerido is not None
    ]
    brier, n_brier = adj.brier(ofertamos)

    # 5. Top 5 recomendaciones de abastecimiento vigentes (las abiertas, mismo orden que la pantalla).
    todas = sorted(
        (r for lista in ab.recomendaciones_ui(session, empresa_id).values() for r in lista),
        key=ab._orden_rec,
    )
    recomendaciones = [{"tipo": ab.ETIQUETAS.get(r.tipo, r.tipo), "texto": r.texto} for r in todas[:TOP]]

    # 6. Catálogo: líneas confirmadas y CA sin detalle de las CA del mes.
    codigos_mes = select(CompraAgil.codigo).where(*en_alcance)
    lineas_ok = session.execute(
        select(func.count()).select_from(CaLinea).where(
            CaLinea.estado == "ok", CaLinea.ca_codigo.in_(codigos_mes)
        )
    ).scalar_one()
    sin_detalle = session.execute(
        select(func.count()).select_from(CompraAgil).where(*en_alcance, CompraAgil.detalle_lineas == "sin_detalle")
    ).scalar_one()

    return {
        "empresa": empresa.nombre if empresa else "",
        "mes": mes,
        "mes_texto": _etiqueta_mes(mes),
        "ca": {"n": int(n_ca), "monto": formato_clp(monto_ca)},
        "compradores": compradores,
        "cotizadas": {
            "n": cotizadas,
            "ganadas": ganadas,
            "perdidas": perdidas,
            "sin_resultado": cotizadas - ganadas - perdidas,
        },
        "margen": {
            "total": formato_clp(margen_total) if ofertas else "—",
            "n": len(ofertas),
            "vendido": formato_clp(vendido) if ofertas else "—",
            "por_familia": [
                {"familia": f, "margen": formato_clp(m)}
                for f, m in sorted(por_familia.items(), key=lambda kv: -kv[1])
            ],
        },
        "predicciones": {
            "n": len(preds),
            "n_intervalo": len(con_int),
            "aciertos": aciertos,
            "pct_intervalo": formato_pct(aciertos / len(con_int), 0) if con_int else "—",
            "brier": f"{brier:.3f}".replace(".", ",") if brier is not None else "—",
            "n_brier": n_brier,
            "p_media": formato_pct(sum(p for p, _ in ofertamos) / n_brier, 0) if n_brier else "—",
            "tasa_ganadas": formato_pct(sum(1 for _, g in ofertamos if g) / n_brier, 0)
            if n_brier
            else "—",
        },
        "recomendaciones": recomendaciones,
        "catalogo": {"lineas_confirmadas": int(lineas_ok), "ca_sin_detalle": int(sin_detalle)},
        "pie": PIE,
    }


# ---------------------------------------------------------------------------
# Envío (job `mercado-informe`)
# ---------------------------------------------------------------------------


def _atendidos(estado: SyncState | None) -> set[int]:
    if estado is None or not estado.cursor:
        return set()
    return {int(x) for x in estado.cursor.split(",") if x.strip().isdigit()}


def _marcar(session: Session, fuente: str, atendidos: set[int], ahora: datetime) -> None:
    estado = session.get(SyncState, fuente)
    if estado is None:
        estado = SyncState(fuente=fuente, requests_usadas_hoy=0)
        session.add(estado)
    estado.cursor = ",".join(str(i) for i in sorted(atendidos))[:500]
    estado.ultima_ejecucion = estado.ultimo_ok = ahora


def _sin_proveedor(settings: Settings) -> bool:
    return not (settings.brevo_api_key or settings.smtp_host)


def enviar_informes(
    session: Session, settings: Settings, ahora: datetime | None = None, mes: str | None = None
) -> dict[str, Any]:
    """Un informe por empresa activa a sus usuarios elegibles. Idempotente por (empresa, mes)."""
    from app.alerts.email import EmailCounter, _jinja, _smtp_send

    ahora = ahora or ahora_utc()
    mes = mes or mes_anterior(ahora)
    res: dict[str, Any] = {
        "mes": mes,
        "empresas": 0,
        "informes_enviados": 0,
        "ya_enviados": 0,
        "sin_destinatarios": 0,
        "pospuestos": 0,
        "errores": 0,
    }
    if _sin_proveedor(settings):
        res["sin_proveedor_correo"] = True
        res["avisos"] = [
            "Sin BREVO_API_KEY ni SMTP en el entorno: no se envió ningún informe (siguen en "
            f"/mesa/informe/{mes})"
        ]
        return res
    counter = EmailCounter(session, settings.email_daily_limit)
    base = settings.app_base_url.strip().rstrip("/")
    empresas = session.execute(
        select(Empresa).where(Empresa.activa.is_(True)).order_by(Empresa.id)
    ).scalars().all()
    for empresa in empresas:
        if not empresa.regiones_prioritarias:
            continue
        res["empresas"] += 1
        usuarios = session.execute(
            select(Usuario).where(
                Usuario.empresa_id == empresa.id,
                Usuario.activo.is_(True),
                Usuario.dias_resumen > 0,
            ).order_by(Usuario.id)
        ).scalars().all()
        if not usuarios:
            res["sin_destinatarios"] += 1
            continue
        fuente = FUENTE_INFORME.format(empresa_id=empresa.id, mes=mes)
        atendidos = _atendidos(session.get(SyncState, fuente))
        pendientes = [u for u in usuarios if u.id not in atendidos]
        if not pendientes:
            res["ya_enviados"] += 1
            continue
        ctx = construir(session, empresa.id, mes, ahora)
        ctx["url_informe"] = f"{base}/mesa/informe/{mes}" if base else f"/mesa/informe/{mes}"
        subject = f"[MP] Informe de {ctx['mes_texto']} — {empresa.nombre}"
        try:
            texto = _jinja.get_template("informe_mensual.txt").render(**ctx)
            html = _jinja.get_template("informe_mensual.html").render(**ctx)
        except Exception:
            _log.error("informe %s: no se pudo armar", fuente, exc_info=True)
            res["errores"] += 1
            continue
        for u in pendientes:
            if counter.remaining() <= 0:
                res["pospuestos"] += 1
                continue
            try:
                _smtp_send(settings, u.email, subject, texto, html)
                atendidos.add(u.id)
                _marcar(session, fuente, atendidos, ahora)
                session.commit()
                counter.consume()
                res["informes_enviados"] += 1
            except Exception:
                _log.error("informe %s: error enviando a un usuario", fuente, exc_info=True)
                session.rollback()
                res["errores"] += 1
    if res["pospuestos"]:
        counter.mark_tope_alcanzado()
        res["avisos"] = [f"Tope diario de correos: {res['pospuestos']} informe(s) pendientes"]
    _log.info("mercado-informe: %s", res)
    return res
