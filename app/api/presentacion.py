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


def _campo_legible(campo: str) -> str:
    return {
        "nombre": "el título",
        "descripcion": "la descripción",
        "producto": "los productos",
    }.get(campo, "el texto")


def razones_legibles(razones: dict[str, Any] | None) -> list[str]:
    """Traduce el dict de razones del match a frases para mostrar al usuario.

    El dict proviene del motor de matching y puede contener:
    keywords_hit (list[str]), campo_hit (str), dias_al_cierre (float),
    ofertas (int|None), monto_no_informado (bool).
    """
    if not razones:
        return []

    frases: list[str] = []

    keywords_hit = razones.get("keywords_hit") or []
    if keywords_hit:
        kws = ", ".join(str(k) for k in keywords_hit)
        campo = _campo_legible(str(razones.get("campo_hit", "")))
        frases.append(f"Coincide en {campo} con: {kws}")

    dias = razones.get("dias_al_cierre")
    if dias is not None:
        try:
            d = float(dias)
        except (TypeError, ValueError):
            d = None
        if d is not None:
            if d < 1:
                frases.append("Cierra hoy")
            elif d <= 7:
                frases.append(f"Cierra pronto: {round(d)} día(s) para el cierre")
            else:
                frases.append(f"{round(d)} días para el cierre")

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
