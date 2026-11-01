"""Tests F-merc-0b: parser de `proveedores_cotizando` del detalle de Compra Ágil v2.

Fixtures SINTÉTICAS (sin RUT ni datos reales) con las CLAVES de la forma real del detalle
[V, sonda 31-oct, docs/01 §10]: el ganador va en `proveedor_seleccionado` al nivel de la
oferta, el estado en `estado` y existe `id_oc`; `seleccion.*` y `estado_cotizacion` (guía
v3.0) no existen y quedan solo como respaldo.
"""

from __future__ import annotations

import copy
import logging

from app.clients.mp_v2 import _parse_ca_detalle

_PRODUCTO = {
    "codigo_producto": "50131700",
    "nombre_producto": "Leche",
    "descripcion": "Leche entera 1 L",
    "cantidad": 10,
    "precio_unitario": 950,
    "monto_total_producto": 9500,
}

_COTIZACION = {
    "rut_proveedor": "11111111-1",
    "razon_social": "Proveedor Uno SpA",
    "es_emt": True,
    "codigo_empresa": 123,
    "codigo_sucursal_empresa": "456",
    "id_cotizacion": 789,
    "estado": {"id": 3, "glosa": "Enviada"},
    "estado_por_comprador": "Aceptada",
    "activo": 1,
    "id_oc": 4455,
    "justificacion_inadmisibilidad": None,
    "proveedor_seleccionado": True,
    "fecha_creacion": "2026-08-03T10:00:00Z",
    "fecha_vigencia": "2026-08-20T23:59:00",
    "valor_neto": 9500,
    "total_impuesto": 1805,
    "monto_despacho": 0,
    "monto_total": 11305,
    "nombre_impuesto": "IVA",
    "porcentaje_impuesto": 19,
    "descripcion_cotizacion": "Entrega en 48 h",
    "productos_cotizados": [_PRODUCTO],
}

_DETALLE = {
    "codigo": "1000-1-COT26",
    "nombre": "Leche para jardín",
    "estado": {"codigo": "proveedor_seleccionado"},
    "fechas": {"fecha_publicacion": "2026-08-01 09:00", "fecha_cierre": "2026-08-05 15:00"},
    "presupuesto": {"monto_disponible_clp": 20000, "tipo_presupuesto": "Neto", "moneda": "CLP"},
    "institucion": {
        "region": 16,
        "organismo_comprador": "Municipalidad Ficticia",
        "rut": "69.000.000-0",
        "unidad_compra": "Educación",
    },
    "entrega": {"direccion_entrega": "Calle Falsa 123", "plazo_entrega_dias": "5"},
    "convocatoria": {"estado_convocatoria": 2, "fecha_cierre_segundo_llamado": "2026-08-07T15:00:00Z"},
    "motivos": {"motivo_desierta": None, "motivo_cancelacion": None},
    "documentos": [{"id": 1, "nombre": "bases.pdf"}, {"id": 2, "nombre": "anexo.pdf"}],
    "id_orden_compra": "1000-50-SE26",
    "productos_solicitados": [
        {"codigo_producto": 50131700, "nombre": "Leche", "cantidad": 10, "unidad_medida": "Litro"}
    ],
    "proveedores_cotizando": [_COTIZACION],
}


def _detalle(**cambios: object) -> dict[str, object]:
    d = copy.deepcopy(_DETALLE)
    d.update(cambios)
    return d


def _con_cotizacion(**cambios: object) -> dict[str, object]:
    c = copy.deepcopy(_COTIZACION)
    c.update(cambios)
    return _detalle(proveedores_cotizando=[c])


