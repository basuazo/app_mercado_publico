"""Queries compartidas entre rutas HTML y API REST."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.presentacion import nombre_region, razones_legibles
from app.matching.perfiles import listar_perfiles
from app.matching.seguimiento import listar_seguidas
from app.models.enums import EstadoOportunidad
from app.models.tables import CompraAgil, Licitacion, OfertaCompetencia, OportunidadMatch


def _url_ficha(fuente: str, codigo: str) -> str:
    """URL de la ficha oficial en Mercado Público.

    Para licitaciones es la ficha estándar (DetailsAcquisition): accesible para
    proveedores con sesión iniciada en procesos abiertos. Para Compra Ágil se
    apunta al buscador público vigente. Ver `mostrar_ficha_oficial` para cuándo
    conviene exponer este enlace en la UI.
    """
    if fuente == "licitaciones":
        return (
            "https://www.mercadopublico.cl/Procurement/Modules/RFB/"
            f"DetailsAcquisition.aspx?qs={codigo}"
        )
    return "https://buscador.mercadopublico.cl/compra-agil"


def mostrar_ficha_oficial(estado: str | None) -> bool:
    """La ficha oficial solo es navegable de forma fiable en procesos abiertos.

    En procesos cerrados/terminales Mercado Público responde
    "No Pertenece a la unidad de la ficha" a quien no es la unidad dueña, así
    que no exponemos el enlace en esos casos.
    """
    return estado == EstadoOportunidad.PUBLICADA.value


def get_oportunidades_usuario(
    session: Session,
    user_id: int,
    fuente: str | None = None,
    region: int | None = None,
    texto: str | None = None,
    perfil_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Retorna (items, total) de oportunidades_match para los perfiles activos del usuario.

    Aplica filtros opcionales en Python (texto, region) después de cargar matches.
    Paginación correcta después de aplicar todos los filtros.
    """
    perfiles = listar_perfiles(session, user_id)
    if not perfiles:
        return [], 0

    perfil_ids = [p.id for p in perfiles]

    stmt = select(OportunidadMatch).where(OportunidadMatch.perfil_id.in_(perfil_ids))

    if fuente:
        stmt = stmt.where(OportunidadMatch.fuente == fuente)
    if perfil_id is not None:
        if perfil_id not in perfil_ids:
            return [], 0
        stmt = stmt.where(OportunidadMatch.perfil_id == perfil_id)

    stmt = stmt.order_by(OportunidadMatch.score.desc())
    matches = list(session.execute(stmt).scalars())

    # Batch-load oportunidades
    lic_codigos = [m.codigo_oportunidad for m in matches if m.fuente == "licitaciones"]
    ca_codigos = [m.codigo_oportunidad for m in matches if m.fuente == "compras_agiles"]

    lics: dict[str, Licitacion] = {}
    if lic_codigos:
        for lic in session.execute(
            select(Licitacion).where(Licitacion.codigo.in_(lic_codigos))
        ).scalars():
            lics[lic.codigo] = lic

    cas: dict[str, CompraAgil] = {}
    if ca_codigos:
        for c in session.execute(
            select(CompraAgil).where(CompraAgil.codigo.in_(ca_codigos))
        ).scalars():
            cas[c.codigo] = c

    ahora = datetime.now(UTC).replace(tzinfo=None)
    result: list[dict[str, Any]] = []

    for m in matches:
        op: Licitacion | CompraAgil | None
        if m.fuente == "licitaciones":
            op = lics.get(m.codigo_oportunidad)
        else:
            op = cas.get(m.codigo_oportunidad)

        if op is None:
            continue

        # Filtro de región (solo CA)
        if region is not None and m.fuente == "compras_agiles" and isinstance(op, CompraAgil) and op.region != region:
            continue

        # Filtro de texto
        if texto and texto.lower() not in op.nombre.lower():
            continue

        dias: float | None = None
        if op.fecha_cierre is not None:
            delta = op.fecha_cierre - ahora
            dias = max(0.0, delta.total_seconds() / 86400)

        monto: float | None = None
        organismo: str | None = None
        reg: int | None = None
        if isinstance(op, Licitacion):
            monto = op.monto_clp
            organismo = op.codigo_organismo
        else:
            monto = op.monto_disponible_clp
            organismo = op.organismo_nombre
            reg = op.region

        result.append({
            "match": m,
            "oportunidad": op,
            "nombre": op.nombre,
            "estado": op.estado,
            "fecha_cierre": op.fecha_cierre,
            "dias_al_cierre": dias,
            "monto": monto,
            "organismo": organismo,
            "region": reg,
            "region_nombre": nombre_region(reg),
            "razones": razones_legibles(m.razones),
            "url_ficha": _url_ficha(m.fuente, m.codigo_oportunidad),
            "mostrar_ficha": mostrar_ficha_oficial(op.estado),
        })

    total = len(result)
    return result[offset : offset + limit], total


