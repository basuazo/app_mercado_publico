"""Regla de elección F-merc-1c: ÚNICA implementación (F-plat-comp).

Un RUT puede mandar varias ofertas a la misma CA [V, sonda 4-nov-2026]. Para todo análisis queda
UNA por RUT: la seleccionada; si ninguna lo está, la de menor precio. Entre dos seleccionadas del
mismo RUT, la más barata. Un precio `None` pierde contra cualquier precio. El n.º de oferentes es
el n.º de RUT distintos (las claves del resultado), no de filas.

La usan `indice` (precio por línea), `adjudicacion` (dataset del modelo), `comprador` (historial
de la mesa) y `competencia`. Módulo sin dependencias para que nadie la vuelva a copiar por evitar
una importación circular.

No descarta el RUT vacío: lo agrupa como un oferente más (comportamiento de `indice` y
`adjudicacion` antes de unificar). `comprador.historial_comprador` y `competencia` filtran los
RUT vacíos antes de llamar: una oferta sin RUT no se puede atribuir a un competidor.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

Of = TypeVar("Of")
_PEOR = float("inf")


def elegir_por_rut(
    ofertas: Iterable[Of],
    rut: Callable[[Of], str],
    seleccionado: Callable[[Of], bool | None],
    precio: Callable[[Of], float | None],
) -> dict[str, Of]:
    """`{rut: oferta elegida}` con la regla F-merc-1c; devuelve las ofertas tal cual llegan."""
    elegida: dict[str, Of] = {}
    for o in ofertas:
        r = rut(o)
        act = elegida.get(r)
        if act is None:
            elegida[r] = o
            continue
        s, s_act = seleccionado(o) is True, seleccionado(act) is True
        p, p_act = precio(o), precio(act)
        v = _PEOR if p is None else p
        v_act = _PEOR if p_act is None else p_act
        if (s and not s_act) or (s == s_act and v < v_act):
            elegida[r] = o
    return elegida
