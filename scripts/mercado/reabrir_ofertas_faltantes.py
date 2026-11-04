"""Reabre las CA guardadas con ofertas de menos para que se vuelvan a pedir (F-merc-1c §3.3).

Hasta F-merc-1c, `ca_cotizaciones` guardaba UNA oferta por RUT y descartaba las demás del
mismo proveedor (~4 % de las ofertas, sonda del 4-nov-2026 [V]). Este script busca las CA
cuyo número de filas en `ca_cotizaciones` es menor que `compras_agiles.total_ofertas` y les
pone `cotizaciones_obtenidas_en = NULL`: `mercado-ca` (cola de terminales sin cotizaciones)
y `mercado-backfill` (pendientes de los meses aún no `completo`) las vuelven a pedir solos,
1 request de detalle por CA.

Antes verifica el supuesto: que `total_ofertas` cuente OFERTAS y no RUT, con las tres CA
de la sonda (44 ofertas de 38 RUT, 23 de 18, 16 de 13). Si alguna de las tres está en la
base y su `total_ofertas` coincide con los RUT y no con las ofertas, NO reabre nada.

`--dry-run` por defecto (transacción READ ONLY en Postgres): imprime cuántas CA y cuántas
requests costaría. Lo ejecuta el humano, después de `alembic upgrade head`; nunca la
migración ni un job. No llama a la API.

Uso (PowerShell, desde la raíz del repo):
    $env:PYTHONIOENCODING = "utf-8"
    python scripts\\mercado\\reabrir_ofertas_faltantes.py              # dry-run
    python scripts\\mercado\\reabrir_ofertas_faltantes.py --aplicar

Fuente: Dirección ChileCompra (API de Mercado Público).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))

from sqlalchemy import Engine, create_engine, func, select, text, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.mercado.territorio import regiones_activas_engine  # noqa: E402
from app.models.tables import CaCotizacion, CompraAgil  # noqa: E402

# (código, ofertas, RUT distintos) de la sonda del 4-nov-2026 [V].
SONDA = (("2728-9-COT26", 44, 38), ("3881-33-COT26", 23, 18), ("4158-26-COT26", 16, 13))


@dataclass
class Resultado:
    verificacion: list[str] = field(default_factory=list)
    # None = sin evidencia en la base; True/False = total_ofertas cuenta ofertas o no.
    cuenta_ofertas: bool | None = None
    ca: list[tuple[str, int, int]] = field(default_factory=list)  # (código, total, filas)
    aplicado: bool = False

    @property
    def requests(self) -> int:
        return len(self.ca)  # un detalle por CA

    @property
    def ofertas_faltantes(self) -> int:
        return sum(total - filas for _, total, filas in self.ca)


def _verificar_sonda(session: Session, r: Resultado) -> None:
    votos: list[bool] = []
    for codigo, ofertas, ruts in SONDA:
        total = session.execute(
            select(CompraAgil.total_ofertas).where(CompraAgil.codigo == codigo)
        ).scalar_one_or_none()
        filas, ruts_base = session.execute(
            select(func.count(CaCotizacion.id), func.count(func.distinct(CaCotizacion.rut_proveedor)))
            .where(CaCotizacion.ca_codigo == codigo)
        ).one()
        if total is None:
            r.verificacion.append(f"{codigo}: no está en la base")
            continue
        veredicto = (
            "cuenta OFERTAS" if total == ofertas else "cuenta RUT" if total == ruts else "no coincide"
        )
        if total in (ofertas, ruts) and ofertas != ruts:
            votos.append(total == ofertas)
        r.verificacion.append(
            f"{codigo}: total_ofertas={total}, sonda {ofertas} ofertas / {ruts} RUT, "
            f"en la base {filas} filas / {ruts_base} RUT → {veredicto}"
        )
    if votos:
        r.cuenta_ofertas = all(votos)


def ca_con_ofertas_de_menos(session: Session, regiones: list[int]) -> list[tuple[str, int, int]]:
    """(código, total_ofertas, filas) de las CA con cotizaciones obtenidas y filas < total."""
    filas = (
        select(CaCotizacion.ca_codigo, func.count(CaCotizacion.id).label("n"))
        .group_by(CaCotizacion.ca_codigo)
        .subquery()
    )
    stmt = (
        select(CompraAgil.codigo, CompraAgil.total_ofertas, filas.c.n)
        .join(filas, filas.c.ca_codigo == CompraAgil.codigo)
        .where(
            CompraAgil.region.in_(regiones),
            CompraAgil.cotizaciones_obtenidas_en.is_not(None),
            filas.c.n < CompraAgil.total_ofertas,
        )
        .order_by(CompraAgil.codigo)
    )
    return [(c, int(t), int(n)) for c, t, n in session.execute(stmt)]


def reabrir(engine: Engine, regiones: list[int], *, aplicar: bool) -> Resultado:
    """Verifica el supuesto, lista las CA y, solo con `aplicar` y el supuesto en pie, las reabre."""
    r = Resultado()
    with Session(engine) as session:
        if not aplicar and engine.dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            _verificar_sonda(session, r)
            r.ca = ca_con_ofertas_de_menos(session, regiones)
            if aplicar and r.ca and r.cuenta_ofertas is not False:
                session.execute(
                    update(CompraAgil)
                    .where(CompraAgil.codigo.in_([c for c, _, _ in r.ca]))
                    .values(cotizaciones_obtenidas_en=None)
                )
                session.commit()
                r.aplicado = True
        finally:
            if not r.aplicado:
                session.rollback()
    return r


def imprimir(r: Resultado, aplicar: bool) -> None:
    print("Verificación de total_ofertas con las CA de la sonda del 4-nov:")
    for linea in r.verificacion:
        print("  " + linea)
    if r.cuenta_ofertas is False:
        print(
            "total_ofertas cuenta RUT, no ofertas: el criterio filas < total_ofertas no sirve. "
            "No se reabre nada; avisar para buscar otro criterio."
        )
    elif r.cuenta_ofertas is None:
        print("Sin evidencia en la base (las CA de la sonda no están): el supuesto queda [I].")
    print(
        f"CA con ofertas de menos: {len(r.ca)} (faltan {r.ofertas_faltantes} ofertas); "
        f"costo: {r.requests} requests de detalle."
    )
    for codigo, total, filas in r.ca[:20]:
        print(f"  {codigo}: {filas} de {total}")
    if len(r.ca) > 20:
        print(f"  … y {len(r.ca) - 20} más")
    if r.aplicado:
        print("Aplicado: cotizaciones_obtenidas_en = NULL; mercado-ca / mercado-backfill las piden.")
    elif not aplicar:
        print("Dry-run: no se escribió nada (usar --aplicar).")
    print("Fuente: Dirección ChileCompra.")


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from app.core.db import normalizar_url_driver
    from app.core.settings import Settings

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--aplicar", action="store_true", help="escribir (por defecto, dry-run)")
    ap.add_argument(
        "--region", type=int, action="append", help="región (repetible; def.: MERCADO_REGIONES)"
    )
    a = ap.parse_args(argv)

    load_dotenv()
    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(normalizar_url_driver(settings.database_url), pool_pre_ping=True)
    regiones = a.region or regiones_activas_engine(engine, settings)
    try:
        r = reabrir(engine, regiones, aplicar=a.aplicar)
    finally:
        engine.dispose()
    imprimir(r, a.aplicar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
