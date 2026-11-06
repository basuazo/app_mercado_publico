"""Nombres y rutas de las tres capas del catálogo (F-plat-capas), en un solo lugar.

Las plantillas los leen como el global `nav` (menú, títulos y migas). Los nombres los decidió
el equipo (Revisión CBS, 5-nov): la oferta propia, los productos de proveedores y la demanda del
mercado. Las URLs no cambian al renombrar (no se rompen enlaces ni pruebas).
"""

from __future__ import annotations

GRUPO_CATALOGO = "Catálogo"

DEMANDA_MP = "Demanda MP"
URL_DEMANDA_MP = "/catalogo"

CATALOGO_PROVEEDORES = "Catálogo Proveedores"
URL_CATALOGO_PROVEEDORES = "/proveedores"

INVENTARIO = "Inventario"
URL_INVENTARIO = "/inventario"
