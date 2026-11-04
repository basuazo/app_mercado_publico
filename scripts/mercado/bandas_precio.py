"""Bandas de precio por producto desde las cotizaciones de Compra Ágil (solo lectura offline).

NO llama a la API ni usa MP_TICKET: lee los `cot_api_{AAAA-MM}.csv.gz` que deja
`scripts/backfill_cot_nuble.py` (una fila por CA x proveedor x producto cotizado).

Qué calcula (sin dependencias fuera de la biblioteca estándar):
  1. Verificación de datos [V]: PrecioUnitario x CantidadCotizada == MontoTotalProducto y
     suma por oferta == ValorNeto (confirma que el precio es neto y unitario).
  2. Ruido intra-CA: dentro de una misma línea todos ofertan lo mismo (producto, cantidad y
     comuna), así que price/mediana mide la dispersión irreducible, sin normalizar nada.
  3. Bandas por producto (CodigoProducto|NombreProductoGenerico): p10/p25/p50/p75/p90 del
     precio ganador y de todas las ofertas, n de CA y marca de confianza.
  4. Homogeneidad: dispersión ENTRE CA (IQR/mediana de la mediana por CA) contra la dispersión
     DENTRO de la CA. Si la primera es mucho mayor, "mismo código" no es "mismo producto" y la
     banda necesita el catálogo canónico (F-merc-3) antes de usarse para decidir.

Uso (PowerShell, desde la raíz del repo):
    python scripts\\mercado\\bandas_precio.py                      # todos los cot_api_*.csv.gz
    python scripts\\mercado\\bandas_precio.py --meses 2026-08 --min-ca 5
Salidas: data/mercado/informe/bandas_precio.csv y bandas_precio.md

Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import statistics as st
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
DIR = RAIZ / "data" / "mercado"
SALIDA = DIR / "informe"


def num(v: str | None) -> float | None:
    """Número tolerante (notación científica incluida); ilegible -> None (regla 6)."""
    if v is None:
        return None
    t = v.strip().replace("$", "").replace(" ", "")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        try:
            return float(t.replace(".", "").replace(",", "."))
        except ValueError:
            return None


def leer(meses: list[str] | None) -> list[dict[str, str]]:
    filas: list[dict[str, str]] = []
    for p in sorted(DIR.glob("cot_api_*.csv.gz")):
        mes = p.name.removeprefix("cot_api_").removesuffix(".csv.gz")
        if meses and mes not in meses:
            continue
        csv.field_size_limit(10_000_000)
        with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
            filas.extend(csv.DictReader(f, delimiter=";"))
    return filas


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q
    i = int(k)
    j = min(i + 1, len(xs) - 1)
    return xs[i] + (xs[j] - xs[i]) * (k - i)


def iqr_rel(xs: list[float]) -> float | None:
    if len(xs) < 4:
        return None
    m = st.median(xs)
    return (pct(xs, 0.75) - pct(xs, 0.25)) / m if m else None


def mediana_o_none(xs: Iterable[float]) -> float | None:
    xs = list(xs)
    return st.median(xs) if xs else None


Oferta = tuple[str, float, bool, float]  # (RUT, precio unitario, seleccionada, cantidad)


def una_por_rut(of: list[Oferta]) -> list[Oferta]:
    """Una oferta por RUT en la línea (F-merc-1c): un RUT puede mandar varias a una CA.

    El ganador cuenta con su oferta seleccionada; cada competidor, con la más barata.
    Con una oferta por RUT devuelve lo mismo, en el mismo orden.
    """
    elegida: dict[str, Oferta] = {}
    for o in of:
        actual = elegida.get(o[0])
        if actual is None or (o[2] and not actual[2]) or (o[2] == actual[2] and o[1] < actual[1]):
            elegida[o[0]] = o
    return list(elegida.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--meses", nargs="*", help="p. ej. 2026-08 2026-09 (default: todos)")
    ap.add_argument("--min-ca", type=int, default=5, help="CA mínimas para marcar una banda como usable")
    a = ap.parse_args()
    filas = leer(a.meses)
    if not filas:
        print("Sin filas: ¿corrió scripts/backfill_cot_nuble.py?", file=sys.stderr)
        return 1

    # --- 1. Verificación de consistencia del precio ----------------------------------
    ok_linea = mal_linea = 0
    ofertas: dict[tuple[str, str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    valor_neto: dict[tuple[str, str, str], float] = {}
    for r in filas:
        pu, cant, mt = num(r.get("PrecioUnitario")), num(r.get("CantidadCotizada")), num(r.get("MontoTotalProducto"))
        if pu is not None and cant is not None and mt is not None:
            if abs(pu * cant - mt) <= max(1.0, 0.005 * mt):
                ok_linea += 1
            else:
                mal_linea += 1
        # Una oferta = (CA, RUT, ValorNeto): un RUT puede mandar varias a una CA (F-merc-1c) y
        # el CSV no trae id de oferta; ValorNeto es de la oferta (igual en todas sus filas).
        k = (r["CodigoCotizacion"], r["RUTProveedor"], r.get("ValorNeto", ""))
        if r["RUTProveedor"] and mt is not None:
            ofertas[k]["suma"] += mt
            vn = num(r.get("ValorNeto"))
            if vn is not None:
                valor_neto[k] = vn
    ok_of = sum(1 for k, v in ofertas.items() if k in valor_neto and abs(v["suma"] - valor_neto[k]) <= max(1.0, 0.005 * valor_neto[k]))
    n_of_cmp = sum(1 for k in ofertas if k in valor_neto)

    # --- 2. Líneas: (CA, producto) con sus ofertas -----------------------------------
    lineas: dict[tuple[str, str, str], list[Oferta]] = defaultdict(list)
    prods_por_ca: dict[str, set[str]] = defaultdict(set)
    for r in filas:
        if not r["RUTProveedor"]:
            continue
        pu = num(r.get("PrecioUnitario"))
        if pu is None or pu <= 0:
            continue
        cantidad = num(r.get("CantidadSolicitada")) or 0.0
        clave = (r["CodigoCotizacion"], r["CodigoProducto"], r["ProductoCotizado"])
        lineas[clave].append((r["RUTProveedor"], pu, r["ProveedorSeleccionado"].strip().lower() == "si", cantidad))
        prods_por_ca[r["CodigoCotizacion"]].add(r["CodigoProducto"] + "|" + r["ProductoCotizado"])
    # N.º de oferentes = RUT distintos; precio de cada uno según `una_por_rut`.
    lineas = defaultdict(list, {k: una_por_rut(v) for k, v in lineas.items()})
    n_ca = len(prods_por_ca)
    n_ca_uno = sum(1 for v in prods_por_ca.values() if len(v) == 1)

    ruido: list[float] = []  # precio / mediana de la línea
    gana_barato = con_2 = con_2_uno = 0
    brechas: list[float] = []
    for (ca_l, _c, _t), of in lineas.items():
        if len(of) < 2:
            continue
        con_2 += 1
        precios = sorted(p for _, p, _, _ in of)
        med = st.median(precios)
        ruido.extend(p / med for p in precios)
        gan = [p for _, p, s, _ in of if s]
        # Ganador y brecha solo en CA de un producto: ahí la oferta ganadora es la línea.
        # En CA multiproducto se adjudica por el total de la oferta, no por línea.
        if gan and len(prods_por_ca[ca_l]) == 1:
            con_2_uno += 1
            gana_barato += gan[0] <= precios[0] * 1.0001
            otros = [p for _, p, s, _ in of if not s]
            if otros:
                brechas.append((min(otros) - gan[0]) / gan[0])

    # --- 3. Bandas por producto ------------------------------------------------------
    grupos: dict[str, dict[str, list]] = defaultdict(lambda: {"gan": [], "todas": [], "med_ca": [], "ca": set(), "of": []})
    nombres: dict[str, str] = {}
    for (ca, cod, texto), of in lineas.items():
        g = grupos[cod]
        nombres.setdefault(cod, texto)
        g["ca"].add(ca)
        precios = [p for _, p, _, _ in of]
        g["todas"].extend(precios)
        g["med_ca"].append(st.median(precios))
        g["of"].append(len(of))
        g["gan"].extend(p for _, p, s, _ in of if s)
    genericos = {r["CodigoProducto"]: r["NombreProductoGenerico"] for r in filas}

    SALIDA.mkdir(parents=True, exist_ok=True)
    cols = ["CodigoProducto", "Generico", "EjemploTexto", "n_CA", "n_ofertas", "gan_p10", "gan_p25", "gan_p50", "gan_p75",
            "gan_p90", "todas_p25", "todas_p50", "todas_p75", "iqr_entre_CA", "confianza"]
    salida: list[dict[str, object]] = []
    ruido_med = st.median(abs(x - 1) for x in ruido) if ruido else None
    for cod, g in grupos.items():
        gan = g["gan"] or g["todas"]
        ncas = len(g["ca"])
        iq = iqr_rel(g["med_ca"])
        if ncas < a.min_ca:
            conf = "baja: pocas CA"
        elif iq is not None and iq > 0.5:
            conf = "baja: producto heterogéneo (normalizar)"
        else:
            conf = "usable"
        salida.append({
            "CodigoProducto": cod, "Generico": genericos.get(cod, ""), "EjemploTexto": nombres[cod][:80],
            "n_CA": ncas, "n_ofertas": len(g["todas"]),
            "gan_p10": round(pct(gan, .10)), "gan_p25": round(pct(gan, .25)), "gan_p50": round(pct(gan, .5)),
            "gan_p75": round(pct(gan, .75)), "gan_p90": round(pct(gan, .90)),
            "todas_p25": round(pct(g["todas"], .25)), "todas_p50": round(pct(g["todas"], .5)),
            "todas_p75": round(pct(g["todas"], .75)),
            "iqr_entre_CA": "" if iq is None else round(iq, 2), "confianza": conf,
        })
    salida.sort(key=lambda d: -int(d["n_CA"]))  # type: ignore[call-overload]
    with (SALIDA / "bandas_precio.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(salida)

    usables = [s for s in salida if s["confianza"] == "usable"]
    heter = [s for s in salida if str(s["confianza"]).startswith("baja: producto")]
    iq_entre = [float(s["iqr_entre_CA"]) for s in salida if s["iqr_entre_CA"] != "" and int(s["n_CA"]) >= a.min_ca]  # type: ignore[call-overload]
    rep = [
        "# Bandas de precio - verificación de viabilidad\n",
        f"Filas: {len(filas):,} · CA con ofertas: {n_ca:,} · líneas (CA x producto): {len(lineas):,} · productos (código): {len(grupos):,}\n",
        "## Datos [V]",
        f"- PrecioUnitario x CantidadCotizada = MontoTotalProducto: {ok_linea:,} de {ok_linea + mal_linea:,} filas "
        f"({100 * ok_linea / max(1, ok_linea + mal_linea):.1f} %).",
        f"- Suma de líneas = ValorNeto por oferta: {ok_of:,} de {n_of_cmp:,} ({100 * ok_of / max(1, n_of_cmp):.1f} %).",
        f"- CA de un solo producto: {n_ca_uno:,} de {n_ca:,} ({100 * n_ca_uno / max(1, n_ca):.0f} %).",
        f"- Líneas con >= 2 ofertas: {con_2:,} ({100 * con_2 / max(1, len(lineas)):.0f} %). "
        f"En CA de un producto con >= 2 ofertas ({con_2_uno:,}), gana el más barato: {100 * gana_barato / max(1, con_2_uno):.0f} %. "
        f"Brecha mediana ganador - siguiente: {100 * (mediana_o_none(brechas) or 0):.1f} %.\n",
        "## Ruido intra-CA (mismo producto, cantidad y comuna)",
        f"- Desviación mediana de una oferta respecto a la mediana de su línea: {100 * (ruido_med or 0):.1f} %. "
        "Es el piso: ninguna banda puede ser más angosta que esto.\n",
        "## Bandas",
        f"- Productos con >= {a.min_ca} CA: {sum(1 for s in salida if int(s['n_CA']) >= a.min_ca):,}  "  # type: ignore[call-overload]
        f"· usables: {len(usables):,} · heterogéneos (IQR entre CA > 50 %): {len(heter):,}.",
        f"- IQR/mediana entre CA, mediana de los productos con n suficiente: "
        f"{100 * (st.median(iq_entre) if iq_entre else 0):.0f} % (a comparar con el ruido intra-CA).",
        "- Limitación: el CSV no trae `unidad_medida` ni el formato; un mismo código puede mezclar "
        "unidad y paquete. Los marcados heterogéneos son candidatos a catálogo canónico.\n",
        "## Top 15 por n de CA",
        "| Código | Genérico | n CA | Ganador p25 | p50 | p75 | IQR entre CA | Confianza |", "|---|---|---|---|---|---|---|---|",
    ]
    for s in salida[:15]:
        rep.append(f"| {s['CodigoProducto']} | {s['Generico']} | {s['n_CA']} | {s['gan_p25']:,} | {s['gan_p50']:,} | "
                   f"{s['gan_p75']:,} | {s['iqr_entre_CA']} | {s['confianza']} |")
    rep.append("\nFuente: Dirección ChileCompra.")
    (SALIDA / "bandas_precio.md").write_text("\n".join(rep), encoding="utf-8")
    print("\n".join(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
