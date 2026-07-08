"""User-visible changelog, versioned with the code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ChangelogEntry:
    fecha: date
    titulo: str
    descripcion: str


CHANGELOG: list[ChangelogEntry] = [
    ChangelogEntry(
        fecha=date(2026, 7, 7),
        titulo="Menos correos, m\u00e1s \u00fatiles",
        descripcion=(
            "Antes llegaba un correo por cada oportunidad que calzaba con tu perfil y se "
            "llenaba la bandeja. Ahora recib\u00eds un solo correo-resumen cada cierto tiempo "
            "('encontramos X oportunidades para tu perfil') que te invita a entrar y "
            "revisarlas ac\u00e1. Eleg\u00ed cada cu\u00e1ntos d\u00edas recibirlo \u2014o desactivalo\u2014 en "
            "Ajustes de tu cuenta. Y en las oportunidades que te interesan, toc\u00e1 "
            "'Activar alertas' para que te avisemos si cambian de estado (por ejemplo, "
            "al adjudicarse)."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 7, 7),
        titulo="Ahora ves las Compras \u00c1giles",
        descripcion=(
            "Corregimos un problema que imped\u00eda que las Compras \u00c1giles aparecieran en "
            "los resultados. Ahora se muestran junto a las licitaciones. Pod\u00e9s filtrar "
            "por fuente (Licitaciones / Compra \u00c1gil) y ajustar la relevancia del feed "
            "(Alta / Media / Todas)."
        ),
    ),
]


def entradas_changelog() -> list[ChangelogEntry]:
    """Return all entries, newest first."""
    return sorted(CHANGELOG, key=lambda e: e.fecha, reverse=True)


def fecha_ultima_novedad() -> date | None:
    """Return the newest changelog date, or None when there are no entries."""
    entradas = entradas_changelog()
    return entradas[0].fecha if entradas else None
