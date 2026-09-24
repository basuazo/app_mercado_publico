"""Cliente para la API Compra Ágil v2 de Mercado Público."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import Engine

from app.clients.base import (
    BaseClient,
    MPAuthError,
    MPParseError,
    QuotaTracker,
    rate_limiter_compartido,
)
from app.clients.types import (
    CompraAgilBasica,
    CompraAgilDetalle,
    CompraAgilItem,
    PaginacionV2,
    RespuestaListadoV2,
    parse_fecha_v2,
    parse_float,
    parse_int,
)
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tiempo import a_naive_como_la_api

_log = get_logger(__name__)

_BASE = "https://api2.mercadopublico.cl"
_LISTADO = _BASE + "/v2/compra-agil"
_DETALLE = _BASE + "/v2/compra-agil/{codigo}"


def _iso_para_la_api(dt: datetime) -> str:
    """Serializa un instante en el formato que la API v2 usa: ISO-8601 sin offset.

    Internamente los instantes viajan naive en UTC (ver ``app/core/tiempo.py``);
    la API los manda y los espera en su propio huso, sin marcarlo. Este es el
    único lugar que deshace la conversión, y es la inversa exacta de la que
    aplica ``parse_fecha_v2`` al leer (que además ignora la `Z` falsa).
    """
    return a_naive_como_la_api(dt).isoformat()


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


def _texto_o_none(v: object) -> str | None:
    """`str(v)` sin espacios, o None si falta o queda vacío.

    Nunca devuelve el texto 'None': `str(None) or None` daba 'None' (F-detalles-match).
    """
    if v is None:
        return None
    txt = str(v).strip()
    return txt or None


def _dict(v: object) -> dict[str, object]:
    return v if isinstance(v, dict) else {}


def _monto_clp(item: dict[str, object]) -> float | None:
    """Monto disponible en CLP.

    Ruta real del DETALLE [V, sonda claves-ca]: `presupuesto.monto_disponible_clp`
    (el detalle no trae `montos`). El LISTADO lo trae en `montos`, que queda de
    respaldo. Regla 6: el primero que se pueda leer gana.
    """
    for contenedor in ("presupuesto", "montos"):
        monto = parse_float(_dict(item.get(contenedor)).get("monto_disponible_clp"))
        if monto is not None:
            return monto
    return None


def _parse_ca_basica(item: dict[str, object]) -> CompraAgilBasica:
    estado_raw = item.get("estado") or {}
    estado_str = ""
    if isinstance(estado_raw, dict):
        estado_str = str(estado_raw.get("codigo") or estado_raw.get("glosa") or "")
    elif isinstance(estado_raw, str):
        estado_str = estado_raw

    fechas = item.get("fechas") or {}
    institucion = _dict(item.get("institucion"))

    region = parse_int(institucion.get("region"))

    resumen = item.get("resumen") or {}
    total_ofertas = (
        parse_int(resumen.get("total_ofertas_recibidas") if isinstance(resumen, dict) else None)
        or 0
    )

    return CompraAgilBasica(
        codigo=str(item.get("codigo") or ""),
        nombre=str(item.get("nombre") or ""),
        estado=estado_str,
        fecha_publicacion=parse_fecha_v2(
            fechas.get("fecha_publicacion") if isinstance(fechas, dict) else None
        ),
        fecha_cierre=parse_fecha_v2(
            fechas.get("fecha_cierre") if isinstance(fechas, dict) else None,
            fin_de_dia=True,
        ),
        fecha_ultimo_cambio=parse_fecha_v2(
            fechas.get("fecha_ultimo_cambio") if isinstance(fechas, dict) else None
        ),
        monto_clp=_monto_clp(item),
        region=region,
        organismo_nombre=_texto_o_none(institucion.get("organismo_comprador")),
        organismo_rut=_texto_o_none(institucion.get("rut")),
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
                    # En el detalle real viene como int [V, sonda claves-ca].
                    codigo_producto=_texto_o_none(p.get("codigo_producto")) or "",
                    nombre=str(p.get("nombre") or ""),
                    cantidad=parse_float(p.get("cantidad")),
                    unidad=str(p.get("unidad_medida") or ""),
                    descripcion=str(p.get("descripcion") or ""),
                )
            )

    # Ruta real [V, sonda claves-ca]: primer nivel. `orden_compra.id_orden_compra`
    # queda de respaldo (forma que asumía F1, no observada). codigo_orden_compra
    # no se usa: viene null aunque exista OC (regla 6).
    id_oc = _texto_o_none(payload.get("id_orden_compra")) or _texto_o_none(
        _dict(payload.get("orden_compra")).get("id_orden_compra")
    )

    # Ruta real: `convocatoria.estado_convocatoria`; respaldo en el primer nivel.
    convocatoria = parse_int(_dict(payload.get("convocatoria")).get("estado_convocatoria"))
    if convocatoria is None:
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
        rl = rate_limiter_compartido(settings.rate_limit_rps)
        quota = QuotaTracker(engine, settings.api_daily_budget)
        self._ticket = settings.mp_ticket
        self._client = BaseClient(
            ticket=settings.mp_ticket,
            rate_limiter=rl,
            quota=quota,
            default_headers={"ticket": settings.mp_ticket},
        )

    def _get(
        self,
        url: str,
        params: dict[str, object] | None = None,
        *,
        reintentar_transitorios: bool = True,
    ) -> dict[str, object]:
        return self._client._request(
            "GET", url, params=params or {}, reintentar_transitorios=reintentar_transitorios
        )

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
        cambio_hasta: datetime | None = None,
    ) -> RespuestaListadoV2:
        # Grupo 1 de la guía oficial: ttl_cambio_ms O cambio_desde/cambio_hasta.
        if ttl_cambio_ms is not None and cambio_desde is not None:
            raise ValueError("ttl_cambio_ms y cambio_desde son mutuamente excluyentes")
        if ttl_cambio_ms is not None and cambio_hasta is not None:
            raise ValueError("ttl_cambio_ms y cambio_hasta son mutuamente excluyentes")

        params: dict[str, object] = {
            "tamano_pagina": min(tamano_pagina, 50),
            "numero_pagina": numero_pagina,
        }
        if ttl_cambio_ms is not None:
            params["ttl_cambio_ms"] = ttl_cambio_ms
        if cambio_desde is not None:
            # El cursor se guarda en UTC naive, pero la API manda (y espera) sus
            # fechas sin offset. Devolverlo tal cual lo correría horas hacia
            # adelante y la ingesta incremental perdería cambios: round-trip
            # simétrico vía a_naive_como_la_api (F-fecha-cierre).
            params["cambio_desde"] = _iso_para_la_api(cambio_desde)
        if cambio_hasta is not None:
            # Mismo huso que cambio_desde: los dos bordes de la ventana hablan el
            # idioma de la API (F-ca-ventana).
            params["cambio_hasta"] = _iso_para_la_api(cambio_hasta)
        if publicado_desde is not None:
            params["publicado_desde"] = _iso_para_la_api(publicado_desde)
        if publicado_hasta is not None:
            params["publicado_hasta"] = _iso_para_la_api(publicado_hasta)
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

    def detalle_compra_agil(
        self, codigo: str, *, reintentar_transitorios: bool = True
    ) -> CompraAgilDetalle:
        """Detalle de una Compra Ágil.

        `reintentar_transitorios=False`: un 5xx o timeout se lanza al primer
        fallo, sin reintento (ver `BaseClient._request`).
        """
        url = _DETALLE.format(codigo=codigo)
        data = self._get(url, reintentar_transitorios=reintentar_transitorios)
        payload = _validar_envelope(data)
        return _parse_ca_detalle(payload)
