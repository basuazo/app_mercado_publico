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
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.catalogos.mercado import norm
from app.mercado.indice import LOTE, _rut
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


def _elegir(
    ofertas: list[tuple[str, str | None, bool | None, float | None]],
) -> dict[str, tuple[str, bool | None, float | None]]:
    """Una oferta por RUT (F-merc-1c): la seleccionada; si no, la de menor valor neto."""
    elegida: dict[str, tuple[str, bool | None, float | None]] = {}
    for rut, nombre, sel, valor in ofertas:
        if not rut:
            continue
        act = elegida.get(rut)
        s, s_act = sel is True, act is not None and act[1] is True
        v = valor if valor is not None else float("inf")
        v_act = act[2] if act is not None and act[2] is not None else float("inf")
        if act is None or (s and not s_act) or (s == s_act and v < v_act):
            elegida[rut] = (nombre or "", sel, valor)
    return elegida


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
            ofertas[ca].append((_rut(rut), nombre, sel, valor))
    n_oferentes: list[int] = []
    ganadas: Counter[str] = Counter()
    nombres: dict[str, str] = {}
    for codigo in codigos:
        elegidas = _elegir(ofertas[codigo])
        if codigo in leidas:
            n_oferentes.append(len(elegidas))
        for rut, (nombre, sel, valor) in elegidas.items():
            if sel is True:
                ganadas[rut] += 1
                nombres.setdefault(rut, nombre)
                if valor is not None:
                    hist.monto_adjudicado += valor
    hist.n_con_ofertas = len(n_oferentes)
    hist.mediana_oferentes = statistics.median(n_oferentes) if n_oferentes else None
    orden = sorted(ganadas.items(), key=lambda kv: (-kv[1], nombres.get(kv[0], ""), kv[0]))
    hist.top_proveedores = [(r, nombres.get(r) or r, n) for r, n in orden[:TOP_PROVEEDORES]]
    return hist
