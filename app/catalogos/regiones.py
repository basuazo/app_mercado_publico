"""Código de región (1–16) desde el texto de oc-da (`RegionUnidadCompra`, `RegionProveedor`).

En oc-da la región viene como TEXTO ("Región del Ñuble", a veces con espacio al final o
"NA") y no hay código [V, F-merc-2 §3]. Desconocido → None y un log por valor distinto (regla 6).
"""

from __future__ import annotations

from app.catalogos.mercado import norm
from app.core.logging import get_logger

_log = get_logger(__name__)

# (fragmento ya normalizado, código). El primero que esté contenido en el texto gana.
_REGIONES: tuple[tuple[str, int], ...] = (
    ("arica", 15), ("tarapaca", 1), ("antofagasta", 2), ("atacama", 3), ("coquimbo", 4),
    ("valparaiso", 5), ("metropolitana", 13), ("higgins", 6), ("maule", 7), ("uble", 16),
    ("biobio", 8), ("bio bio", 8), ("araucania", 9), ("los rios", 14), ("los lagos", 10),
    ("aysen", 11), ("magallanes", 12),
)  # fmt: skip
_SIN_DATO = {"", "na", "n/a", "null", "sin informacion", "sin dato"}
_AVISADOS: set[str] = set()


def region_codigo(texto: str | None) -> int | None:
    t = norm(texto)
    if t in _SIN_DATO:
        return None
    for fragmento, codigo in _REGIONES:
        if fragmento in t:
            return codigo
    if t not in _AVISADOS:
        _AVISADOS.add(t)
        _log.warning("region_codigo: región desconocida %r; queda sin código", texto)
    return None
