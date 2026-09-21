"""Funciones puras de presentación para la capa web.

Convierten estructuras internas (razones de match, códigos de región) en
textos legibles para las plantillas. Sin acceso a BD ni a la red: testables
directamente.
"""

from __future__ import annotations

from typing import Any

from app.catalogos.unspsc import nombre_rubro
from app.models.seeds import REGIONES

# Mapa código → nombre de región (derivado de seeds, fuente única de verdad).
_REGIONES: dict[int, str] = dict(REGIONES)


def nombre_region(codigo: int | None) -> str | None:
    """Nombre de la región a partir de su código numérico.

    Devuelve None si el código es None; el código tal cual (str) si es
    desconocido, para no perder información.
    """
    if codigo is None:
        return None
    nombre = _REGIONES.get(codigo)
    if nombre is None:
        return f"Región {codigo}"
    return nombre


def formato_clp(valor: float | int | None) -> str:
    """Monto en pesos chilenos con punto como separador de miles.

    None -> "No informado": el organismo no lo publica con frecuencia y el
    hueco se lee como error de la aplicación.
    """
    if valor is None:
        return "No informado"
    return "$" + f"{valor:,.0f}".replace(",", ".")


def formato_numero(valor: float | int | None) -> str:
    """Cantidad sin símbolo de moneda, mismo separador. None -> '—'."""
    if valor is None:
        return "—"
    return f"{valor:,.0f}".replace(",", ".")


def banda_relevancia(score: float, corte_alta: int, corte_media: int) -> str:
    """'alta' | 'media' | 'baja', con los mismos cortes que los presets del feed.

    Los cortes los pasa quien renderiza (la ruta), que es donde vive la única
    definición de "alta" y "media"; aquí no se duplican valores.
    """
    if score >= corte_alta:
        return "alta"
    if score >= corte_media:
        return "media"
    return "baja"


def registrar_filtros(env: Any) -> None:
    """Registra los formateadores como filtros Jinja (`clp`, `numero`).

    Recibe el `Environment` de un `Jinja2Templates`; se tipa como Any para que
    este módulo siga sin depender de la capa web.
    """
    env.filters["clp"] = formato_clp
    env.filters["numero"] = formato_numero


def _campo_legible(campo: str) -> str:
    return {
        "nombre": "el título",
        "descripcion": "la descripción",
        "producto": "los productos",
    }.get(campo, "el texto")


def razones_legibles(razones: dict[str, Any] | None) -> list[str]:
    """Traduce el dict de razones del match a frases para mostrar al usuario.

    El dict proviene del motor de matching y puede contener:
    keywords_hit (list[str]), campo_hit (str), ofertas (int|None),
    monto_no_informado (bool), categorias_hit (list[str]),
    organismo_seguido (bool).

    `dias_al_cierre` se ignora a propósito: la cercanía del cierre ya la
    muestra el badge de cierre de la tarjeta y de la ficha, y repetirla aquí
    decía el mismo dato dos veces en el mismo bloque visual.
    """
    if not razones:
        return []

    frases: list[str] = []

    keywords_hit = razones.get("keywords_hit") or []
    if keywords_hit:
        kws = ", ".join(str(k) for k in keywords_hit)
        campo = _campo_legible(str(razones.get("campo_hit", "")))
        frases.append(f"Coincide en {campo} con: {kws}")

    ofertas = razones.get("ofertas")
    if ofertas is not None:
        try:
            n = int(ofertas)
        except (TypeError, ValueError):
            n = None
        if n is not None:
            if n == 0:
                frases.append("Aún sin ofertas competidoras")
            elif n <= 3:
                frases.append(f"Poca competencia: {n} oferta(s)")
            else:
                frases.append(f"{n} ofertas registradas")

    if razones.get("monto_no_informado"):
        frases.append("Monto no informado por el organismo")

    categorias_hit = razones.get("categorias_hit") or []
    if categorias_hit:
        nombres = [nombre_rubro(str(c)) or str(c) for c in categorias_hit]
        frases.append(f"Coincide con rubro que sigues: {', '.join(nombres)}")

    if razones.get("organismo_seguido"):
        frases.append("De un organismo que sigues")

    return frases
