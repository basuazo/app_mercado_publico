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

Dos jueces (F-plat-lectura): el ESTRICTO (el de F-plat-2: texto por Jaccard, cantidad y unidad
tal cual) y el CORREGIDO, que no castiga lo que no es un error de lectura: «1kg» vs «1 kg»,
«300cc» vs «300 ml», singular/plural, botella/caja/bolsa/paquete vs «unidad», 750 g vs 0,75 kg.
El criterio se aplica al corregido; el estricto se muestra para comparar con las corridas
anteriores (medición manual del 6-nov: 84,1 % estricto, 98,0 % sin contar la unidad).

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


# Clase «envase/conteo» del juez corregido: dentro de ella, la unidad cuenta como igual.
_ENVASE = {
    "u", "botella", "caja", "cajita", "tarro", "bolsa", "bolsita", "paquete", "pack", "frasco",
    "sobre", "manga", "rollo", "bandeja", "set", "pote", "saco", "lata",
}  # fmt: skip
_MASA = {"kg": 1000.0, "g": 1.0}
_VOLUMEN = {"L": 1000.0, "ml": 1.0}
_FAMILIAS = (_MASA, _VOLUMEN)


def unidad_norm(u: str | None) -> str:
    t = norm(u).strip().rstrip(".")
    if t.endswith("es") and t[:-2] in ("unidad", "bolsa", "caja", "botella", "pote", "frasco"):
        t = t[:-2]
    elif t.endswith("s") and (t[:-1] in _UNIDADES or t[:-1] in _ENVASE):
        t = t[:-1]
    return _UNIDADES.get(t, t)


_PEGADA = re.compile(r"(\d)\s*(kgs?|kilos?|k|grs?|g|lts?|l|ml|cc)(?![a-z0-9])", re.IGNORECASE)
_EQUIV = {
    "k": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "gr": "g", "grs": "g",
    "lt": "l", "lts": "l", "cc": "ml",
}  # fmt: skip
_FORMATO = re.compile(r"\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml)\b")


def normalizar_texto_juez(s: str) -> str:
    """Texto comparable del juez corregido: «1kg»/«1k»/«1lt» → «1 kg»/«1 l», «300cc» → «300 ml»,
    sin «c/u» y con singular/plural parejo (se quita -es/-s y una -e final de las palabras de
    4+ letras)."""
    t = (s or "").lower().replace("c/u", " ")
    t = _PEGADA.sub(
        lambda m: f"{m.group(1)} {_EQUIV.get(m.group(2).lower(), m.group(2).lower())}", t
    )
    out: list[str] = []
    for w in normalizar_texto(t).split():
        if len(w) >= 4 and w.isalpha():
            if w.endswith("es"):
                w = w[:-2]
            elif w.endswith("s"):
                w = w[:-1]
            if len(w) >= 4 and w.endswith("e"):
                w = w[:-1]
        out.append(w)
    return " ".join(out)


