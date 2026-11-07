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

# F-dis-1: «Ir a» de la paleta ⌘K. `inicial` = se ve sin escribir (con su atajo `G` + letra);
# las demás aparecen al buscar. `requiere`: "empresa" (datos comerciales, regla 26), "admin" o
# None. Documentos apunta a /cuenta hasta F-dis-docs.
PALETA_IR_A: list[dict[str, object]] = [
    {"texto": "Hoy", "sub": "Lo que te necesita hoy", "href": "/?hoy=1", "tecla": "G H",
     "inicial": True, "requiere": "empresa_o_admin"},
    {"texto": "Mesa", "sub": "Compras Ágiles vigentes del territorio", "href": "/mesa",
     "tecla": "G M", "inicial": True, "requiere": "empresa"},
    {"texto": "En trabajo", "sub": "Lotes abiertos y CA en curso", "href": "/trabajo",
     "tecla": "G T", "inicial": True, "requiere": "empresa"},
    {"texto": "Ventas", "sub": "Ofertas enviadas y su seguimiento", "href": "/ventas",
     "tecla": "G V", "inicial": True, "requiere": "empresa"},
    {"texto": DEMANDA_MP, "sub": "Líneas por revisar, anexos e impuestos", "href": URL_DEMANDA_MP,
     "tecla": "G D", "inicial": True, "requiere": "empresa"},
    {"texto": "Actividad", "sub": "Lo que hizo el equipo", "href": "/actividad",
     "requiere": "empresa"},
    {"texto": "Competencia", "sub": "Ranking del territorio y perfil de oferentes",
     "href": "/competencia", "requiere": "empresa"},
    {"texto": "Estrategia", "sub": "Mapa de mercado comprador × familia", "href": "/estrategia",
     "requiere": "empresa"},
    {"texto": "Abastecimiento", "sub": "Pronóstico de los próximos 3 meses",
     "href": "/abastecimiento", "requiere": "empresa"},
    {"texto": "Proveedores", "sub": "Productos y listas de precios", "href": URL_CATALOGO_PROVEEDORES,
     "requiere": "empresa"},
    {"texto": INVENTARIO, "sub": "Stock propio y mínimos", "href": URL_INVENTARIO,
     "requiere": "empresa"},
    {"texto": "Logística", "sub": "Tarifas y costo de despacho", "href": "/proveedores/logistica",
     "requiere": "empresa"},
    {"texto": "Documentos", "sub": "Documentos de la empresa", "href": "/cuenta",
     "requiere": "empresa"},
    {"texto": "Configuración", "sub": "Apariencia, cuenta y administración", "href": "/cuenta",
     "requiere": None},
]  # fmt: skip