def test_detalle_completo():
    d = _parse_ca_detalle(_detalle())

    assert d.unidad_compra == "Educación"
    assert d.direccion_entrega == "Calle Falsa 123"
    assert d.plazo_entrega_dias == 5
    assert d.tipo_presupuesto == "Neto"
    assert d.moneda == "CLP"
    assert d.fecha_cierre_segundo_llamado is not None
    assert d.n_documentos == 2
    assert len(d.cotizaciones) == 1
    c = d.cotizaciones[0]
    assert c.rut_proveedor == "11111111-1"
    assert c.razon_social == "Proveedor Uno SpA"
    assert c.es_emt is True
    assert c.codigo_empresa == "123"
    assert c.codigo_sucursal == "456"
    assert c.id_cotizacion == "789"
    assert c.id_oc == "4455"
    assert (c.estado_id, c.estado_glosa) == (3, "Enviada")
    assert c.activo is True
    assert c.seleccionado is True
    assert c.monto_total == 11305.0
    assert c.porcentaje_impuesto == 19.0
    assert c.descripcion == "Entrega en 48 h"
    assert c.fecha_creacion is not None
    p = c.productos[0]
    assert p.codigo_producto == "50131700"
    assert p.precio_unitario == 950.0
    assert p.monto_total_producto == 9500.0
    assert p.cantidad == 10.0


def test_lo_de_antes_no_cambia():
    """Los campos nuevos son aditivos: los consumidores actuales ven lo mismo."""
    d = _parse_ca_detalle(_detalle())
    assert d.codigo == "1000-1-COT26"
    assert d.monto_clp == 20000
    assert d.region == 16
    assert d.id_orden_compra == "1000-50-SE26"
    assert d.estado_convocatoria == 2
    assert d.productos[0].codigo_producto == "50131700"


def test_sin_proveedores_cotizando():
    payload = _detalle()
    del payload["proveedores_cotizando"]
    assert _parse_ca_detalle(payload).cotizaciones == []


def test_proveedores_cotizando_vacio_o_raro():
    assert _parse_ca_detalle(_detalle(proveedores_cotizando=[])).cotizaciones == []
    assert _parse_ca_detalle(_detalle(proveedores_cotizando=None)).cotizaciones == []
    assert _parse_ca_detalle(_detalle(proveedores_cotizando="x")).cotizaciones == []
    raro = _detalle(proveedores_cotizando=[None, 3, {"rut_proveedor": "2-7"}])
    assert [c.rut_proveedor for c in _parse_ca_detalle(raro).cotizaciones] == ["2-7"]


def test_producto_sin_precio_unitario():
    prod = {k: v for k, v in _PRODUCTO.items() if k != "precio_unitario"}
    d = _parse_ca_detalle(_con_cotizacion(productos_cotizados=[prod]))
    p = d.cotizaciones[0].productos[0]
    assert p.precio_unitario is None
    assert p.monto_total_producto == 9500.0


def test_proveedor_seleccionado_al_nivel_de_la_oferta():
    ganadora = _parse_ca_detalle(_con_cotizacion(proveedor_seleccionado=True)).cotizaciones[0]
    perdedora = _parse_ca_detalle(_con_cotizacion(proveedor_seleccionado=False)).cotizaciones[0]
    assert ganadora.seleccionado is True
    assert perdedora.seleccionado is False


def test_proveedor_seleccionado_como_texto_o_numero():
    for crudo, esperado in (("NO", False), ("si", True), (1, True), (0, False)):
        d = _parse_ca_detalle(_con_cotizacion(proveedor_seleccionado=crudo))
        assert d.cotizaciones[0].seleccionado is esperado, crudo


def test_ausente_da_none_y_no_false():
    c = {k: v for k, v in _COTIZACION.items() if k != "proveedor_seleccionado"}
    d = _parse_ca_detalle(_detalle(proveedores_cotizando=[c]))
    assert d.cotizaciones[0].seleccionado is None


def test_seleccion_de_la_guia_queda_de_respaldo():
    c = {k: v for k, v in _COTIZACION.items() if k != "proveedor_seleccionado"}
    c["seleccion"] = {"proveedor_seleccionado": True, "motivo_seleccion": "Menor precio",
                      "criterio_seleccion": "Precio"}
    oferta = _parse_ca_detalle(_detalle(proveedores_cotizando=[c])).cotizaciones[0]
    assert oferta.seleccionado is True
    assert oferta.motivo_seleccion == "Menor precio"
    assert oferta.criterio_seleccion == "Precio"


