"""Comprador de una Compra Ágil: su clave y su historial descriptivo (F-mesa-v0).

`clave_unidad` es la ÚNICA definición de "quién compra": RUT del organismo + unidad de compra,
normalizados. La usan el modelo de adjudicación (F-plat-4), la mesa (F-mesa-v0) y, después,
F-plat-5, F-plat-6 y F-plat-comp. No crear otra.

`historial_comprador` es descriptivo (últimos 12 meses); el análisis de competencia es de
F-plat-comp. Reglas de elección de F-merc-1c: un RUT puede mandar varias ofertas a una CA; el
ganador es su oferta seleccionada y el n.º de oferentes cuenta RUT distintos, no filas.
Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from operator import itemgetter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.catalogos.mercado import norm
from app.mercado.eleccion import elegir_por_rut
from app.mercado.indice import LOTE, _rut
from app.mercado.proveedores import normalizar_rut
from app.models.tables import CaCotizacion, CompraAgil

DIAS_HISTORIAL = 365  # «últimos 12 meses»
TOP_PROVEEDORES = 3


def clave_unidad(organismo_rut: str | None, unidad_compra: str | None) -> str:
    """Clave de comprador: `RUT|unidad`, ambos normalizados (RUT sin puntos ni espacios en
    mayúscula; unidad sin tildes ni mayúsculas). Unidad nula → clave `RUT|`."""
    return f"{_rut(organismo_rut)}|{norm(unidad_compra)}"


@dataclass
class HistorialComprador:
    clave: str
    n_ca: int = 0  # CA del comprador cerradas en los últimos 12 meses
    n_con_ofertas: int = 0  # de ellas, con ofertas ya leídas (base de la mediana)
    monto_adjudicado: float = 0.0
    mediana_oferentes: float | None = None
    top_proveedores: list[tuple[str, str, int]] = field(default_factory=list)  # rut, nombre, n


def _rut_sql(col: Any) -> Any:
    """`_rut` en SQL: sin puntos ni espacios, en mayúscula (para acotar por organismo)."""
    return func.upper(func.replace(func.replace(func.coalesce(col, ""), ".", ""), " ", ""))


def rut_competidor(valor: str | None) -> tuple[str, bool]:
    """(RUT, válido) de un proveedor que oferta (F-plat-comp §6.3).

    `ca_cotizaciones.rut_proveedor` se guarda tal como viene de la API. Se normaliza con
    `proveedores.normalizar_rut` (la forma de `empresas.rut` y de `oc_lineas_ca`: sin puntos, con
    guion); si no es un RUT válido, cae a la forma laxa de `indice._rut` y avisa con `False`. Lo
    usan la mesa (top proveedores) y la competencia, para que el enlace de la mesa a
    `/competencia/{rut}` siempre encuentre al competidor. La clave de comprador NO usa esto."""
    try:
        return normalizar_rut(valor) or "", True
    except ValueError:
        return _rut(valor), False


def historial_comprador(
    session: Session,
    organismo_rut: str | None,
    unidad_compra: str | None,
    *,
    ahora: datetime,
) -> HistorialComprador:
    """Resumen de los últimos 12 meses del comprador (`clave_unidad`) hasta `ahora` (UTC naive)."""
    clave = clave_unidad(organismo_rut, unidad_compra)
    hist = HistorialComprador(clave=clave)
    desde = ahora - timedelta(days=DIAS_HISTORIAL)
    candidatas = session.execute(
        select(
            CompraAgil.codigo,
            CompraAgil.organismo_rut,
            CompraAgil.unidad_compra,
            CompraAgil.cotizaciones_obtenidas_en,
        ).where(
            _rut_sql(CompraAgil.organismo_rut) == _rut(organismo_rut),
            CompraAgil.fecha_cierre >= desde,
            CompraAgil.fecha_cierre < ahora,
        )
    ).all()
    codigos = [c[0] for c in candidatas if clave_unidad(c[1], c[2]) == clave]
    hist.n_ca = len(codigos)
    if not codigos:
        return hist
    leidas = {c[0] for c in candidatas if c[3] is not None}
    ofertas: dict[str, list[tuple[str, str | None, bool | None, float | None]]] = {
        c: [] for c in codigos
    }
    for i in range(0, len(codigos), LOTE):
        for ca, rut, nombre, sel, valor in session.execute(
            select(
                CaCotizacion.ca_codigo,
                CaCotizacion.rut_proveedor,
                CaCotizacion.razon_social,
                CaCotizacion.seleccionado,
                CaCotizacion.valor_neto,
            ).where(CaCotizacion.ca_codigo.in_(codigos[i : i + LOTE]))
        ):
            r = rut_competidor(rut)[0]
            if r:  # una oferta sin RUT no se atribuye a nadie
                ofertas[ca].append((r, nombre, sel, valor))
    n_oferentes: list[int] = []
    ganadas: Counter[str] = Counter()
    nombres: dict[str, str] = {}
    for codigo in codigos:
        elegidas = elegir_por_rut(ofertas[codigo], itemgetter(0), itemgetter(2), itemgetter(3))
        if codigo in leidas:
            n_oferentes.append(len(elegidas))
        for rut, (_, nombre, sel, valor) in elegidas.items():
            if sel is True:
                ganadas[rut] += 1
                nombres.setdefault(rut, nombre or "")
                if valor is not None:
                    hist.monto_adjudicado += valor
    hist.n_con_ofertas = len(n_oferentes)
    hist.mediana_oferentes = statistics.median(n_oferentes) if n_oferentes else None
    orden = sorted(ganadas.items(), key=lambda kv: (-kv[1], nombres.get(kv[0], ""), kv[0]))
    hist.top_proveedores = [(r, nombres.get(r) or r, n) for r, n in orden[:TOP_PROVEEDORES]]
    return hist
