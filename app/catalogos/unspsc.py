"""Catálogo de rubros UNSPSC (segmento/familia) para matching aditivo (F9b).

Fuente: data/unspsc_rubros.csv (export UNGM UNSPSC, 22-jun-2026; columnas
nivel(segmento|familia), codigo, nombre). Se carga una sola vez en memoria;
funciones puras, sin BD, testables directamente.
"""

from __future__ import annotations

import csv
import functools
from pathlib import Path

from app.core.logging import get_logger

_log = get_logger(__name__)

_CSV_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "unspsc_rubros.csv"

_Rubros = tuple[tuple[str, str], ...]


@functools.cache
def _datos() -> tuple[_Rubros, _Rubros]:
    segs: list[tuple[str, str]] = []
    fams: list[tuple[str, str]] = []
    try:
        with _CSV_PATH.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                nivel = (row.get("nivel") or "").strip()
                codigo = (row.get("codigo") or "").strip()
                nombre = (row.get("nombre") or "").strip()
                if not codigo or not nombre:
                    continue
                if nivel == "segmento":
                    segs.append((codigo, nombre))
                elif nivel == "familia":
                    fams.append((codigo, nombre))
    except OSError:
        _log.warning("catalogo_unspsc: no se encontró %s; catálogo vacío", _CSV_PATH)
        return (), ()
    return tuple(segs), tuple(fams)


def segmentos() -> list[tuple[str, str]]:
    """Lista (codigo, nombre) de segmentos UNSPSC (2 dígitos)."""
    return list(_datos()[0])


def familias() -> list[tuple[str, str]]:
    """Lista (codigo, nombre) de familias UNSPSC (4 dígitos). familia[:2] = su segmento."""
    return list(_datos()[1])


def nombre_rubro(prefijo: str) -> str | None:
    """Nombre legible para un prefijo UNSPSC.

    Match exacto contra familia (4 díg) o segmento (2 díg). Para prefijos más
    finos (6/8 dígitos de clase/commodity, no presentes en el catálogo),
    resuelve por su familia ([:4]) y, si tampoco existe, por su segmento ([:2]).
    """
    p = prefijo.strip()
    if not p:
        return None
    fam = dict(familias())
    seg = dict(segmentos())
    if p in fam:
        return fam[p]
    if p in seg:
        return seg[p]
    if len(p) >= 4 and p[:4] in fam:
        return fam[p[:4]]
    if len(p) >= 2 and p[:2] in seg:
        return seg[p[:2]]
    return None
