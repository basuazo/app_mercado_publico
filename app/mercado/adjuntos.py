"""Qué adjuntos de Compra Ágil faltan por bajar y qué pasó con cada uno (F-plat-anexos-auto).

Solo base de datos: lo usan el job `mercado-anexos` (la cola), la ficha (el botón «Volver a bajar
y leer») y /salud (cuánto falta). La red la toca `app.clients.mp_documentos`.

Los ids de los archivos salen de `listar/{codigo}` del servicio de adjuntos: en producción no hay
ningún `compras_agiles.documentos[].id` guardado [V, Neon, 7-nov: 88 CA vigentes de Ñuble con
`n_documentos > 0`, 0 con ids], así que listar es obligatorio. Estado por CA en
`ca_adjunto_descargas`:
- Fila marcador `documento_id = LISTADO` («(listar)»): `bajado` = ya se listó; `fallido` = el
  listado falló (`motivo`, `intentos`).
- Una fila por archivo listado: `listado` (falta bajarlo) | `bajado` | `fallido` | `pedido` (una
  persona pidió bajarlo de nuevo).

Cola, en este orden de prioridad: lo pedido por una persona; después alimentos (la familia de las
empresas, como `anexos_pendientes`); después el cierre más próximo. Entra:
- un archivo `listado` o `pedido`, o `fallido` por algo transitorio con menos de `MAX_INTENTOS`;
- una CA vigente del territorio con `n_documentos > 0`, sin «Anexo resuelto» y sin listar (o con
  el listado fallido y reintentable), salvo que una persona ya haya cargado un anexo que se leyó
  (`hecha`/`pendiente`, fuente distinta de `descarga_directa`): la persona ya hizo el trabajo y un
  archivo distinto del mismo documento duplicaría líneas.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mercado.anexos import ESTADOS_RELEIBLES
from app.mercado.anexos_pendientes import LIMITE_CANDIDATAS, _es_alimentos
from app.mercado.mesa_dia import condiciones_vigente
from app.models.tables import CaAdjuntoDescarga, CaAnexo, CaProducto, CompraAgil

LISTADO = "(listar)"  # documento_id de la fila marcador del listado de la CA
BAJADO = "bajado"
FALLIDO = "fallido"
PEDIDO = "pedido"
POR_BAJAR = "listado"
MAX_INTENTOS = 3
# Motivos que no se arreglan reintentando: el documento no se vuelve a pedir solo.
MOTIVOS_DEFINITIVOS = frozenset(
    {"no_archivo", "muy_grande", "invalido", "http_404", "http_410", "sin_archivos"}
)


@dataclass(frozen=True)
class AdjuntoPendiente:
    """Un archivo por bajar o, si `documento_id` es None, una CA por listar."""

    codigo: str
    documento_id: str | None
    nombre: str
    fecha_cierre: datetime | None
    pedido: bool = False
    alimentos: bool = True

    @property
    def es_listado(self) -> bool:
        return self.documento_id is None


def _reintentable(fila: CaAdjuntoDescarga) -> bool:
    return (
        fila.estado == FALLIDO
        and fila.intentos < MAX_INTENTOS
        and (fila.motivo or "") not in MOTIVOS_DEFINITIVOS
    )


def _entra(fila: CaAdjuntoDescarga) -> bool:
    return fila.estado in (POR_BAJAR, PEDIDO) or _reintentable(fila)


def cola(
    session: Session, regiones: list[int], ahora: datetime, limite: int | None = None
) -> list[AdjuntoPendiente]:
    """Archivos por bajar y CA por listar, en orden de prioridad (ver el docstring del módulo)."""
    if not regiones:
        return []
    candidatas = list(
        session.execute(
            select(CompraAgil)
            .where(
                *condiciones_vigente(regiones, ahora),
                CompraAgil.n_documentos > 0,
                CompraAgil.anexo_resuelto_en.is_(None),
            )
            .order_by(CompraAgil.fecha_cierre.asc(), CompraAgil.codigo)
            .limit(LIMITE_CANDIDATAS)
        ).scalars()
    )
    if not candidatas:
        return []
    codigos = [c.codigo for c in candidatas]
    filas: dict[str, list[CaAdjuntoDescarga]] = defaultdict(list)
    for f in session.execute(
        select(CaAdjuntoDescarga)
        .where(CaAdjuntoDescarga.ca_codigo.in_(codigos))
        .order_by(CaAdjuntoDescarga.id)
    ).scalars():
        filas[f.ca_codigo].append(f)
    cubiertas = set(
        session.execute(
            select(CaAnexo.ca_codigo).where(
                CaAnexo.ca_codigo.in_(codigos),
                CaAnexo.fuente != "descarga_directa",
                CaAnexo.extraccion_estado.in_(("hecha", "pendiente")),
            )
        ).scalars()
    )
    productos: dict[str, list[tuple[str | None, str | None]]] = defaultdict(list)
    for codigo, cod, nombre in session.execute(
        select(CaProducto.ca_codigo, CaProducto.codigo_producto, CaProducto.nombre).where(
            CaProducto.ca_codigo.in_(codigos)
        )
    ):
        productos[codigo].append((cod, nombre))
    salida: list[AdjuntoPendiente] = []
    for ca in candidatas:
        alimentos = _es_alimentos(productos.get(ca.codigo, ()))
        de_la_ca = filas.get(ca.codigo, [])
        marcador = next((f for f in de_la_ca if f.documento_id == LISTADO), None)
        archivos = [f for f in de_la_ca if f.documento_id != LISTADO]
        for f in archivos:
            if _entra(f):
                salida.append(
                    AdjuntoPendiente(
                        codigo=ca.codigo, documento_id=f.documento_id, nombre=f.nombre or "",
                        fecha_cierre=ca.fecha_cierre, pedido=f.estado == PEDIDO,
                        alimentos=alimentos,
                    )
                )  # fmt: skip
        sin_listar = marcador is None or _reintentable(marcador)
        if sin_listar and not archivos and ca.codigo not in cubiertas:
            salida.append(
                AdjuntoPendiente(
                    codigo=ca.codigo, documento_id=None, nombre="",
                    fecha_cierre=ca.fecha_cierre, alimentos=alimentos,
                )
            )  # fmt: skip
    salida.sort(key=lambda p: (not p.pedido, not p.alimentos))  # estable: conserva el cierre
    return salida if limite is None else salida[:limite]


def _fila(session: Session, codigo: str, documento_id: str) -> CaAdjuntoDescarga:
    fila = session.execute(
        select(CaAdjuntoDescarga).where(
            CaAdjuntoDescarga.ca_codigo == codigo, CaAdjuntoDescarga.documento_id == documento_id
        )
    ).scalar_one_or_none()
    if fila is None:
        fila = CaAdjuntoDescarga(ca_codigo=codigo, documento_id=documento_id, intentos=0)
        session.add(fila)
    return fila


def registrar_listado(
    session: Session, codigo: str, archivos: list[tuple[str, str]], ahora: datetime
) -> list[AdjuntoPendiente]:
    """Anota lo que devolvió `listar`: el marcador queda `bajado` (o `fallido/sin_archivos` si la
    CA no trae archivos) y cada archivo nuevo queda `listado`. Devuelve los que hay que bajar
    (los nuevos y los que ya estaban por bajar). No commitea."""
    marcador = _fila(session, codigo, LISTADO)
    marcador.intentos = (marcador.intentos or 0) + 1
    marcador.actualizado_en = ahora
    if not archivos:
        marcador.estado, marcador.motivo = FALLIDO, "sin_archivos"
        return []
    marcador.estado, marcador.motivo = BAJADO, None
    ca = session.get(CompraAgil, codigo)
    salida: list[AdjuntoPendiente] = []
    for doc_id, nombre in archivos:
        if doc_id == LISTADO:
            continue
        fila = _fila(session, codigo, doc_id)
        if fila.estado is None:  # nueva
            fila.estado, fila.nombre, fila.actualizado_en = POR_BAJAR, nombre[:200], ahora
        if _entra(fila):
            salida.append(
                AdjuntoPendiente(
                    codigo=codigo, documento_id=doc_id, nombre=nombre,
                    fecha_cierre=ca.fecha_cierre if ca is not None else None,
                    pedido=fila.estado == PEDIDO,
                )
            )  # fmt: skip
    return salida


def marcar_bajado(
    session: Session, codigo: str, documento_id: str, anexo_id: int | None, ahora: datetime
) -> None:
    """El archivo llegó y pasó por la lectura. No commitea."""
    fila = _fila(session, codigo, documento_id)
    fila.estado, fila.motivo, fila.anexo_id = BAJADO, None, anexo_id
    fila.intentos = (fila.intentos or 0) + 1
    fila.actualizado_en = ahora


def marcar_fallido(
    session: Session, codigo: str, documento_id: str, motivo: str, ahora: datetime
) -> None:
    """No llegó un archivo utilizable, o el listado falló (`motivo`). No commitea."""
    fila = _fila(session, codigo, documento_id)
    fila.estado, fila.motivo = FALLIDO, motivo[:40]
    fila.intentos = (fila.intentos or 0) + 1
    fila.actualizado_en = ahora


def _anexo_releible(a: CaAnexo) -> bool:
    """Igual que la ficha (F-plat-lectura): la lectura falló, pero el archivo se aceptó.
    Un archivo rechazado por inválido no invita a releer."""
    return (
        a.extraccion_estado in ESTADOS_RELEIBLES
        and a.mime != "desconocido"
        and bool(a.paginas or a.texto_extraido)
    )


def _archivos(session: Session, codigo: str) -> list[CaAdjuntoDescarga]:
    return [
        f
        for f in session.execute(
            select(CaAdjuntoDescarga).where(CaAdjuntoDescarga.ca_codigo == codigo)
        ).scalars()
        if f.documento_id != LISTADO
    ]


def hay_pedido(session: Session, codigo: str) -> bool:
    """¿Algún archivo de la CA espera al job porque una persona lo pidió?"""
    return (
        session.execute(
            select(CaAdjuntoDescarga.id)
            .where(CaAdjuntoDescarga.ca_codigo == codigo, CaAdjuntoDescarga.estado == PEDIDO)
            .limit(1)
        ).first()
        is not None
    )


def puede_rebajar(session: Session, ca: CompraAgil) -> bool:
    """¿Mostrar «Volver a bajar y leer»? CA vigente, sin «Anexo resuelto», con archivos ya
    listados por el job y alguno fallido o con una lectura releíble, y sin un pedido en cola."""
    from app.core.tiempo import ahora_utc
    from app.models.enums import EstadoOportunidad

    if ca.anexo_resuelto_en is not None:
        return False
    if ca.estado != EstadoOportunidad.PUBLICADA.value or not (
        ca.fecha_cierre and ca.fecha_cierre > ahora_utc()
    ):
        return False
    filas = _archivos(session, ca.codigo)
    if not filas or any(f.estado == PEDIDO for f in filas):
        return False
    if any(f.estado == FALLIDO for f in filas):
        return True
    return any(
        _anexo_releible(a)
        for a in session.execute(select(CaAnexo).where(CaAnexo.ca_codigo == ca.codigo)).scalars()
    )


def pedir_de_nuevo(
    session: Session, ca: CompraAgil, usuario_id: int | None, ahora: datetime
) -> int:
    """Encola para el job los archivos de la CA que no quedaron leídos (botón de la ficha).

    Se salta un archivo ya bajado cuyo anexo quedó `hecha` o `pendiente`, y los que ya esperan
    (`listado`). Los demás (fallidos o con lectura `sin_presupuesto`/`error`) pasan a `pedido`
    con `intentos = 0`. Devuelve cuántos encoló. No commitea."""
    anexos: dict[int, Any] = {
        a.id: a
        for a in session.execute(select(CaAnexo).where(CaAnexo.ca_codigo == ca.codigo)).scalars()
    }
    n = 0
    for fila in _archivos(session, ca.codigo):
        if fila.estado == POR_BAJAR:
            continue
        if (
            fila.estado == BAJADO
            and fila.anexo_id in anexos
            and anexos[fila.anexo_id].extraccion_estado not in ESTADOS_RELEIBLES
        ):
            continue
        fila.estado, fila.motivo, fila.intentos = PEDIDO, None, 0
        fila.pedido_por = usuario_id
        fila.actualizado_en = ahora
        n += 1
    return n
