"""Borrador de canasta para F-plat-1 (4-nov-2026). Solo lectura: no toca la base ni la API.

Lee data/mercado/oc_ca2_*.csv.gz (últimos 12 meses; líneas de OC de Compra Ágil, datos abiertos),
filtra compradores de Ñuble y grupos de alimentos, asigna cada línea a un producto candidato con
`canasta_reglas.py` (EspecificacionProveedor si es útil; si no, la del comprador; kits, servicios,
anexos y textos con 3+ categorías quedan fuera) y escribe data/mercado/canasta_borrador_candidatos.xlsx.

Uso (desde la raíz del repo): python scripts/mercado/canasta_borrador.py

Fuente: Dirección ChileCompra (datos abiertos).
"""

from __future__ import annotations

import collections
import csv
import glob
import gzip
import os
import re
import statistics
import sys
import unicodedata

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from canasta_reglas import K, R
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from app.catalogos.mercado import G_ALIM, es_nuble, num

csv.field_size_limit(10**8)
rows = []
for f in sorted(glob.glob("data/mercado/oc_ca2_*.csv.gz"))[-12:]:
    with gzip.open(f, "rt", encoding="utf-8-sig") as h:
        for r in csv.DictReader(h, delimiter=";"):
            if es_nuble(r["RegionUnidadCompra"]) and r["grupo"] in G_ALIM:
                rows.append(r)


