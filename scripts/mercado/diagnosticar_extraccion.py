"""Diagnóstico de la evaluación de extracción de anexos: ¿falla el modelo o el juez? (5-nov,
docs/decisiones.md «Extracción de anexos con LLM: en pausa»). Lo corre Boris, no CI.

Uso:
    python scripts/mercado/diagnosticar_extraccion.py --modelos google/gemma-4-26b-a4b-it,anthropic/claude-haiku-5.5

**No llama a OpenRouter ni gasta presupuesto:** solo lee lo que `evaluar_extraccion.py` dejó en
`llm_cache` (Postgres del .env). Un anexo sin caché sale `sin_cache` (la respuesta fue inválida
—p. ej. JSON cortado por `LLM_MAX_TOKENS`— o el texto extraído cambió desde la evaluación).

Clasifica cada línea esperada con el mismo emparejamiento del evaluador y explica la falla:
  ok · sin_extraccion (el modelo no devolvió filas) · texto (ninguna fila parecida, Jaccard < 0,5)
  · cantidad (texto calza, cantidad no; `multiplicador` si difiere por un factor entero)
  · unidad (texto y cantidad calzan, unidad no).
Además cuenta las filas sobrantes y calcula dos jueces relajados para medir cuánto pesa el juez:
  J1 = Jaccard ≥ 0,3 + cantidad (sin unidad) · J2 = solo cantidad entre las filas no usadas.

Salida: resumen en pantalla y el detalle en
`data/mercado/anexos_eval/resultados/diagnostico_<fecha>.csv` (fuera de git: trae el texto de
los anexos para revisarlo; en pantalla no se imprime contenido).
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

import evaluar_extraccion as ev  # noqa: E402
from app.clients.openrouter import OpenRouterClient, clave_cache  # noqa: E402
from app.core.db import normalizar_url_driver  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.mercado.anexos import detectar_mime, extraer_texto, texto_de_linea  # noqa: E402

Extraida = tuple[str, float | None, str]


def _cant_ok(e: float | None, x: float | None) -> bool:
    if e is None or x is None:
        return e is None and x is None
    return abs(x - e) <= 0.01 * max(1.0, e)


def _factor_entero(e: float | None, x: float | None) -> bool:
    if not e or not x:
        return False
    r = max(e, x) / min(e, x)
    return r >= 2 and abs(r - round(r)) < 0.01


def clasificar(esperadas: list[ev.Esperada], extraidas: list[Extraida]) -> list[dict[str, object]]:
    idx = ev.emparejar_indices(esperadas, extraidas)
    usadas = {i for i in idx if i is not None}
    filas: list[dict[str, object]] = []
    for e, i in zip(esperadas, idx, strict=True):
        if i is not None:
            txt, c, u = extraidas[i]
            filas.append(dict(clase="ok", sim=1.0, x_texto=txt, x_cant=c, x_uni=u, e=e))
            continue
        if not extraidas:
            filas.append(dict(clase="sin_extraccion", sim=0.0, x_texto="", x_cant=None, x_uni="", e=e))
            continue
        libres = [k for k in range(len(extraidas)) if k not in usadas] or list(range(len(extraidas)))
        k = max(libres, key=lambda j: ev.similitud(e.producto, extraidas[j][0]))
        txt, c, u = extraidas[k]
        sim = ev.similitud(e.producto, txt)
        if sim < ev.UMBRAL_TEXTO:
            clase = "texto"
        elif not _cant_ok(e.cantidad, c):
            clase = "multiplicador" if _factor_entero(e.cantidad, c) else "cantidad"
        else:
            clase = "unidad"
        filas.append(dict(clase=clase, sim=round(sim, 2), x_texto=txt, x_cant=c, x_uni=u, e=e))
    return filas


def juez_relajado(esperadas: list[ev.Esperada], extraidas: list[Extraida], umbral: float) -> int:
    usadas: set[int] = set()
    ok = 0
    for e in esperadas:
        cands = [
            (ev.similitud(e.producto, t), i)
            for i, (t, c, _u) in enumerate(extraidas)
            if i not in usadas and _cant_ok(e.cantidad, c)
        ]
        cands = [x for x in cands if x[0] >= umbral]
        if cands:
            usadas.add(max(cands)[1])
            ok += 1
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modelos", required=True)
    args = ap.parse_args()
    load_dotenv(ev.RAIZ / ".env")
    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(normalizar_url_driver(settings.database_url), pool_pre_ping=True)
    verdad, _fuera = ev.leer_verdad(ev.DIR_EVAL / "lineas_esperadas.csv")
    modelos = [m.strip() for m in args.modelos.split(",") if m.strip()]
    salida = ev.DIR_EVAL / "resultados" / f"diagnostico_{time.strftime('%Y-%m-%d_%H%M')}.csv"
    detalle: list[dict[str, object]] = []
    resumen_anexos: list[dict[str, object]] = []

    textos: dict[str, tuple[object, str, str]] = {}  # archivo → (res, tipo, sha)
    vistos: set[str] = set()
    for archivo in sorted(verdad):
        p = ev.DIR_EVAL / "archivos" / archivo
        if not p.exists():
            continue
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha in vistos or not data:
            continue
        vistos.add(sha)
        mime = detectar_mime(data)
        if mime is None:
            continue
        res = extraer_texto(data, mime, max_paginas_imagen=settings.llm_max_paginas_imagen)
        textos[archivo] = (res, ev._tipo(mime, res.escaneado), sha)

    for modelo in modelos:
        cfg = settings.model_copy(update={"llm_modelo_extraccion": modelo})
        cli = OpenRouterClient(cfg, engine)  # type: ignore[arg-type]
        clases: collections.Counter[str] = collections.Counter()
        por_tipo: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
        tot = j1 = j2 = sobr = sin_cache = 0
        print(f"\n--- {modelo} ---")
        print(f"  {'anexo':42} {'tipo':10} {'pág':>4} {'omit':>4} {'esp':>4} {'extr':>4} {'ok':>4}  cache")
        for archivo, (res, tipo, _sha) in textos.items():
            esperadas = verdad[archivo]
            clave = clave_cache(modelo, "lineas", res.texto or None, [b for b, _ in res.imagenes])  # type: ignore[attr-defined]
            lineas = cli._leer_cache(clave)
            if lineas is None:
                sin_cache += 1
            extr: list[Extraida] = [
                (texto_de_linea(ln), ln.cantidad, ln.unidad or "") for ln in (lineas or [])
            ]
            filas = clasificar(esperadas, extr)
            n_ok = sum(1 for f in filas if f["clase"] == "ok")
            sobr += max(0, len(extr) - n_ok)
            tot += len(esperadas)
            j1 += juez_relajado(esperadas, extr, 0.3)
            j2 += juez_relajado(esperadas, extr, 0.0)
            for n, f in enumerate(filas, 1):
                e = f["e"]
                clase = str(f["clase"]) if lineas is not None else "sin_cache"
                clases[clase] += 1
                por_tipo[tipo][clase] += 1
                detalle.append({
                    "modelo": modelo, "anexo": archivo, "tipo": tipo, "n": n, "clase": clase,
                    "sim": f["sim"], "esp_producto": e.producto, "esp_cantidad": e.cantidad,  # type: ignore[attr-defined]
                    "esp_unidad": e.unidad, "no_alimento": e.no_alimento,  # type: ignore[attr-defined]
                    "x_producto": f["x_texto"], "x_cantidad": f["x_cant"], "x_unidad": f["x_uni"],
                })  # fmt: skip
            resumen_anexos.append({"modelo": modelo, "anexo": archivo, "ok": n_ok, "esp": len(esperadas), "extr": len(extr)})
            print(
                f"  {archivo[:42]:42} {tipo:10} {res.paginas or 0:>4} {res.paginas_omitidas:>4} "  # type: ignore[attr-defined]
                f"{len(esperadas):>4} {len(extr):>4} {n_ok:>4}  {'sí' if lineas is not None else 'NO'}"
            )
        pct = lambda n: f"{100 * n / tot:5.1f}%" if tot else "  -  "  # noqa: E731
        print(f"\n  Líneas esperadas: {tot} · anexos sin caché: {sin_cache} · filas sobrantes: {sobr}")
        for c in ("ok", "texto", "cantidad", "multiplicador", "unidad", "sin_extraccion", "sin_cache"):
            print(f"    {c:15} {clases[c]:4}  {pct(clases[c])}")
        print(f"  Juez actual {pct(clases['ok'])} · J1 (Jaccard≥0,3 + cantidad, sin unidad) {pct(j1)} · J2 (solo cantidad) {pct(j2)}")
        for tipo, cc in sorted(por_tipo.items()):
            n = sum(cc.values())
            print(f"    {tipo:10} " + " ".join(f"{k}={v}" for k, v in cc.most_common()) + f"  (n={n})")

    salida.parent.mkdir(parents=True, exist_ok=True)
    with salida.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(detalle[0]) if detalle else ["modelo"], delimiter=";")
        w.writeheader()
        w.writerows(detalle)
    print(f"\nDetalle por línea en {salida}")


if __name__ == "__main__":
    main()
