"""Qué Compras Ágiles necesitan que alguien les adjunte el anexo (F-plat-anexos-boton §2).

Un solo criterio, :func:`necesita_anexo` (pura), para la lista web `/catalogo/anexos/pendientes`,
el bloque de «Hoy» y `GET /api/anexos/pendientes`. La cola se arma por cierre: primero las que
cierran antes. Solo lee la base; no llama a nadie (regla 9: la descarga la hace una persona).

«Familias de la empresa»: la empresa aún no declara rubros y las líneas (`ca_lineas`) solo se
crean para alimentos (`G_ALIM`, F-plat-1), así que la familia en alcance es alimentos [I]. Una CA
sin productos conocidos no se descarta: no hay con qué decidir.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalogos.canonico import detectar_sin_detalle
from app.catalogos.mercado import G_ALIM, grupo_de
from app.core.tiempo import TZ_CHILE
from app.mercado.mesa_dia import condiciones_vigente
from app.models.enums import EstadoOportunidad
from app.models.tables import CaAnexo, CaLinea, CaProducto, CompraAgil, Empresa

LIMITE_CANDIDATAS = 1000  # tope de CA que se miran por consulta (regla 12: nada sin cota)
LIMITE_LISTA = 200  # máx. de la lista y de la API
N_FICHAS_DEFECTO = 10  # «Abrir fichas»: cuántas pestañas por defecto
N_FICHAS_MAX = 30

MOTIVO_SIN_LINEAS = "La API no trae líneas"
MOTIVO_PLACEHOLDER = "La API dice «ver anexo»"
MOTIVO_POBRES = "Líneas de la API sin reconocer"
MOTIVO_REMITE = "Una línea remite al adjunto"


@dataclass(frozen=True)
class LineaInfo:
    """Lo que `necesita_anexo` mira de una fila de `ca_lineas`."""

    origen: str
    estado: str
    texto: str = ""
    cantidad: float | None = None


def motivo_anexo(
    *,
    n_documentos: int | None,
    vigente: bool,
    alimentos: bool,
    resuelta: bool,
    lineas: Sequence[LineaInfo],
) -> str | None:
    """Por qué la CA necesita su anexo, o None si no lo necesita (pura).

    Necesita: `n_documentos > 0`, vigente, de la familia en alcance, sin «Anexo resuelto» ni
    anexos cargados (`resuelta`), y además una de:

    - sin líneas de la API (las `reemplazada` no cuentan);
    - el placeholder «sin detalle (anexo)» (origen anexo, pendiente);
    - líneas de la API pobres (caso D de anexos-2): ninguna `ok`;
    - una línea de la API cuyo texto remite al adjunto («detalle en archivo adjunto»).

    Una línea `ok` manual o de anexo ya cubre la CA: no necesita nada más."""
    if not (n_documentos and n_documentos > 0) or not vigente or not alimentos or resuelta:
        return None
    activas = [x for x in lineas if x.estado != "reemplazada"]
    if any(x.origen in ("manual", "anexo") and x.estado == "ok" for x in activas):
        return None
    if not activas:
        return MOTIVO_SIN_LINEAS
    if any(x.origen == "anexo" and x.estado == "pendiente" for x in activas):
        return MOTIVO_PLACEHOLDER
    de_api = [x for x in activas if x.origen == "api"]
    if any(detectar_sin_detalle(x.texto, x.cantidad) for x in de_api):
        return MOTIVO_REMITE
    if de_api and not any(x.estado == "ok" for x in de_api):
        return MOTIVO_POBRES
    return None


def necesita_anexo(
    *,
    n_documentos: int | None,
    vigente: bool,
    alimentos: bool,
    resuelta: bool,
    lineas: Sequence[LineaInfo],
) -> bool:
    """¿Esta CA necesita que le suban su anexo? (ver :func:`motivo_anexo`)."""
    return (
        motivo_anexo(
            n_documentos=n_documentos,
            vigente=vigente,
            alimentos=alimentos,
            resuelta=resuelta,
            lineas=lineas,
        )
        is not None
    )


@dataclass
class Pendiente:
    codigo: str
    nombre: str
    comprador: str | None
    fecha_cierre: datetime | None  # UTC naive, como en la base
    n_documentos: int
    documentos: list[str] = field(default_factory=list)
    motivo: str = ""


def _es_alimentos(productos: Iterable[tuple[str | None, str | None]]) -> bool:
    items = list(productos)
    if not items:
        return True  # sin productos conocidos no hay con qué descartarla
    return any(grupo_de((c or "").strip(), n) in G_ALIM for c, n in items)


def pendientes(
    session: Session, regiones: list[int], ahora: datetime, limite: int = LIMITE_LISTA
) -> list[Pendiente]:
    """CA que necesitan anexo en `regiones`, por cierre (más próximo primero), máx. `limite`.

    SQL descarta lo barato (vigente, con documentos, sin «Anexo resuelto», sin anexos cargados);
    el resto del criterio lo decide :func:`motivo_anexo` sobre las líneas y productos, cargados
    en dos consultas para el lote (no una por CA)."""
    if not regiones:
        return []
    con_anexo = select(CaAnexo.id).where(CaAnexo.ca_codigo == CompraAgil.codigo).exists()
    candidatas = list(
        session.execute(
            select(CompraAgil)
            .where(
                *condiciones_vigente(regiones, ahora),
                CompraAgil.n_documentos > 0,
                CompraAgil.anexo_resuelto_en.is_(None),
                ~con_anexo,
            )
            .order_by(CompraAgil.fecha_cierre.asc(), CompraAgil.codigo)
            .limit(LIMITE_CANDIDATAS)
        ).scalars()
    )
    if not candidatas:
        return []
    motivos = _motivos(session, candidatas)
    salida: list[Pendiente] = []
    for ca in candidatas:
        motivo = motivos.get(ca.codigo)
        if motivo is None:
            continue
        docs: list[Any] = ca.documentos if isinstance(ca.documentos, list) else []
        salida.append(
            Pendiente(
                codigo=ca.codigo,
                nombre=ca.nombre or "",
                comprador=ca.organismo_nombre,
                fecha_cierre=ca.fecha_cierre,
                n_documentos=int(ca.n_documentos or 0),
                documentos=[str(d.get("nombre") or "") for d in docs if isinstance(d, dict)],
                motivo=motivo,
            )
        )
        if len(salida) >= limite:
            break
    return salida


def _motivos(session: Session, candidatas: Sequence[Any]) -> dict[str, str]:
    """`{codigo: motivo}` de las candidatas que necesitan anexo. Las candidatas ya pasaron el
    filtro barato en SQL (vigentes, con documentos, sin «Anexo resuelto» ni anexos cargados); acá
    se decide el resto con :func:`motivo_anexo`, con líneas y productos en dos consultas para el
    lote (no una por CA)."""
    codigos = [c.codigo for c in candidatas]
    lineas: dict[str, list[LineaInfo]] = defaultdict(list)
    for codigo, origen, estado, texto, cantidad in session.execute(
        select(
            CaLinea.ca_codigo, CaLinea.origen, CaLinea.estado, CaLinea.texto_origen,
            CaLinea.cantidad,
        ).where(CaLinea.ca_codigo.in_(codigos))
    ):  # fmt: skip
        lineas[codigo].append(LineaInfo(origen, estado, texto, cantidad))
    productos: dict[str, list[tuple[str | None, str | None]]] = defaultdict(list)
    for codigo, cod, nombre in session.execute(
        select(CaProducto.ca_codigo, CaProducto.codigo_producto, CaProducto.nombre).where(
            CaProducto.ca_codigo.in_(codigos)
        )
    ):
        productos[codigo].append((cod, nombre))
    out: dict[str, str] = {}
    for ca in candidatas:
        motivo = motivo_anexo(
            n_documentos=ca.n_documentos,
            vigente=True,  # ya filtrado en SQL con la definición única de «vigente»
            alimentos=_es_alimentos(productos.get(ca.codigo, ())),
            resuelta=False,  # ídem: sin «Anexo resuelto» y sin anexos
            lineas=lineas.get(ca.codigo, ()),
        )
        if motivo is not None:
            out[ca.codigo] = motivo
    return out


def motivos_de(session: Session, codigos: Sequence[str], ahora: datetime) -> dict[str, str]:
    """`{codigo: motivo}` de las CA de `codigos` que esperan su anexo: el mismo criterio de
    :func:`pendientes`, sin filtrar por región (quien llama ya validó el territorio). Tres
    consultas como máximo sin importar cuántos códigos (regla 12; señales de la mesa)."""
    if not codigos:
        return {}
    con_anexo = select(CaAnexo.id).where(CaAnexo.ca_codigo == CompraAgil.codigo).exists()
    # Solo columnas livianas: nunca `raw_json` (regla 12).
    candidatas = list(
        session.execute(
            select(CompraAgil.codigo, CompraAgil.n_documentos).where(
                CompraAgil.codigo.in_(list(codigos)),
                CompraAgil.estado == EstadoOportunidad.PUBLICADA.value,
                CompraAgil.fecha_cierre > ahora,
                CompraAgil.n_documentos > 0,
                CompraAgil.anexo_resuelto_en.is_(None),
                ~con_anexo,
            )
        ).all()
    )
    return _motivos(session, candidatas) if candidatas else {}


def filas_pendientes(session: Session, empresa_id: int, ahora: datetime) -> list[dict[str, Any]]:
    """Las CA que esperan anexo en las regiones de la EMPRESA, listas para pintar (la lista web
    `/catalogo/anexos/pendientes`, «Hoy» y «Te necesita»). Movida desde `routes/anexos.py`
    (F-dis-datos) para que ninguna pantalla importe una función de otra ruta."""
    from app.api.query import url_ficha_ca  # lazy: la capa web solo arma el enlace público

    empresa = session.get(Empresa, empresa_id)
    regiones = [int(r) for r in (empresa.regiones_prioritarias or [])] if empresa else []
    return [
        {
            "codigo": p.codigo,
            "nombre": p.nombre,
            "comprador": p.comprador,
            "cierre": _cierre_chile(p.fecha_cierre),
            "fecha_cierre": p.fecha_cierre,
            "n_documentos": p.n_documentos,
            "motivo": p.motivo,
            "url_ficha": url_ficha_ca(p.codigo),
        }
        for p in pendientes(session, regiones, ahora)
    ]


def _cierre_chile(valor: datetime | None) -> str:
    if valor is None:
        return "—"
    return str(valor.replace(tzinfo=UTC).astimezone(TZ_CHILE).strftime("%d-%m %H:%M"))