def test_el_nivel_de_la_oferta_manda_sobre_seleccion():
    d = _parse_ca_detalle(_con_cotizacion(
        proveedor_seleccionado=False, seleccion={"proveedor_seleccionado": True}))
    assert d.cotizaciones[0].seleccionado is False


def test_estado_de_la_oferta_dict_texto_o_respaldo():
    assert _parse_ca_detalle(_con_cotizacion(estado={"id": 7, "glosa": "Aceptada"})
                             ).cotizaciones[0].estado_glosa == "Aceptada"
    texto = _parse_ca_detalle(_con_cotizacion(estado="Enviada")).cotizaciones[0]
    assert (texto.estado_id, texto.estado_glosa) == (None, "Enviada")
    c = {k: v for k, v in _COTIZACION.items() if k != "estado"}
    c["estado_cotizacion"] = {"id": 9, "glosa": "Rechazada"}
    respaldo = _parse_ca_detalle(_detalle(proveedores_cotizando=[c])).cotizaciones[0]
    assert (respaldo.estado_id, respaldo.estado_glosa) == (9, "Rechazada")


def test_la_unidad_de_compra_tambien_llega_en_el_listado():
    from app.clients.mp_v2 import _parse_ca_basica

    ca = _parse_ca_basica({"codigo": "CA-1", "estado": "publicada",
                           "institucion": {"unidad_compra": "Educación"}})
    assert ca.unidad_compra == "Educación"
    assert _parse_ca_basica({"codigo": "CA-1", "estado": "publicada"}).unidad_compra is None


def test_codigo_producto_int():
    prod = {**_PRODUCTO, "codigo_producto": 50131700}
    d = _parse_ca_detalle(_con_cotizacion(productos_cotizados=[prod]))
    assert d.cotizaciones[0].productos[0].codigo_producto == "50131700"


def test_montos_como_string_con_coma():
    prod = {**_PRODUCTO, "precio_unitario": "950,5", "monto_total_producto": "9.505,0"}
    d = _parse_ca_detalle(
        _con_cotizacion(monto_total="1.234.567", valor_neto="11305", productos_cotizados=[prod])
    )
    c = d.cotizaciones[0]
    assert c.monto_total == 1234567.0
    assert c.valor_neto == 11305.0
    assert c.productos[0].precio_unitario == 950.5
    assert c.productos[0].monto_total_producto == 9505.0


def test_montos_ilegibles_dan_none():
    d = _parse_ca_detalle(_con_cotizacion(monto_total="no informa", valor_neto=True))
    assert d.cotizaciones[0].monto_total is None
    assert d.cotizaciones[0].valor_neto is None


def test_oferta_minima_no_revienta():
    d = _parse_ca_detalle(_detalle(proveedores_cotizando=[{}]))
    c = d.cotizaciones[0]
    assert c.rut_proveedor == ""
    assert c.seleccionado is None
    assert c.productos == []
    assert c.estado_glosa is None


def test_bloques_nuevos_rotos_no_revientan():
    d = _parse_ca_detalle(
        _detalle(entrega="x", motivos=[], documentos={"a": 1}, institucion=None)
    )
    assert d.direccion_entrega is None
    assert d.motivo_desierta is None
    assert d.n_documentos is None
    assert d.unidad_compra is None


def test_clave_ausente_se_loguea_una_vez(caplog):
    import app.clients.mp_v2 as mp_v2

    mp_v2._claves_ausentes.discard("proveedores_cotizando[].razon_social")
    c = {k: v for k, v in _COTIZACION.items() if k != "razon_social"}
    with caplog.at_level(logging.DEBUG, logger="app.clients.mp_v2"):
        _parse_ca_detalle(_detalle(proveedores_cotizando=[c, c]))
        _parse_ca_detalle(_detalle(proveedores_cotizando=[c]))
    avisos = [r for r in caplog.records if "razon_social" in r.getMessage()]
    assert len(avisos) == 1
