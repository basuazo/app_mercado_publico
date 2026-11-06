"""Contexto del selector «producto + formato del catálogo» para formularios (F-plat-capas).

Lo usan el producto de un proveedor y el inventario: el parcial `_catalogo_selector.html` se
genera con un `destino` (prefijo de ids del DOM) en vez del `linea_id` de las líneas de CA.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import ProductoCanonico, ProductoFormato


def entero_o_none(texto: object) -> int | None:
    t = str(texto or "").strip()
    return int(t) if t.isdigit() and len(t) < 18 else None


def contexto_selector(
    session: Session, producto_id: int | None, formato_id: int | None, destino: str
) -> dict[str, Any]:
    """Producto y formato ya elegidos (si hay) para pintar el selector; el resto se busca por
    HTMX en `/catalogo/buscar-producto?destino=…`."""
    producto = session.get(ProductoCanonico, producto_id) if producto_id else None
    formatos = (
        list(
            session.execute(
                select(ProductoFormato)
                .where(ProductoFormato.producto_id == producto.id)
                .order_by(ProductoFormato.factor_base, ProductoFormato.nombre)
            ).scalars()
        )
        if producto
        else []
    )
    return {
        "destino": destino,
        "productos": [{"id": producto.id, "nombre": producto.nombre}] if producto else [],
        "producto_sel": producto.id if producto else None,
        "formatos": formatos,
        "formato_sel": formato_id,
    }
