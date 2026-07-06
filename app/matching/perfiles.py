"""CRUD de perfiles_busqueda con ownership obligatorio.

Regla 17 CLAUDE.md: Ownership SIEMPRE verificado en servidor.
Un usuario solo ve/edita sus propios perfiles.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.tables import PerfilBusqueda

_log = get_logger(__name__)


class PerfilInvalido(ValueError):
    """Perfil sin al menos 1 keyword o 1 filtro estructurado."""


def _validar(
    keywords: list[str],
    regiones: list[int],
    monto_min: float | None,
    monto_max: float | None,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
) -> None:
    tiene_keywords = bool(keywords)
    tiene_filtro = bool(regiones) or monto_min is not None or monto_max is not None
    tiene_rubro_organismo = bool(categorias_unspsc) or bool(organismos_seguidos)
    if not (tiene_keywords or tiene_filtro or tiene_rubro_organismo):
        raise PerfilInvalido(
            "Se necesita al menos 1 keyword o 1 filtro estructurado "
            "(región, monto, rubro UNSPSC u organismo seguido)"
        )


def crear_perfil(
    session: Session,
    owner_id: int,
    nombre: str,
    *,
    keywords: list[str] | None = None,
    keywords_excluir: list[str] | None = None,
    regiones: list[int] | None = None,
    monto_min_clp: float | None = None,
    monto_max_clp: float | None = None,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
    fuentes: list[str] | None = None,
) -> PerfilBusqueda:
    """Crea un perfil de búsqueda. Lanza PerfilInvalido si no cumple mínimo."""
    kw = list(keywords or [])
    kw_excluir = list(keywords_excluir or [])
    regs = list(regiones or [])
    cats = list(categorias_unspsc or [])
    orgs = list(organismos_seguidos or [])
    fuentes_list: list[str] = list(fuentes or ["licitaciones", "compras_agiles"])

    _validar(kw, regs, monto_min_clp, monto_max_clp, cats, orgs)

    perfil = PerfilBusqueda(
        owner_id=owner_id,
        nombre=nombre,
        keywords=kw,
        keywords_excluir=kw_excluir,
        regiones=regs,
        monto_min_clp=monto_min_clp,
        monto_max_clp=monto_max_clp,
        categorias_unspsc=cats,
        organismos_seguidos=orgs,
        fuentes=fuentes_list,
        activo=True,
    )
    session.add(perfil)
    session.flush()
    return perfil


def obtener_perfil(
    session: Session,
    perfil_id: int,
    owner_id: int,
) -> PerfilBusqueda | None:
    """Retorna el perfil solo si pertenece al owner. None en caso contrario."""
    p = session.get(PerfilBusqueda, perfil_id)
    if p is None or p.owner_id != owner_id:
        return None
    return p


def listar_perfiles(session: Session, owner_id: int) -> list[PerfilBusqueda]:
    """Devuelve todos los perfiles activos del usuario."""
    return list(
        session.execute(
            select(PerfilBusqueda).where(
                PerfilBusqueda.owner_id == owner_id,
                PerfilBusqueda.activo.is_(True),
            )
        ).scalars()
    )


def actualizar_perfil(
    session: Session,
    perfil_id: int,
    owner_id: int,
    **campos: Any,
) -> PerfilBusqueda | None:
    """Actualiza campos del perfil; retorna None si no existe o no es del owner."""
    p = obtener_perfil(session, perfil_id, owner_id)
    if p is None:
        return None
    for k, v in campos.items():
        if hasattr(p, k):
            setattr(p, k, v)
    kw = cast(list[str], list(p.keywords or []))
    regs = cast(list[int], list(p.regiones or []))
    cats = cast(list[str], list(p.categorias_unspsc or []))
    orgs = cast(list[str], list(p.organismos_seguidos or []))
    _validar(kw, regs, p.monto_min_clp, p.monto_max_clp, cats, orgs)
    return p


def eliminar_perfil(
    session: Session,
    perfil_id: int,
    owner_id: int,
) -> bool:
    """Elimina el perfil si pertenece al owner. Retorna False si no encontrado."""
    p = obtener_perfil(session, perfil_id, owner_id)
    if p is None:
        return False
    session.delete(p)
    return True
