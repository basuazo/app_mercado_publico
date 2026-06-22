"""Queries compartidas entre rutas HTML y API REST."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.presentacion import nombre_region, razones_legibles
from app.matching.perfiles import listar_perfiles
from app.models.enums import EstadoOportunidad
from app.models.tables import CompraAgil, Licitacion, OportunidadMatch


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
