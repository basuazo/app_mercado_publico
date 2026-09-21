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
        fecha=date(2026, 9, 21),
        titulo="La búsqueda deja de insistir cuando la fuente la rechaza",
        descripcion=(
            "Cuando Mercado Público nos cerraba la puerta por el día, la app seguía "
            "pidiendo datos igual y gastaba el cupo diario sin traerse nada. Ahora se "
            "detiene apenas aparece el rechazo, guarda lo que alcanzó a bajar y el "
            "contador de consultas del día refleja el uso real."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 21),
        titulo="Mejoras de navegación, formato de montos y accesibilidad",
        descripcion=(
            "Arreglamos la navegación en celulares, los montos ahora se muestran en "
            "formato chileno y mejoramos el uso con lector de pantalla."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 20),
        titulo="La b\u00fasqueda de oportunidades ya no depende de que la app est\u00e9 despierta",
        descripcion=(
            "Hasta ahora, las cargas de datos solo ocurr\u00edan mientras la app segu\u00eda "
            "encendida, y bastaba que se apagara un rato para que dejaran de correr. "
            "Ahora se disparan desde afuera en su horario, funcione o no la app en ese "
            "momento. En la pr\u00e1ctica: menos cortes silenciosos y datos m\u00e1s al d\u00eda."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 20),
        titulo="Avisamos si la carga de datos se detiene",
        descripcion=(
            "Antes, si la b\u00fasqueda de nuevas oportunidades dejaba de funcionar, no se "
            "notaba: la pantalla se ve\u00eda igual, solo con menos resultados. Ahora el "
            "sistema guarda un registro de cada carga de datos y avisa solo cuando una "
            "lleva demasiado tiempo sin completarse, as\u00ed se arregla r\u00e1pido."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 7, 10),
        titulo="Mejor detecci\u00f3n por rubro en licitaciones",
        descripcion=(
            "La carga de \u00edtems UNSPSC de licitaciones ahora revisa el mes vigente y "
            "meses anteriores de datos abiertos. Esto mejora los resultados cuando una "
            "licitaci\u00f3n sigue publicada pero fue creada en un mes anterior."
        ),
    ),
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
