"""Comunas de Ñuble por provincia (F-plat-4): índice de precios y logística por provincia.

Lista [I, de conocimiento general]: Boris la confirma o la contrasta con la fuente oficial antes
de marcarla [V]. Las tres claves son las de `app.mercado.proveedores.PROVINCIAS_NUBLE` (las mismas
de `tarifas_logistica`), importadas para no repetir los nombres.
"""

from __future__ import annotations

import re

from app.catalogos.mercado import norm
from app.mercado.proveedores import PROVINCIAS_NUBLE

REGION_NUBLE = 16

_DIGUILLIN, _ITATA, _PUNILLA = PROVINCIAS_NUBLE

COMUNAS_POR_PROVINCIA: dict[str, tuple[str, ...]] = {
    _DIGUILLIN: (
        "Bulnes", "Chillán", "Chillán Viejo", "El Carmen", "Pemuco", "Pinto", "Quillón",
        "San Ignacio", "Yungay",
    ),
    _ITATA: ("Cobquecura", "Coelemu", "Ninhue", "Portezuelo", "Quirihue", "Ránquil", "Treguaco"),
    _PUNILLA: ("Coihueco", "Ñiquén", "San Carlos", "San Fabián", "San Nicolás"),
}  # fmt: skip

# Otras grafías → nombre canónico. "Trehuaco" es como lo escribe el municipio.
_ALIAS: dict[str, str] = {"trehuaco": "Treguaco"}

_CANONICA: dict[str, str] = {
    norm(c): c for comunas in COMUNAS_POR_PROVINCIA.values() for c in comunas
} | _ALIAS
_PROVINCIA: dict[str, str] = {c: p for p, comunas in COMUNAS_POR_PROVINCIA.items() for c in comunas}
# Las más largas primero: "chillan viejo" antes que "chillan".
_BUSQUEDA: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"\b{re.escape(clave)}\b"), canon)
    for clave, canon in sorted(_CANONICA.items(), key=lambda kv: -len(kv[0]))
]


def comuna_canonica(texto: str | None) -> str | None:
    """'CHILLAN' / 'chillán' / 'Trehuaco' → nombre canónico; fuera de Ñuble o vacío → None."""
    return _CANONICA.get(norm(texto))


def provincia_de(comuna: str | None) -> str | None:
    """Provincia de una comuna de Ñuble (cualquier grafía); None si no es de Ñuble."""
    canon = comuna_canonica(comuna)
    return _PROVINCIA.get(canon) if canon else None


def comuna_en_texto(texto: str | None) -> str | None:
    """Primera comuna de Ñuble nombrada en un texto libre (palabra completa, la más larga gana)."""
    t = norm(texto)
    if not t:
        return None
    for patron, canon in _BUSQUEDA:
        if patron.search(t):
            return canon
    return None
