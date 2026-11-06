"""Calibración de la confirmación automática de líneas de anexo (F-plat-auto-revision §2).
Lo corre Boris, no CI.

Uso:
    python scripts/mercado/calibrar_auto_revision.py [--modelo M] [--respaldo R] [--paso 0.025]

Sobre `data/mercado/anexos_eval/` (los anexos con la verdad revisada de `lineas_esperadas.csv`,
mismo filtro que `evaluar_extraccion.py`) calcula el puntaje de cada línea extraída
(`app.mercado.auto_revision.puntaje_linea`) y la curva de precisión y cobertura por umbral, en
total y por tipo de documento. Elige el umbral MÁS BAJO con precisión ≥ 98 % (y al menos 20
líneas auto-confirmadas) y lo imprime para fijarlo en `AUTO_CONFIRMAR_UMBRAL`.

- NO llama al modelo ni gasta presupuesto: lee las lecturas de la caché `llm_cache` (las deja
  `evaluar_extraccion.py`). Lo que falte en caché se lista al final con cómo conseguirlo.
- Modelo principal y de respaldo: `--modelo/--respaldo` o `LLM_MODELO_EXTRACCION` /
  `LLM_MODELO_RESPALDO` del `.env`. Sin respaldo en caché, la señal de acuerdo no opina.
- La segunda lectura se simula como en producción: solo cuenta en un anexo si a alguna línea le
  falta solo el acuerdo para llegar al umbral.
- Señal de la API: la CA de cada anexo sale de `muestra_30_ca_alimentos.xlsx` (columnas `codigo`
  y `archivo`) y se lee de DATABASE_URL; si la CA no está en esa base, la señal no opina.
- «Bien leída» = calza con una fila de la verdad (texto parecido + cantidad + unidad), igual que
  en `evaluar_extraccion.py`. La verdad no trae el producto canónico: que el producto sugerido
  sea el correcto lo cubren la señal de catálogo y el muestreo de control semanal [I].

Todo fuera de git (`data/`). No imprime el contenido de los anexos.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluar_extraccion as ev  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.catalogos.mercado import norm  # noqa: E402
from app.clients.openrouter import clave_cache  # noqa: E402
from app.clients.types import LineaExtraida  # noqa: E402
from app.core.db import normalizar_url_driver  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.mercado import auto_revision as ar  # noqa: E402
from app.mercado.anexos import (  # noqa: E402
    _sugerir,
    detectar_mime,
    extraer_texto,
    texto_de_linea,
)
from app.mercado.catalogo import cargar_catalogo  # noqa: E402
from app.models.tables import CompraAgil, LlmCache  # noqa: E402

DIR_EVAL = RAIZ / "data" / "mercado" / "anexos_eval"
_CAMPOS = ("texto", "cantidad", "unidad", "formato", "observacion", "precio")


def _lineas_de_cache(session: Session, clave: str) -> list[LineaExtraida] | None:
    fila = session.execute(select(LlmCache).where(LlmCache.clave == clave)).scalar_one_or_none()
    if fila is None:
        return None
    filas = (fila.respuesta or {}).get("lineas", [])
    return [
        LineaExtraida(**{k: f.get(k) for k in _CAMPOS})
        for f in filas
        if isinstance(f, dict) and f.get("texto")
    ]


def _codigos_por_archivo() -> dict[str, str]:
    from openpyxl import load_workbook

    path = DIR_EVAL / "muestra_30_ca_alimentos.xlsx"
    if not path.exists():
        return {}
    wb = load_workbook(path, read_only=True)
    filas = list(wb.active.iter_rows(values_only=True))
    if not filas:
        return {}
    cab = [norm(str(c or "")) for c in filas[0]]
    if "codigo" not in cab or "archivo" not in cab:
        return {}
    i_cod, i_arch = cab.index("codigo"), cab.index("archivo")
    return {
        str(r[i_arch]).strip(): str(r[i_cod]).strip()
        for r in filas[1:]
        if r[i_arch] and r[i_cod]
    }


def _tabla(titulo: str, puntos: list[ar.PuntoCurva]) -> list[str]:
    out = [f"--- {titulo} ---", f"{'umbral':>7} {'auto':>6} {'bien':>6} {'precisión':>10} {'cobertura':>10}"]
    for p in puntos:
        prec = f"{p.precision * 100:9.1f}%" if p.precision is not None else f"{'—':>10}"
        out.append(f"{p.umbral:7.3f} {p.auto:6d} {p.correctas:6d} {prec} {p.cobertura * 100:9.1f}%")
    return out


def main() -> None:
    # La consola de Windows (cp1252) no imprime «≥» ni «→».
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--modelo", default="", help="Modelo principal (default: LLM_MODELO_EXTRACCION)")
    ap.add_argument("--respaldo", default="", help="Modelo de respaldo (default: LLM_MODELO_RESPALDO)")
    ap.add_argument("--paso", type=float, default=0.025, help="Paso de la curva de umbrales")
    ap.add_argument("--objetivo", type=float, default=ar.PRECISION_OBJETIVO)
    args = ap.parse_args()
    load_dotenv(RAIZ / ".env")
    settings = Settings()  # type: ignore[call-arg]
    modelo = (args.modelo or settings.llm_modelo_extraccion).strip()
    respaldo = (args.respaldo or settings.llm_modelo_respaldo).strip()
    faltan: list[str] = []
    if not modelo:
        sys.exit(
            "Falta el modelo principal: todavía no hay modelo elegido. Corre "
            "`evaluar_extraccion.py --modelos ...`, fija LLM_MODELO_EXTRACCION (o pasa --modelo) "
            "y vuelve a correr este script."
        )
    if not respaldo:
        faltan.append(
            "Sin modelo de respaldo (LLM_MODELO_RESPALDO o --respaldo): la señal de acuerdo de dos "
            "lecturas no opina en la curva."
        )
    engine = create_engine(normalizar_url_driver(settings.database_url), pool_pre_ping=True)
    verdad, fuera = ev.leer_verdad(DIR_EVAL / "lineas_esperadas.csv")
    codigos = _codigos_por_archivo()
    docs: list[ar.DocumentoCalibracion] = []
    vistos: set[str] = set()
    sin_ca: list[str] = []
    with Session(engine) as session:
        catalogo = cargar_catalogo(session)
        for archivo in sorted(verdad):
            path = DIR_EVAL / "archivos" / archivo
            if not path.exists():
                faltan.append(f"Falta el archivo {archivo}")
                continue
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if not data or sha in vistos:
                continue
            vistos.add(sha)
            mime = detectar_mime(data)
            if mime is None:
                continue
            res = extraer_texto(data, mime, max_paginas_imagen=settings.llm_max_paginas_imagen)
            if res.error or not res.hay_contenido:
                continue
            imgs = [b for b, _ in res.imagenes]
            principal = _lineas_de_cache(
                session, clave_cache(modelo, "lineas", res.texto or None, imgs)
            )
            if principal is None:
                faltan.append(f"Sin lectura de {modelo} en caché: {archivo}")
                continue
            segunda = (
                _lineas_de_cache(session, clave_cache(respaldo, "lineas", res.texto or None, imgs))
                if respaldo
                else None
            )
            if respaldo and segunda is None:
                faltan.append(f"Sin lectura de {respaldo} (respaldo) en caché: {archivo}")
            utiles = [ln for ln in principal if (ln.texto or "").strip()]
            if not utiles:
                continue
            codigo = codigos.get(archivo)
            productos = {p for p, _ in (_sugerir(ln, catalogo) for ln in utiles) if p}
            if codigo and session.get(CompraAgil, codigo) is not None:
                ctx = ar.contexto_ca(session, codigo, productos)
            else:
                sin_ca.append(archivo)
                ctx = ar.ContextoCA()
            tipo = ar.tipo_documento(mime, res.escaneado)
            monto = ar.monto_cuadra(utiles, ctx.presupuesto)
            senales = tuple(
                ar.senales_de_linea(
                    ln, catalogo, ctx, tipo_doc=tipo,
                    paginas_omitidas=bool(res.paginas_omitidas), monto=monto,
                )
                for ln in utiles
            )  # fmt: skip
            extraidas = [(texto_de_linea(ln), ln.cantidad, ln.unidad or "") for ln in utiles]
            usados = {i for i in ev.emparejar_indices(verdad[archivo], extraidas) if i is not None}
            docs.append(
                ar.DocumentoCalibracion(
                    tipo=tipo,
                    senales=senales,
                    acuerdos=tuple(ar.acuerdo_lecturas(utiles, segunda))
                    if segunda is not None
                    else None,
                    correctas=tuple(i in usados for i in range(len(utiles))),
                )
            )
    umbrales = ar.umbrales_candidatos(args.paso)
    out = [
        f"Calibración auto-revisión — modelo {modelo}, respaldo {respaldo or '(ninguno)'}",
        f"Anexos con la verdad revisada: {len(verdad)} (fuera por filas sin revisar: {len(fuera)}); "
        f"evaluados: {len(docs)}; líneas: {sum(len(d.senales) for d in docs)}",
        "",
    ]
    total = ar.curva(docs, umbrales)
    out += _tabla("Total", total)
    for tipo in ar.TIPOS_DOC:
        sub = [d for d in docs if d.tipo == tipo]
        if sub:
            out += ["", *_tabla(f"{tipo} ({len(sub)} anexos)", ar.curva(sub, umbrales))]
    elegido = ar.elegir_umbral(total, objetivo=args.objetivo)
    out.append("")
    if elegido is None:
        out.append(
            f"NINGÚN umbral cumple precisión ≥ {args.objetivo * 100:.0f} % con al menos "
            f"{ar.MIN_AUTO_CALIBRACION} líneas: dejar AUTO_CONFIRMAR_UMBRAL vacío (apagado)."
        )
    else:
        p = next(x for x in total if x.umbral == elegido)
        out.append(
            f"UMBRAL: AUTO_CONFIRMAR_UMBRAL={elegido:.3f} (precisión "
            f"{(p.precision or 0) * 100:.1f} %, cobertura {p.cobertura * 100:.1f} %, "
            f"{p.auto} líneas). Fijarlo en .env y en Render, y anotarlo en docs/decisiones.md."
        )
    if sin_ca:
        out.append(f"\nCA sin datos en la base (la señal de la API no opina): {len(sin_ca)} anexo(s).")
    if faltan:
        out += ["", "Qué falta:", *(f"  - {f}" for f in faltan)]
        out.append(
            "  → Las lecturas faltantes se generan con "
            "`python scripts/mercado/evaluar_extraccion.py --modelos <modelo>` (eso sí llama a "
            "OpenRouter y usa presupuesto); después se vuelve a correr este script."
        )
    texto = "\n".join(out)
    print(texto)
    destino = DIR_EVAL / "resultados" / f"calibracion_{time.strftime('%Y-%m-%d_%H%M')}.txt"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto + "\n", encoding="utf-8")
    print(f"\nCurva guardada en {destino}")


if __name__ == "__main__":
    main()
