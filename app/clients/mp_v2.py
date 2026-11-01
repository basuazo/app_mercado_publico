"""Cliente para la API Compra Ágil v2 de Mercado Público."""

from __future__ import annotations

import re
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
    CotizacionCA,
    CotizacionCAProducto,
    PaginacionV2,
    RespuestaListadoV2,
    parse_binario,
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
        unidad_compra=_texto_o_none(institucion.get("unidad_compra")),
    )


# Claves de cotización ausentes ya avisadas: un log.debug por clave y proceso,
# no uno por oferta (F-merc-0b).
_claves_ausentes: set[str] = set()

_RE_MILES = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def _campo(d: dict[str, object], clave: str, contexto: str) -> object:
    """`d[clave]`, o None si falta; la primera ausencia de cada clave se loguea."""
    if clave in d:
        return d[clave]
    etiqueta = f"{contexto}.{clave}"
    if etiqueta not in _claves_ausentes:
        _claves_ausentes.add(etiqueta)
        _log.debug("Detalle CA: falta %s (queda None)", etiqueta)
    return None


def _monto(v: object) -> float | None:
    """Monto tolerante: número, '1234.5', '1234,5', '1.234,5' o '1.234.567'.

    `parse_float` lee '1.234' como 1,234; acá un texto con puntos de miles se
    lee como entero chileno. Ilegible → None (regla 6).
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    txt = str(v).strip().replace("$", "").replace(" ", "")
    if not txt:
        return None
    if "," in txt and "." in txt:
        txt = txt.replace(".", "").replace(",", ".")
    elif "," in txt:
        txt = txt.replace(",", ".")
    elif _RE_MILES.match(txt):
        txt = txt.replace(".", "")
    return parse_float(txt)


def _parse_producto_cotizado(p: dict[str, object]) -> CotizacionCAProducto:
    ctx = "productos_cotizados[]"
    return CotizacionCAProducto(
        # int|str → str, como en productos_solicitados.
        codigo_producto=_texto_o_none(_campo(p, "codigo_producto", ctx)),
        nombre_producto=_texto_o_none(_campo(p, "nombre_producto", ctx)),
        descripcion=_texto_o_none(_campo(p, "descripcion", ctx)),
        cantidad=_monto(_campo(p, "cantidad", ctx)),
        precio_unitario=_monto(_campo(p, "precio_unitario", ctx)),
        monto_total_producto=_monto(_campo(p, "monto_total_producto", ctx)),
    )


def _parse_cotizacion(c: dict[str, object]) -> CotizacionCA:
    ctx = "proveedores_cotizando[]"
    rut = _texto_o_none(_campo(c, "rut_proveedor", ctx))
    if rut is None:
        _log.warning("Detalle CA: oferta sin rut_proveedor; se conserva con RUT vacío")

    # Forma real [V, sonda 31-oct]: `estado` (dict o texto). `estado_cotizacion`, que
    # documenta la guía, queda de respaldo.
    estado = c.get("estado")
    if estado is None:
        estado = c.get("estado_cotizacion")
    if isinstance(estado, dict):
        estado_id = parse_int(estado.get("id"))
        estado_glosa = _texto_o_none(estado.get("glosa")) or _texto_o_none(estado.get("codigo"))
    else:
        estado_id, estado_glosa = None, _texto_o_none(estado)

    # Forma real [V]: `proveedor_seleccionado` al nivel de la oferta. `seleccion.*`
    # (guía v3.0, no existe en la respuesta real) queda de respaldo. Ausente → None,
    # no False: el cliente no infiere ganadores.
    seleccionado = parse_binario(_campo(c, "proveedor_seleccionado", ctx))
    seleccion = _dict(c.get("seleccion"))
    if seleccionado is None:
        seleccionado = parse_binario(seleccion.get("proveedor_seleccionado"))
    motivo = _texto_o_none(seleccion.get("motivo_seleccion"))
    criterio = _texto_o_none(seleccion.get("criterio_seleccion"))

    productos_raw = _campo(c, "productos_cotizados", ctx)
    productos = (
        [_parse_producto_cotizado(p) for p in productos_raw if isinstance(p, dict)]
        if isinstance(productos_raw, list)
        else []
    )

    descripcion = _texto_o_none(c.get("descripcion_cotizacion")) or _texto_o_none(
        c.get("descripcion")
    )
    return CotizacionCA(
        rut_proveedor=rut or "",
        razon_social=_texto_o_none(_campo(c, "razon_social", ctx)),
        es_emt=parse_binario(_campo(c, "es_emt", ctx)),
        codigo_empresa=_texto_o_none(_campo(c, "codigo_empresa", ctx)),
        codigo_sucursal=_texto_o_none(_campo(c, "codigo_sucursal_empresa", ctx)),
        id_cotizacion=_texto_o_none(_campo(c, "id_cotizacion", ctx)),
        id_oc=_texto_o_none(_campo(c, "id_oc", ctx)),
        estado_id=estado_id,
        estado_glosa=estado_glosa,
        estado_por_comprador=_texto_o_none(_campo(c, "estado_por_comprador", ctx)),
        activo=parse_binario(_campo(c, "activo", ctx)),
        justificacion_inadmisibilidad=_texto_o_none(c.get("justificacion_inadmisibilidad")),
        seleccionado=seleccionado,
        motivo_seleccion=motivo,
        criterio_seleccion=criterio,
        fecha_creacion=parse_fecha_v2(_campo(c, "fecha_creacion", ctx)),
        fecha_vigencia=parse_fecha_v2(_campo(c, "fecha_vigencia", ctx)),
        valor_neto=_monto(_campo(c, "valor_neto", ctx)),
        total_impuesto=_monto(_campo(c, "total_impuesto", ctx)),
        monto_despacho=_monto(_campo(c, "monto_despacho", ctx)),
        monto_total=_monto(_campo(c, "monto_total", ctx)),
        nombre_impuesto=_texto_o_none(_campo(c, "nombre_impuesto", ctx)),
        porcentaje_impuesto=_monto(_campo(c, "porcentaje_impuesto", ctx)),
        descripcion=descripcion,
        productos=productos,
    )


def _parse_cotizaciones(payload: dict[str, object]) -> list[CotizacionCA]:
    """`proveedores_cotizando[]`: ausente, null o no-lista → [] (regla 6).

    Según la guía refleja solo el llamado vigente y llega completo desde
    "Cerrada en segundo llamado" [I].
    """
    raw = payload.get("proveedores_cotizando")
    if not isinstance(raw, list):
        return []
    return [_parse_cotizacion(c) for c in raw if isinstance(c, dict)]


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

    # Rutas de la guía (F-merc-0b), no verificadas en una respuesta real [I].
    entrega = _dict(payload.get("entrega"))
    presupuesto = _dict(payload.get("presupuesto"))
    motivos = _dict(payload.get("motivos"))
    documentos = payload.get("documentos")

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
        cotizaciones=_parse_cotizaciones(payload),
        unidad_compra=base.unidad_compra,
        direccion_entrega=_texto_o_none(entrega.get("direccion_entrega")),
        plazo_entrega_dias=parse_int(entrega.get("plazo_entrega_dias")),
        tipo_presupuesto=_texto_o_none(presupuesto.get("tipo_presupuesto")),
        moneda=_texto_o_none(presupuesto.get("moneda")),
        fecha_cierre_segundo_llamado=parse_fecha_v2(
            _dict(payload.get("convocatoria")).get("fecha_cierre_segundo_llamado")
        ),
        motivo_desierta=_texto_o_none(motivos.get("motivo_desierta")),
        motivo_cancelacion=_texto_o_none(motivos.get("motivo_cancelacion")),
        n_documentos=len(documentos) if isinstance(documentos, list) else None,
    )


class MercadoPublicoV2Client:
    """Acceso a la API Compra Ágil v2 de Mercado Público."""

    def __init__(self, settings: Settings, engine: Engine, *, reserva: int = 0) -> None:
        # `reserva`: requests del día que este cliente deja para `ca` (F-datos-1).
        rl = rate_limiter_compartido(settings.rate_limit_rps)
        quota = QuotaTracker(engine, settings.api_daily_budget, reserva=reserva)
        self._ticket = settings.mp_ticket
        self._client = BaseClient(
            ticket=settings.mp_ticket,
            rate_limiter=rl,
            quota=quota,
            default_headers={"ticket": settings.mp_ticket},
            pausa_tasa_s=settings.pausa_tasa_min * 60,
            pausa_429_desconocido_s=settings.pausa_429_desconocido_min * 60,
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
