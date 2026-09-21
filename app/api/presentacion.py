"""Funciones puras de presentación para la capa web.

Convierten estructuras internas (razones de match, códigos de región) en
textos legibles para las plantillas. Sin acceso a BD ni a la red: testables
directamente.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.catalogos.unspsc import nombre_rubro
from app.core.settings import VERSION_ESTATICOS
from app.models.enums import FamiliaEstado, familia_de_estado
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
    """Prepara el entorno Jinja: filtros `clp` y `numero`, más la versión de
    los estáticos, que `base.html` necesita en TODAS las páginas (incluida la
    de login, que no pasa por el contexto del dashboard).

    Recibe el `Environment` de un `Jinja2Templates`; se tipa como Any para que
    este módulo siga sin depender de la capa web.
    """
    env.filters["clp"] = formato_clp
    env.filters["numero"] = formato_numero
    env.globals["version_estaticos"] = VERSION_ESTATICOS


def _campo_legible(campo: str) -> str:
    return {
        "nombre": "el título",
        "descripcion": "la descripción",
        "producto": "los productos",
    }.get(campo, "el texto")


def razones_tipificadas(razones: dict[str, Any] | None) -> list[dict[str, str]]:
    """Razones del match como chips: `{"texto": ..., "tipo": ...}`.

    `tipo` es uno de:
    - "match": por qué calzó con el perfil (keyword, rubro, organismo seguido).
    - "oportunidad": señal a favor de presentarse (poca o ninguna competencia).
    - "advertencia": señal de cautela (mucha competencia, monto sin informar).

    Es la ÚNICA fuente de las frases: `razones_legibles` se deriva de acá para
    que la ficha y la tarjeta nunca digan cosas distintas.

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

    chips: list[dict[str, str]] = []

    keywords_hit = razones.get("keywords_hit") or []
    if keywords_hit:
        kws = ", ".join(str(k) for k in keywords_hit)
        campo = _campo_legible(str(razones.get("campo_hit", "")))
        chips.append({"texto": f"Coincide en {campo} con: {kws}", "tipo": "match"})

    ofertas = razones.get("ofertas")
    if ofertas is not None:
        try:
            n = int(ofertas)
        except (TypeError, ValueError):
            n = None
        if n is not None:
            if n == 0:
                chips.append({"texto": "Aún sin ofertas competidoras", "tipo": "oportunidad"})
            elif n <= 3:
                chips.append({"texto": f"Poca competencia: {n} oferta(s)", "tipo": "oportunidad"})
            else:
                # Varias ofertas ya en juego: para quien decide presentarse es
                # una señal de cautela, no una ventaja.
                chips.append({"texto": f"{n} ofertas registradas", "tipo": "advertencia"})

    if razones.get("monto_no_informado"):
        chips.append({"texto": "Monto no informado por el organismo", "tipo": "advertencia"})

    categorias_hit = razones.get("categorias_hit") or []
    if categorias_hit:
        nombres = [nombre_rubro(str(c)) or str(c) for c in categorias_hit]
        chips.append(
            {"texto": f"Coincide con rubro que sigues: {', '.join(nombres)}", "tipo": "match"}
        )

    if razones.get("organismo_seguido"):
        chips.append({"texto": "De un organismo que sigues", "tipo": "match"})

    return chips


def razones_legibles(razones: dict[str, Any] | None) -> list[str]:
    """Las mismas frases de `razones_tipificadas`, sin el tipo."""
    return [c["texto"] for c in razones_tipificadas(razones)]


# ---------------------------------------------------------------------------
# Familias de estado (F-feed-ui-1)
# ---------------------------------------------------------------------------

# Etiqueta legible y sufijo de clase CSS por familia. La plantilla no decide
# nada: pide la etiqueta y la clase, y las pinta.
_FAMILIAS: dict[FamiliaEstado, tuple[str, str]] = {
    FamiliaEstado.ABIERTA: ("Abierta", "familia_abierta"),
    FamiliaEstado.EN_EVALUACION: ("En evaluación", "familia_eval"),
    FamiliaEstado.ADJUDICADA: ("Adjudicada", "familia_adj"),
    FamiliaEstado.COMPLETADA: ("Completada", "familia_completada"),
    FamiliaEstado.SIN_EFECTO: ("Sin efecto", "familia_sinefecto"),
    FamiliaEstado.DESCONOCIDO: ("Estado no informado", "familia_desconocido"),
}


def presentacion_estado(estado: object) -> dict[str, str]:
    """`{"familia", "etiqueta", "clase"}` para el badge de estado.

    Un estado que la fuente todavía no declaraba cae en DESCONOCIDO y se
    muestra como "Estado no informado": nunca un badge vacío (regla 6).
    """
    familia = familia_de_estado(estado)
    etiqueta, clase = _FAMILIAS[familia]
    return {"familia": str(familia), "etiqueta": etiqueta, "clase": clase}


# ---------------------------------------------------------------------------
# Cierre: urgencia y texto (F-feed-ui-1)
# ---------------------------------------------------------------------------


def banda_urgencia(dias_al_cierre: float | None) -> str:
    """'critica' | 'alta' | 'media' | 'baja', para el borde izquierdo de la tarjeta.

    Sin fecha de cierre → 'baja': un borde gris no afirma nada.
    """
    if dias_al_cierre is None:
        return "baja"
    if dias_al_cierre <= 1:
        return "critica"
    if dias_al_cierre <= 3:
        return "alta"
    if dias_al_cierre <= 7:
        return "media"
    return "baja"


def texto_cierre(
    fecha_cierre: datetime | None,
    dias_al_cierre: float | None,
    fuente: str,
) -> str:
    """Texto del badge de cierre.

    La hora SOLO se muestra en Compra Ágil. En licitaciones la hora es
    fabricada por el parser (`parse_fecha_v1` corta el ISO a 10 caracteres y
    `_fecha_a_dt` expande a medianoche), así que publicar "00:00" sería
    presentar como dato de la fuente algo que la fuente no entregó. Vuelve en
    F-fecha-cierre. Ver docs/00-estado-actual.md.

    Tampoco se afirma zona horaria en Compra Ágil: el huso de la fuente no
    está verificado.
    """
    if fecha_cierre is None:
        return "Sin fecha de cierre"

    if dias_al_cierre is None:
        return f"Cierra el {fecha_cierre.strftime('%d/%m/%Y')}"

    if dias_al_cierre <= 0:
        return f"Cerró el {fecha_cierre.strftime('%d/%m')}"

    dias = int(dias_al_cierre)
    if dias == 0:
        cuando = "Cierra hoy"
    elif dias == 1:
        cuando = "Cierra mañana"
    else:
        cuando = f"Cierra en {dias} días"

    if fuente == "compras_agiles":
        return f"{cuando} · {fecha_cierre.strftime('%d/%m/%Y %H:%M')}"
    return f"{cuando} · {fecha_cierre.strftime('%d/%m/%Y')}"