def listar_seguidas_detalle(
    session: Session,
    user_id: int,
    *,
    incluir_archivadas: bool = False,
) -> list[dict[str, Any]]:
    """Seguimientos del usuario enriquecidos con el estado/datos actuales de la oportunidad.

    Si la oportunidad subyacente ya no existe (caso raro), degrada a los datos
    mínimos guardados en el seguimiento en vez de romper el render.
    """
    seguidas = listar_seguidas(session, user_id, incluir_archivadas=incluir_archivadas)

    lic_codigos = [s.codigo_oportunidad for s in seguidas if s.fuente == "licitaciones"]
    ca_codigos = [s.codigo_oportunidad for s in seguidas if s.fuente == "compras_agiles"]

    lics: dict[str, Licitacion] = {}
    if lic_codigos:
        for lic in session.execute(
            select(Licitacion).where(Licitacion.codigo.in_(lic_codigos))
        ).scalars():
            lics[lic.codigo] = lic

    cas: dict[str, CompraAgil] = {}
    if ca_codigos:
        for c in session.execute(
            select(CompraAgil).where(CompraAgil.codigo.in_(ca_codigos))
        ).scalars():
            cas[c.codigo] = c

    result: list[dict[str, Any]] = []
    for s in seguidas:
        op: Licitacion | CompraAgil | None
        if s.fuente == "licitaciones":
            op = lics.get(s.codigo_oportunidad)
        else:
            op = cas.get(s.codigo_oportunidad)

        result.append({
            "seguimiento": s,
            "nombre": op.nombre if op is not None else s.codigo_oportunidad,
            "estado": op.estado if op is not None else s.estado_visto,
            "fecha_cierre": op.fecha_cierre if op is not None else None,
            "url_ficha_app": f"/oportunidad/{s.fuente}/{s.codigo_oportunidad}",
        })
    return result


def resumen_competencia(session: Session, licitacion_codigo: str) -> list[dict[str, Any]]:
    """Resumen de competencia por proveedor (F-competencia): total adjudicado
    (suma de monto_linea_adjudicada de ofertas seleccionadas) e ítems ganados,
    ordenado desc. Agrupa por rut_proveedor (más estable que el nombre, ver
    docs/05-competencia.md §3). Vacío si no hay ofertas capturadas aún."""
    ofertas = list(
        session.execute(
            select(OfertaCompetencia)
            .where(OfertaCompetencia.licitacion_codigo == licitacion_codigo)
            .where(OfertaCompetencia.seleccionada.is_(True))
        ).scalars()
    )
    por_proveedor: dict[str, dict[str, Any]] = {}
    for o in ofertas:
        entry = por_proveedor.setdefault(
            o.rut_proveedor,
            {
                "rut_proveedor": o.rut_proveedor,
                "nombre_proveedor": o.nombre_proveedor,
                "total_adjudicado": 0.0,
                "items_ganados": 0,
            },
        )
        entry["total_adjudicado"] += o.monto_linea_adjudicada or 0.0
        entry["items_ganados"] += 1
    resumen = list(por_proveedor.values())
    resumen.sort(key=lambda d: d["total_adjudicado"], reverse=True)
    return resumen


def detalle_competencia(session: Session, licitacion_codigo: str) -> list[dict[str, Any]]:
    """Detalle por ítem de todas las ofertas (seleccionadas y no) de una licitación."""
    ofertas = session.execute(
        select(OfertaCompetencia)
        .where(OfertaCompetencia.licitacion_codigo == licitacion_codigo)
        .order_by(OfertaCompetencia.codigo_item, OfertaCompetencia.seleccionada.desc())
    ).scalars()
    return [
        {
            "codigo_item": o.codigo_item,
            "rut_proveedor": o.rut_proveedor,
            "nombre_proveedor": o.nombre_proveedor,
            "monto_unitario": o.monto_unitario,
            "monto_linea_adjudicada": o.monto_linea_adjudicada,
            "seleccionada": o.seleccionada,
        }
        for o in ofertas
    ]


def check_oportunidad_access(
    session: Session,
    user_id: int,
    fuente: str,
    codigo: str,
) -> OportunidadMatch | None:
    """Retorna el match si el usuario tiene acceso, None si no."""
    perfiles = listar_perfiles(session, user_id)
    if not perfiles:
        return None
    perfil_ids = [p.id for p in perfiles]
    return session.execute(
        select(OportunidadMatch)
        .where(
            OportunidadMatch.perfil_id.in_(perfil_ids),
            OportunidadMatch.fuente == fuente,
            OportunidadMatch.codigo_oportunidad == codigo,
        )
        .limit(1)
    ).scalar_one_or_none()
