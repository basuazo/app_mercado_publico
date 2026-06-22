"""Script de validación manual de cobertura UNSPSC en datos ya ingestados.

Calcula, sobre licitacion_items y ca_productos:
- nº total y % con codigo_producto no vacío.
- de los no vacíos, % que parecen UNSPSC válido (8 dígitos numéricos).
- top 15 prefijos de 2 dígitos (familia) y 4 dígitos (clase) más frecuentes,
  combinando ambas tablas.

Es de SOLO LECTURA: ninguna consulta escribe ni modifica datos. NO se agenda
ni se llama desde la app — es un script manual, igual que scripts/smoke_test.py.

Uso (PowerShell, con .env configurado con DATABASE_URL real):
    python scripts/validar_unspsc.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api.main import make_engine  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.models.tables import CaProducto, LicitacionItem  # noqa: E402

_UNSPSC_VALIDO = r"^[0-9]{8}$"

_TABLAS: list[tuple[type[Any], Any]] = [
    (LicitacionItem, LicitacionItem.codigo_producto),
    (CaProducto, CaProducto.codigo_producto),
]


def _pct(parte: int, total: int) -> str:
    return f"{(parte / total * 100):.1f}%" if total else "0.0%"


def _contar_cobertura(session: Session, modelo: type[Any], columna: Any) -> tuple[int, int, int]:
    """Retorna (total, no_vacios, unspsc_validos) para el modelo dado."""
    total = session.execute(select(func.count()).select_from(modelo)).scalar_one()
    no_vacios = session.execute(
        select(func.count()).select_from(modelo).where(columna != "")
    ).scalar_one()
    validos = session.execute(
        select(func.count())
        .select_from(modelo)
        .where(columna != "", columna.op("~")(_UNSPSC_VALIDO))
    ).scalar_one()
    return total, no_vacios, validos


def _prefijos_combinados(session: Session, largo: int) -> Counter[str]:
    """Cuenta prefijos de `largo` dígitos entre los codigo_producto UNSPSC válidos,
    combinando licitacion_items y ca_productos."""
    contador: Counter[str] = Counter()
    for modelo, columna in _TABLAS:
        prefijo = func.substr(columna, 1, largo)
        filas = session.execute(
            select(prefijo, func.count())
            .select_from(modelo)
            .where(columna.op("~")(_UNSPSC_VALIDO))
            .group_by(prefijo)
        ).all()
        for pref, cnt in filas:
            contador[pref] += cnt
    return contador


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    engine = make_engine(settings)

    with Session(engine) as session:
        lic_total, lic_no_vacios, lic_validos = _contar_cobertura(
            session, LicitacionItem, LicitacionItem.codigo_producto
        )
        ca_total, ca_no_vacios, ca_validos = _contar_cobertura(
            session, CaProducto, CaProducto.codigo_producto
        )
        top_familia = _prefijos_combinados(session, 2).most_common(15)
        top_clase = _prefijos_combinados(session, 4).most_common(15)

    print("=== Validación de cobertura UNSPSC (solo lectura) ===\n")

    print("Licitación items:")
    print(f"  Total: {lic_total}")
    print(
        f"  Con codigo_producto no vacío: {lic_no_vacios} "
        f"({_pct(lic_no_vacios, lic_total)})"
    )
    print(
        f"  De esos, con formato UNSPSC válido (8 dígitos): {lic_validos} "
        f"({_pct(lic_validos, lic_no_vacios)})"
    )

    print("\nCompra Ágil productos:")
    print(f"  Total: {ca_total}")
    print(
        f"  Con codigo_producto no vacío: {ca_no_vacios} "
        f"({_pct(ca_no_vacios, ca_total)})"
    )
    print(
        f"  De esos, con formato UNSPSC válido (8 dígitos): {ca_validos} "
        f"({_pct(ca_validos, ca_no_vacios)})"
    )

    print("\nTop 15 prefijos de 2 dígitos (familia UNSPSC) — licitaciones + CA:")
    for pref, cnt in top_familia:
        print(f"  {pref}: {cnt}")

    print("\nTop 15 prefijos de 4 dígitos (clase UNSPSC) — licitaciones + CA:")
    for pref, cnt in top_clase:
        print(f"  {pref}: {cnt}")

    print("\n=== Fin del reporte — no se modificó ningún dato ===")


if __name__ == "__main__":
    main()
