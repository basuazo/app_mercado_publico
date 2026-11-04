"""Territorio que se ingesta: unión de las regiones priorizadas de las empresas activas (R-p2).

Única fuente de verdad para los consumidores de `mercado_regiones` (ingesta en vivo,
backfill, `mercado-oc`, purga y scripts). El setting queda solo como fallback.
Las comunas priorizadas NO filtran la ingesta (la API no filtra por comuna; regla 7).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.settings import Settings
from app.models.tables import Empresa


def regiones_activas(session: Session, settings: Settings) -> list[int]:
    """Unión ordenada de `regiones_prioritarias` de empresas activas; vacía → el setting."""
    filas = session.execute(
        select(Empresa.regiones_prioritarias).where(Empresa.activa.is_(True))
    ).scalars()
    regiones: set[int] = set()
    for lista in filas:
        for r in lista or []:
            try:
                regiones.add(int(r))
            except (TypeError, ValueError):
                continue
    if regiones:
        return sorted(regiones)
    return list(settings.mercado_regiones)


def regiones_activas_engine(engine: Engine, settings: Settings) -> list[int]:
    """Como :func:`regiones_activas`, abriendo su propia sesión (jobs que no traen una)."""
    with Session(engine) as session:
        return regiones_activas(session, settings)
