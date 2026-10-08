"""Cliente para la API clásica v1 de Mercado Público."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from sqlalchemy import Engine

from app.clients.base import BaseClient, QuotaTracker, rate_limiter_compartido
from app.clients.types import (
    Comprador,
    ItemLicitacion,
    LicitacionBasica,
    LicitacionDetalle,
    OrdenCompraBasica,
    Proveedor,
    parse_binario,
    parse_fecha_v1,
    parse_fecha_v1_dt,
    parse_float,
    parse_int,
)
from app.core.logging import get_logger
from app.core.settings import Settings
from app.models.seeds import REGIONES

_log = get_logger(__name__)

_BASE = "https://api.mercadopublico.cl/servicios/v1/publico/"
_LICITACIONES = _BASE + "licitaciones.json"
_ORDENES = _BASE + "ordenesdecompra.json"
_PROVEEDOR = _BASE + "Empresas/BuscarProveedor"
_COMPRADORES = _BASE + "Empresas/BuscarComprador"


def _fecha_v1(d: date) -> str:
    return f"{d.day:02d}{d.month:02d}{d.year:04d}"


def _texto(v: object) -> str | None:
    """`str(v)` sin espacios, o None si falta o queda vacío."""
    if v is None:
        return None
    txt = str(v).strip()
    return txt or None


def _dict(v: object) -> dict[str, object]:
    return v if isinstance(v, dict) else {}


# Palabras que no distinguen una región de otra: "Región del Maule" y "Maule"
# tienen que caer en el mismo código.
_RELLENO_REGION = frozenset({"region", "de", "del", "y"})


def _clave_region(nombre: str) -> str:
    """Minúsculas, sin tildes ni signos y sin las palabras de relleno.

    Se pega todo: así "O´Higgins" y "O'Higgins", o "Bío-Bío" y "Biobío", dan la
    misma clave.
    """
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFKD", nombre.lower()) if not unicodedata.combining(c)
    )
    palabras = re.sub(r"[^a-z0-9]+", " ", sin_tildes).split()
    return "".join(p for p in palabras if p not in _RELLENO_REGION)


_REGION_POR_CLAVE: dict[str, int] = {_clave_region(n): c for c, n in REGIONES}
_regiones_desconocidas: set[str] = set()


def region_desde_nombre(nombre: object) -> int | None:
    """Código de región (1–16) desde el nombre que manda v1 en `Comprador.RegionUnidad`.

    Regla 6: un nombre que no calza da None y se loguea una sola vez por valor.
    """
    txt = _texto(nombre)
    if txt is None:
        return None
    codigo = _REGION_POR_CLAVE.get(_clave_region(txt))
    if codigo is None and txt not in _regiones_desconocidas:
        _regiones_desconocidas.add(txt)
        _log.warning("Región v1 no reconocida: %r", txt)
    return codigo


def _parse_licitacion_basica(item: dict[str, object]) -> LicitacionBasica:
    # En el detalle, organismo y región viven bajo `Comprador`, y las fechas bajo
    # `Fechas`; el primer nivel viene null [V, sonda claves-lic, 08-oct-2026]. El
    # listado de activas sí trae `FechaCierre` en el primer nivel: se lee primero
    # el primer nivel y el bloque queda de respaldo, sin cambiar lo que ya andaba.
    comprador = _dict(item.get("Comprador"))
    fechas = _dict(item.get("Fechas"))
    return LicitacionBasica(
        codigo=str(item.get("CodigoExterno") or item.get("Codigo") or ""),
        nombre=str(item.get("Nombre") or ""),
        estado=parse_int(item.get("CodigoEstado")),
        fecha_publicacion=parse_fecha_v1_dt(
            item.get("FechaPublicacion") or fechas.get("FechaPublicacion")
        ),
        fecha_cierre=parse_fecha_v1_dt(
            item.get("FechaCierre") or fechas.get("FechaCierre"), fin_de_dia=True
        ),
        tipo=str(item.get("Tipo") or item.get("CodigoTipo") or "") or None,
        codigo_organismo=_texto(comprador.get("CodigoOrganismo"))
        or _texto(item.get("CodigoOrganismo")),
        organismo_nombre=_texto(comprador.get("NombreOrganismo"))
        or _texto(item.get("NombreOrganismo")),
        region=region_desde_nombre(comprador.get("RegionUnidad")),
    )


def _parse_licitacion_detalle(data: dict[str, object]) -> LicitacionDetalle:
    licitacion = data.get("Listado", [data])
    item: dict[str, object] = licitacion[0] if isinstance(licitacion, list) and licitacion else data

    items_raw = item.get("Items", {})
    items_lista: list[dict[str, object]] = []
    if isinstance(items_raw, dict):
        raw = items_raw.get("Listado") or []
        items_lista = raw if isinstance(raw, list) else []
    elif isinstance(items_raw, list):
        items_lista = items_raw

    items = [
        ItemLicitacion(
            codigo_producto=str(i.get("CodigoProducto") or ""),
            nombre=str(i.get("NombreProducto") or i.get("Nombre") or ""),
            cantidad=parse_float(i.get("Cantidad")),
            unidad=str(i.get("UnidadMedida") or ""),
        )
        for i in items_lista
        if isinstance(i, dict)
    ]

    base = _parse_licitacion_basica(item)
    return LicitacionDetalle(
        codigo=base.codigo,
        nombre=base.nombre,
        estado=base.estado,
        fecha_publicacion=base.fecha_publicacion,
        fecha_cierre=base.fecha_cierre,
        tipo=base.tipo,
        codigo_organismo=base.codigo_organismo,
        organismo_nombre=base.organismo_nombre,
        region=base.region,
        descripcion=str(item.get("Descripcion") or ""),
        moneda=str(item.get("Moneda") or ""),
        monto_estimado=parse_float(item.get("MontoEstimado")),
        tipo_monto=parse_int(item.get("TipoConvocatoria")),
        items=items,
        informada=parse_binario(item.get("Informada")),
        contrato=parse_binario(item.get("Contrato")),
        obras=parse_binario(item.get("Obras")),
    )


class MercadoPublicoV1Client:
    """Acceso a la API clásica de Mercado Público (v1)."""

    def __init__(self, settings: Settings, engine: Engine, *, reserva: int = 0) -> None:
        # `reserva`: requests del día que este cliente deja para `ca` (F-datos-1).
        rl = rate_limiter_compartido(settings.rate_limit_rps)
        quota = QuotaTracker(engine, settings.api_daily_budget, reserva=reserva)
        self._ticket = settings.mp_ticket
        self._client = BaseClient(ticket=settings.mp_ticket, rate_limiter=rl, quota=quota)

    def _get(
        self,
        url: str,
        params: dict[str, object] | None = None,
        *,
        reintentar_transitorios: bool = True,
    ) -> dict[str, object]:
        p: dict[str, object] = {"ticket": self._ticket}
        if params:
            p.update(params)
        return self._client._request(
            "GET", url, params=p, reintentar_transitorios=reintentar_transitorios
        )

    # --- Licitaciones ---

    def licitaciones_por_fecha(
        self,
        fecha: date,
        estado: str | None = None,
        codigo_organismo: str | None = None,
        codigo_proveedor: str | None = None,
    ) -> list[LicitacionBasica]:
        params: dict[str, object] = {"fecha": _fecha_v1(fecha)}
        if estado:
            params["estado"] = estado
        if codigo_organismo:
            params["CodigoOrganismo"] = codigo_organismo
        if codigo_proveedor:
            params["CodigoProveedor"] = codigo_proveedor
        data = self._get(_LICITACIONES, params)
        listado = data.get("Listado") or []
        if not isinstance(listado, list):
            return []
        return [_parse_licitacion_basica(item) for item in listado if isinstance(item, dict)]

    def licitaciones_activas(self) -> list[LicitacionBasica]:
        data = self._get(_LICITACIONES, {"estado": "activas"})
        listado = data.get("Listado") or []
        if not isinstance(listado, list):
            return []
        if listado and isinstance(listado[0], dict):
            _log.debug("Formato crudo FechaCierre (listado activas): %r", listado[0].get("FechaCierre"))
        return [_parse_licitacion_basica(item) for item in listado if isinstance(item, dict)]

    def licitacion_detalle(
        self, codigo: str, *, reintentar_transitorios: bool = True
    ) -> LicitacionDetalle:
        """Detalle de una licitación.

        `reintentar_transitorios=False`: un 5xx o timeout se lanza al primer
        fallo, sin reintento (ver `BaseClient._request`).
        """
        data = self._get(
            _LICITACIONES, {"codigo": codigo}, reintentar_transitorios=reintentar_transitorios
        )
        return _parse_licitacion_detalle(data)

    # --- Órdenes de Compra ---

    def ordenes_por_fecha(
        self,
        fecha: date,
        estado: str | None = None,
        codigo_organismo: str | None = None,
        codigo_proveedor: str | None = None,
    ) -> list[OrdenCompraBasica]:
        params: dict[str, object] = {"fecha": _fecha_v1(fecha)}
        if estado:
            params["estado"] = estado
        if codigo_organismo:
            params["CodigoOrganismo"] = codigo_organismo
        if codigo_proveedor:
            params["CodigoProveedor"] = codigo_proveedor
        data = self._get(_ORDENES, params)
        listado = data.get("Listado") or []
        if not isinstance(listado, list):
            return []
        return [self._parse_oc(item) for item in listado if isinstance(item, dict)]

    def orden_detalle(self, codigo: str) -> OrdenCompraBasica:
        data = self._get(_ORDENES, {"codigo": codigo})
        listado = data.get("Listado") or [data]
        item = listado[0] if isinstance(listado, list) and listado else data
        return self._parse_oc(item if isinstance(item, dict) else {})

    @staticmethod
    def _parse_oc(item: dict[str, object]) -> OrdenCompraBasica:
        return OrdenCompraBasica(
            codigo=str(item.get("Codigo") or item.get("CodigoExterno") or ""),
            nombre=str(item.get("Nombre") or ""),
            estado=parse_int(item.get("CodigoEstado")),
            tipo=parse_int(item.get("Tipo") or item.get("CodigoTipo")),
            fecha_creacion=parse_fecha_v1(item.get("FechaCreacion")),
            codigo_organismo=str(item.get("CodigoOrganismo") or "") or None,
            monto=parse_float(item.get("Monto") or item.get("MontoTotal")),
            moneda=str(item.get("Moneda") or "") or None,
        )

    # --- Proveedores / Compradores ---

    def buscar_proveedor(self, rut: str) -> list[Proveedor]:
        data = self._get(_PROVEEDOR, {"rutempresaproveedor": rut})
        listado = data.get("Listado") or []
        if not isinstance(listado, list):
            return []
        return [
            Proveedor(
                rut=str(p.get("RutProveedor") or ""),
                nombre=str(p.get("NombreProveedor") or p.get("Nombre") or ""),
                codigo=str(p.get("CodigoProveedor") or "") or None,
            )
            for p in listado
            if isinstance(p, dict)
        ]

    def listar_compradores(self) -> list[Comprador]:
        data = self._get(_COMPRADORES)
        listado = data.get("Listado") or []
        if not isinstance(listado, list):
            return []
        return [
            Comprador(
                codigo=str(c.get("CodigoOrganismo") or ""),
                nombre=str(c.get("NombreOrganismo") or c.get("Nombre") or ""),
                rut=str(c.get("RutOrganismo") or "") or None,
            )
            for c in listado
            if isinstance(c, dict)
        ]
