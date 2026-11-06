"""Siembra las tasas de los impuestos específicos verificadas en la fuente (F-plat-impuestos §0).

Fuente primaria (leída el 6-nov-2026): DL 825, art. 42, texto vigente en Ley Chile
(https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma=6369, versión 5-feb-2026) y la pregunta
frecuente del SII https://www.sii.cl/preguntas_frecuentes/impuestos_mensuales/001_130_1250.htm.
Las tasas rigen desde el 1-oct-2014 (Ley 20.780, art. 2° N° 10 + art. vigesimoprimero transitorio).
La base imponible es la misma del IVA (art. 42 inc. 1°) y el IVA no incluye el ILA (art. 15 inc.
final). Los contribuyentes del art. 43 tienen crédito fiscal por el ILA recargado (art. 44).

`recuperable` = true por defecto (decisión de Boris). Si el contador de Aralia dice que el ILA NO
se recupera, corre con `--no-recuperable`: el impuesto queda dentro del costo neto.

Idempotente: inserta una fila solo si no existe (codigo, vigente_desde). Nunca edita una fila
vieja: una tasa nueva es otra fila con otra `vigente_desde`. `--dry-run` por defecto.

Lo ejecuta el humano, después de `alembic upgrade head`; nunca la migración ni un job.

Uso (PowerShell, desde la raíz del repo):
    $env:PYTHONIOENCODING = "utf-8"
    python scripts\\mercado\\sembrar_impuestos.py              # muestra lo que haría
    python scripts\\mercado\\sembrar_impuestos.py --aplicar
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models.tables import ImpuestoEspecifico  # noqa: E402

LEY = "https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma=6369"
SII = "https://www.sii.cl/preguntas_frecuentes/impuestos_mensuales/001_130_1250.htm"
DESDE = date(2014, 10, 1)

# (codigo, nombre, tasa, notas). Verificado en la ley el 6-nov-2026.
TASAS: list[tuple[str, str, float, str]] = [
    ("ila_analcoholica_baja", "ILA bebidas analcohólicas", 0.10,
     "DL 825 art. 42 a): bebidas analcohólicas, energizantes, jarabes y aguas minerales con "
     "colorante, sabor o edulcorante. Agua mineral sin aditivos: no gravada. " + SII),
    ("ila_analcoholica_alta", "ILA bebidas analcohólicas alto azúcar", 0.18,
     "DL 825 art. 42 a): más de 15 g de azúcar por cada 240 ml (Ley 20.606 art. 5°). " + SII),
    ("ila_destilados", "ILA licores y destilados", 0.315,
     "DL 825 art. 42 b): licores, piscos, whisky, aguardientes y destilados. " + SII),
    ("ila_vinos_cervezas", "ILA vinos, cervezas y chichas", 0.205,
     "DL 825 art. 42 c): vinos, espumosos, chichas, sidras, cervezas y otras alcohólicas. " + SII),
]  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--aplicar", action="store_true", help="escribe (sin esto: solo muestra)")
    p.add_argument("--no-recuperable", action="store_true", help="el ILA no se recupera: va al costo")
    a = p.parse_args(argv)

    from app.core.db import normalizar_url_driver
    from app.core.settings import Settings

    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(normalizar_url_driver(settings.database_url), pool_pre_ping=True)
    with Session(engine) as s:
        existentes = {
            (c, d) for c, d in s.execute(
                select(ImpuestoEspecifico.codigo, ImpuestoEspecifico.vigente_desde)
            )
        }  # fmt: skip
        nuevas = 0
        for codigo, nombre, tasa, notas in TASAS:
            if (codigo, DESDE) in existentes:
                print(f"= {codigo} desde {DESDE}: ya está")
                continue
            print(f"+ {codigo}: {nombre} {tasa * 100:g} % desde {DESDE}")
            nuevas += 1
            s.add(
                ImpuestoEspecifico(
                    codigo=codigo, nombre=nombre, tasa=tasa,
                    recuperable=not a.no_recuperable, vigente_desde=DESDE,
                    fuente_url=LEY, notas=notas,
                )
            )  # fmt: skip
        if a.aplicar:
            s.commit()
            print(f"Listo: {nuevas} fila(s) nueva(s).")
        else:
            s.rollback()
            print(f"Dry-run: {nuevas} fila(s) por crear. Usa --aplicar para escribir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
