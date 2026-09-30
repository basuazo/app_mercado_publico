"""Explorador de Compras Ágiles (F-ca-explorar): todas las CA vigentes, filtrables.

Todo sale de la base: este módulo NUNCA llama a la API (regla 3) y NUNCA carga el
universo en Python (regla 12): filtros, orden, conteo y paginación son SQL. Lo único
que vuelve a Python es una página de `PAGE_SIZE` filas.

Rubro en dos niveles, porque el rubro de una CA solo se conoce con su detalle y
apenas ~3 % lo tiene [V, Paso 0 del 26-sep]:
- **confirmado**: la CA tiene productos (`ca_productos`) con ese prefijo UNSPSC;
- **posible**: el nombre/descripción calza (`compras_agiles.tsv`) con el vocabulario
  típico del rubro (`rubro_vocabulario`, job `vocabulario-rubros`). Con 10 términos
  y lift >= 10 encuentra el 91 % de las CA que sí calzan, con 7 % de precisión: sirve
  para recorrer, no para alertar, y por eso la pantalla lo rotula "posible".

Los lexemas del vocabulario ya vienen con el stemmer de `spanish` aplicado (salen de
`to_tsvector`), así que se comparan con `to_tsquery('simple', ...)`: pasarlos otra
vez por `spanish` los estemmearía dos veces.

Toda query es parametrizada (ORM o `text().bindparams`); nada de valores interpolados.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    ColumnElement,
    and_,
    bindparam,
    delete,
    exists,
    false,
    func,
    literal_column,
    or_,
    select,
    text,
)
from sqlalchemy.orm import Session

from app.api.presentacion import banda_urgencia, nombre_region, texto_cierre
from app.catalogos.unspsc import nombre_rubro
from app.catalogos.vocabulario_rubro import LEXEMA_RE
from app.core.tiempo import TZ_CHILE, ahora_utc, borde_del_dia_utc_naive
from app.core.vigencia import condicion_ca_vigente
from app.models.enums import ValorFeedback
from app.models.tables import CaProducto, CompraAgil, MatchFeedback, RubroFavorito, RubroVocabulario

FUENTE = "compras_agiles"
PAGE_SIZE = 50
# Tope de familias de las que se rotula el "posible": una consulta por familia sobre
# a lo más PAGE_SIZE códigos. Con más, las restantes salen sin familia (siguen "posibles").
_MAX_FAMILIAS_ETIQUETA = 60
_MAX_TEXTO = 200

PREFIJO_RE = re.compile(r"^\d{2,8}$")

ORDENES = ("cierre", "monto", "reciente")
# Atajos del filtro de cierre: días hacia adelante, contados en hora de Chile.
CIERRES = {"hoy": 0, "3d": 3, "7d": 7}

_TSV: Any = literal_column("compras_agiles.tsv")


@dataclass
class FiltrosExplorador:
    usuario_id: int
    prefijos: list[str] = field(default_factory=list)
    solo_confirmados: bool = False
    regiones: list[int] = field(default_factory=list)
    monto_min: float | None = None
    monto_max: float | None = None
    incluir_sin_monto: bool = True
    cierre: str | None = None
    texto: str = ""
    organismo: str = ""
    orden: str = "cierre"


@dataclass
class ResultadoExplorador:
    total: int
    items: list[dict[str, Any]]
    pagina: int
    total_paginas: int


# ---------------------------------------------------------------------------
# Rubro: prefijos, vocabulario y condiciones
# ---------------------------------------------------------------------------


def prefijos_validos(valores: list[str]) -> list[str]:
    """Deja solo prefijos `^\\d{2,8}$` (acepta listas separadas por coma), sin
    duplicar y en orden de aparición."""
    salida: list[str] = []
    for v in valores:
        for parte in v.split(","):
            p = parte.strip()
            if PREFIJO_RE.match(p) and p not in salida:
                salida.append(p)
    return salida


def vocabulario_de_prefijos(session: Session, prefijos: list[str]) -> dict[str, list[str]]:
    """Lexemas por familia (4 dígitos) para los prefijos elegidos.

    Un prefijo de 2 dígitos (segmento) reúne las familias que empiezan con él; uno
    de 4 a 8 usa su familia (`[:4]`). Se re-valida cada lexema: es lo que después
    se une con " | " en la tsquery.
    """
    if not prefijos:
        return {}
    familias = sorted({p[:4] for p in prefijos if len(p) >= 4})
    segmentos = sorted({p for p in prefijos if len(p) == 2})
    condiciones: list[ColumnElement[bool]] = []
    if familias:
        condiciones.append(RubroVocabulario.prefijo.in_(familias))
    for seg in segmentos:
        condiciones.append(RubroVocabulario.prefijo.startswith(seg, autoescape=True))
    filas = session.execute(
        select(RubroVocabulario.prefijo, RubroVocabulario.lexema)
        .where(or_(*condiciones))
        .order_by(RubroVocabulario.prefijo, RubroVocabulario.df_rubro.desc())
    ).all()
    vocab: dict[str, list[str]] = {}
    for prefijo, lexema in filas:
        if LEXEMA_RE.match(lexema):
            vocab.setdefault(prefijo, []).append(lexema)
    return vocab


def _tsquery_or(lexemas: list[str]) -> str:
    """Los lexemas (ya validados) unidos con OR, para `to_tsquery('simple', :q)`."""
    return " | ".join(sorted(set(lexemas)))


def _cond_confirmado(prefijos: list[str]) -> ColumnElement[bool]:
    con_prefijo = select(CaProducto.ca_codigo).where(
        or_(*[CaProducto.codigo_producto.startswith(p, autoescape=True) for p in prefijos])
    )
    return CompraAgil.codigo.in_(con_prefijo)


def _cond_posible(vocab: dict[str, list[str]]) -> ColumnElement[bool]:
    lexemas = [lx for lista in vocab.values() for lx in lista]
    if not lexemas:
        return false()
    calza: ColumnElement[bool] = _TSV.op("@@")(
        func.to_tsquery(literal_column("'simple'"), _tsquery_or(lexemas))
    )
    return calza


# ---------------------------------------------------------------------------
# Universo y filtros
# ---------------------------------------------------------------------------


def _condiciones(
    filtros: FiltrosExplorador,
    ahora: datetime,
    vocab: dict[str, list[str]],
) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = [condicion_ca_vigente(ahora)]

    # Descartadas por ESTE usuario (las de otro no le afectan, regla 17).
    conds.append(
        ~exists().where(
            MatchFeedback.usuario_id == filtros.usuario_id,
            MatchFeedback.fuente == FUENTE,
            MatchFeedback.codigo_oportunidad == CompraAgil.codigo,
            MatchFeedback.valor == ValorFeedback.DESCARTE.value,
        )
    )

    if filtros.prefijos:
        confirmado = _cond_confirmado(filtros.prefijos)
        conds.append(confirmado if filtros.solo_confirmados else or_(confirmado, _cond_posible(vocab)))

    if filtros.regiones:
        conds.append(CompraAgil.region.in_(filtros.regiones))

    monto = CompraAgil.monto_disponible_clp
    if filtros.monto_min is not None or filtros.monto_max is not None:
        rango: list[ColumnElement[bool]] = []
        if filtros.monto_min is not None:
            rango.append(monto >= filtros.monto_min)
        if filtros.monto_max is not None:
            rango.append(monto <= filtros.monto_max)
        dentro = and_(*rango)
        conds.append(or_(dentro, monto.is_(None)) if filtros.incluir_sin_monto else dentro)
    elif not filtros.incluir_sin_monto:
        conds.append(monto.is_not(None))

    if filtros.cierre in CIERRES:
        hoy = datetime.now(TZ_CHILE).date()
        limite = borde_del_dia_utc_naive(hoy + timedelta(days=CIERRES[filtros.cierre]), fin_de_dia=True)
        # Una CA sin fecha de cierre no se puede decir que "cierra pronto": queda fuera.
        conds.append(and_(CompraAgil.fecha_cierre.is_not(None), CompraAgil.fecha_cierre <= limite))

    texto = filtros.texto.strip()[:_MAX_TEXTO]
    if texto:
        # Misma semántica que los perfiles: websearch_to_tsquery('spanish', ...).
        conds.append(_TSV.op("@@")(func.websearch_to_tsquery(literal_column("'spanish'"), texto)))

    organismo = filtros.organismo.strip()[:_MAX_TEXTO]
    if organismo:
        conds.append(CompraAgil.organismo_nombre.icontains(organismo, autoescape=True))

    return conds


def _orden(orden: str) -> list[Any]:
    if orden == "monto":
        return [CompraAgil.monto_disponible_clp.desc().nulls_last(), CompraAgil.codigo]
    if orden == "reciente":
        return [CompraAgil.fecha_publicacion.desc().nulls_last(), CompraAgil.codigo]
    return [
        CompraAgil.fecha_cierre.asc().nulls_last(),
        CompraAgil.fecha_publicacion.desc().nulls_last(),
        CompraAgil.codigo,
    ]


def _vocab_para(session: Session, filtros: FiltrosExplorador) -> dict[str, list[str]]:
    """Con "solo confirmados" el vocabulario no participa: ni se lee."""
    if filtros.solo_confirmados:
        return {}
    return vocabulario_de_prefijos(session, filtros.prefijos)


def contar(session: Session, filtros: FiltrosExplorador, *, ahora: datetime | None = None) -> int:
    ahora = ahora or ahora_utc()
    conds = _condiciones(filtros, ahora, _vocab_para(session, filtros))
    return int(session.execute(select(func.count()).select_from(CompraAgil).where(*conds)).scalar_one())


def buscar(
    session: Session,
    filtros: FiltrosExplorador,
    *,
    pagina: int = 1,
    page_size: int = PAGE_SIZE,
    ahora: datetime | None = None,
) -> ResultadoExplorador:
    """Una página del explorador (`LIMIT/OFFSET`) y el total por `count(*)` aparte."""
    ahora = ahora or ahora_utc()
    vocab = _vocab_para(session, filtros)
    conds = _condiciones(filtros, ahora, vocab)

    total = int(session.execute(select(func.count()).select_from(CompraAgil).where(*conds)).scalar_one())
    total_paginas = max(1, -(-total // page_size))
    pagina = max(1, pagina)
    if pagina > total_paginas:
        return ResultadoExplorador(total=total, items=[], pagina=pagina, total_paginas=total_paginas)

    columnas: list[Any] = [
        CompraAgil.codigo,
        CompraAgil.nombre,
        CompraAgil.organismo_nombre,
        CompraAgil.region,
        CompraAgil.monto_disponible_clp,
        CompraAgil.fecha_cierre,
        CompraAgil.fecha_publicacion,
    ]
    if filtros.prefijos:
        columnas.append(_cond_confirmado(filtros.prefijos).label("confirmado"))
    filas = session.execute(
        select(*columnas)
        .where(*conds)
        .order_by(*_orden(filtros.orden))
        .limit(page_size)
        .offset((pagina - 1) * page_size)
    ).all()

    sin_confirmar = [f.codigo for f in filas if filtros.prefijos and not f.confirmado]
    familias_posibles = _familias_posibles(session, sin_confirmar, vocab)

    items: list[dict[str, Any]] = []
    for f in filas:
        dias = None
        if f.fecha_cierre is not None:
            dias = max(0.0, (f.fecha_cierre - ahora).total_seconds() / 86400)
        confirmado = bool(filtros.prefijos and f.confirmado)
        items.append(
            {
                "codigo": f.codigo,
                "nombre": f.nombre,
                "organismo": f.organismo_nombre,
                "region_nombre": nombre_region(f.region),
                "monto": f.monto_disponible_clp,
                "fecha_cierre": f.fecha_cierre,
                "cierre_texto": texto_cierre(f.fecha_cierre, dias, FUENTE),
                "urgencia": banda_urgencia(dias),
                "confirmado": confirmado,
                # Con rubros elegidos y sin confirmar, es "posible" aunque no se
                # pueda nombrar la familia (tope de etiquetas).
                "posible": bool(filtros.prefijos) and not confirmado,
                "familia_posible": familias_posibles.get(f.codigo),
            }
        )
    return ResultadoExplorador(total=total, items=items, pagina=pagina, total_paginas=total_paginas)


def _familias_posibles(
    session: Session, codigos: list[str], vocab: dict[str, list[str]]
) -> dict[str, str]:
    """Para cada CA "posible" de la página, el nombre de la primera familia cuyo
    vocabulario calza. Una consulta por familia sobre a lo más una página de códigos."""
    if not codigos or not vocab:
        return {}
    encontradas: dict[str, str] = {}
    for familia in sorted(vocab)[:_MAX_FAMILIAS_ETIQUETA]:
        pendientes = [c for c in codigos if c not in encontradas]
        if not pendientes:
            break
        stmt = text(
            "SELECT codigo FROM compras_agiles "
            "WHERE codigo IN :codigos AND tsv @@ to_tsquery('simple', :q)"
        ).bindparams(bindparam("codigos", value=pendientes, expanding=True))
        nombre = nombre_rubro(familia) or familia
        for codigo in session.execute(stmt, {"q": _tsquery_or(vocab[familia])}).scalars():
            encontradas.setdefault(codigo, nombre)
    return encontradas


# ---------------------------------------------------------------------------
# Rubros favoritos (dueño = usuario; regla 17)
# ---------------------------------------------------------------------------


def listar_favoritos(session: Session, usuario_id: int) -> list[str]:
    return list(
        session.execute(
            select(RubroFavorito.prefijo)
            .where(RubroFavorito.owner_id == usuario_id)
            .order_by(RubroFavorito.prefijo)
        ).scalars()
    )


def agregar_favorito(session: Session, usuario_id: int, prefijo: str) -> bool:
    """Idempotente: True si lo creó, False si ya estaba. Prefijo inválido -> ValueError."""
    prefijo = prefijo.strip()
    if not PREFIJO_RE.match(prefijo):
        raise ValueError(f"prefijo de rubro inválido: {prefijo!r}")
    existe = session.execute(
        select(RubroFavorito.id).where(
            RubroFavorito.owner_id == usuario_id, RubroFavorito.prefijo == prefijo
        )
    ).first()
    if existe is not None:
        return False
    session.add(RubroFavorito(owner_id=usuario_id, prefijo=prefijo))
    session.flush()
    return True


def quitar_favorito(session: Session, usuario_id: int, prefijo: str) -> bool:
    res = session.execute(
        delete(RubroFavorito).where(
            RubroFavorito.owner_id == usuario_id, RubroFavorito.prefijo == prefijo.strip()
        )
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]
