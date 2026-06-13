"""Smoke test manual — requiere .env con MP_TICKET y DATABASE_URL reales.

NO ejecutar en CI ni en tests automáticos. Solo para verificación manual.
Uso: python scripts/smoke_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Añadir la raíz del proyecto al path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import create_engine  # noqa: E402

from app.clients.mp_v1 import MercadoPublicoV1Client  # noqa: E402
from app.clients.mp_v2 import MercadoPublicoV2Client  # noqa: E402
from app.core.settings import Settings  # noqa: E402


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(settings.database_url)

    v1 = MercadoPublicoV1Client(settings, engine)
    v2 = MercadoPublicoV2Client(settings, engine)

    print("=== Smoke Test F1 ===\n")

    # 1. Licitaciones activas
    activas = v1.licitaciones_activas()
    print(f"Licitaciones activas: {len(activas)}")
    if activas:
        print(f"  Primera: {activas[0].codigo} — {activas[0].nombre[:60]}")

        # 2. Detalle de la primera licitación
        detalle = v1.licitacion_detalle(activas[0].codigo)
        print(f"\nDetalle licitación {detalle.codigo}:")
        print(f"  Nombre: {detalle.nombre[:60]}")
        print(f"  Estado: {detalle.estado}")
        print(f"  Ítems: {len(detalle.items)}")

    # 3. Primera página de Compras Ágiles publicadas
    print("\nCompras Ágiles publicadas (1 página):")
    resp = v2.listar_compra_agil(estados=["publicada"], tamano_pagina=1)
    print(f"  Total resultados: {resp.paginacion.total_resultados}")
    print(f"  Total páginas: {resp.paginacion.total_paginas}")
    if resp.items:
        ca = resp.items[0]
        print(f"  Primera: {ca.codigo} — {ca.nombre[:60]}")

    # 4. Cuota restante
    from app.clients.base import QuotaTracker

    qt = QuotaTracker(engine, settings.api_daily_budget)
    print(f"\nCuota restante hoy: {qt.remaining()} / {settings.api_daily_budget}")
    print("\n=== Smoke test completado ===")


if __name__ == "__main__":
    main()