def similitud_corregida(a: str, b: str) -> float:
    ta, tb = set(normalizar_texto_juez(a).split()), set(normalizar_texto_juez(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _formatos(texto: str) -> list[tuple[float, str]]:
    """Formatos «número + unidad de medida» del texto, con la unidad normalizada (kg, g, L, ml)."""
    out: list[tuple[float, str]] = []
    for m in _FORMATO.finditer(normalizar_texto_juez(texto)):
        numero, unidad = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(\w+)", m.group(0)).groups()  # type: ignore[union-attr]
        out.append((float(numero.replace(",", ".")), "L" if unidad == "l" else unidad))
    return out


def cantidad_igual_corregida(
    e_cant: float | None, e_uni: str, cant: float | None, uni: str
) -> bool:
    """Igual tal cual o, si las dos unidades son de la misma familia (g↔kg, ml↔L), convertida."""
    if e_cant is None or cant is None:
        return e_cant is None and cant is None
    if abs(cant - e_cant) <= 0.01 * max(1.0, e_cant):
        return True
    for fam in _FAMILIAS:
        if e_uni in fam and uni in fam:
            a, b = e_cant * fam[e_uni], cant * fam[uni]
            return abs(a - b) <= 0.01 * max(1.0, a)
    return False


def _cruce_medida_envase(
    e_cant: float, e_uni: str, cant: float, uni: str, textos: tuple[str, ...]
) -> bool:
    """«10 kg» esperado vs «10 unidades» extraído (o al revés) solo vale si el formato del texto
    lo explica: formato 1 (1 kg, 1 L, 1000 g…) con la misma cantidad, o cantidad × formato igual a
    la cantidad de la medida, en la misma familia (tolerancia 1 %). «Arroz 5 kg» 10 kg vs 10
    unidades NO vale (son 50 kg)."""
    medida_es_esperada = e_uni in _MASA or e_uni in _VOLUMEN
    unidad_medida = e_uni if medida_es_esperada else uni
    familia = _MASA if unidad_medida in _MASA else _VOLUMEN
    cant_medida, cant_envase = (e_cant, cant) if medida_es_esperada else (cant, e_cant)
    for texto in textos:
        for numero, u in _formatos(texto):
            if u not in familia:
                continue
            total = cant_envase * numero * familia[u] / familia[unidad_medida]
            if abs(total - cant_medida) <= 0.01 * max(1.0, cant_medida):
                return True
    return False


def cantidad_y_unidad_corregidas(
    e_cant: float | None,
    e_uni: str,
    cant: float | None,
    uni: str,
    texto_e: str,
    texto_x: str,
) -> bool:
    """Cantidad y unidad juntas (la unidad de un cruce medida↔envase decide cómo se lee la
    cantidad). Sin unidad esperada, solo cuenta la cantidad."""
    uni = uni or "u"
    if not e_uni:
        return cantidad_igual_corregida(e_cant, e_uni, cant, uni)
    medida = {*_MASA, *_VOLUMEN}
    cruce = (e_uni in medida and uni in _ENVASE) or (uni in medida and e_uni in _ENVASE)
    if cruce:
        if e_cant is None or cant is None:
            return e_cant is None and cant is None
        return _cruce_medida_envase(e_cant, e_uni, cant, uni, (texto_e, texto_x))
    mismo = (
        uni == e_uni
        or (uni in _ENVASE and e_uni in _ENVASE)
        or any(uni in fam and e_uni in fam for fam in _FAMILIAS)
    )
    return mismo and cantidad_igual_corregida(e_cant, e_uni, cant, uni)


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
    # `correctas*` son del juez corregido; estos, del estricto (el de F-plat-2).
    est_correctas: int = 0
    est_correctas_alim: int = 0


@dataclass
class ResumenModelo:
    modelo: str
    anexos: list[ResultadoAnexo] = field(default_factory=list)

    def pct(
        self, tipo: str | None = None, *, alimentos: bool = False, estricto: bool = False
    ) -> float:
        filas = [a for a in self.anexos if tipo is None or a.tipo == tipo]
        esp = sum(a.esperadas_alim if alimentos else a.esperadas for a in filas)
        if estricto:
            ok = sum(a.est_correctas_alim if alimentos else a.est_correctas for a in filas)
        else:
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
    esperadas: list[Esperada],
    extraidas: list[tuple[str, float | None, str]],
    *,
    corregido: bool = True,
) -> list[bool]:
    """Por cada esperada, ¿hay una extraída (no usada) con texto parecido, misma cantidad y unidad?"""
    return [i is not None for i in emparejar_indices(esperadas, extraidas, corregido=corregido)]


def emparejar_indices(
    esperadas: list[Esperada],
    extraidas: list[tuple[str, float | None, str]],
    *,
    corregido: bool = True,
) -> list[int | None]:
    """Como :func:`emparejar`, pero devuelve el índice de la extraída que calzó con cada esperada
    (None si ninguna). La calibración de F-plat-auto-revision lo usa al revés: una extraída es
    correcta si calzó con alguna esperada.

    `corregido=True` (el default; también para `calibrar_auto_revision.py`, F-plat-lectura) usa
    el juez corregido; `False`, el estricto de F-plat-2."""
    sim = similitud_corregida if corregido else similitud
    usadas: set[int] = set()
    ok: list[int | None] = []
    for e in esperadas:
        mejor, idx = 0.0, -1
        for i, (txt, _c, _u) in enumerate(extraidas):
            if i in usadas:
                continue
            s = sim(e.producto, txt)
            if s > mejor:
                mejor, idx = s, i
        if idx < 0 or mejor < UMBRAL_TEXTO:
            ok.append(None)
            continue
        txt, cant, uni = extraidas[idx]
        if corregido:
            u = unidad_norm(uni) or "u"
            cant_ok = uni_ok = cantidad_y_unidad_corregidas(
                e.cantidad, e.unidad, cant, u, e.producto, txt
            )
        else:
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
        ok_est = emparejar(esperadas, extraidas, corregido=False)
        alim = [o for o, e in zip(ok, esperadas, strict=True) if not e.no_alimento]
        alim_est = [o for o, e in zip(ok_est, esperadas, strict=True) if not e.no_alimento]
        resumen.anexos.append(
            ResultadoAnexo(
                archivo=archivo, tipo=_tipo(mime, escaneado), esperadas=len(esperadas),
                correctas=sum(ok), esperadas_alim=len(alim), correctas_alim=sum(alim),
                costo=costo, latencia=latencia, error=error,
                est_correctas=sum(ok_est), est_correctas_alim=sum(alim_est),
            )
        )  # fmt: skip
        a = resumen.anexos[-1]
        print(
            f"  {archivo[:50]:50} {a.tipo:10} {a.correctas:3}/{a.esperadas:<3} "
            f"(estricto {a.est_correctas:3}) {error}"
        )
    previo_path.parent.mkdir(parents=True, exist_ok=True)
    previo_path.write_text(json.dumps(previo, indent=1), encoding="utf-8")
    return resumen


REFERENCIA_COWORK = ("claude-cowork (6-nov, manual)", 98.0)  # lectura de Claude, sin la unidad


def informe(resumenes: list[ResumenModelo]) -> str:
    out = ["", "=== Resultado por modelo ===", ""]
    out.append("Juez CORREGIDO (el del criterio); «estricto» = el de F-plat-2, solo de referencia.")
    cab = f"{'modelo':40} {'estricto':>8} {'total':>7} {'s/no-alim':>9} {'texto':>7} {'escan.':>7} {'docx':>7} {'100%':>6} {'USD':>8} {'lat. med':>8}"
    out.append(cab)
    aptos: list[tuple[float, str]] = []
    for r in resumenes:
        costo = sum(a.costo for a in r.anexos)
        lat = [a.latencia for a in r.anexos if a.latencia]
        out.append(
            f"{r.modelo[:40]:40} {r.pct(estricto=True):7.1f}% {r.pct():6.1f}% "
            f"{r.pct(alimentos=True):8.1f}% {r.pct('texto'):6.1f}% "
            f"{r.pct('escaneado'):6.1f}% {r.pct('docx-xlsx'):6.1f}% {r.pct_anexos_completos():5.1f}% "
            f"{costo:8.4f} {statistics.median(lat) if lat else 0:7.1f}s"
        )
        if r.pct() >= CRITERIO_TOTAL and r.pct("escaneado") >= CRITERIO_ESCANEADOS:
            aptos.append((costo, r.modelo))
    nombre, pct_ref = REFERENCIA_COWORK
    out.append(
        f"{nombre[:40]:40} {'—':>8} {str(pct_ref).replace('.', ',')} % sin unidad "
        "(no es el juez corregido; referencia fija, no compite)"
    )
    out.append("")
    out.append(
        f"Criterio (juez corregido): el más barato con ≥ {CRITERIO_TOTAL:.0f} % total y ≥ {CRITERIO_ESCANEADOS:.0f} % en escaneados."
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
