"""Cliente para la API clásica v1 de Mercado Público."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Engine

from app.clients.base import BaseClient, QuotaTracker, RateLimiter
from app.clients.types import (
    Comprador,
    ItemLicitacion,
    LicitacionBasica,
    LicitacionDetalle,
    OrdenCompraBasica,
    Proveedor,
    parse_binario,
    parse_fecha_v1,
    parse_float,
    parse_int,
)
from app.core.logging import get_logger
from app.core.settings import Settings

_log = get_logger(__name__)

_BASE = "https://api.mercadopublico.cl/servicios/v1/publico/"
_LICITACIONES = _BASE + "licitaciones.json"
_ORDENES = _BASE + "ordenesdecompra.json"
_PROVEEDOR = _BASE + "Empresas/BuscarProveedor"
_COMPRADORES = _BASE + "Empresas/BuscarComprador"


def _fecha_v1(d: date) -> str:
    return f"{d.day:02d}{d.month:02d}{d.year:04d}"


def _parse_licitacion_basica(item: dict[str, object]) -> LicitacionBasica:
    return LicitacionBasica(
        codigo=str(item.get("CodigoExterno") or item.get("Codigo") or ""),
        nombre=str(item.get("Nombre") or ""),
        estado=parse_int(item.get("CodigoEstado")),
        fecha_publicacion=parse_fecha_v1(item.get("FechaPublicacion")),
        fecha_cierre=parse_fecha_v1(item.get("FechaCierre")),
        tipo=str(item.get("Tipo") or item.get("CodigoTipo") or "") or None,
        codigo_organismo=str(item.get("CodigoOrganismo") or "") or None,
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

    def __init__(self, settings: Settings, engine: Engine) -> None:
        rl = RateLimiter(settings.rate_limit_rps)
        quota = QuotaTracker(engine, settings.api_daily_budget)
        self._ticket = settings.mp_ticket
        self._client = BaseClient(ticket=settings.mp_ticket, rate_limiter=rl, quota=quota)

    def _get(self, url: str, params: dict[str, object] | None = None) -> dict[str, object]:
        p: dict[str, object] = {"ticket": self._ticket}
        if params:
            p.update(params)
        return self._client._request("GET", url, params=p)

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
        return [_parse_licitacion_basica(item) for item in listado if isinstance(item, dict)]

    def licitacion_detalle(self, codigo: str) -> LicitacionDetalle:
        data = self._get(_LICITACIONES, {"codigo": codigo})
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
