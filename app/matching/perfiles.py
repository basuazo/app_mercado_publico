"""CRUD de perfiles_busqueda con ownership obligatorio.

Regla 17 CLAUDE.md: Ownership SIEMPRE verificado en servidor.
Un usuario solo ve/edita sus propios perfiles.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.tables import PerfilBusqueda

_log = get_logger(__name__)


class PerfilInvalido(ValueError):
    """Perfil sin al menos 1 keyword o 1 filtro estructurado."""


def _validar(
    keywords: list[str],
    regiones: list[int],
    monto_min: float | None,
    monto_max: float | None,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
) -> None:
    tiene_keywords = bool(keywords)
    tiene_filtro = bool(regiones) or monto_min is not None or monto_max is not None
    tiene_rubro_organismo = bool(categorias_unspsc) or bool(organismos_seguidos)
    if not (tiene_keywords or tiene_filtro or tiene_rubro_organismo):
        raise PerfilInvalido(
            "Se necesita al menos 1 keyword o 1 filtro estructurado "
            "(región, monto, rubro UNSPSC u organismo seguido)"
        )


def crear_perfil(
    session: Session,
    owner_id: int,
    nombre: str,
    *,
    keywords: list[str] | None = None,
    keywords_excluir: list[str] | None = None,
    regiones: list[int] | None = None,
    monto_min_clp: float | None = None,
    monto_max_clp: float | None = None,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
    fuentes: list[str] | None = None,
) -> PerfilBusqueda:
    """Crea un perfil de búsqueda. Lanza PerfilInvalido si no cumple mínimo."""
    kw = list(keywords or [])
    kw_excluir = list(keywords_excluir or [])
    regs = list(regiones or [])
    cats = list(categorias_unspsc or [])
    orgs = list(organismos_seguidos or [])
    fuentes_list: list[str] = list(fuentes or ["licitaciones", "compras_agiles"])

    _validar(kw, regs, monto_min_clp, monto_max_clp, cats, orgs)
    verificar_exclusiones(session, kw, kw_excluir)

    perfil = PerfilBusqueda(
        owner_id=owner_id,
        nombre=nombre,
        keywords=kw,
        keywords_excluir=kw_excluir,
        regiones=regs,
        monto_min_clp=monto_min_clp,
        monto_max_clp=monto_max_clp,
        categorias_unspsc=cats,
        organismos_seguidos=orgs,
        fuentes=fuentes_list,
        activo=True,
    )
    session.add(perfil)
    session.flush()
    return perfil


def obtener_perfil(
    session: Session,
    perfil_id: int,
    owner_id: int,
) -> PerfilBusqueda | None:
    """Retorna el perfil solo si pertenece al owner. None en caso contrario."""
    p = session.get(PerfilBusqueda, perfil_id)
    if p is None or p.owner_id != owner_id:
        return None
    return p


def listar_perfiles(session: Session, owner_id: int) -> list[PerfilBusqueda]:
    """Devuelve todos los perfiles activos del usuario."""
    return list(
        session.execute(
            select(PerfilBusqueda).where(
                PerfilBusqueda.owner_id == owner_id,
                PerfilBusqueda.activo.is_(True),
            )
        ).scalars()
    )


def actualizar_perfil(
    session: Session,
    perfil_id: int,
    owner_id: int,
    **campos: Any,
) -> PerfilBusqueda | None:
    """Actualiza campos del perfil; retorna None si no existe o no es del owner."""
    p = obtener_perfil(session, perfil_id, owner_id)
    if p is None:
        return None
    # Las exclusiones ya guardadas no se revisan (solo se previene hacia adelante).
    ya = {str(w).lower() for w in (p.keywords_excluir or [])}
    nuevas = [w for w in campos.get("keywords_excluir", []) if str(w).lower() not in ya]
    verificar_exclusiones(
        session, list(campos["keywords"]) if "keywords" in campos else list(p.keywords or []), nuevas
    )
    for k, v in campos.items():
        if hasattr(p, k):
            setattr(p, k, v)
    kw = cast(list[str], list(p.keywords or []))
    regs = cast(list[int], list(p.regiones or []))
    cats = cast(list[str], list(p.categorias_unspsc or []))
    orgs = cast(list[str], list(p.organismos_seguidos or []))
    _validar(kw, regs, p.monto_min_clp, p.monto_max_clp, cats, orgs)
    return p


def eliminar_perfil(
    session: Session,
    perfil_id: int,
    owner_id: int,
) -> bool:
    """Elimina el perfil si pertenece al owner. Retorna False si no encontrado."""
    p = obtener_perfil(session, perfil_id, owner_id)
    if p is None:
        return False
    session.delete(p)
    return True


# ---------------------------------------------------------------------------
# "Descartar y excluir" (F-guardar, sección E)
# ---------------------------------------------------------------------------

MAX_PALABRAS_EXCLUIR = 5
_LARGO_PALABRA = (2, 60)


def normalizar_palabras_excluir(palabras: list[str]) -> list[str]:
    """Mismo criterio que el formulario de perfiles (separar por coma y recortar),
    más un largo razonable; sin duplicados (sin distinguir mayúsculas). Lanza
    PerfilInvalido si quedan más de MAX_PALABRAS_EXCLUIR."""
    salida: list[str] = []
    vistas: set[str] = set()
    for valor in palabras:
        for parte in valor.split(","):
            p = " ".join(parte.split())
            if not (_LARGO_PALABRA[0] <= len(p) <= _LARGO_PALABRA[1]):
                continue
            if p.lower() not in vistas:
                vistas.add(p.lower())
                salida.append(p)
    if len(salida) > MAX_PALABRAS_EXCLUIR:
        raise PerfilInvalido(f"Se pueden excluir hasta {MAX_PALABRAS_EXCLUIR} palabras por vez")
    return salida


def verificar_exclusiones(session: Session, keywords: list[str], palabras: list[str]) -> None:
    """Lanza PerfilInvalido si excluir alguna de `palabras` sacaría lo que las
    `keywords` del perfil buscan (F-ajustes). Sin Postgres no verifica."""
    from app.matching.engine import exclusiones_que_chocan

    chocan = exclusiones_que_chocan(session, keywords, palabras)
    if chocan:
        listado = ", ".join(f"«{w}»" for w in chocan)
        raise PerfilInvalido(
            f"{listado} sacaría{'n' if len(chocan) > 1 else ''} todo lo que trae una palabra "
            "de búsqueda de este perfil; elige otra palabra"
        )


def excluir_palabras(
    session: Session,
    owner_id: int,
    perfil_id: int,
    palabras: list[str],
) -> tuple[list[str], int] | None:
    """Agrega `palabras` a las exclusiones del perfil (regla 17: solo el dueño) y
    borra los matches vigentes que ya no calzan. Devuelve (agregadas, borrados),
    o None si el perfil no es del usuario. Seguidas y feedback quedan intactos.
    Si alguna palabra choca con las keywords del perfil lanza PerfilInvalido sin
    agregar nada. No hace commit."""
    from app.matching.engine import limpiar_matches_perfil

    p = obtener_perfil(session, perfil_id, owner_id)
    if p is None:
        return None
    actuales = cast(list[str], list(p.keywords_excluir or []))
    ya = {a.lower() for a in actuales}
    agregadas = [w for w in normalizar_palabras_excluir(palabras) if w.lower() not in ya]
    verificar_exclusiones(session, cast(list[str], list(p.keywords or [])), agregadas)
    if agregadas:
        # Lista nueva: JSONB no detecta mutaciones in-place.
        p.keywords_excluir = actuales + agregadas  # type: ignore[assignment]
        session.flush()
    borrados = limpiar_matches_perfil(session, p)
    return agregadas, borrados


def quitar_exclusiones(
    session: Session,
    owner_id: int,
    perfil_id: int,
    palabras: list[str],
) -> PerfilBusqueda | None:
    """Deshacer de "Descartar y excluir": saca esas palabras de las exclusiones.
    El llamador re-ejecuta `match_perfil` para recrear los matches. No hace commit."""
    p = obtener_perfil(session, perfil_id, owner_id)
    if p is None:
        return None
    quitar = {w.strip().lower() for w in palabras}
    actuales = cast(list[str], list(p.keywords_excluir or []))
    p.keywords_excluir = [a for a in actuales if a.lower() not in quitar]  # type: ignore[assignment]
    session.flush()
    return p


# Palabras que no sirven como exclusión (artículos, preposiciones y términos de
# trámite que aparecen en casi cualquier nombre de compra).
_STOPWORDS = frozenset(
    ["para", "con", "por", "del", "las", "los", "una", "uno", "unos", "unas", "que", "sin", "sus", "entre", "sobre", "desde", "hasta", "segun", "según", "como", "más", "mas", "este", "esta", "estos", "estas", "otro", "otros", "otra", "otras", "adquisicion", "adquisición", "compra", "compras", "servicio", "servicios", "contratacion", "contratación", "suministro", "suministros", "licitacion", "licitación", "agil", "ágil", "publica", "pública", "año", "anio"]
)
_PALABRA_NOMBRE_RE = re.compile(r"[a-záéíóúüñ]{4,}")
_MAX_SUGERIDAS = 8


def _sin_tildes(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )


def palabras_sugeridas(
    nombre: str, keywords_perfil: list[str], session: Session | None = None
) -> list[str]:
    """Términos del nombre de la oportunidad que se pueden ofrecer para excluir:
    sin stopwords ni las palabras clave del perfil (excluirlas vaciaría el perfil).
    Con `session` (Postgres) también descarta las de la misma raíz que una keyword
    ("saludable" con "salud"), en una sola consulta (`exclusiones_que_chocan`)."""
    propias = {_sin_tildes(k.lower()) for kw in keywords_perfil for k in kw.split()}
    salida: list[str] = []
    vistas: set[str] = set()
    for w in _PALABRA_NOMBRE_RE.findall(nombre.lower()):
        base = _sin_tildes(w)
        if w in _STOPWORDS or base in _STOPWORDS or base in propias or base in vistas:
            continue
        vistas.add(base)
        salida.append(w)
        if len(salida) == _MAX_SUGERIDAS:
            break
    if session is not None and salida:
        from app.matching.engine import exclusiones_que_chocan

        chocan = set(exclusiones_que_chocan(session, keywords_perfil, salida))
        salida = [w for w in salida if w not in chocan]
    return salida
