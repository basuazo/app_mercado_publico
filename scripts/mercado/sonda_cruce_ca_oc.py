"""Sonda de solo lectura: ¿con qué código de OC cruzan las CA con `oc_lineas_ca`? (F-plat-6-cruce §1).

`demanda.py` cruzaba por `compras_agiles.id_orden_compra`, que solo llenaba el job viejo `ca`
(apagado desde el 31-oct). La ingesta actual guarda la OC en `ca_cotizaciones.id_oc`. Esta sonda
muestra ejemplos de las tres columnas y cuenta cuántos `id_oc` distintos calzan con
`oc_lineas_ca.codigo_oc` tal cual y tras normalizar (`codigo_oc_normalizado`: trim + mayúsculas).

Transacción READ ONLY: no escribe nada. No llama a la API.

Uso (PowerShell, desde la raíz del repo):
    $env:PYTHONIOENCODING = "utf-8"
    python scripts\\mercado\\sonda_cruce_ca_oc.py          # DATABASE_URL (dev)
    python scripts\\mercado\\sonda_cruce_ca_oc.py --prod   # DATABASE_URL_PROD

Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))

from sqlalchemy import Engine, create_engine, func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.catalogos.mercado import codigo_oc_normalizado  # noqa: E402
from app.models.tables import CaCotizacion, CompraAgil, OcLineaCa  # noqa: E402

N_EJEMPLOS = 20


def sondear(engine: Engine) -> dict[str, object]:
    r: dict[str, object] = {}
    with Session(engine) as s:
        if engine.dialect.name == "postgresql":
            s.execute(text("SET TRANSACTION READ ONLY"))
        try:
            r["ej_id_oc"] = s.execute(
                select(CaCotizacion.ca_codigo, CaCotizacion.id_oc, CaCotizacion.seleccionado)
                .where(CaCotizacion.id_oc.is_not(None))
                .order_by(CaCotizacion.seleccionado.desc(), CaCotizacion.id.desc())
                .limit(N_EJEMPLOS)
            ).all()
            r["ej_id_orden_compra"] = s.execute(
                select(CompraAgil.codigo, CompraAgil.id_orden_compra)
                .where(CompraAgil.id_orden_compra.is_not(None))
                .order_by(CompraAgil.codigo.desc())
                .limit(N_EJEMPLOS)
            ).all()
            r["ej_codigo_oc"] = s.execute(
                select(OcLineaCa.codigo_oc).distinct().order_by(OcLineaCa.codigo_oc.desc())
                .limit(N_EJEMPLOS)
            ).scalars().all()  # fmt: skip
            r["filas_id_oc"] = s.execute(
                select(func.count()).where(CaCotizacion.id_oc.is_not(None))
            ).scalar_one()
            r["filas_id_oc_seleccionado"] = s.execute(
                select(func.count()).where(
                    CaCotizacion.id_oc.is_not(None), CaCotizacion.seleccionado.is_(True)
                )
            ).scalar_one()
            r["cas_con_id_orden_compra"] = s.execute(
                select(func.count()).where(CompraAgil.id_orden_compra.is_not(None))
            ).scalar_one()
            ids = {v for v in s.execute(
                select(CaCotizacion.id_oc).where(CaCotizacion.id_oc.is_not(None)).distinct()
            ).scalars()}  # fmt: skip
            codigos = set(s.execute(select(OcLineaCa.codigo_oc).distinct()).scalars())
            norm_codigos = {codigo_oc_normalizado(c) for c in codigos}
            r["id_oc_distintos"] = len(ids)
            r["codigo_oc_distintos"] = len(codigos)
            r["calzan_tal_cual"] = sum(1 for v in ids if v in codigos)
            r["calzan_normalizado"] = sum(
                1 for v in ids if codigo_oc_normalizado(v) in norm_codigos
            )
            ioc = {v for v in s.execute(
                select(CompraAgil.id_orden_compra)
                .where(CompraAgil.id_orden_compra.is_not(None)).distinct()
            ).scalars()}  # fmt: skip
            r["id_orden_compra_distintos"] = len(ioc)
            r["id_orden_compra_calzan_normalizado"] = sum(
                1 for v in ioc if codigo_oc_normalizado(v) in norm_codigos
            )
            # CA con varias ofertas seleccionadas con OC distintas
            r["cas_varias_oc_seleccionadas"] = s.execute(
                select(func.count()).select_from(
                    select(CaCotizacion.ca_codigo)
                    .where(CaCotizacion.seleccionado.is_(True), CaCotizacion.id_oc.is_not(None))
                    .group_by(CaCotizacion.ca_codigo)
                    .having(func.count(func.distinct(CaCotizacion.id_oc)) > 1)
                    .subquery()
                )
            ).scalar_one()
            # id_oc en ofertas NO seleccionadas (¿la API lo repite en todas las ofertas?)
            r["filas_id_oc_no_seleccionado"] = s.execute(
                select(func.count()).where(
                    CaCotizacion.id_oc.is_not(None), CaCotizacion.seleccionado.is_not(True)
                )
            ).scalar_one()
        finally:
            s.rollback()
    return r


def imprimir(r: dict[str, object]) -> None:
    print("ca_cotizaciones.id_oc no nulos (ejemplos, seleccionadas primero):")
    for ca, oc, sel in r["ej_id_oc"]:  # type: ignore[attr-defined]
        print(f"  {ca}: {oc!r} seleccionado={sel}")
    print("compras_agiles.id_orden_compra no nulos (ejemplos):")
    for ca, oc in r["ej_id_orden_compra"]:  # type: ignore[attr-defined]
        print(f"  {ca}: {oc!r}")
    print("oc_lineas_ca.codigo_oc (ejemplos):")
    for c in r["ej_codigo_oc"]:  # type: ignore[attr-defined]
        print(f"  {c!r}")
    for k in (
        "filas_id_oc", "filas_id_oc_seleccionado", "filas_id_oc_no_seleccionado",
        "cas_varias_oc_seleccionadas", "cas_con_id_orden_compra", "id_oc_distintos",
        "codigo_oc_distintos", "calzan_tal_cual", "calzan_normalizado",
        "id_orden_compra_distintos", "id_orden_compra_calzan_normalizado",
    ):  # fmt: skip
        print(f"{k}: {r[k]}")
    print("Fuente: Dirección ChileCompra.")


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from app.core.db import normalizar_url_driver

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--prod", action="store_true", help="usar DATABASE_URL_PROD")
    a = ap.parse_args(argv)
    load_dotenv()
    var = "DATABASE_URL_PROD" if a.prod else "DATABASE_URL"
    url = os.environ.get(var)
    if not url:
        print(f"Falta {var}.")
        return 2
    engine = create_engine(normalizar_url_driver(url), pool_pre_ping=True)
    try:
        imprimir(sondear(engine))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