def nz(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


PRI = {"Nutrición": 0, "Preparados": 1, "Pastelería": 2, "Panadería": 3}
R = sorted(R, key=lambda x: PRI.get(x[0], 9))
RC = [(a, b, c, d, re.compile(e)) for a, b, c, d, e in R]
KC = [(a, b, re.compile(c)) for a, b, c in K]
GEN = {"na", "", "se solicita factura con el detalle de los productos o guia de despacho"}


def prod(t):
    for a, b, c, d, e in RC:
        if e.search(t):
            return (a, b, c, d)


def clas(t, anexo):
    for a, b, e in KC:
        if (anexo or a != "Anexo") and e.search(t):
            return (a, b, "", "")
    cats = {a for a, b, c, d, e in RC if e.search(t)}
    if len(cats) >= 3:
        return ("Anexo", "Varios productos en un texto (sin línea propia)", "", "")
    return prod(t)


agg = collections.defaultdict(
    lambda: {
        "m": 0,
        "n": 0,
        "oc": set(),
        "uc": set(),
        "mes": set(),
        "ej": collections.Counter(),
        "u": collections.Counter(),
        "cod": collections.Counter(),
        "pu": collections.defaultdict(list),
        "org": collections.Counter(),
    }
)
for r in rows:
    tp = nz(r["EspecificacionProveedor"])
    tc = nz(r["EspecificacionComprador"])
    key = None
    org = ""
    if tp not in GEN and len(tp) > 3:
        key = clas(tp, False)
        org = "prov"
    if not key:
        key = clas(tc, True)
        org = "comp"
    if key and key[0] in ("Kit", "Servicio", "Anexo"):
        org = "kit"
    if not key:
        key = ("Sin clasificar", "Sin detalle: " + r["NombreroductoGenerico"], "", "")
        org = "-"
    g = agg[key]
    m = num(r["totalLineaNeto"]) or 0
    g["m"] += m
    g["n"] += 1
    g["oc"].add(r["Codigo"])
    g["uc"].add(r["RutUnidadCompra"])
    g["mes"].add(r["archivo"])
    g["ej"][(tp if org == "prov" else tc)[:110]] += m
    g["u"][r["UnidadMedida"]] += 1
    g["cod"][r["codigoProductoONU"]] += 1
    g["org"][org] += 1
    p = num(r["precioNeto"])
    if p:
        g["pu"][r["UnidadMedida"]].append(p)
tot = sum(g["m"] for g in agg.values())
out = []
for k, g in agg.items():
    u = g["u"].most_common(1)[0][0]
    pu = g["pu"].get(u) or [0]
    out.append(
        dict(
            categoria=k[0],
            producto=k[1],
            formato_sugerido=k[2],
            unidad_base=k[3],
            monto_12m=round(g["m"]),
            pct=g["m"] / tot,
            lineas=g["n"],
            oc=len(g["oc"]),
            compradores=len(g["uc"]),
            meses=len(g["mes"]),
            unidad_oc=u,
            precio_mediano_unidad_oc=round(statistics.median(pu)),
            unspsc=g["cod"].most_common(1)[0][0],
            ejemplos=" || ".join(t for t, _ in g["ej"].most_common(3)),
        )
    )
out.sort(key=lambda x: -x["monto_12m"])
NOP = ("Kit", "Servicio", "Anexo", "Sin clasificar")
cand = [x for x in out if x["categoria"] not in NOP]
esp = [
    x
    for x in cand
    if "sin detalle" not in x["producto"].lower() and "sin formato" not in x["producto"].lower()
]
gen = [x for x in cand if x not in esp]
for i, x in enumerate(esp):
    x["propuesta"] = "entra" if i < 80 else "reserva"
for x in gen:
    x["propuesta"] = "genérico: repartir en sus hijos"
cand = sorted(esp + gen, key=lambda x: -x["monto_12m"])
wb = Workbook()
ws = wb.active
ws.title = "candidatos"
cols = [
    "rank",
    "propuesta",
    "decision_boris",
    "categoria",
    "producto",
    "formato_sugerido",
    "unidad_base",
    "monto_12m",
    "pct_alimentos",
    "pct_acum",
    "lineas",
    "oc",
    "compradores",
    "meses",
    "unidad_oc",
    "precio_mediano_unidad_oc",
    "unspsc",
    "ejemplos",
    "nombre_final_boris",
    "notas_boris",
]
ws.append(cols)
acc = 0
for i, x in enumerate(cand, 1):
    acc += x["pct"]
    ws.append(
        [
            i,
            x["propuesta"],
            "",
            x["categoria"],
            x["producto"],
            x["formato_sugerido"],
            x["unidad_base"],
            x["monto_12m"],
            round(x["pct"], 4),
            round(acc, 4),
            x["lineas"],
            x["oc"],
            x["compradores"],
            x["meses"],
            x["unidad_oc"],
            x["precio_mediano_unidad_oc"],
            x["unspsc"],
            x["ejemplos"],
            "",
            "",
        ]
    )
ws2 = wb.create_sheet("fuera_de_canasta")
ws2.append(
    ["tipo", "que_es", "monto_12m", "pct_alimentos", "lineas", "oc", "compradores", "ejemplos"]
)
for x in out:
    if x["categoria"] in NOP:
        ws2.append(
            [
                x["categoria"],
                x["producto"],
                x["monto_12m"],
                round(x["pct"], 4),
                x["lineas"],
                x["oc"],
                x["compradores"],
                x["ejemplos"],
            ]
        )
m80 = sum(x["monto_12m"] for x in esp[:80])
mesp = sum(x["monto_12m"] for x in esp)
mgen = sum(x["monto_12m"] for x in gen)
mk = {k: sum(x["monto_12m"] for x in out if x["categoria"] == k) for k in NOP}
ws3 = wb.create_sheet("resumen", 0)
L = [
    ["Borrador de canasta para F-plat-1 · 4-nov-2026 · para revisión de Boris"],
    [],
    ["Fuente: Dirección ChileCompra (datos abiertos oc-da, líneas de OC de Compra Ágil)."],
    [
        "Alcance: compradores de Ñuble, grupos de alimentos de app/catalogos/mercado.py, oct-2025 a sep-2026 (12 meses). Monto neto de OC emitidas (lo comprado, no lo cotizado)."
    ],
    [],
    ["Total alimentos 12 m (CLP)", round(tot)],
    ["Candidatos específicos", len(esp), round(mesp), round(mesp / tot, 3)],
    ["  de ellos, propuesta 'entra' (top 80)", 80, round(m80), round(m80 / tot, 3)],
    ["Genéricos 'sin detalle' (repartir)", len(gen), round(mgen), round(mgen / tot, 3)],
]
for k in NOP:
    L.append([f"Fuera de canasta: {k}", "", round(mk[k]), round(mk[k] / tot, 3)])
L += [
    [],
    ["Cómo leerlo"],
    [
        "1. El producto se asigna por reglas de texto (EspecificacionProveedor si es útil; si no, la del comprador). Es una aproximación [I]: hay ruido."
    ],
    [
        "2. 'Kit', 'Servicio' y 'Anexo' no son productos: colaciones armadas, cajas familiares, cócteles, y líneas cuyo detalle está en un adjunto. Es más de la mitad del monto: la canasta sola no lo cubre; eso lo resuelve F-plat-2 (anexos)."
    ],
    [
        "3. precio_mediano_unidad_oc es la mediana del precio neto por la unidad más frecuente en la OC (Unidad, kg, Caja…): mezcla formatos, sirve solo de orientación [I]."
    ],
    [
        "4. Columna decision_boris: entra / fuera / fusionar con <rank> / dividir. nombre_final_boris: el nombre que verá la mesa (español chileno)."
    ],
    [
        "5. Reglas de clasificación: scripts/mercado/canasta_reglas.py; generador: scripts/mercado/canasta_borrador.py."
    ],
]
for r in L:
    ws3.append(r)
for w in (ws, ws2):
    for c in w[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="305496")
    w.freeze_panes = "A2"
ws3["A1"].font = Font(bold=True, size=13)
W = {
    "A": 6,
    "B": 14,
    "C": 14,
    "D": 12,
    "E": 38,
    "F": 22,
    "G": 8,
    "H": 13,
    "I": 9,
    "J": 9,
    "R": 80,
    "S": 28,
    "T": 28,
}
for k, v in W.items():
    ws.column_dimensions[k].width = v
for k, v in {"A": 12, "B": 55, "C": 13, "H": 80}.items():
    ws2.column_dimensions[k].width = v
ws3.column_dimensions["A"].width = 60
for row in ws.iter_rows(min_row=2):
    row[7].number_format = "#,##0"
    row[8].number_format = "0.0%"
    row[9].number_format = "0.0%"
    row[15].number_format = "#,##0"
    if row[1].value == "entra":
        row[1].fill = PatternFill("solid", fgColor="E2EFDA")
for row in ws2.iter_rows(min_row=2):
    row[2].number_format = "#,##0"
    row[3].number_format = "0.0%"
ws.auto_filter.ref = ws.dimensions
p = "data/mercado/canasta_borrador_candidatos.xlsx"
wb.save(p)
print(p, len(esp), len(gen), m80 / tot, mesp / tot, {k: round(v / tot, 3) for k, v in mk.items()})
