"""Exporta las cotizaciones de Compra Ágil guardadas en la base a `cot_api_{AAAA-MM}.csv.gz` (F-merc-1).

Solo lectura (`SET TRANSACTION READ ONLY` + rollback): nada se escribe en la base ni se
llama a la API. Mismas columnas y mismas filas que `scripts/backfill_cot_nuble.py`
(se reutiliza `filas_de_detalle`), así `scripts/mercado/mercado_observatorio.py` las lee
igual que las del puente F-merc-0b y, para un mismo mes, las prefiere a `COT_`.

Va en `scripts/mercado/` y no en `data/` (como pedía el prompt): `data/` está en
.gitignore. Un archivo que ya existe NO se pisa sin `--sobrescribir`: puede venir del
puente F-merc-0b y tener más CA que la base mientras el backfill no termine ese mes.

Uso (PowerShell, desde la raíz del repo):
    $env:PYTHONIOENCODING = "utf-8"
    python scripts\\mercado\\exportar_cot_api.py                       # todos los meses en la base
    python scripts\\mercado\\exportar_cot_api.py --meses 2026-08 --sobrescribir

Fuente: Dirección ChileCompra (API de Mercado Público).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))

from sqlalchemy import Engine, create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.clients.types import (  # noqa: E402
    CompraAgilDetalle,
    CompraAgilItem,
    CotizacionCA,
    CotizacionCAProducto,
)
from app.core.tiempo import a_naive_como_la_api  # noqa: E402
from app.models.tables import (  # noqa: E402
    CaCotizacion,
    CaCotizacionProducto,
    CaProducto,
    CompraAgil,
)

DIR_SALIDA = RAIZ / "data" / "mercado"


def _cargar_backfill() -> ModuleType:
    """`scripts/backfill_cot_nuble.py` no es un paquete: se carga por ruta."""
    ruta = RAIZ / "scripts" / "backfill_cot_nuble.py"
    spec = importlib.util.spec_from_file_location("backfill_cot_nuble", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    # @dataclass busca el módulo en sys.modules al procesar las anotaciones.
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def mes_de(ca: CompraAgil) -> str | None:
    """Mes de publicación en hora de Chile (la base guarda UTC naive)."""
    if ca.fecha_publicacion is None:
        return None
    return a_naive_como_la_api(ca.fecha_publicacion).strftime("%Y-%m")


def detalle_desde_la_base(
    ca: CompraAgil,
    productos: list[CaProducto],
    cotizaciones: list[tuple[CaCotizacion, list[CaCotizacionProducto]]],
) -> CompraAgilDetalle:
    """Rearma el `CompraAgilDetalle` que `filas_de_detalle` sabe escribir."""
    return CompraAgilDetalle(
        codigo=ca.codigo,
        nombre=ca.nombre,
        estado=ca.estado,
        fecha_publicacion=ca.fecha_publicacion,
        fecha_cierre=ca.fecha_cierre,
        fecha_ultimo_cambio=ca.fecha_ultimo_cambio,
        monto_clp=ca.monto_disponible_clp,
        region=ca.region,
        organismo_nombre=ca.organismo_nombre,
        organismo_rut=ca.organismo_rut,
        total_ofertas=ca.total_ofertas,
        unidad_compra=ca.unidad_compra,
        descripcion=ca.descripcion,
        productos=[
            CompraAgilItem(
                codigo_producto=p.codigo_producto,
                nombre=p.nombre,
                cantidad=p.cantidad,
                unidad=p.unidad,
                descripcion=p.descripcion,
            )
            for p in productos
        ],
        direccion_entrega=ca.direccion_entrega,
        plazo_entrega_dias=ca.plazo_entrega_dias,
        cotizaciones=[
            CotizacionCA(
                rut_proveedor=c.rut_proveedor,
                razon_social=c.razon_social,
                es_emt=c.es_emt,
                seleccionado=c.seleccionado,
                valor_neto=c.valor_neto,
                total_impuesto=c.total_impuesto,
                monto_despacho=c.monto_despacho,
                monto_total=c.monto_total,
                productos=[
                    CotizacionCAProducto(
                        codigo_producto=p.codigo_producto,
                        nombre_producto=p.nombre_producto,
                        descripcion=p.descripcion,
                        cantidad=p.cantidad,
                        precio_unitario=p.precio_unitario,
                        monto_total_producto=p.monto_total_producto,
                    )
                    for p in prods
                ],
            )
            for c, prods in cotizaciones
        ],
    )


def detalles_por_mes(
    session: Session, region: int, meses: set[str] | None
) -> Iterator[tuple[str, CompraAgilDetalle]]:
    """(mes, detalle) de cada CA de la región con cotizaciones ya obtenidas, por código."""
    cas = session.execute(
        select(CompraAgil)
        .where(CompraAgil.region == region, CompraAgil.cotizaciones_obtenidas_en.is_not(None))
        .order_by(CompraAgil.codigo)
    ).scalars()
    for ca in cas:
        mes = mes_de(ca)
        if mes is None or (meses is not None and mes not in meses):
            continue
        productos = list(
            session.execute(
                select(CaProducto).where(CaProducto.ca_codigo == ca.codigo).order_by(CaProducto.id)
            ).scalars()
        )
        cots = list(
            session.execute(
                select(CaCotizacion)
                .where(CaCotizacion.ca_codigo == ca.codigo)
                .order_by(CaCotizacion.id)
            ).scalars()
        )
        prods_por_cot: dict[int, list[CaCotizacionProducto]] = defaultdict(list)
        if cots:
            for p in session.execute(
                select(CaCotizacionProducto)
                .where(CaCotizacionProducto.cotizacion_id.in_([c.id for c in cots]))
                .order_by(CaCotizacionProducto.id)
            ).scalars():
                prods_por_cot[p.cotizacion_id].append(p)
        yield mes, detalle_desde_la_base(ca, productos, [(c, prods_por_cot[c.id]) for c in cots])


def exportar(
    engine: Engine,
    *,
    region: int,
    meses: set[str] | None,
    dir_salida: Path,
    sobrescribir: bool,
) -> dict[str, int]:
    """Escribe un `cot_api_{mes}.csv.gz` por mes. Devuelve filas escritas por mes."""
    bf = _cargar_backfill()
    filas: dict[str, list[dict[str, str]]] = defaultdict(list)
    with Session(engine) as session:
        if engine.dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            for mes, det in detalles_por_mes(session, region, meses):
                filas[mes].extend(bf.filas_de_detalle(det, mes))
        finally:
            session.rollback()

    escritas: dict[str, int] = {}
    for mes in sorted(filas):
        ruta = dir_salida / f"cot_api_{mes}.csv.gz"
        if ruta.exists() and not sobrescribir:
            print(f"{mes}: {ruta.name} ya existe; no se pisa (usar --sobrescribir)")
            continue
        tmp = ruta.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        bf.escribir_filas(tmp, filas[mes])
        tmp.replace(ruta)
        escritas[mes] = len(filas[mes])
        print(f"{mes}: {len(filas[mes])} filas → {ruta.name}")
    return escritas


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from app.core.db import normalizar_url_driver
    from app.core.settings import Settings

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--meses", default="", help="AAAA-MM separados por coma (def.: todos)")
    ap.add_argument("--region", type=int, default=16)
    ap.add_argument("--sobrescribir", action="store_true")
    a = ap.parse_args(argv)

    load_dotenv()
    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(normalizar_url_driver(settings.database_url), pool_pre_ping=True)
    meses = {m.strip() for m in a.meses.split(",") if m.strip()} or None
    try:
        escritas = exportar(
            engine,
            region=a.region,
            meses=meses,
            dir_salida=DIR_SALIDA,
            sobrescribir=a.sobrescribir,
        )
    finally:
        engine.dispose()
    print(f"Meses escritos: {len(escritas)}. Fuente: Dirección ChileCompra.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
