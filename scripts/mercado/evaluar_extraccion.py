"""Set de evaluación de la extracción de anexos (F-plat-2 §1.5). Lo corre Boris, no CI.

Uso:
    python scripts/mercado/evaluar_extraccion.py --modelos proveedor/modelo-a,proveedor/modelo-b

Lee `data/mercado/anexos_eval/archivos/` y la verdad `lineas_esperadas.csv` (`;`, BOM UTF-8;
columnas `anexo;n;producto_texto;cantidad;unidad;revisado;nota`). Solo evalúa los anexos con
TODAS sus filas `revisado = si` (una verdad escrita por un LLM no evalúa a otros LLM sin
control humano) e informa cuántos quedan fuera. Los duplicados exactos (mismo sha256) se
evalúan una vez.

Por modelo: % de líneas correctas (producto normalizado + cantidad + unidad), con y sin las
filas "no alimento", % de anexos al 100 %, costo y latencia mediana, separado por tipo
(texto / escaneado / docx-xlsx). Criterio (OK de Boris, 4-nov-2026): el más barato con
≥ 95 % de líneas correctas en total y ≥ 90 % en escaneados.

Usa OPENROUTER_API_KEY y DATABASE_URL del .env (caché y presupuesto en Postgres: re-correr
no vuelve a pagar). El costo de la primera corrida se guarda en
`data/mercado/anexos_eval/resultados/<modelo>.json` para comparar aunque después salga de la
caché. Todo fuera de git (`data/`). No imprime el contenido de los anexos.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from app.catalogos.canonico import normalizar_texto  # noqa: E402
from app.catalogos.mercado import norm  # noqa: E402
from app.clients.openrouter import LLMError, OpenRouterClient  # noqa: E402
from app.core.db import normalizar_url_driver  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.mercado.anexos import (  # noqa: E402
    DOCX,
    XLSX,
    detectar_mime,
    extraer_texto,
    texto_de_linea,
)

RAIZ = Path(__file__).resolve().parents[2]
DIR_EVAL = RAIZ / "data" / "mercado" / "anexos_eval"
UMBRAL_TEXTO = 0.5  # Jaccard de tokens normalizados para emparejar una fila
CRITERIO_TOTAL = 95.0
CRITERIO_ESCANEADOS = 90.0

_UNIDADES = {
    "u": "u", "un": "u", "und": "u", "unid": "u", "unidad": "u", "c/u": "u", "cu": "u",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilogramo": "kg",
    "g": "g", "gr": "g", "grs": "g", "gramo": "g",
    "l": "L", "lt": "L", "lts": "L", "litro": "L",
    "ml": "ml", "cc": "ml",
}  # fmt: skip


def unidad_norm(u: str | None) -> str:
    t = norm(u).strip().rstrip(".")
    if t.endswith("es") and t[:-2] in ("unidad",):
        t = t[:-2]
    elif t.endswith("s") and t[:-1] in _UNIDADES:
        t = t[:-1]
    return _UNIDADES.get(t, t)


def _tokens(s: str) -> set[str]:
    return set(normalizar_texto(s).split())


def similitud(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _num(v: str | None) -> float | None:
    """'200', '1,5', '1.000' (miles) o '1.000,5' → número; vacío o ilegible → None."""
    t = (v or "").strip()
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return float(t) if t else None
    except ValueError:
        return None


@dataclass
class Esperada:
    producto: str
    cantidad: float | None
    unidad: str
    no_alimento: bool


@dataclass
class ResultadoAnexo:
    archivo: str
    tipo: str
    esperadas: int
    correctas: int
    esperadas_alim: int
    correctas_alim: int
    costo: float
    latencia: float
    error: str = ""


@dataclass
class ResumenModelo:
    modelo: str
    anexos: list[ResultadoAnexo] = field(default_factory=list)

    def pct(self, tipo: str | None = None, *, alimentos: bool = False) -> float:
        filas = [a for a in self.anexos if tipo is None or a.tipo == tipo]
        esp = sum(a.esperadas_alim if alimentos else a.esperadas for a in filas)
        ok = sum(a.correctas_alim if alimentos else a.correctas for a in filas)
        return 100.0 * ok / esp if esp else 100.0

    def pct_anexos_completos(self) -> float:
        if not self.anexos:
            return 0.0
        return 100.0 * sum(1 for a in self.anexos if a.correctas == a.esperadas) / len(self.anexos)


def leer_verdad(path: Path) -> tuple[dict[str, list[Esperada]], list[str]]:
    filas: dict[str, list[dict[str, str]]] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            filas.setdefault(r["anexo"], []).append(r)
    verdad: dict[str, list[Esperada]] = {}
    fuera: list[str] = []
    for anexo, rs in filas.items():
        if not all(norm(r.get("revisado")) == "si" for r in rs):
            fuera.append(anexo)
            continue
        verdad[anexo] = [
            Esperada(
                producto=r["producto_texto"],
                cantidad=_num(r.get("cantidad")),
                unidad=unidad_norm(r.get("unidad")),
                no_alimento="no alimento" in norm(r.get("nota")),
            )
            for r in rs
            if (r.get("producto_texto") or "").strip()
        ]
    return verdad, fuera


def emparejar(
    esperadas: list[Esperada], extraidas: list[tuple[str, float | None, str]]
) -> list[bool]:
    """Por cada esperada, ¿hay una extraída (no usada) con texto parecido, misma cantidad y unidad?"""
    return [i is not None for i in emparejar_indices(esperadas, extraidas)]


def emparejar_indices(
    esperadas: list[Esperada], extraidas: list[tuple[str, float | None, str]]
) -> list[int | None]:
    """Como :func:`emparejar`, pero devuelve el índice de la extraída que calzó con cada esperada
    (None si ninguna). La calibración de F-plat-auto-revision lo usa al revés: una extraída es
    correcta si calzó con alguna esperada."""
    usadas: set[int] = set()
    ok: list[int | None] = []
    for e in esperadas:
        mejor, idx = 0.0, -1
        for i, (txt, _c, _u) in enumerate(extraidas):
            if i in usadas:
                continue
            s = similitud(e.producto, txt)
            if s > mejor:
                mejor, idx = s, i
        if idx < 0 or mejor < UMBRAL_TEXTO:
            ok.append(None)
            continue
        _txt, cant, uni = extraidas[idx]
        cant_ok = (
            e.cantidad is None
            and cant is None
            or (
                e.cantidad is not None
                and cant is not None
                and abs(cant - e.cantidad) <= 0.01 * max(1.0, e.cantidad)
            )
        )
        # Sin unidad en la respuesta se lee como "unidad" (lo más común en los anexos).
        uni_ok = not e.unidad or (unidad_norm(uni) or "u") == e.unidad
        if cant_ok and uni_ok:
            usadas.add(idx)
            ok.append(idx)
        else:
            ok.append(None)
    return ok


def _tipo(mime: str | None, escaneado: bool) -> str:
    if mime in (DOCX, XLSX):
        return "docx-xlsx"
    return "escaneado" if escaneado else "texto"


def _slug(modelo: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", modelo)


def evaluar(
    settings: Settings, engine: object, modelo: str, verdad: dict[str, list[Esperada]]
) -> ResumenModelo:
    cfg = settings.model_copy(update={"llm_modelo_extraccion": modelo})
    cliente = OpenRouterClient(cfg, engine)  # type: ignore[arg-type]
    previo_path = DIR_EVAL / "resultados" / f"{_slug(modelo)}.json"
    previo: dict[str, dict[str, float]] = (
        json.loads(previo_path.read_text(encoding="utf-8")) if previo_path.exists() else {}
    )
    resumen = ResumenModelo(modelo=modelo)
    vistos: set[str] = set()
    for archivo in sorted(verdad):
        path = DIR_EVAL / "archivos" / archivo
        if not path.exists():
            print(f"  (falta el archivo {archivo}: se omite)")
            continue
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha in vistos:
            continue  # duplicado exacto: se evalúa una vez
        vistos.add(sha)
        esperadas = verdad[archivo]
        mime = detectar_mime(data) if data else None
        extraidas: list[tuple[str, float | None, str]] = []
        costo = latencia = 0.0
        error = ""
        escaneado = False
        if data and mime is not None:
            res = extraer_texto(data, mime, max_paginas_imagen=cfg.llm_max_paginas_imagen)
            escaneado = res.escaneado
            if res.error:
                error = res.error
            elif res.hay_contenido:
                t0 = time.monotonic()
                try:
                    ext = cliente.extraer_lineas(res.texto or None, res.imagenes)
                except LLMError as exc:
                    error = str(exc)[:120]
                else:
                    latencia = time.monotonic() - t0
                    if ext.desde_cache:
                        costo = previo.get(sha, {}).get("costo", 0.0)
                        latencia = previo.get(sha, {}).get("latencia", latencia)
                    else:
                        costo = ext.costo_usd
                        previo[sha] = {"costo": costo, "latencia": latencia}
                    extraidas = [
                        (texto_de_linea(ln), ln.cantidad, ln.unidad or "") for ln in ext.lineas
                    ]
        ok = emparejar(esperadas, extraidas)
        alim = [o for o, e in zip(ok, esperadas, strict=True) if not e.no_alimento]
        resumen.anexos.append(
            ResultadoAnexo(
                archivo=archivo, tipo=_tipo(mime, escaneado), esperadas=len(esperadas),
                correctas=sum(ok), esperadas_alim=len(alim), correctas_alim=sum(alim),
                costo=costo, latencia=latencia, error=error,
            )
        )  # fmt: skip
        a = resumen.anexos[-1]
        print(f"  {archivo[:50]:50} {a.tipo:10} {a.correctas:3}/{a.esperadas:<3} {error}")
    previo_path.parent.mkdir(parents=True, exist_ok=True)
    previo_path.write_text(json.dumps(previo, indent=1), encoding="utf-8")
    return resumen


def informe(resumenes: list[ResumenModelo]) -> str:
    out = ["", "=== Resultado por modelo ===", ""]
    cab = f"{'modelo':40} {'total':>7} {'s/no-alim':>9} {'texto':>7} {'escan.':>7} {'docx':>7} {'100%':>6} {'USD':>8} {'lat. med':>8}"
    out.append(cab)
    aptos: list[tuple[float, str]] = []
    for r in resumenes:
        costo = sum(a.costo for a in r.anexos)
        lat = [a.latencia for a in r.anexos if a.latencia]
        out.append(
            f"{r.modelo[:40]:40} {r.pct():6.1f}% {r.pct(alimentos=True):8.1f}% {r.pct('texto'):6.1f}% "
            f"{r.pct('escaneado'):6.1f}% {r.pct('docx-xlsx'):6.1f}% {r.pct_anexos_completos():5.1f}% "
            f"{costo:8.4f} {statistics.median(lat) if lat else 0:7.1f}s"
        )
        if r.pct() >= CRITERIO_TOTAL and r.pct("escaneado") >= CRITERIO_ESCANEADOS:
            aptos.append((costo, r.modelo))
    out.append("")
    out.append(
        f"Criterio: el más barato con ≥ {CRITERIO_TOTAL:.0f} % total y ≥ {CRITERIO_ESCANEADOS:.0f} % en escaneados."
    )
    if aptos:
        costo, modelo = min(aptos)
        out.append(
            f"GANADOR: {modelo} ({costo:.4f} USD en el set). Fijar LLM_MODELO_EXTRACCION y anotar en docs/decisiones.md."
        )
    else:
        out.append("NINGÚN modelo cumple el criterio: no fijar modelo; revisar los peores anexos.")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--modelos", required=True, help="Modelos de OpenRouter separados por coma")
    args = ap.parse_args()
    load_dotenv(RAIZ / ".env")
    settings = Settings()  # type: ignore[call-arg]
    if not settings.openrouter_api_key:
        sys.exit("Falta OPENROUTER_API_KEY en el .env")
    engine = create_engine(normalizar_url_driver(settings.database_url), pool_pre_ping=True)
    verdad, fuera = leer_verdad(DIR_EVAL / "lineas_esperadas.csv")
    # Un archivo vacío no tiene filas en la verdad: espera extracción vacía (`abarrotes emergencia.docx`).
    for p in (DIR_EVAL / "archivos").iterdir():
        if p.is_file() and p.stat().st_size == 0 and p.name not in verdad:
            verdad[p.name] = []
    print(
        f"Anexos con la verdad revisada: {len(verdad)}; fuera por filas sin revisar: {len(fuera)}"
    )
    for a in fuera:
        print(f"  fuera: {a}")
    resumenes = []
    for modelo in [m.strip() for m in args.modelos.split(",") if m.strip()]:
        print(f"\n--- {modelo} ---")
        resumenes.append(evaluar(settings, engine, modelo, verdad))
    texto = informe(resumenes)
    print(texto)
    destino = DIR_EVAL / "resultados" / f"informe_{time.strftime('%Y-%m-%d_%H%M')}.txt"
    destino.write_text(texto + "\n", encoding="utf-8")
    print(f"\nInforme guardado en {destino}")


if __name__ == "__main__":
    main()
