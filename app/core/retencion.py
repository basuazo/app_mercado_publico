"""Política de retención de datos — límite Neon 0.5 GB.

Regla: raw_json se guarda SOLO cuando la oportunidad tiene al menos un match.
purgar_terminales() limpia raw_json e items/productos de oportunidades terminales
antiguas, pero nunca toca filas vigentes, matches con alertas pendientes ni
oportunidades guardadas (seguidas, archivadas o no; F-guardar), ni las CA de las
regiones del piloto de mercado (F-merc-1: excepción a la regla 11, ver decisiones.md).
También purga el historial de corridas de jobs (job_runs), que crece sin techo.

Las demás tablas con retención propia van en `PURGAS` (F-plat-7 §1.4): una lista de
`Purga(nombre, fn)`, no código suelto. `purgar_terminales` las corre todas y deja cada conteo en
su resultado (`<nombre>_borrados`), que el job `retencion` graba en `job_runs`. Una fase que
agrega una tabla con retención suma UNA entrada con `registrar_purga`.

F-plat-purga-legado: `ca_fuera_territorio` es la única purga que BORRA filas de `compras_agiles`
(las CA de fuera del territorio activo, ya no vigentes y sin nada que las proteja; criterio en
:func:`filtro_ca_fuera_territorio`). El script `scripts/mantenimiento/purgar_legado.py` usa el
mismo criterio para el borrado masivo con respaldo.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, CompoundSelect, Select, delete, or_, select, text, update
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.tiempo import ahora_utc
from app.models.enums import ESTADOS_TERMINALES
from app.models.tables import (
    Alerta,
    CaAnexo,
    CaComentario,
    CaCotizacion,
    CaEvento,
    CaLinea,
    CaProducto,
    CaRequisito,
    CaTrabajo,
    CompraAgil,
    EvaluacionCache,
    JobRun,
    Licitacion,
    LicitacionItem,
    LlmCache,
    LoteCa,
    MatchFeedback,
    OfertaPropia,
    OportunidadMatch,
    OportunidadSeguida,
    Prediccion,
    Venta,
)

_log = get_logger(__name__)

DIAS_LLM_CACHE = 180
DIAS_EVALUACIONES_CACHE = 90
# F-plat-purga-legado: una CA fuera del territorio cerrada hace más de 90 días se borra (o antes,
# si su estado ya es terminal). Tope por corrida del job, para no alargarlo; tandas de 5.000.
DIAS_CA_FUERA_TERRITORIO = 90
TOPE_CA_FUERA_TERRITORIO = 20_000
TANDA_BORRADO = 5_000


@dataclass(frozen=True)
class Purga:
    """Una regla de retención de una tabla. `fn(session, ahora)` borra y devuelve las filas
    borradas, sin hacer commit (lo hace el job `retencion`). `nombre` da la clave del conteo.

    Con `con_regiones=True` la firma es `fn(session, ahora, regiones_protegidas)`: recibe el
    territorio activo que calcula el job (regiones de las empresas activas ∪ el setting)."""

    nombre: str
    fn: Callable[..., int]
    con_regiones: bool = False


def _filas(r: object) -> int:
    return int(getattr(r, "rowcount", 0) or 0)


def _purgar_eventos_equipo(session: Session, ahora: datetime) -> int:
    """Registro de actividad del equipo (F-equipo): 12 meses; `ca_trabajo` no se purga."""
    from app.mercado.equipo import purgar_eventos

    return purgar_eventos(session, ahora)


def _purgar_llm_cache(session: Session, ahora: datetime) -> int:
    """Respuestas de OpenRouter con más de 180 días (F-plat-2: el mismo anexo no se paga dos veces,
    pero uno de hace medio año ya no vuelve a llegar)."""
    corte = ahora - timedelta(days=DIAS_LLM_CACHE)
    return _filas(session.execute(delete(LlmCache).where(LlmCache.creado_en < corte)))


def _purgar_evaluaciones_cache(session: Session, ahora: datetime) -> int:
    """Evaluaciones de CA cerradas hace más de 90 días: la mesa solo lee las de CA vigentes, y es
    derivada (el job `mercado-mesa` la reescribe). Las predicciones y ofertas propias NO se tocan."""
    corte = ahora - timedelta(days=DIAS_EVALUACIONES_CACHE)
    cerradas = select(CompraAgil.codigo).where(CompraAgil.fecha_cierre < corte)
    return _filas(session.execute(delete(EvaluacionCache).where(EvaluacionCache.ca_codigo.in_(cerradas))))


# ---------------------------------------------------------------------------
# CA fuera del territorio (F-plat-purga-legado)
# ---------------------------------------------------------------------------


def codigos_ca_protegidas() -> CompoundSelect[tuple[str]]:
    """Códigos de CA que ninguna purga borra: las que ya protege `purgar_terminales` (match con
    alerta pendiente, seguida, `ca_trabajo`, predicción, oferta propia) y las que referencia
    cualquier dato del pivote: lote, requisito, venta, cotización, línea, anexo, evaluación, y
    también el hilo y el registro del equipo (`ca_comentarios`, `ca_eventos`, sin FK)."""
    con_alerta_pendiente = select(Alerta.match_id).where(Alerta.estado == "pendiente")
    return select(OportunidadMatch.codigo_oportunidad).where(
        OportunidadMatch.fuente == "compras_agiles",
        OportunidadMatch.id.in_(con_alerta_pendiente),
    ).union(
        select(OportunidadSeguida.codigo_oportunidad).where(
            OportunidadSeguida.fuente == "compras_agiles"
        ),
        select(CaTrabajo.ca_codigo),
        select(Prediccion.ca_codigo),
        select(OfertaPropia.ca_codigo),
        select(LoteCa.ca_codigo),
        select(CaRequisito.ca_codigo),
        select(Venta.ca_codigo),
        select(CaCotizacion.ca_codigo),
        select(CaLinea.ca_codigo),
        select(CaAnexo.ca_codigo),
        select(EvaluacionCache.ca_codigo),
        select(CaComentario.ca_codigo),
        select(CaEvento.ca_codigo),
    )


def filtro_ca_fuera_territorio(
    regiones_activas: Sequence[int], ahora: datetime, dias: int = DIAS_CA_FUERA_TERRITORIO
) -> list[ColumnElement[bool]]:
    """Condiciones (todas) para BORRAR una CA (F-plat-purga-legado §1). Función pura.

    - `region` fuera de `regiones_activas`, o NULL;
    - ya no vigente: estado terminal, o `fecha_cierre` anterior a `ahora - dias`;
    - fuera de :func:`codigos_ca_protegidas`.

    Con `regiones_activas` vacía devuelve un filtro que no calza nada: sin territorio conocido
    no se borra ninguna CA (si no, una configuración vacía borraría Ñuble)."""
    regiones = sorted({int(r) for r in regiones_activas})
    if not regiones:
        return [CompraAgil.codigo.is_(None)]  # PK: nunca NULL
    corte = ahora - timedelta(days=dias)
    return [
        or_(CompraAgil.region.is_(None), CompraAgil.region.not_in(regiones)),
        or_(
            CompraAgil.estado.in_([e.value for e in ESTADOS_TERMINALES]),
            CompraAgil.fecha_cierre < corte,
        ),
        CompraAgil.codigo.not_in(codigos_ca_protegidas()),
    ]


def select_ca_fuera_territorio(
    regiones_activas: Sequence[int], ahora: datetime, dias: int = DIAS_CA_FUERA_TERRITORIO
) -> Select[tuple[str]]:
    """`SELECT codigo` de las CA a borrar, en orden de código (tandas deterministas)."""
    return (
        select(CompraAgil.codigo)
        .where(*filtro_ca_fuera_territorio(regiones_activas, ahora, dias))
        .order_by(CompraAgil.codigo)
    )


def borrar_cas(session: Session, codigos: Sequence[str]) -> dict[str, int]:
    """Borra estas CA y lo que cuelga de ellas, sin commit: sus `oportunidades_match` (con sus
    `alertas`) y su `match_feedback` de `fuente = 'compras_agiles'`, sus `ca_productos` y la CA.

    Las alertas y los productos caen por CASCADE en Postgres; se borran explícitos igual para que
    el conteo sea real y para que valga sin FK activas. Solo se llama con códigos que ya pasaron
    :func:`filtro_ca_fuera_territorio`: no vuelve a verificar la protección."""
    codigos = list(codigos)
    if not codigos:
        return dict.fromkeys(
            ("alertas", "oportunidades_match", "match_feedback", "ca_productos", "compras_agiles"),
            0,
        )
    matches = select(OportunidadMatch.id).where(
        OportunidadMatch.fuente == "compras_agiles",
        OportunidadMatch.codigo_oportunidad.in_(codigos),
    )
    alertas = _filas(session.execute(delete(Alerta).where(Alerta.match_id.in_(matches))))
    om = _filas(
        session.execute(
            delete(OportunidadMatch).where(
                OportunidadMatch.fuente == "compras_agiles",
                OportunidadMatch.codigo_oportunidad.in_(codigos),
            )
        )
    )
    mf = _filas(
        session.execute(
            delete(MatchFeedback).where(
                MatchFeedback.fuente == "compras_agiles",
                MatchFeedback.codigo_oportunidad.in_(codigos),
            )
        )
    )
    prods = _filas(session.execute(delete(CaProducto).where(CaProducto.ca_codigo.in_(codigos))))
    cas = _filas(session.execute(delete(CompraAgil).where(CompraAgil.codigo.in_(codigos))))
    return {
        "alertas": alertas,
        "oportunidades_match": om,
        "match_feedback": mf,
        "ca_productos": prods,
        "compras_agiles": cas,
    }


def purgar_ca_fuera_territorio(
    session: Session,
    ahora: datetime,
    regiones_protegidas: Sequence[int] | None,
    tope: int = TOPE_CA_FUERA_TERRITORIO,
) -> int:
    """Entrada `ca_fuera_territorio` de `PURGAS`: borra por fila hasta `tope` CA (tandas de
    5.000 códigos en memoria, regla 12). Devuelve las CA borradas. Sin territorio, no borra."""
    if not regiones_protegidas:
        return 0
    borradas = 0
    while borradas < tope:
        tanda = list(
            session.execute(
                select_ca_fuera_territorio(regiones_protegidas, ahora).limit(
                    min(TANDA_BORRADO, tope - borradas)
                )
            ).scalars()
        )
        if not tanda:
            break
        n = borrar_cas(session, tanda)["compras_agiles"]
        if n == 0:
            break  # defensivo: nada cambió, no girar en vacío
        borradas += n
    return borradas


PURGAS: list[Purga] = [
    Purga("eventos_equipo", _purgar_eventos_equipo),
    Purga("llm_cache", _purgar_llm_cache),
    Purga("evaluaciones_cache", _purgar_evaluaciones_cache),
    Purga("ca_fuera_territorio", purgar_ca_fuera_territorio, con_regiones=True),
]


def registrar_purga(nombre: str, fn: Callable[..., int], *, con_regiones: bool = False) -> None:
    """Suma una regla a la retención (idempotente por `nombre`: reemplaza una anterior)."""
    PURGAS[:] = [p for p in PURGAS if p.nombre != nombre] + [Purga(nombre, fn, con_regiones)]


def purgar_terminales(
    session: Session, dias: int = 90, regiones_protegidas: list[int] | None = None
) -> dict[str, int]:
    """Purga raw_json e items de oportunidades terminales con más de `dias` días sin actualizar.

    No toca oportunidades vigentes, ni aquellas con alertas pendientes, ni las
    que algún usuario tiene en oportunidades_seguidas (archivada o no).
    `regiones_protegidas` (territorio activo, F-plat-3): sus CA no se purgan; sus
    cotizaciones tampoco (la purga nunca borra filas de compras_agiles).
    Devuelve dict con conteos de filas afectadas por tipo.
    """
    corte = ahora_utc() - timedelta(days=dias)
    estados_str = [e.value for e in ESTADOS_TERMINALES]

    # IDs de matches que tienen alertas pendientes → protegerlos
    matches_con_alerta_pendiente = select(Alerta.match_id).where(Alerta.estado == "pendiente")
    codigos_licitacion_protegidos = select(OportunidadMatch.codigo_oportunidad).where(
        OportunidadMatch.fuente == "licitaciones",
        OportunidadMatch.id.in_(matches_con_alerta_pendiente),
    ).union(
        select(OportunidadSeguida.codigo_oportunidad).where(
            OportunidadSeguida.fuente == "licitaciones"
        )
    )
    codigos_ca_protegidos = select(OportunidadMatch.codigo_oportunidad).where(
        OportunidadMatch.fuente == "compras_agiles",
        OportunidadMatch.id.in_(matches_con_alerta_pendiente),
    ).union(
        select(OportunidadSeguida.codigo_oportunidad).where(
            OportunidadSeguida.fuente == "compras_agiles"
        ),
        # F-equipo: una CA que algún equipo trabajó no se purga (como F-plat-5 §12).
        select(CaTrabajo.ca_codigo),
        # F-plat-5: tampoco una con predicción (el juez del modo sombra) u oferta propia.
        select(Prediccion.ca_codigo),
        select(OfertaPropia.ca_codigo),
    )

    # -- Licitaciones terminales antiguas --
    # Ejecutar UNA sola vez para evitar subquery duplicada en DELETE y UPDATE.
    codigos_lic = list(
        session.execute(
            select(Licitacion.codigo).where(
                Licitacion.estado.in_(estados_str),
                Licitacion.actualizado_en < corte,
                Licitacion.codigo.not_in(codigos_licitacion_protegidos),
            )
        ).scalars()
    )

    if codigos_lic:
        r = session.execute(
            delete(LicitacionItem).where(LicitacionItem.licitacion_codigo.in_(codigos_lic))
        )
        items_borrados: int = r.rowcount  # type: ignore[attr-defined]
        r = session.execute(
            update(Licitacion).where(Licitacion.codigo.in_(codigos_lic)).values(raw_json=None)
        )
        lic_purgadas: int = r.rowcount  # type: ignore[attr-defined]
    else:
        items_borrados = 0
        lic_purgadas = 0

    # -- Compras Ágiles terminales antiguas --
    filtro_ca = [
        CompraAgil.estado.in_(estados_str),
        CompraAgil.actualizado_en < corte,
        CompraAgil.codigo.not_in(codigos_ca_protegidos),
    ]
    if regiones_protegidas:
        # Una CA sin región no es del piloto: se purga como siempre.
        filtro_ca.append(
            or_(CompraAgil.region.is_(None), CompraAgil.region.not_in(regiones_protegidas))
        )
    codigos_ca = list(session.execute(select(CompraAgil.codigo).where(*filtro_ca)).scalars())

    if codigos_ca:
        r = session.execute(delete(CaProducto).where(CaProducto.ca_codigo.in_(codigos_ca)))
        prods_borrados: int = r.rowcount  # type: ignore[attr-defined]
        r = session.execute(
            update(CompraAgil).where(CompraAgil.codigo.in_(codigos_ca)).values(raw_json=None)
        )
        ca_purgadas: int = r.rowcount  # type: ignore[attr-defined]
    else:
        prods_borrados = 0
        ca_purgadas = 0

    # -- Historial de corridas de jobs --
    job_runs_borrados = purgar_job_runs(session, dias=dias)

    # -- Tablas con retención propia (lista `PURGAS`): equipo, llm_cache, evaluaciones_cache... --
    ahora = ahora_utc()
    extras = {
        f"{p.nombre}_borrados": (
            p.fn(session, ahora, regiones_protegidas) if p.con_regiones else p.fn(session, ahora)
        )
        for p in PURGAS
    }

    _log.info(
        "purgar_terminales(dias=%d): licitaciones=%d items=%d ca=%d productos=%d job_runs=%d %s",
        dias,
        lic_purgadas,
        items_borrados,
        ca_purgadas,
        prods_borrados,
        job_runs_borrados,
        extras,
    )
    return {
        "licitaciones_purgadas": lic_purgadas,
        "items_borrados": items_borrados,
        "ca_purgadas": ca_purgadas,
        "productos_borrados": prods_borrados,
        "job_runs_borrados": job_runs_borrados,
        **extras,
    }


def purgar_job_runs(session: Session, dias: int = 90) -> int:
    """Borra el historial de corridas de jobs con más de `dias` días.

    job_runs crece una fila por corrida y por disparador; sin purga la tabla
    crece sin techo (regla 11: Neon son 0,5 GB). Devuelve las filas borradas.
    """
    corte = ahora_utc() - timedelta(days=dias)
    r = session.execute(delete(JobRun).where(JobRun.iniciado_en < corte))
    return int(r.rowcount or 0)  # type: ignore[attr-defined]


def tamano_bd(session: Session) -> int | None:
    """Retorna el tamaño de la BD en bytes usando pg_database_size().

    Devuelve None si no está disponible (ej. SQLite en tests).
    """
    try:
        row = session.execute(text("SELECT pg_database_size(current_database())")).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def tamano_tabla(session: Session, nombre_tabla: str) -> int | None:
    """Tamaño de una tabla (datos + índices + TOAST) en bytes vía
    pg_total_relation_size. None si no está disponible (SQLite en tests).

    `nombre_tabla` nunca viene de un usuario final (siempre un literal fijo
    en el código que llama), pero igual se pasa como bindparam — cast a
    regclass no admite parámetros preparados normales en algunas versiones,
    así que se arma con to_regclass, que sí acepta texto parametrizado."""
    try:
        row = session.execute(
            text("SELECT pg_total_relation_size(to_regclass(:t))"), {"t": nombre_tabla}
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return None
