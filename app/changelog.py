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
        fecha=date(2026, 9, 22),
        titulo="La descarga de datos ya no se rinde ante un rechazo pasajero",
        descripcion=(
            "Mercado Público a veces rechaza una consulta porque le llegaron varias "
            "casi al mismo tiempo. Hasta ahora eso se trataba como si se hubiera "
            "agotado el cupo del día y la descarga se detenía. Ahora la aplicación "
            "reconoce ese aviso, espera entre medio minuto y dos minutos y vuelve a "
            "intentar hasta tres veces. Si el rechazo sigue, corta y el siguiente "
            "ciclo programado corre con normalidad. Además, cuando Mercado Público "
            "tarda demasiado en responder, la aplicación hace una pausa de un minuto "
            "antes de consultarle cualquier otra cosa, y todas las consultas pasan por "
            "un mismo regulador de ritmo, para no mandarlas de a dos."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 22),
        titulo="Panel de filtros en el tablero",
        descripcion=(
            "Ahora la lista de oportunidades tiene un panel al costado para acotarla "
            "por perfil, fuente, región, monto, fecha de cierre, estado del proceso y "
            "nivel de coincidencia. Cada opción muestra cuántas oportunidades hay "
            "antes de aplicarla, así se sabe de antemano si vale la pena. Los filtros "
            "puestos aparecen como etiquetas que se pueden quitar una por una, y la "
            "dirección de la página guarda la búsqueda completa: se puede compartir "
            "por correo o volver con el botón atrás del navegador. En el celular el "
            "panel se abre con el botón Filtros. La lista pasa a mostrarse sin "
            "agrupar —cada oportunidad una sola vez, con sus motivos de coincidencia "
            "en la propia tarjeta— y el selector de arriba permite volver a agrupar "
            "por motivo, región o fuente cuando convenga."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 22),
        titulo="La ficha y los correos dicen lo mismo que la lista sobre el cierre",
        descripcion=(
            "La ficha de una oportunidad y los correos mostraban una hora de cierre "
            "que Mercado Público nunca entregó: aparecía una medianoche inventada "
            "que además no calzaba con lo que decía la misma oportunidad en la lista. "
            "Ahora las tres pantallas usan el mismo texto, y la hora solo se muestra "
            "cuando la fuente la informa de verdad. Además, al descartar una "
            "oportunidad el teclado queda en la siguiente de la lista en vez de "
            "perderse al principio de la página."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 22),
        titulo="Preparamos los filtros nuevos de la lista de oportunidades",
        descripcion=(
            "Dejamos lista por dentro la maquinaria para acotar la lista por monto, "
            "por fecha de cierre y por la situación del proceso (abierta, en "
            "evaluación, adjudicada y las demás), para ordenarla de mayor a menor "
            "monto y para saber cuántas oportunidades llegaron hoy. Las "
            "oportunidades a las que Mercado Público no le informa el monto o la "
            "fecha de cierre siguen apareciendo: no se esconden por falta de dato. "
            "Los controles para usar todo esto llegan en la próxima actualización "
            "de la pantalla."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 21),
        titulo="Ya no se nos escapan las oportunidades en su último día",
        descripcion=(
            "Guardábamos la fecha de cierre sin la hora, y eso hacía que una "
            "licitación desapareciera de la lista y dejara de avisar durante todo su "
            "último día, justo cuando más importaba. Ahora se guarda la hora real "
            "de cierre que entrega Mercado Público, y cuando la fuente solo da el día "
            "la oportunidad sigue apareciendo hasta que ese día termina."
        ),
    ),
    ChangelogEntry(
        fecha=date(2026, 9, 21),
        titulo="Nueva vista de oportunidades",
        descripcion=(
            "Rediseñamos la lista de oportunidades: ahora cada una muestra su nivel de "
            "coincidencia en un círculo, el estado del proceso con su color e icono, y "
            "una franja de color a la izquierda que deja ver de un vistazo cuáles "
            "cierran primero. Al descartar una aparece un aviso con la opción de "
            "deshacer, por si fue sin querer."
        ),
    ),
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
