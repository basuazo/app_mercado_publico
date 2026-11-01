"""PDF del observatorio de Compra Ágil (Ñuble) a partir de data/mercado/informe/resumen.json.

Estructura: alimentos (giro principal) primero; rubros de expansión después. Las cifras salen de
resumen.json; la lectura (hallazgos, plan, decisiones) va en textos.json, escrita por una persona.

Uso:  python scripts/mercado/mercado_informe_pdf.py [--dir data/mercado/informe]
Requiere reportlab y matplotlib. Fuente de los datos: Dirección ChileCompra.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import cm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.pdfmetrics import registerFontFamily  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

for nombre, archivo in [("DV", "DejaVuSans.ttf"), ("DVB", "DejaVuSans-Bold.ttf")]:
    for base in ("/usr/share/fonts/truetype/dejavu/", "C:/Windows/Fonts/", str(Path(matplotlib.get_data_path()) / "fonts/ttf") + "/"):
        if Path(base + archivo).exists():
            pdfmetrics.registerFont(TTFont(nombre, base + archivo))
            break
registerFontFamily("DV", normal="DV", bold="DVB", italic="DV", boldItalic="DVB")

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#616e7c")
ACC = colors.HexColor("#2f6f8f")
LINE = colors.HexColor("#d9e2ec")
SOFT = colors.HexColor("#f0f4f8")
C_ACC, C_CLARO, C_WARM = "#2f6f8f", "#9fb3c8", "#c05621"
ST = {
    "t": ParagraphStyle("t", fontName="DVB", fontSize=20, leading=25, textColor=INK, spaceAfter=4),
    "sub": ParagraphStyle("sub", fontName="DV", fontSize=10, leading=14, textColor=MUTED, spaceAfter=14),
    "parte": ParagraphStyle("parte", fontName="DVB", fontSize=16, leading=21, textColor=INK, spaceBefore=4, spaceAfter=2),
    "h1": ParagraphStyle("h1", fontName="DVB", fontSize=13, leading=17, textColor=ACC, spaceBefore=11, spaceAfter=5),
    "h2": ParagraphStyle("h2", fontName="DVB", fontSize=10.3, leading=14, textColor=INK, spaceBefore=7, spaceAfter=3),
    "p": ParagraphStyle("p", fontName="DV", fontSize=9.2, leading=13, textColor=INK, spaceAfter=5),
    "b": ParagraphStyle("b", fontName="DV", fontSize=9.2, leading=13, textColor=INK, leftIndent=12, bulletIndent=2, spaceAfter=2.5),
    "small": ParagraphStyle("s", fontName="DV", fontSize=7.7, leading=10.3, textColor=MUTED, spaceAfter=4),
    "cell": ParagraphStyle("c", fontName="DV", fontSize=7.7, leading=9.8, textColor=INK),
    "cellb": ParagraphStyle("cb", fontName="DVB", fontSize=7.7, leading=9.8, textColor=colors.white),
    "kn": ParagraphStyle("kn", fontName="DVB", fontSize=15, leading=18, textColor=INK),
    "kl": ParagraphStyle("kl", fontName="DV", fontSize=7.8, leading=10, textColor=MUTED),
}


def P(t, s="p"):
    return Paragraph(t, ST[s])


def B(t):
    return Paragraph(t, ST["b"], bulletText="•")


def M(x, d=0):
    return (f"{x / 1e6:,.{d}f}").replace(",", "X").replace(".", ",").replace("X", ".")


def PC(x, d=0):
    return "—" if x is None else (f"{100 * x:.{d}f} %").replace(".", ",")


def N(x):
    return f"{x:,.0f}".replace(",", ".")


def tabla(rows, widths, num=()):
    data = [[Paragraph(str(c), ST["cellb"]) for c in rows[0]]] + [[Paragraph(str(c), ST["cell"]) for c in r] for r in rows[1:]]
    t = Table(data, colWidths=widths, repeatRows=1)
    s = [("BACKGROUND", (0, 0), (-1, 0), ACC), ("VALIGN", (0, 0), (-1, -1), "TOP"),
         ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE), ("TOPPADDING", (0, 0), (-1, -1), 2.6),
         ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    s += [("BACKGROUND", (0, i), (-1, i), SOFT) for i in range(2, len(data), 2)]
    s += [("ALIGN", (c, 0), (c, -1), "RIGHT") for c in num]
    t.setStyle(TableStyle(s))
    return t


def caja(flow):
    t = Table([[flow]], colWidths=[17 * cm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("LINEBEFORE", (0, 0), (0, -1), 3, ACC),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return t


def kpis(items):
    cells = [[[P(n, "kn"), P(lbl, "kl")] for n, lbl in items]]
    t = Table(cells, colWidths=[17 * cm / len(items)] * len(items))
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("LINEBEFORE", (1, 0), (-1, -1), 0.6, colors.white),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def _ejes(ax):
    ax.grid(axis="x", color="#e4e7eb", lw=0.6)
    ax.set_axisbelow(True)


def graficos(R, d: Path, destacar: set[str]) -> dict:
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8.5, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.edgecolor": "#9aa5b1", "xtick.color": "#3e4c59",
                         "ytick.color": "#3e4c59"})
    A = R["alimentos"]
    alto = {}
    ser = A["serie"]
    fig, ax = plt.subplots(figsize=(7.2, 2.7), dpi=200)
    x = range(len(ser))
    env = [s[1] / 1e6 for s in ser]
    prep = [s[2] / 1e6 for s in ser]
    ax.bar(x, env, color=C_ACC, width=0.72, label="Envasados y abarrotes")
    ax.bar(x, prep, bottom=env, color=C_CLARO, width=0.72, label="Preparados")
    ax.set_xticks(list(x), [("ene" if s[0][5:] == "01" else "abr" if s[0][5:] == "04" else "jul" if s[0][5:] == "07"
                             else "oct" if s[0][5:] == "10" else "") + (f"\n{s[0][:4]}" if s[0][5:] in ("01", "10") else "")
                            for s in ser])
    ax.set_ylabel("Millones de CLP")
    ax.legend(frameon=False, fontsize=7.5, loc="upper left")
    ax.grid(axis="y", color="#e4e7eb", lw=0.6)
    ax.set_axisbelow(True)
    plt.tight_layout()
    plt.savefig(d / "g_serie_alim.png")
    plt.close()
    alto["serie"] = 2.7
    fams = sorted(A["familias"], key=lambda f: f["monto"])
    h = 0.27 * len(fams) + 0.7
    fig, ax = plt.subplots(figsize=(7.2, h), dpi=200)
    y = range(len(fams))
    loc = [f["monto"] * f["local"] / 1e6 for f in fams]
    far = [f["monto"] * (1 - f["local"]) / 1e6 for f in fams]
    ax.barh(y, loc, color=C_ACC, height=0.66, label="Gana proveedor de Ñuble")
    ax.barh(y, far, left=loc, color=C_CLARO, height=0.66, label="Gana proveedor de otra región")
    ax.set_yticks(list(y), [f["familia"] for f in fams])
    for i, f in enumerate(fams):
        ax.text(f["monto"] / 1e6 + 1.5, i, M(f["monto"]), va="center", fontsize=7, color="#3e4c59")
    ax.set_xlabel("Millones de CLP netos, últimos 12 meses")
    ax.legend(frameon=False, fontsize=7.5, loc="lower right")
    _ejes(ax)
    plt.tight_layout()
    plt.savefig(d / "g_fam_alim.png")
    plt.close()
    alto["fam"] = h
    gs = sorted(R["grupos"], key=lambda g: g["monto12"])
    h = 0.25 * len(gs) + 0.8
    fig, ax = plt.subplots(figsize=(7.2, h), dpi=200)
    y = range(len(gs))
    loc = [g["monto12"] * g["local"] / 1e6 for g in gs]
    far = [g["foraneo12"] / 1e6 for g in gs]
    ax.barh(y, loc, color=C_ACC, height=0.66, label="Gana proveedor de Ñuble")
    ax.barh(y, far, left=loc, color=C_CLARO, height=0.66, label="Gana proveedor de otra región")
    ax.set_yticks(list(y), [g["grupo"] for g in gs])
    for t in ax.get_yticklabels():
        if t.get_text() in destacar:
            t.set_color(C_WARM)
            t.set_fontweight("bold")
    for i, g in enumerate(gs):
        ax.text(g["monto12"] / 1e6 + 8, i, M(g["monto12"]), va="center", fontsize=7, color="#3e4c59")
    ax.set_xlabel("Millones de CLP netos, últimos 12 meses")
    ax.legend(frameon=False, fontsize=7.5, loc="lower right")
    _ejes(ax)
    plt.tight_layout()
    plt.savefig(d / "g_grupos.png")
    plt.close()
    alto["grupos"] = h
    com = [c for c in A["comunas"] if c[0] and c[0][0].isalpha()][:21][::-1]
    fig, ax = plt.subplots(figsize=(7.2, 3.2), dpi=200)
    ax.barh([c[0] for c in com], [c[1] / 1e6 for c in com], color=C_ACC, height=0.66)
    ax.tick_params(axis="y", labelsize=7)
    ax.set_xlabel("Millones de CLP netos en alimentos, últimos 12 meses")
    _ejes(ax)
    plt.tight_layout()
    plt.savefig(d / "g_comunas_alim.png")
    plt.close()
    alto["comunas"] = 3.2
    return alto


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parents[2] / "data" / "mercado" / "informe"))
    a = ap.parse_args()
    d = Path(a.dir)
    R = json.loads((d / "resumen.json").read_text(encoding="utf-8"))
    T = json.loads((d / "textos.json").read_text(encoding="utf-8"))
    A, C = R["alimentos"], R.get("cot")
    alto = graficos(R, d, set(T.get("grupos_nuevos", [])))
    out = d / "informe-compra-agil-nuble.pdf"
    W = 17 * cm

    def img(nombre, h_in, ancho=W):
        return Image(str(d / nombre), width=ancho, height=ancho * h_in / 7.2)

    def pie(c, doc):
        c.saveState()
        c.setFont("DV", 7.3)
        c.setFillColor(MUTED)
        c.drawString(2 * cm, 1.2 * cm, f"Fuente de los datos: Dirección ChileCompra (datos abiertos oc-da y COT_). {T['pie']}")
        c.drawRightString(19 * cm, 1.2 * cm, str(doc.page))
        c.restoreState()

    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm,
                            bottomMargin=1.9 * cm, title=T["titulo"], author=T["autor"], subject=T["subtitulo"])
    s = [P(T["titulo"], "t"), P(T["subtitulo"], "sub"),
         caja([P("<b>En una página</b>")] + [B(x) for x in T["resumen"]])]
    s += [P("1. Qué se midió", "h1")] + [P(x) for x in T["metodo_intro"]] + [P(T["fuera"], "small")]

    # ------------------------- Parte A: alimentos -------------------------
    s += [PageBreak(), P("Parte A · Alimentos", "parte"), P(T["parte_a_intro"], "sub")]
    s += [P("2. Tamaño y evolución", "h1"),
          kpis([(f"${M(A['total12'])} M", "comprado en 12 meses (envasados y preparados)"),
                (N(A["oc12"]), "órdenes de compra"),
                (PC(A["local12"]), "lo ganan proveedores de Ñuble"),
                (f"{N(A['n_compradores'])}", f"unidades compradoras ({N(A['compradores_recurrentes'])} compran 6 o más meses al año)")]),
          Spacer(1, 6), img("g_serie_alim.png", alto["serie"]),
          P("Monto neto mensual de Compra Ágil en alimentos de compradores de Ñuble, octubre 2024 a septiembre 2026.", "small")]
    s += [P(x) for x in T["alim_tamano"]]

    s += [P("3. Qué se compra", "h1"), img("g_fam_alim.png", alto["fam"])]
    rows = [["Familia", "MM$ 12 m", "% del total", "OC", "Proveedores", "Compradores", "Proveedor local"]]
    for f in A["familias"]:
        rows.append([f["familia"], M(f["monto"]), PC(f["monto"] / A["total12"]), N(f["oc"]), N(f["proveedores"]),
                     N(f["compradores"]), PC(f["local"])])
    s += [tabla(rows, [5.4 * cm, 1.8 * cm, 1.8 * cm, 1.4 * cm, 2 * cm, 2 * cm, 2.6 * cm], (1, 2, 3, 4, 5, 6))]
    s += [P(x) for x in T["alim_que"]]

    s += [P("4. Canasta: los 30 productos de mayor monto", "h1"), P(T["canasta_intro"])]
    rows = [["Producto (nombre genérico)", "MM$", "OC", "Comp.", "Local", "Unidad", "Mediana Ñuble", "p25 país"]]
    for c in A["canasta"][:30]:
        p = c["precio"] or {}
        uni = {"kg": "$/kg", "L": "$/L", "u": "$/u"}.get(p.get("base"), "—")
        nom = c["producto"] + (" <font color='#c05621'>(preparado)</font>" if c["preparado"] else "")
        rows.append([nom, M(c["monto"], 1), N(c["oc"]), N(c["compradores"]), PC(c["local"]), uni,
                     N(p["mediana_nuble"]) if p.get("mediana_nuble") else "—",
                     N(p["p25_nacional"]) if p.get("p25_nacional") else "—"])
    s += [tabla(rows, [5.9 * cm, 1.4 * cm, 1.2 * cm, 1.2 * cm, 1.4 * cm, 1.3 * cm, 2.2 * cm, 2.4 * cm], (1, 2, 3, 4, 6, 7)),
          P(T["canasta_nota"], "small")]

    s += [P("5. Quién compra", "h1"), P(T["compradores_intro"])]
    rows = [["Sector", "MM$ 12 m", "% del total"]]
    for k, v in A["sector"]:
        if v >= 1e6:
            rows.append([k if k != "NA" else "Sin dato", M(v), PC(v / A["total12"])])
    s += [tabla(rows, [9 * cm, 4 * cm, 4 * cm], (1, 2)), Spacer(1, 6), P("Las 20 unidades que más compran", "h2")]
    rows = [["Comprador", "Unidad", "Comuna", "MM$", "OC", "Meses", "Local"]]
    for c in A["compradores"][:20]:
        rows.append([c["comprador"].title()[:42], c["unidad"].title()[:30], c["comuna"], M(c["monto"], 1), N(c["oc"]),
                     f"{c['meses']}/12", PC(c["local"])])
    s += [tabla(rows, [5.1 * cm, 3.9 * cm, 2 * cm, 1.4 * cm, 1.1 * cm, 1.3 * cm, 1.4 * cm], (3, 4, 5, 6)),
          P("\"Meses\": en cuántos de los últimos 12 meses compró alimentos por Compra Ágil. \"Local\": parte de su compra "
            "que ganó un proveedor de Ñuble.", "small")]
    s += [P(x) for x in T["compradores_lectura"]]
    s += [P("Dónde", "h2"), img("g_comunas_alim.png", alto["comunas"], 15.5 * cm), P(T["comunas_alim"])]

    s += [P("6. Tamaño de cada compra", "h1")]
    tot_oc = sum(t["oc"] for t in A["tickets"]) or 1
    rows = [["Monto de la orden (solo alimentos)", "OC", "% de las OC", "MM$ 12 m", "% del monto"]]
    for t in A["tickets"]:
        rows.append([t["banda"], N(t["oc"]), PC(t["oc"] / tot_oc), M(t["monto"]), PC(t["monto"] / A["total12"])])
    s += [tabla(rows, [6 * cm, 2.5 * cm, 2.6 * cm, 2.9 * cm, 3 * cm], (1, 2, 3, 4))] + [P(x) for x in T["tickets_lectura"]]

    s += [P("7. Contra quién se compite", "h1"),
          P(f"{N(A['n_proveedores'])} proveedores vendieron alimentos a compradores de Ñuble en 12 meses; "
            f"{N(A['n_proveedores_locales'])} son de Ñuble. Los 5 mayores suman el {PC(A['top5_cuota'])} del monto "
            f"(índice de concentración HHI {N(A['hhi'])}: mercado no concentrado).")]
    tot = A["total12"] or 1
    rows = [["Proveedor", "Comuna", "Región", "MM$", "Cuota", "OC", "Compradores", "Comunas"]]
    for p in A["proveedores"][:15]:
        reg = "Ñuble" if p["local"] else (p["region"].replace("Región del ", "").replace("Región de ", "") or "—")
        rows.append([p["proveedor"].title()[:36], p["comuna"], reg, M(p["monto"], 1), PC(p["monto"] / tot, 1), N(p["oc"]),
                     N(p["compradores"]), N(p["comunas"])])
    s += [tabla(rows, [4.9 * cm, 2 * cm, 1.7 * cm, 1.4 * cm, 1.4 * cm, 1.2 * cm, 2.1 * cm, 1.6 * cm], (3, 4, 5, 6, 7))]
    s += [P(x) for x in T["competidores_lectura"]]

    if C:
        t = C["alimentos_total"]
        s += [P("8. Cómo se gana una Compra Ágil de alimentos", "h1"),
              kpis([(f"{t['ofertas_mediana']:.0f}", "ofertas por solicitud (mediana)"),
                    (PC(t["una_oferta"]), "de las solicitudes reciben una sola oferta"),
                    (PC(t["gana_barato"]), "las gana el más barato"),
                    (PC(t["ganador_vs_disponible"]), "del presupuesto disponible ofrece el ganador (mediana)")]),
              Spacer(1, 4),
              kpis([(PC(t["gap_mediana"], 1), "más caro queda el segundo (mediana)"),
                    (PC(t["ofertas_local"]), "de las ofertas son de proveedores de Ñuble"),
                    (PC(t["victoria_local"]), "de las ofertas locales ganan"),
                    (PC(t["victoria_foraneo"]), "de las ofertas de otras regiones ganan")]),
              Spacer(1, 6)]
        rows = [["Familia", "CA", "Adjud. MM$", "Ofertas", "1 oferta", "Gana el más barato", "Ganador vs 2º",
                 "Ganador / disponible"]]
        for f in C["familias_alimentos"]:
            if f["ca"] < 5:
                continue
            rows.append([f["familia"], N(f["ca"]), M(f["adjudicado"], 1), f"{f['ofertas_mediana']:.0f}", PC(f["una_oferta"]),
                         PC(f["gana_barato"]), PC(f["gap_mediana"], 1), PC(f["ganador_vs_disponible"])])
        s += [tabla(rows, [4.6 * cm, 1.2 * cm, 1.8 * cm, 1.4 * cm, 1.6 * cm, 2.2 * cm, 2 * cm, 2.2 * cm], (1, 2, 3, 4, 5, 6, 7)),
              P(f"Cotizaciones de {C['n_meses']} meses ({', '.join(C['lista_meses'])}); {N(t['ca'])} solicitudes de alimentos. "
                "\"Ganador / disponible\": monto ofertado por el ganador dividido por el presupuesto que publicó el comprador.",
                "small")]
        s += [P(x) for x in T["cot_alim_lectura"]]

    wf = R["walkforward"]["por_grupo"].get("Alimentos envasados y abarrotes")
    s += [P("9. ¿Sirven los precios históricos para cotizar alimentos?", "h1")]
    if wf:
        s += [P(T["wf_alim"].format(n=N(wf["n"]), err=PC(wf["mediana"]), d20=PC(wf["dentro20"])))]

    # ------------------------- Parte B: expansión -------------------------
    s += [PageBreak(), P("Parte B · Rubros de expansión", "parte"), P(T["parte_b_intro"], "sub")]
    s += [P("10. Panorama de los 22 grupos", "h1"), img("g_grupos.png", alto["grupos"]),
          P("En naranjo, los grupos agregados en esta versión. Monto neto de Compra Ágil de compradores de Ñuble.", "small")]
    cotg = {g["grupo"]: g for g in (C["grupos"] if C else [])}
    rows = [["Grupo", "MM$ 12 m", "Local", "MM$ de afuera", "Ofertas", "Ofertas locales", "Gana local", "Gana de afuera"]]
    for g in R["grupos"]:
        cg = cotg.get(g["grupo"], {})
        rows.append([g["grupo"], M(g["monto12"]), PC(g["local"]), M(g["foraneo12"]),
                     f"{cg['ofertas_mediana']:.0f}" if cg else "—", PC(cg.get("ofertas_local")),
                     PC(cg.get("victoria_local")), PC(cg.get("victoria_foraneo"))])
    rows.append(["<b>Total</b>", f"<b>{M(R['total12'])}</b>", f"<b>{PC(R['local12'])}</b>",
                 f"<b>{M(R['total12'] * (1 - R['local12']))}</b>", "", "", "", ""])
    s += [tabla(rows, [4.6 * cm, 1.7 * cm, 1.4 * cm, 2 * cm, 1.4 * cm, 1.9 * cm, 1.9 * cm, 2.1 * cm], (1, 2, 3, 4, 5, 6, 7)),
          P("\"Gana local / de afuera\": porcentaje de las ofertas de cada origen que resultan ganadoras (cotizaciones).", "small")]
    s += [P(x) for x in T["expansion_lectura"]]
    s += [P("Líneas donde más monto se va fuera de la región", "h2")]
    rows = [["Familia", "Grupo", "Producto principal", "MM$ 12 m", "Local", "MM$ de afuera"]]
    for x in R["lineas_top_foraneo"][:12]:
        rows.append([x["familia"], x["grupo"], x["nombre"][:38], M(x["monto12"]), PC(x["local"]), M(x["foraneo12"])])
    s += [tabla(rows, [1.7 * cm, 3.8 * cm, 4.9 * cm, 1.9 * cm, 1.5 * cm, 2.2 * cm], (3, 4, 5))]
    s += [P("11. Ranking de rubros de expansión", "h1"), P(T["ranking_intro"]),
          tabla([["#", "Grupo", "Por qué", "Cuidado con"]] + [[str(i + 1)] + r for i, r in enumerate(T["ranking"])],
                [0.7 * cm, 3.6 * cm, 7 * cm, 5.7 * cm])]
    s += [P("12. Ortopedia: qué dice la regulación", "h1")] + [B(x) for x in T["ortopedia"]]

    # ------------------------- Parte C: plan -------------------------
    s += [P("13. Plan", "h1"), tabla([["Peldaño", "Qué se hace", "Pasa si"]] + T["plan"], [3.4 * cm, 8.1 * cm, 5.5 * cm])]
    s += [P("14. Decisiones pendientes", "h1"),
          tabla([["Decisión", "Por qué importa", "Propuesta"]] + T["decisiones"], [4.2 * cm, 6.4 * cm, 6.4 * cm])]
    s += [P("15. Notas de método y fuentes", "h1")] + [B(x) for x in T["notas"]] + [Spacer(1, 4), P("Fuente: Dirección ChileCompra.", "small")]

    k, i = [], 0
    while i < len(s):
        f = s[i]
        if isinstance(f, Paragraph) and f.style.name in ("h1", "h2", "parte") and i + 1 < len(s):
            grp, j = [f, s[i + 1]], i + 2
            if j < len(s) and not isinstance(s[j], PageBreak) and not (isinstance(s[j], Paragraph) and s[j].style.name in ("h1", "h2", "parte")):
                grp.append(s[j])
                j += 1
            k.append(KeepTogether(grp))
            i = j
        else:
            k.append(f)
            i += 1
    doc.build(k, onFirstPage=pie, onLaterPages=pie)
    print(out)


if __name__ == "__main__":
    main()
