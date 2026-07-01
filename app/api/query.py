"""Queries compartidas entre rutas HTML y API REST."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.presentacion import nombre_region, razones_legibles
from app.matching.feedback import listar_descartadas, listar_feedback_usuario, obtener_feedback
from app.matching.perfiles import listar_perfiles
from app.matching.seguimiento import listar_seguidas, obtener_seguimiento
from app.models.enums import SECTOR_SIN_CLASIFICACION, EstadoOportunidad, ValorFeedback
from app.models.tables import (
    CompraAgil,
    InstitucionPAC,
    Licitacion,
    OfertaCompetencia,
    OportunidadMatch,
)


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


def _construir_item(
    m: OportunidadMatch,
    op: Licitacion | CompraAgil,
    *,
    feedback_valor: str | None,
    siguiendo: bool,
    ahora: datetime,
) -> dict[str, Any]:
    """Arma el dict de presentación de una oportunidad para el feed o una
    tarjeta individual (re-render HTMX tras seguir/me-sirve)."""
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

    return {
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
        "siguiendo": siguiendo,
        "feedback": feedback_valor,
    }


def get_item_oportunidad(
    session: Session,
    user_id: int,
    fuente: str,
    codigo: str,
) -> dict[str, Any] | None:
    """Arma el mismo dict que `get_oportunidades_usuario` para UNA oportunidad,
    usado para re-renderizar su tarjeta tras una acción HTMX (seguir/me-sirve).

    None si el usuario no tiene acceso (ownership, regla 17) o la oportunidad
    subyacente ya no existe.
    """
    m = check_oportunidad_access(session, user_id, fuente, codigo)
    if m is None:
        return None

    op: Licitacion | CompraAgil | None
    op = session.get(Licitacion, codigo) if fuente == "licitaciones" else session.get(CompraAgil, codigo)
    if op is None:
        return None

    feedback = obtener_feedback(session, user_id, fuente, codigo)
    siguiendo = obtener_seguimiento(session, user_id, fuente, codigo) is not None
    ahora = datetime.now(UTC).replace(tzinfo=None)
    return _construir_item(
        m,
        op,
        feedback_valor=feedback.valor if feedback is not None else None,
        siguiendo=siguiendo,
        ahora=ahora,
    )


def get_oportunidades_usuario(
    session: Session,
    user_id: int,
    fuente: str | None = None,
    region: int | None = None,
    texto: str | None = None,
    perfil_id: int | None = None,
    orden: str = "score",
    min_score: int = 0,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int, int]:
    """Retorna (items, total, total_sin_filtro_relevancia) de oportunidades_match
    para los perfiles activos del usuario.

    Aplica filtros opcionales en Python (texto, region, min_score) después de
    cargar matches. Excluye las oportunidades que el usuario descartó (feedback
    F10 parte 2) — esas solo se ven en la vista "ver descartadas"
    (`listar_descartadas_detalle`).
    `orden`: "score" (default, mejor match primero) o "cierre" (cierran antes
    primero, sin fecha al final). Paginación correcta después de aplicar todos
    los filtros y el orden.
    `min_score`: piso de `OportunidadMatch.score` (umbral de relevancia del
    feed); 0 = sin piso, muestra todo. `total_sin_filtro_relevancia` es el total
    que habría sin aplicar `min_score` (mismos filtros de fuente/perfil/texto/
    región/descartadas), para poder mostrar "N ocultas por baja relevancia".
    """
    perfiles = listar_perfiles(session, user_id)
    if not perfiles:
        return [], 0, 0

    perfil_ids = [p.id for p in perfiles]

    stmt = select(OportunidadMatch).where(OportunidadMatch.perfil_id.in_(perfil_ids))

    if fuente:
        stmt = stmt.where(OportunidadMatch.fuente == fuente)
    if perfil_id is not None:
        if perfil_id not in perfil_ids:
            return [], 0, 0
        stmt = stmt.where(OportunidadMatch.perfil_id == perfil_id)

    stmt = stmt.order_by(OportunidadMatch.score.desc())
    matches = list(session.execute(stmt).scalars())

    feedback_map = listar_feedback_usuario(session, user_id)
    siguiendo_set = {(s.fuente, s.codigo_oportunidad) for s in listar_seguidas(session, user_id)}

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

        feedback = feedback_map.get((m.fuente, m.codigo_oportunidad))
        if feedback is not None and feedback.valor == ValorFeedback.DESCARTE.value:
            continue

        result.append(
            _construir_item(
                m,
                op,
                feedback_valor=feedback.valor if feedback is not None else None,
                siguiendo=(m.fuente, m.codigo_oportunidad) in siguiendo_set,
                ahora=ahora,
            )
        )

    if orden == "cierre":
        result.sort(key=lambda r: (r["dias_al_cierre"] is None, r["dias_al_cierre"]))
    else:
        result.sort(key=lambda r: r["match"].score, reverse=True)

    total_sin_filtro = len(result)
    if min_score > 0:
        result = [r for r in result if r["match"].score >= min_score]

    total = len(result)
    return result[offset : offset + limit], total, total_sin_filtro


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


def listar_descartadas_detalle(session: Session, user_id: int) -> list[dict[str, Any]]:
    """Oportunidades descartadas del usuario (F10 parte 2), enriquecidas con
    el nombre/estado actuales — mismo patrón que `listar_seguidas_detalle`.

    Si la oportunidad subyacente ya no existe, degrada al código crudo en vez
    de romper el render (regla 6)."""
    descartadas = listar_descartadas(session, user_id)

    lic_codigos = [d.codigo_oportunidad for d in descartadas if d.fuente == "licitaciones"]
    ca_codigos = [d.codigo_oportunidad for d in descartadas if d.fuente == "compras_agiles"]

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
    for d in descartadas:
        op: Licitacion | CompraAgil | None
        if d.fuente == "licitaciones":
            op = lics.get(d.codigo_oportunidad)
        else:
            op = cas.get(d.codigo_oportunidad)

        result.append({
            "feedback": d,
            "nombre": op.nombre if op is not None else d.codigo_oportunidad,
            "estado": op.estado if op is not None else None,
        })
    return result


def resumen_competencia(session: Session, licitacion_codigo: str) -> list[dict[str, Any]]:
    """Resumen de competencia por proveedor (F-competencia), incluyendo a quienes
    ofertaron pero NO ganaron — panorama competitivo completo, no solo ganadores
    (ver deuda señalada en docs/00-estado-actual.md, resuelta en F10 parte 3).
    Por proveedor: items_ofertados (cuántos ítems ofertó en total), items_ganados
    (cuántos le fueron adjudicados) y total_adjudicado (suma de
    monto_linea_adjudicada de sus ofertas seleccionadas). Agrupa por
    rut_proveedor (más estable que el nombre, ver docs/05-competencia.md §3).
    Orden: ganadores primero por total_adjudicado desc; no-ganadores después
    por items_ofertados desc. Vacío si no hay ofertas capturadas aún."""
    ofertas = list(
        session.execute(
            select(OfertaCompetencia).where(OfertaCompetencia.licitacion_codigo == licitacion_codigo)
        ).scalars()
    )
    por_proveedor: dict[str, dict[str, Any]] = {}
    for o in ofertas:
        entry = por_proveedor.setdefault(
            o.rut_proveedor,
            {
                "rut_proveedor": o.rut_proveedor,
                "nombre_proveedor": o.nombre_proveedor,
                "items_ofertados": 0,
                "items_ganados": 0,
                "total_adjudicado": 0.0,
            },
        )
        entry["items_ofertados"] += 1
        if o.seleccionada:
            entry["items_ganados"] += 1
            entry["total_adjudicado"] += o.monto_linea_adjudicada or 0.0
    resumen = list(por_proveedor.values())
    resumen.sort(
        key=lambda d: (
            d["items_ganados"] == 0,
            -d["total_adjudicado"],
            -d["items_ofertados"],
        )
    )
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


def buscar_instituciones_pac(
    session: Session,
    texto: str,
    *,
    limit: int = 20,
) -> list[InstitucionPAC]:
    """Autocomplete del Plan Anual de Compra: instituciones por razón social
    (caché de app.ingest.plan_compra.sync_instituciones_pac). Vacío si `texto`
    está vacío — no lista las ~1.333 instituciones de una sola vez."""
    texto = texto.strip()
    if not texto:
        return []
    return list(
        session.execute(
            select(InstitucionPAC)
            .where(InstitucionPAC.razon_social.ilike(f"%{texto}%"))
            .order_by(InstitucionPAC.razon_social)
            .limit(limit)
        ).scalars()
    )


def listar_organismos_catalogo(session: Session) -> list[InstitucionPAC]:
    """Catálogo completo de organismos (F-plan + sector de F-datos) para el
    multi-select agrupado por sector del formulario de perfiles (F10).

    Orden alfabético por `sector` deja "Sin clasificación" al final de forma
    natural (empieza con "Si", posterior a los 7 sectores nombrados en orden
    alfabético — ver docs/08-datos-organismos.md §3-bis b), sin necesidad de
    un caso especial. Vacío si el catálogo aún no se sincronizó (regla 6: la
    ruta debe degradar a un campo de texto libre en ese caso, no romper)."""
    orden_sector = func.coalesce(InstitucionPAC.sector, SECTOR_SIN_CLASIFICACION)
    return list(
        session.execute(select(InstitucionPAC).order_by(orden_sector, InstitucionPAC.razon_social)).scalars()
    )


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
