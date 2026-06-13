"""Cliente para la API Compra Ágil v2 de Mercado Público."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import Engine

from app.clients.base import BaseClient, MPAuthError, MPParseError, QuotaTracker, RateLimiter
from app.clients.types import (
    CompraAgilBasica,
    CompraAgilDetalle,
    CompraAgilItem,
    PaginacionV2,
    RespuestaListadoV2,
    parse_fecha_iso,
    parse_float,
    parse_int,
)
from app.core.logging import get_logger
from app.core.settings import Settings

_log = get_logger(__name__)

_BASE = "https://api2.mercadopublico.cl"
_LISTADO = _BASE + "/v2/compra-agil"
_DETALLE = _BASE + "/v2/compra-agil/{codigo}"


def _validar_envelope(data: dict[str, object]) -> dict[str, object]:
    success = data.get("success")
    if success == "OK":
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise MPParseError(f"payload no es dict: {type(payload)}")
        return payload
    errors = data.get("errors") or []
    if isinstance(errors, list):
        for e in errors:
            if isinstance(e, dict) and str(e.get("codigo") or "") == "401":
                raise MPAuthError("Ticket inválido (error en envelope v2)")
    raise MPParseError(f"success={success!r} errors={errors}")


def _parse_ca_basica(item: dict[str, object]) -> CompraAgilBasica:
    estado_raw = item.get("estado") or {}
    estado_str = ""
    if isinstance(estado_raw, dict):
        estado_str = str(estado_raw.get("codigo") or estado_raw.get("glosa") or "")
    elif isinstance(estado_raw, str):
        estado_str = estado_raw

    fechas = item.get("fechas") or {}
    montos = item.get("montos") or {}
    institucion = item.get("institucion") or {}

    region_raw = institucion.get("region") if isinstance(institucion, dict) else None
    region = parse_int(region_raw)

    resumen = item.get("resumen") or {}
    total_ofertas = (
        parse_int(resumen.get("total_ofertas_recibidas") if isinstance(resumen, dict) else None)
        or 0
    )

    return CompraAgilBasica(
        codigo=str(item.get("codigo") or ""),
        nombre=str(item.get("nombre") or ""),
        estado=estado_str,
        fecha_publicacion=parse_fecha_iso(
            fechas.get("fecha_publicacion") if isinstance(fechas, dict) else None
        ),
        fecha_cierre=parse_fecha_iso(
            fechas.get("fecha_cierre") if isinstance(fechas, dict) else None
        ),
        fecha_ultimo_cambio=parse_fecha_iso(
            fechas.get("fecha_ultimo_cambio") if isinstance(fechas, dict) else None
        ),
        monto_clp=parse_float(
            montos.get("monto_disponible_clp") if isinstance(montos, dict) else None
        ),
        region=region,
        organismo_nombre=str(
            institucion.get("organismo_comprador") if isinstance(institucion, dict) else ""
        )
        or None,
        organismo_rut=str(institucion.get("rut") if isinstance(institucion, dict) else "") or None,
        total_ofertas=total_ofertas,
    )


def _parse_ca_detalle(payload: dict[str, object]) -> CompraAgilDetalle:
    base = _parse_ca_basica(payload)
    productos_raw = payload.get("productos_solicitados") or []
    productos = []
    if isinstance(productos_raw, list):
        for p in productos_raw:
            if not isinstance(p, dict):
                continue
            productos.append(
                CompraAgilItem(
                    codigo_producto=str(p.get("codigo_producto") or ""),
                    nombre=str(p.get("nombre") or ""),
                    cantidad=parse_float(p.get("cantidad")),
                    unidad=str(p.get("unidad_medida") or ""),
                )
            )

    oc = payload.get("orden_compra") or {}
    id_oc = None
    if isinstance(oc, dict):
        id_oc = str(oc.get("id_orden_compra") or "") or None

    convocatoria = parse_int(payload.get("estado_convocatoria"))

    return CompraAgilDetalle(
        codigo=base.codigo,
        nombre=base.nombre,
        estado=base.estado,
        fecha_publicacion=base.fecha_publicacion,
        fecha_cierre=base.fecha_cierre,
        fecha_ultimo_cambio=base.fecha_ultimo_cambio,
        monto_clp=base.monto_clp,
        region=base.region,
        organismo_nombre=base.organismo_nombre,
        organismo_rut=base.organismo_rut,
        total_ofertas=base.total_ofertas,
        descripcion=str(payload.get("descripcion") or ""),
        productos=productos,
        id_orden_compra=id_oc,
        estado_convocatoria=convocatoria,
    )


class MercadoPublicoV2Client:
    """Acceso a la API Compra Ágil v2 de Mercado Público."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        rl = RateLimiter(settings.rate_limit_rps)
        quota = QuotaTracker(engine, settings.api_daily_budget)
        self._ticket = settings.mp_ticket
        self._client = BaseClient(
            ticket=settings.mp_ticket,
            rate_limiter=rl,
            quota=quota,
            default_headers={"ticket": settings.mp_ticket},
        )

    def _get(self, url: str, params: dict[str, object] | None = None) -> dict[str, object]:
        return self._client._request("GET", url, params=params or {})

    def listar_compra_agil(
        self,
        cambio_desde: datetime | None = None,
        ttl_cambio_ms: int | None = None,
        publicado_desde: datetime | None = None,
        publicado_hasta: datetime | None = None,
        estados: list[str] | None = None,
        regiones: list[int] | None = None,
        q: str | None = None,
        tamano_pagina: int = 50,
        numero_pagina: int = 1,
        ordenar_por: str | None = None,
    ) -> RespuestaListadoV2:
        if ttl_cambio_ms is not None and cambio_desde is not None:
            raise ValueError("ttl_cambio_ms y cambio_desde son mutuamente excluyentes")

        params: dict[str, object] = {
            "tamano_pagina": min(tamano_pagina, 50),
            "numero_pagina": numero_pagina,
        }
        if ttl_cambio_ms is not None:
            params["ttl_cambio_ms"] = ttl_cambio_ms
        if cambio_desde is not None:
            params["cambio_desde"] = cambio_desde.isoformat()
        if publicado_desde is not None:
            params["publicado_desde"] = publicado_desde.isoformat()
        if publicado_hasta is not None:
            params["publicado_hasta"] = publicado_hasta.isoformat()
        if estados:
            params["estado"] = ",".join(estados)
        if regiones:
            params["region"] = ",".join(str(r) for r in regiones)
        if q:
            params["q"] = q
        if ordenar_por:
            params["ordenar_por"] = ordenar_por

        data = self._get(_LISTADO, params)
        payload = _validar_envelope(data)

        items_raw = payload.get("convocatorias") or payload.get("items") or []
        items: list[CompraAgilBasica] = []
        if isinstance(items_raw, list):
            items = [_parse_ca_basica(i) for i in items_raw if isinstance(i, dict)]

        paginacion_raw = payload.get("paginacion") or {}
        paginacion = PaginacionV2(
            total_paginas=parse_int(
                paginacion_raw.get("total_paginas") if isinstance(paginacion_raw, dict) else None
            )
            or 1,
            total_resultados=parse_int(
                paginacion_raw.get("total_resultados") if isinstance(paginacion_raw, dict) else None
            )
            or 0,
            numero_pagina=parse_int(
                paginacion_raw.get("numero_pagina") if isinstance(paginacion_raw, dict) else None
            )
            or numero_pagina,
            tamano_pagina=parse_int(
                paginacion_raw.get("tamano_pagina") if isinstance(paginacion_raw, dict) else None
            )
            or tamano_pagina,
        )

        return RespuestaListadoV2(items=items, paginacion=paginacion)

    def iterar_compra_agil(
        self,
        cambio_desde: datetime | None = None,
        ttl_cambio_ms: int | None = None,
        publicado_desde: datetime | None = None,
        publicado_hasta: datetime | None = None,
        estados: list[str] | None = None,
        regiones: list[int] | None = None,
        q: str | None = None,
        tamano_pagina: int = 50,
        ordenar_por: str | None = None,
    ) -> Iterator[CompraAgilBasica]:
        pagina = 1
        while True:
            resp = self.listar_compra_agil(
                cambio_desde=cambio_desde,
                ttl_cambio_ms=ttl_cambio_ms,
                publicado_desde=publicado_desde,
                publicado_hasta=publicado_hasta,
                estados=estados,
                regiones=regiones,
                q=q,
                tamano_pagina=tamano_pagina,
                numero_pagina=pagina,
                ordenar_por=ordenar_por,
            )
            yield from resp.items
            if pagina >= resp.paginacion.total_paginas:
                break
            pagina += 1

    def detalle_compra_agil(self, codigo: str) -> CompraAgilDetalle:
        url = _DETALLE.format(codigo=codigo)
        data = self._get(url)
        payload = _validar_envelope(data)
        return _parse_ca_detalle(payload)
