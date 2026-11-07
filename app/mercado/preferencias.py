"""Preferencias del usuario (F-dis-1): apariencia y filtros recordados por pantalla.

Sin HTML ni `Request`. La apariencia vive en tres columnas de `usuarios`; los filtros de cada
pantalla, en `usuario_vista` (uno por usuario y pantalla, `datos` ≤ 2 KB). Solo se guardan las
claves de la lista blanca de cada pantalla: lo demás se descarta en silencio (una URL vieja o
manipulada no ensucia la fila). Cada fase de pantalla agrega aquí sus claves.

Ownership (regla 17): todo se lee y escribe por `usuario_id`; la ruta pasa el del usuario de la
sesión, nunca uno que venga del formulario.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tiempo import ahora_utc
from app.models.tables import Usuario, UsuarioVista

TEMAS: dict[str, str] = {"claro": "Claro", "oscuro": "Oscuro", "sistema": "Según el sistema"}
DENSIDADES: dict[str, str] = {"comoda": "Cómoda", "compacta": "Compacta"}
INICIOS: dict[str, str] = {
    "hoy": "Hoy",
    "mesa": "Mesa",
    "trabajo": "En trabajo",
    "ventas": "Ventas",
}
# Pantalla de inicio → ruta. «hoy» es la portada misma.
URL_INICIO: dict[str, str] = {
    "hoy": "/",
    "mesa": "/mesa",
    "trabajo": "/trabajo",
    "ventas": "/ventas",
}

TEMA_DEFECTO = "claro"
DENSIDAD_DEFECTO = "comoda"
INICIO_DEFECTO = "hoy"

MAX_BYTES_VISTA = 2048

# Lista blanca: pantalla → claves que se recuerdan. Las fases de pantalla agregan las suyas.
CLAVES_POR_PANTALLA: dict[str, frozenset[str]] = {
    # Hoy (Main.dc.html): período de las cifras y filtro de «Te necesita».
    "hoy": frozenset({"periodo", "filtro"}),
}


class PreferenciaInvalida(ValueError):
    """Valor fuera de los permitidos, pantalla desconocida o vista demasiado grande."""


# ---------------------------------------------------------------------------
# Apariencia
# ---------------------------------------------------------------------------


def apariencia(user: Usuario | None) -> dict[str, str]:
    """`{"tema", "densidad", "inicio"}` con defecto para lo que falte o sea inválido.

    Sin usuario (login) o con un valor que ya no existe, manda el defecto: la página nunca
    queda sin tema."""
    if user is None:
        return {"tema": TEMA_DEFECTO, "densidad": DENSIDAD_DEFECTO, "inicio": INICIO_DEFECTO}
    tema = getattr(user, "tema", None)
    densidad = getattr(user, "densidad", None)
    inicio = getattr(user, "inicio", None)
    return {
        "tema": tema if tema in TEMAS else TEMA_DEFECTO,
        "densidad": densidad if densidad in DENSIDADES else DENSIDAD_DEFECTO,
        "inicio": inicio if inicio in INICIOS else INICIO_DEFECTO,
    }


def guardar_apariencia(user: Usuario, tema: str, densidad: str, inicio: str) -> None:
    """Valida los tres valores y los asigna (el commit lo hace quien llama).

    Un valor inválido lanza `PreferenciaInvalida` y no cambia nada."""
    if tema not in TEMAS:
        raise PreferenciaInvalida(f"Tema inválido: {tema!r}")
    if densidad not in DENSIDADES:
        raise PreferenciaInvalida(f"Densidad inválida: {densidad!r}")
    if inicio not in INICIOS:
        raise PreferenciaInvalida(f"Pantalla de inicio inválida: {inicio!r}")
    user.tema = tema
    user.densidad = densidad
    user.inicio = inicio


def url_inicio(user: Usuario) -> str:
    """Ruta de la pantalla de inicio elegida («/» si es Hoy)."""
    return URL_INICIO[apariencia(user)["inicio"]]


# ---------------------------------------------------------------------------
# Filtros recordados por pantalla
# ---------------------------------------------------------------------------


def _valor_simple(valor: Any) -> bool:
    if isinstance(valor, bool | int | float | str) or valor is None:
        return True
    if isinstance(valor, list):
        return all(isinstance(v, bool | int | float | str) for v in valor)
    return False


def filtrar_vista(pantalla: str, datos: dict[str, Any]) -> dict[str, Any]:
    """Solo las claves de la lista blanca con valores simples (texto, número, booleano o lista
    de ellos). Lanza `PreferenciaInvalida` si la pantalla no tiene lista o el resultado pesa
    más de `MAX_BYTES_VISTA`."""
    permitidas = CLAVES_POR_PANTALLA.get(pantalla)
    if permitidas is None:
        raise PreferenciaInvalida(f"Pantalla sin vista recordable: {pantalla!r}")
    limpio = {k: v for k, v in datos.items() if k in permitidas and _valor_simple(v)}
    tamano = len(json.dumps(limpio, ensure_ascii=False).encode("utf-8"))
    if tamano > MAX_BYTES_VISTA:
        raise PreferenciaInvalida(
            f"La vista de {pantalla!r} pesa {tamano} bytes (máximo {MAX_BYTES_VISTA})"
        )
    return limpio


def vista_de(session: Session, usuario_id: int, pantalla: str) -> dict[str, Any]:
    """Filtros recordados del usuario en esa pantalla (`{}` si no hay)."""
    fila = session.execute(
        select(UsuarioVista).where(
            UsuarioVista.usuario_id == usuario_id, UsuarioVista.pantalla == pantalla
        )
    ).scalar_one_or_none()
    if fila is None or not isinstance(fila.datos, dict):
        return {}
    permitidas = CLAVES_POR_PANTALLA.get(pantalla, frozenset())
    return {k: v for k, v in fila.datos.items() if k in permitidas}


def guardar_vista(
    session: Session, usuario_id: int, pantalla: str, datos: dict[str, Any]
) -> dict[str, Any]:
    """Guarda (crea o reemplaza) la vista y devuelve lo que quedó guardado. Hace `flush`, no
    `commit`: el commit es de quien llama. Una vista vacía borra la fila."""
    limpio = filtrar_vista(pantalla, datos)
    fila = session.execute(
        select(UsuarioVista).where(
            UsuarioVista.usuario_id == usuario_id, UsuarioVista.pantalla == pantalla
        )
    ).scalar_one_or_none()
    if not limpio:
        if fila is not None:
            session.delete(fila)
            session.flush()
        return {}
    if fila is None:
        fila = UsuarioVista(usuario_id=usuario_id, pantalla=pantalla, datos=limpio)
        session.add(fila)
    else:
        fila.datos = limpio
    fila.actualizado_en = ahora_utc()
    session.flush()
    return limpio
