"""Enganche de `proveedor_productos` al catálogo canónico (F-plat-capas §1.3).

Hasta F-plat-3 nadie escribía `producto_id` / `formato_id`: `mejor_costo(producto_id=…)`, los
candidatos de «mi oferta» y `CostosEmpresa.tiene_costos` devolvían vacío con datos reales.
`guardar_producto` ya vincula al crear; acá vive la acción para los que YA existen:
«Vincular pendientes» (por empresa, cuando una persona la pulsa; no hay migración de datos).
Sin commit: lo hace quien llama. Idempotente: nunca pisa un vínculo existente.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.mercado.catalogo import cargar_catalogo
from app.mercado.proveedores import _autovincular
from app.models.tables import Proveedor, ProveedorProducto


def contar_sin_vincular(
    session: Session, empresa_id: int, proveedor_id: int | None = None
) -> tuple[int, int]:
    """`(sin vincular, total)` de los productos de los proveedores de la empresa (o de UNO)."""
    cond = [Proveedor.empresa_id == empresa_id]
    if proveedor_id is not None:
        cond.append(Proveedor.id == proveedor_id)
    total, vinculados = session.execute(
        select(func.count(ProveedorProducto.id), func.count(ProveedorProducto.producto_id))
        .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
        .where(*cond)
    ).one()
    return int(total) - int(vinculados), int(total)


def vincular_pendientes(session: Session, empresa_id: int) -> dict[str, int]:
    """Aplica la sugerencia única y de confianza alta a los productos de proveedores de la
    empresa que no tienen producto del catálogo. Los demás quedan «sin vincular».

    Devuelve `{vinculados, sin_vincular}` (este último, lo que sigue pendiente)."""
    pendientes = list(
        session.execute(
            select(ProveedorProducto)
            .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
            .where(Proveedor.empresa_id == empresa_id, ProveedorProducto.producto_id.is_(None))
            .order_by(ProveedorProducto.id)
        ).scalars()
    )
    if not pendientes:
        return {"vinculados": 0, "sin_vincular": 0}
    catalogo = cargar_catalogo(session)
    vinculados = sum(
        1 for pp in pendientes if _autovincular(session, pp, catalogo, pp.codigo_unspsc)
    )
    session.flush()
    return {"vinculados": vinculados, "sin_vincular": len(pendientes) - vinculados}
