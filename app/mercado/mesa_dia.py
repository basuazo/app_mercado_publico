"""Mesa del día v0 (F-mesa-v0): Compras Ágiles vigentes del territorio, sin predicción.

- Vigente = `estado = 'publicada'` y `fecha_cierre` futura, en las regiones activas (regla 25).
  Las fechas se guardan naive en UTC (`app.core.tiempo`); los cortes de "hoy / mañana / esta
  semana" son DÍAS CALENDARIO de Chile, convertidos con `borde_del_dia_utc_naive`.
- Nunca se carga `raw_json` (regla 12): solo columnas livianas; el listado completo de vigentes
  (cientos de filas) se resuelve como tuplas y solo la página pedida se lee en detalle.
- La comuna detectada en la dirección de entrega es **[I]** (inferida por texto): se marca así.
- Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.api.presentacion import banda_urgencia
from app.catalogos.mercado import norm
from app.core.tiempo import TZ_CHILE, borde_del_dia_utc_naive
from app.mercado.adjudicacion import familia_de_codigo
from app.models.enums import EstadoOportunidad
from app.models.tables import CaLinea, CaProducto, CompraAgil

PAGINA = 50
CIERRES = ("hoy", "manana", "semana")
DETALLES = ("sin_detalle", "incompleto", "completo")
_LIKE_ESCAPE = "\\"

# Columnas de la tarjeta: nunca `raw_json` ni `descripcion` (regla 12).
_COLUMNAS = (
    CompraAgil.codigo,
    CompraAgil.nombre,
    CompraAgil.organismo_nombre,
    CompraAgil.organismo_rut,
    CompraAgil.unidad_compra,
    CompraAgil.direccion_entrega,
    CompraAgil.fecha_cierre,
    CompraAgil.monto_disponible_clp,
    CompraAgil.detalle_lineas,
    CompraAgil.n_documentos,
    CompraAgil.plazo_entrega_dias,
    CompraAgil.total_ofertas,
)


@dataclass(frozen=True)
class FiltrosMesa:
    familia: str = ""
    cierre: str = ""
    detalle: str = ""
    con_anexos: bool = False
    monto_min: int | None = None
    q: str = ""


def hoy_en_chile(ahora: datetime) -> date:
    """Día calendario de Chile de un instante naive UTC."""
    return ahora.replace(tzinfo=UTC).astimezone(TZ_CHILE).date()


def fin_de_dia(d: date) -> datetime:
    """Último instante del día `d` en Chile, como naive UTC."""
    return borde_del_dia_utc_naive(d, fin_de_dia=True)


def _condiciones_vigente(regiones: list[int], ahora: datetime) -> list[Any]:
    return [
        CompraAgil.region.in_(regiones),
        CompraAgil.estado == EstadoOportunidad.PUBLICADA.value,
        CompraAgil.fecha_cierre > ahora,
    ]


def contar_vigentes(session: Session, regiones: list[int], ahora: datetime) -> tuple[int, int]:
    """(vigentes, de ellas las que cierran hoy en Chile). Una definición para «Hoy» y la mesa."""
    cond = _condiciones_vigente(regiones, ahora)
    total = session.execute(select(func.count()).select_from(CompraAgil).where(*cond)).scalar_one()
    hoy = session.execute(
        select(func.count())
        .select_from(CompraAgil)
        .where(*cond, CompraAgil.fecha_cierre <= fin_de_dia(hoy_en_chile(ahora)))
    ).scalar_one()
    return int(total), int(hoy)


def _escapar_like(texto: str) -> str:
    return (
        texto.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", r"\%").replace("_", r"\_")
    )


def _condiciones_filtros(f: FiltrosMesa, ahora: datetime) -> list[Any]:
    cond: list[Any] = []
    hoy = hoy_en_chile(ahora)
    if f.cierre == "hoy":
        cond.append(CompraAgil.fecha_cierre <= fin_de_dia(hoy))
    elif f.cierre == "manana":
        cond += [
            CompraAgil.fecha_cierre > fin_de_dia(hoy),
            CompraAgil.fecha_cierre <= fin_de_dia(hoy + timedelta(days=1)),
        ]
    elif f.cierre == "semana":  # hasta el domingo de esta semana (lunes = 0)
        cond.append(CompraAgil.fecha_cierre <= fin_de_dia(hoy + timedelta(days=6 - hoy.weekday())))
    if f.detalle in DETALLES:
        cond.append(CompraAgil.detalle_lineas == f.detalle)
    if f.con_anexos:
        cond.append(CompraAgil.n_documentos > 0)
    if f.monto_min:
        cond.append(CompraAgil.monto_disponible_clp >= f.monto_min)
    if f.q.strip():
        # ILIKE parametrizado (no hay FTS para el nombre de la CA); `%` y `_` del usuario, escapados.
        cond.append(
            CompraAgil.nombre.ilike(f"%{_escapar_like(f.q.strip())}%", escape=_LIKE_ESCAPE)
        )
    return cond


def familias_por_ca(
    session: Session, regiones: list[int], ahora: datetime
) -> dict[str, list[str]]:
    """codigo → familias distintas de sus productos (la más frecuente primero), de las vigentes."""
    productos: dict[str, list[tuple[str | None, str | None]]] = defaultdict(list)
    for codigo, cod, nombre in session.execute(
        select(CaProducto.ca_codigo, CaProducto.codigo_producto, CaProducto.nombre)
        .join(CompraAgil, CompraAgil.codigo == CaProducto.ca_codigo)
        .where(*_condiciones_vigente(regiones, ahora))
    ):
        productos[codigo].append((cod, nombre))
    out: dict[str, list[str]] = {}
    for codigo, items in productos.items():
        cuenta = Counter(familia_de_codigo(c, n) for c, n in items)
        out[codigo] = [f for f, _ in sorted(cuenta.items(), key=lambda kv: (-kv[1], kv[0]))]
    return out


def familias_de_ca(session: Session, codigo: str) -> list[str]:
    """Familias distintas de UNA CA, como `familias_por_ca` (la más frecuente primero)."""
    cuenta = Counter(
        familia_de_codigo(c, n)
        for c, n in session.execute(
            select(CaProducto.codigo_producto, CaProducto.nombre).where(
                CaProducto.ca_codigo == codigo
            )
        )
    )
    return [f for f, _ in sorted(cuenta.items(), key=lambda kv: (-kv[1], kv[0]))]


def etiqueta_familia(familias: list[str]) -> str:
    if not familias:
        return "Sin productos"
    return familias[0] if len(familias) == 1 else "Varias"


def comuna_en_direccion(direccion: str | None, comunas: list[str]) -> str | None:
    """Primera comuna prioritaria nombrada en la dirección, comparando sin tildes ni mayúsculas
    y por palabra completa (la más larga primero). [I]: es texto libre del organismo."""
    texto = norm(direccion)
    if not texto:
        return None
    for comuna in sorted((c for c in comunas if norm(c)), key=lambda c: -len(norm(c))):
        patron = rf"(?<![a-z0-9]){re.escape(norm(comuna))}(?![a-z0-9])"
        if re.search(patron, texto):
            return comuna
    return None


def buscar_codigos(
    session: Session,
    regiones: list[int],
    comunas: list[str],
    filtros: FiltrosMesa,
    ahora: datetime,
    familias: dict[str, list[str]] | None = None,
) -> list[str]:
    """Códigos de las CA vigentes que pasan los filtros, en el orden de la mesa: primero las de
    comunas prioritarias (si hay), luego cierre ascendente. Solo tuplas livianas."""
    consulta: Select[Any] = (
        select(CompraAgil.codigo, CompraAgil.direccion_entrega)
        .where(*_condiciones_vigente(regiones, ahora), *_condiciones_filtros(filtros, ahora))
        .order_by(CompraAgil.fecha_cierre, CompraAgil.codigo)
    )
    filas = session.execute(consulta).all()
    if filtros.familia:
        if familias is None:
            familias = familias_por_ca(session, regiones, ahora)
        filas = [f for f in filas if filtros.familia in familias.get(f[0], [])]
    if comunas:
        # sort estable: dentro de cada grupo se conserva el orden por cierre.
        filas = sorted(filas, key=lambda f: comuna_en_direccion(f[1], comunas) is None)
    return [f[0] for f in filas]


def cuenta_regresiva(cierre: datetime | None, ahora: datetime) -> str:
    if cierre is None:
        return "Sin fecha de cierre"
    resto = cierre - ahora
    minutos = int(resto.total_seconds() // 60)
    if minutos <= 0:
        return "Cerrada"
    dias, resto_min = divmod(minutos, 1440)
    horas, mins = divmod(resto_min, 60)
    if dias:
        return f"Cierra en {dias} d {horas} h"
    if horas:
        return f"Cierra en {horas} h {mins} min"
    return f"Cierra en {mins} min"


def cierre_chile(cierre: datetime | None) -> str:
    """`dd/mm/aaaa HH:MM` en hora de Chile de un instante naive UTC."""
    if cierre is None:
        return "—"
    return cierre.replace(tzinfo=UTC).astimezone(TZ_CHILE).strftime("%d/%m/%Y %H:%M")


def _urgencia(cierre: datetime | None, ahora: datetime) -> str:
    return banda_urgencia(None if cierre is None else (cierre - ahora).total_seconds() / 86400)


def tarjetas(
    session: Session,
    codigos: list[str],
    comunas: list[str],
    familias: dict[str, list[str]],
    ahora: datetime,
) -> list[dict[str, Any]]:
    """Datos de pantalla de las CA `codigos`, en ese orden (la página ya recortada)."""
    if not codigos:
        return []
    filas = {
        f.codigo: f
        for f in session.execute(select(*_COLUMNAS).where(CompraAgil.codigo.in_(codigos))).all()
    }
    n_lineas: dict[str, int] = {
        ca: int(n)
        for ca, n in session.execute(
            select(CaLinea.ca_codigo, func.count())
            .where(CaLinea.ca_codigo.in_(codigos))
            .group_by(CaLinea.ca_codigo)
        )
    }
    out: list[dict[str, Any]] = []
    for codigo in codigos:
        f = filas.get(codigo)
        if f is None:
            continue
        comuna = comuna_en_direccion(f.direccion_entrega, comunas)
        out.append(
            {
                "codigo": f.codigo,
                "nombre": f.nombre,
                "organismo": f.organismo_nombre,
                "unidad": f.unidad_compra,
                "comuna_detectada": comuna,
                "cierre": cierre_chile(f.fecha_cierre),
                "cuenta_regresiva": cuenta_regresiva(f.fecha_cierre, ahora),
                "urgencia": _urgencia(f.fecha_cierre, ahora),
                "monto": f.monto_disponible_clp,
                "n_lineas": int(n_lineas.get(codigo, 0)),
                "detalle": f.detalle_lineas,
                "n_anexos": f.n_documentos or 0,
                "plazo_dias": f.plazo_entrega_dias,
                "familia": etiqueta_familia(familias.get(codigo, [])),
                "total_ofertas": f.total_ofertas,
            }
        )
    return out
