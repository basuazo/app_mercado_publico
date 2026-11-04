"""Observatorio de Compra Ágil — productos intermediables (piloto Ñuble). Solo lectura.

Peldaño 1 del pivote (docs/16-pivote-compra-agil-nuble.md). Fuentes 100 % datos abiertos de
ChileCompra, sin ticket ni cuota de la API:
  - oc-da/{año}-{mes}.zip        líneas de órdenes de compra (EsCompraAgil = Si) con precio neto,
                                 UNSPSC, cantidad, unidad, comuna del comprador y del proveedor.
  - trnspchc/COT_{año}-{mm}.zip  cotizaciones de Compra Ágil, también las que pierden [V 31-oct-2026]:
                                 monto total por oferta, proveedor, tamaño, seleccionado. No trae precio
                                 por producto en CA de varios productos.

  - cot_api_{año}-{mm}.csv.gz    (F-merc-0b) cotizaciones de Ñuble desde la API, con PrecioUnitario por
                                 producto; las genera scripts/backfill_cot_nuble.py. Para un mismo mes
                                 ganan a cot_nuble_* (COT_).

Nada toca la base ni la API. Salida en data/mercado/ (data/ está en .gitignore).

Uso (PowerShell, desde la raíz del repo; el host de datos abiertos responde desde el PC de Boris):
    $env:PYTHONIOENCODING = "utf-8"
    python scripts\\mercado\\mercado_observatorio.py bajar --meses 24 --meses-cot 12   # ~2-3 h
    python scripts\\mercado\\mercado_observatorio.py informe                            # minutos

v2 (31-oct): alcance por grupos intermediables (incluye material educativo, ferretería,
gasfitería, neumáticos, cocina comercial, bodega, mascotas y ortopedia estándar), cotizaciones
de Ñuble para 12 meses y resumen.json para el PDF. Los archivos v1 (oc_ca_*.csv.gz) ya no se leen.

Fuente: Dirección ChileCompra (datos abiertos, https://datos-abiertos.chilecompra.cl/descargas).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import statistics as st
import sys
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "data" / "mercado"  # salidas fuera de git
TMP = OUT / "_tmp"
INF = OUT / "informe"
_BLOB = "https://transparenciachc.blob.core.windows.net/{ruta}"
csv.field_size_limit(1 << 30)

# ---------------------------------------------------------------------------
# Alcance: prefijo UNSPSC (2 o 4 dígitos) -> grupo. Gana el prefijo más largo.
# Propuesta: se decide en docs/16 §3. Fuera: 51 medicamentos, resto de 42 (insumos de
# tratamiento y equipos clínicos), combustibles, vehículos, materiales pesados y servicios.
# ---------------------------------------------------------------------------
# Los grupos viven en app/catalogos/mercado.py desde F-merc-1 (los usa la ingesta).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.catalogos.mercado import (  # noqa: E402, F401
    EXCLUIR,
    FAMILIAS_ALIMENTOS,
    G_ALIM,
    G_PREP,
    GRUPOS,
    es_nuble,
    familia_alimento,
    grupo_de,
    norm,
    normalizar,
    num,
    rut,
)

# ---------------------------------------------------------------------------
# bajar
# ---------------------------------------------------------------------------
COLS_OC = ["Codigo", "CodigoAbreviadoTipoOC", "EsCompraAgil", "FechaCreacion", "codigoEstado",
           "CodigoOrganismoPublico", "OrganismoPublico", "sector", "RutUnidadCompra", "UnidadCompra",
           "CiudadUnidadCompra", "RegionUnidadCompra", "RutSucursal", "CodigoProveedor", "NombreProveedor",
           "ComunaProveedor", "RegionProveedor", "codigoProductoONU", "NombreroductoGenerico",
           "EspecificacionComprador", "EspecificacionProveedor", "cantidad", "UnidadMedida",
           "precioNeto", "totalLineaNeto", "TipoMonedaOC"]


def meses_cerrados(n: int, hoy: date | None = None) -> list[tuple[int, int]]:
    hoy = hoy or date.today()
    y, m, out = hoy.year, hoy.month, []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append((y, m))
    return sorted(out)


def descargar(url: str, destino: Path) -> bool:
    TMP.mkdir(parents=True, exist_ok=True)
    for intento in range(3):
        try:
            with urllib.request.urlopen(url, timeout=180) as r, destino.open("wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            print(f"   bajado {destino.stat().st_size / 1e6:.0f} MB")
            return True
        except Exception as e:  # noqa: BLE001 - script de diagnóstico
            destino.unlink(missing_ok=True)
            if "404" in str(e):
                print(f"   no existe ({e})")
                return False
            print(f"   fallo descarga ({e}); reintento {intento + 1}/3 en 30 s")
            time.sleep(30)
    return False


def _filas_zip(z: Path):
    with zipfile.ZipFile(z) as zf:
        interno = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(interno) as raw:
            rd = csv.reader(io.TextIOWrapper(raw, encoding="latin-1", newline=""), delimiter=";")
            header = next(rd)
            yield header
            yield from rd


def bajar_oc(y: int, m: int, conservar: bool) -> None:
    destino = OUT / f"oc_ca2_{y}-{m:02d}.csv.gz"
    if destino.exists():
        print(f"oc-da {y}-{m}: ya existe, salto")
        return
    z = TMP / f"oc-da_{y}-{m}.zip"
    print(f"oc-da {y}-{m}")
    if not z.exists() and not descargar(_BLOB.format(ruta=f"oc-da/{y}-{m}.zip"), z):
        print("   SALTADO")
        return
    t0, n, k = time.time(), 0, 0
    it = _filas_zip(z)
    header = next(it)
    pos = {h.strip(): i for i, h in enumerate(header)}
    faltan = [c for c in COLS_OC if c not in pos]
    if faltan:
        print(f"   AVISO columnas faltantes (parseo defensivo, quedan vacías): {faltan}")
    tmp = destino.with_suffix(".parcial")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(COLS_OC + ["grupo", "archivo"])
        for row in it:
            n += 1
            def g(c: str, row: list[str] = row) -> str:
                return row[pos[c]] if c in pos and pos[c] < len(row) else ""
            if g("EsCompraAgil").strip().lower() != "si":
                continue
            gr = grupo_de(g("codigoProductoONU"), g("NombreroductoGenerico"))
            if gr:
                w.writerow([g(c) for c in COLS_OC] + [gr, f"{y}-{m:02d}"])
                k += 1
    tmp.replace(destino)
    print(f"   {n:,} líneas, {k:,} CA en alcance ({time.time() - t0:.0f}s)")
    if not conservar:
        z.unlink(missing_ok=True)


_RUTS_NUBLE: set[str] | None = None


def ruts_compradores_nuble() -> set[str]:
    """RUT de unidades compradoras de Ñuble según oc-da: respaldo cuando COT_ trae la región vacía o distinta."""
    global _RUTS_NUBLE
    if _RUTS_NUBLE is None:
        _RUTS_NUBLE = set()
        for p in sorted(OUT.glob("oc_ca2_*.csv.gz")):
            with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f, delimiter=";"):
                    if es_nuble(r.get("RegionUnidadCompra", "")):
                        _RUTS_NUBLE.add(rut(r.get("RutUnidadCompra", "")))
        _RUTS_NUBLE.discard("")
        print(f"   ({len(_RUTS_NUBLE)} RUT de compradores de Ñuble en oc-da, para el cruce de respaldo)")
    return _RUTS_NUBLE


def bajar_cot(y: int, m: int, conservar: bool) -> None:
    """Guarda todas las filas de Ñuble (todos los rubros) del archivo COT_ del mes.

    Ñuble = columna Region con "uble" o código 16, o RUT de la unidad compradora que en oc-da es de Ñuble
    (en mar, abr, jun, jul y ago de 2026 la columna Region no calzó con "Ñuble": 0 filas en la primera corrida).
    El RUT del comprador se usa solo cuando la región viene vacía; si no, se imprimen los valores para revisarlos.
    """
    destino = OUT / f"cot_nuble_{y}-{m:02d}.csv.gz"
    if destino.exists() and destino.stat().st_size > 2000:
        print(f"COT_ {y}-{m}: ya existe, salto")
        return
    print(f"COT_ {y}-{m}")
    z = None
    for nombre in (f"COT_{y}-{m:02d}.zip", f"COT_{y}-{m}.zip"):
        cand = TMP / nombre
        if cand.exists() or descargar(_BLOB.format(ruta=f"trnspchc/{nombre}"), cand):
            z = cand
            break
    if z is None:
        print("   SALTADO (ninguno de los dos patrones)")
        return
    it = _filas_zip(z)
    header = [h.strip() for h in next(it)]
    ir = header.index("Region") if "Region" in header else None
    iu = header.index("RUTUnidaddeCompra") if "RUTUnidaddeCompra" in header else None
    if ir is None:
        print(f"   AVISO: sin columna Region; columnas: {header}")
    ruts = ruts_compradores_nuble()
    n = k = k_reg = k_rut = 0
    regiones = Counter()
    tmp = destino.with_suffix(".parcial")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header + ["archivo"])
        for row in it:
            n += 1
            reg = row[ir] if ir is not None and ir < len(row) else ""
            regiones[reg] += 1
            por_reg = es_nuble(reg) or reg.strip() in ("16", "XVI")
            # El RUT solo decide si la región viene vacía o sin dato: hay servicios con un RUT para varias regiones.
            sin_region = norm(reg) in ("", "na", "sin informacion", "null")
            por_rut = sin_region and iu is not None and iu < len(row) and rut(row[iu]) in ruts
            if por_reg or por_rut:
                w.writerow(row + [f"{y}-{m:02d}"])
                k += 1
                k_reg += por_reg
                k_rut += por_rut and not por_reg
    tmp.replace(destino)
    print(f"   {n:,} filas, {k:,} de Ñuble ({k_reg:,} por región, {k_rut:,} solo por RUT del comprador)")
    if k_reg == 0:
        print(f"   AVISO: ninguna fila con región Ñuble. Valores más comunes de Region: {regiones.most_common(8)}")
        print(f"   Columnas: {header}")
    if not conservar:
        z.unlink(missing_ok=True)


def diagnostico_cot(y: int, m: int) -> None:
    """¿Por qué un COT_ no trae filas de Ñuble? Regiones, compradores de Ñuble por RUT y listado del contenedor."""
    print(f"Diagnóstico COT_ {y}-{m}")
    lista = f"https://transparenciachc.blob.core.windows.net/trnspchc?restype=container&comp=list&prefix=COT_{y}"
    try:
        with urllib.request.urlopen(lista, timeout=60) as r:
            nombres = re.findall(r"<Name>([^<]+)</Name>", r.read().decode("utf-8", "replace"))
        print(f"   Archivos COT_{y}* en el contenedor: {nombres}")
    except Exception as e:  # noqa: BLE001
        print(f"   No se pudo listar el contenedor ({e})")
    z = TMP / f"COT_{y}-{m:02d}.zip"
    if not z.exists() and not descargar(_BLOB.format(ruta=f"trnspchc/COT_{y}-{m:02d}.zip"), z):
        return
    ruts = ruts_compradores_nuble()
    it = _filas_zip(z)
    header = [h.strip() for h in next(it)]
    ir, iu, idir = header.index("Region"), header.index("RUTUnidaddeCompra"), header.index("DireccionEntrega")
    ifp = header.index("FechaPublicacionParaCotizar")
    reg, reg_rut, fechas = Counter(), Counter(), Counter()
    chillan = 0
    for row in it:
        reg[row[ir]] += 1
        fechas[row[ifp][:7]] += 1
        if rut(row[iu]) in ruts:
            reg_rut[row[ir]] += 1
        chillan += "chillan" in norm(row[idir])
    print(f"   Regiones ({len(reg)}): {reg.most_common()}")
    print(f"   Filas de compradores con RUT de Ñuble, por región declarada: {reg_rut.most_common()}")
    print(f"   Filas con 'Chillán' en la dirección de entrega: {chillan}")
    print(f"   Mes de publicación de las filas: {fechas.most_common(6)}")
    print(f"   (El ZIP queda en {z} para revisarlo; bórralo cuando termines.)")


# ---------------------------------------------------------------------------
# informe
# ---------------------------------------------------------------------------
def leer_oc():
    for p in sorted(OUT.glob("oc_ca2_*.csv.gz")):
        with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
            yield from csv.DictReader(f, delimiter=";")


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))]


def _mes_menos(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m -= k
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y}-{m:02d}"


RUT_REGION: dict[str, str] = {}
NOM_REGION: dict[str, str] = {}


def analizar_oc() -> dict:
    meses = sorted({p.name[7:14] for p in OUT.glob("oc_ca2_*.csv.gz")})
    if not meses:
        sys.exit("No hay archivos oc_ca2_*.csv.gz: corre primero `bajar`.")
    desde12 = _mes_menos(meses[-1], 11)
    R = {"meses": [meses[0], meses[-1]], "desde12": desde12}
    g12, gnac12, goc12, gloc12 = Counter(), Counter(), defaultdict(set), defaultdict(Counter)
    gnorm = defaultdict(Counter)
    com12, prod12 = Counter(), defaultdict(Counter)
    prov24 = defaultdict(Counter)  # grupo -> proveedor -> monto (Ñuble, 24 m)
    fam24 = defaultdict(Counter)
    precios = defaultdict(list)
    nombre_prod: dict[str, str] = {}
    nub_lineas = []
    rut_region: dict[str, Counter] = defaultdict(Counter)
    nom_region: dict[str, Counter] = defaultdict(Counter)  # respaldo si el archivo no trae RutSucursal
    provs: dict[str, dict] = {}
    fam12 = defaultdict(lambda: {"monto": 0.0, "local": 0.0, "grupo": "", "nombre": Counter(), "prov": set()})
    origen12 = defaultdict(Counter)  # grupo -> región del proveedor -> monto
    n = 0
    for r in leer_oc():
        n += 1
        gr, mes = r["grupo"], r["archivo"]
        monto = num(r.get("totalLineaNeto", "")) or 0.0
        nub = es_nuble(r.get("RegionUnidadCompra", ""))
        en12 = mes >= desde12
        nz = normalizar(r)
        if en12:
            gnac12[gr] += monto
        if nz:
            clave, p = nz
            nombre_prod.setdefault(clave, r.get("NombreroductoGenerico", ""))
            precios[(clave, "nacional")].append((mes, p))
            if nub:
                precios[(clave, "nuble")].append((mes, p))
                nub_lineas.append((mes, clave, p, monto, gr))
        rp = rut(r.get("RutSucursal", ""))
        reg = (r.get("RegionProveedor", "") or "").strip()
        if reg and reg != "NA":
            if rp:
                rut_region[rp][reg] += 1
            nom_region[norm(r.get("NombreProveedor", ""))][reg] += 1
        if not nub:
            continue
        prov = r.get("NombreProveedor", "") or r.get("CodigoProveedor", "")
        if en12:
            local = es_nuble(reg)
            k = rp or prov
            pv = provs.setdefault(k, {"rut": rp, "nombre": prov, "comuna": r.get("ComunaProveedor", ""), "region": reg,
                                      "local": local, "monto": 0.0, "grupos": Counter(), "oc": set(),
                                      "compradores": set(), "comunas": set()})
            pv["monto"] += monto
            pv["grupos"][gr] += monto
            pv["oc"].add(r.get("Codigo"))
            pv["compradores"].add(r.get("RutUnidadCompra", ""))
            pv["comunas"].add(r.get("CiudadUnidadCompra", ""))
            fm = fam12[r.get("codigoProductoONU", "")[:4]]
            fm["monto"] += monto
            fm["local"] += monto if local else 0.0
            fm["grupo"] = gr
            fm["nombre"][r.get("NombreroductoGenerico", "")] += monto
            fm["prov"].add(k)
            origen12[gr]["Ñuble" if local else (reg if reg and reg != "NA" else "Sin dato")] += monto
        prov24[gr][prov] += monto
        fam24[r.get("codigoProductoONU", "")[:4]][prov] += monto
        if en12:
            g12[gr] += monto
            goc12[gr].add(r.get("Codigo"))
            gloc12[gr][es_nuble(r.get("RegionProveedor", ""))] += monto
            gnorm[gr][nz is not None] += 1
            com12[r.get("CiudadUnidadCompra", "") or "?"] += monto
            prod12[gr][r.get("NombreroductoGenerico", "")] += monto
    R["lineas_leidas"] = n
    oc_total = set().union(*goc12.values()) if goc12 else set()
    R["total12"] = sum(g12.values())
    R["oc12"] = len(oc_total)
    R["local12"] = sum(c[True] for c in gloc12.values()) / max(1, R["total12"])
    grupos = []
    for gr in sorted(g12, key=lambda g: -g12[g]):
        c = prov24[gr]
        tot = sum(c.values()) or 1
        top, v1 = c.most_common(1)[0]
        loc = gloc12[gr][True] / max(1, g12[gr])
        grupos.append({
            "grupo": gr, "monto12": g12[gr], "nacional12": gnac12[gr], "oc12": len(goc12[gr]),
            "ticket": g12[gr] / max(1, len(goc12[gr])), "local": loc, "foraneo12": g12[gr] * (1 - loc),
            "proveedores24": len(c), "top1": top, "cuota_top1": v1 / tot,
            "hhi": sum((v / tot * 100) ** 2 for v in c.values()),
            "normalizable": gnorm[gr][True] / max(1, sum(gnorm[gr].values())),
            "top_productos": [[k, v] for k, v in prod12[gr].most_common(8)],
        })
    R["grupos"] = grupos
    # Proveedores locales por línea
    INF.mkdir(parents=True, exist_ok=True)
    with (INF / "proveedores_nuble.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["rut", "proveedor", "comuna", "region", "local", "monto_12m", "oc", "compradores", "comunas_compradoras",
                    "grupo_principal", "grupos"])
        for pv in sorted(provs.values(), key=lambda p: -p["monto"]):
            w.writerow([pv["rut"], pv["nombre"], pv["comuna"], pv["region"], "si" if pv["local"] else "no", round(pv["monto"]),
                        len(pv["oc"]), len(pv["compradores"]), len(pv["comunas"]), pv["grupos"].most_common(1)[0][0],
                        " | ".join(f"{g}: {round(v)}" for g, v in pv["grupos"].most_common())])
    loc_g = []
    for g in grupos:
        gr = g["grupo"]
        ps = [p for p in provs.values() if gr in p["grupos"]]
        locs = sorted((p for p in ps if p["local"]), key=lambda p: -p["grupos"][gr])
        fors = sorted((p for p in ps if not p["local"]), key=lambda p: -p["grupos"][gr])
        orig = origen12[gr]
        tot = sum(orig.values()) or 1
        loc_g.append({"grupo": gr, "local": g["local"], "n_locales": len(locs), "n_foraneos": len(fors),
                      "top_locales": [[p["nombre"], p["comuna"], p["grupos"][gr]] for p in locs[:5]],
                      "top_foraneos": [[p["nombre"], p["region"], p["grupos"][gr]] for p in fors[:5]],
                      "origen": [[k, v / tot] for k, v in orig.most_common(4)]})
    R["locales_grupo"] = loc_g
    fams = []
    for fam, d in fam12.items():
        fams.append({"familia": fam, "grupo": d["grupo"], "nombre": d["nombre"].most_common(1)[0][0], "monto12": d["monto"],
                     "local": d["local"] / max(1, d["monto"]), "foraneo12": d["monto"] - d["local"], "proveedores": len(d["prov"])})
    fams.sort(key=lambda x: -x["foraneo12"])
    with (INF / "lineas_local_vs_foraneo.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["familia", "grupo", "producto_principal", "monto_12m", "pct_local", "monto_foraneo_12m", "proveedores"])
        for x in fams:
            w.writerow([x["familia"], x["grupo"], x["nombre"], round(x["monto12"]), round(x["local"], 3), round(x["foraneo12"]),
                        x["proveedores"]])
    R["lineas_top_foraneo"] = fams[:20]
    R["proveedores_locales_activos"] = sum(1 for p in provs.values() if p["local"])
    R["proveedores_activos"] = len(provs)
    global RUT_REGION, NOM_REGION
    RUT_REGION = {k: c.most_common(1)[0][0] for k, c in rut_region.items()}
    NOM_REGION = {k: c.most_common(1)[0][0] for k, c in nom_region.items() if k}
    R["comunas12"] = [[k, v] for k, v in com12.most_common()]
    # Brechas (solo referencia; dominadas por errores de unidad hasta el catálogo canónico)
    filas = []
    for (clave, amb), xs in precios.items():
        if amb != "nuble" or len(xs) < 5:
            continue
        nac = [p for _, p in precios[(clave, "nacional")]]
        if len(nac) < 10:
            continue
        pn = [p for _, p in xs]
        filas.append([clave, nombre_prod.get(clave, ""), len(pn), round(st.median(pn)), round(st.median(nac)),
                      round(pct(nac, .25)), round(100 * (st.median(pn) / pct(nac, .25) - 1), 1)])
    filas.sort(key=lambda f: -f[6] * f[2])
    INF.mkdir(parents=True, exist_ok=True)
    with (INF / "precios_nuble_vs_nacional.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["clave", "producto", "n_nuble", "mediana_nuble", "mediana_nacional", "p25_nacional",
                    "brecha_vs_p25_%"])
        w.writerows(filas)
    with (INF / "competencia_nuble.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["familia", "proveedor", "monto_24m", "cuota"])
        for fam, c in fam24.items():
            tot = sum(c.values()) or 1
            for prov, v in c.most_common():
                w.writerow([fam, prov, round(v), round(v / tot, 4)])
    # Juez walk-forward en el último mes
    ult = meses[-1]
    hist = defaultdict(list)
    for (clave, amb), xs in precios.items():
        for m, p in xs:
            if m < ult:
                hist[(clave, amb)].append(p)
    err_base, err_grupo = defaultdict(list), defaultdict(list)
    for m, clave, p, _monto, gr in nub_lineas:
        if m != ult:
            continue
        ref = hist.get((clave, "nuble"), [])
        if len(ref) < 5:
            ref = hist.get((clave, "nacional"), [])
        if len(ref) < 5:
            continue
        e = abs(st.median(ref) / p - 1)
        err_base[clave.rsplit("|", 1)[1]].append(e)
        err_grupo[gr].append(e)
    todos = [e for es in err_base.values() for e in es]

    def res(es):
        return {"n": len(es), "mediana": st.median(es), "dentro20": sum(e <= .2 for e in es) / len(es)} if es else None

    R["walkforward"] = {"mes": ult, "total": res(todos), "por_base": {b: res(es) for b, es in err_base.items()},
                        "por_grupo": {g: res(es) for g, es in err_grupo.items() if len(es) >= 20}}
    return R


NOMBRE_COT: dict[str, str] = {}


def _origen(r_: str) -> str:
    reg = RUT_REGION.get(rut(r_), "") or NOM_REGION.get(NOMBRE_COT.get(r_, ""), "")
    return "nd" if not reg else ("loc" if es_nuble(reg) else "for")


def _stats_cot(cas) -> dict:
    d = {"ca": 0, "adj": 0.0, "ofertas": [], "una": 0, "cero": 0, "sel": 0, "barato": [], "gap": [], "un_producto": 0,
         "desierta": 0, "of_loc": 0, "of_for": 0, "of_nd": 0, "gan_loc": 0, "gan_for": 0, "gan_nd": 0, "loc_pierde": [],
         "vs_disp": []}
    for x in cas:
        d["ca"] += 1
        no = len(x["prov"])
        d["ofertas"].append(no)
        d["una"] += no == 1
        d["cero"] += no == 0
        d["un_producto"] += len(x["prods"]) == 1
        d["desierta"] += norm(x["estado"]) in ("desierta", "cancelada")
        for r_ in x["prov"]:
            d["of_" + _origen(r_)] += 1
        if not x["sel"]:
            continue
        o = _origen(x["sel"])
        d["gan_" + o] += 1
        d["sel"] += 1
        mw = x["prov"][x["sel"]][0] or 0
        d["adj"] += mw
        if mw and x["disp"]:
            d["vs_disp"].append(mw / x["disp"])
        otros = [m for r_, (m, _) in x["prov"].items() if r_ != x["sel"] and m]
        if mw and otros:
            d["barato"].append(mw <= min(otros))
            d["gap"].append(min(otros) / mw - 1)
        if mw and o != "loc":
            ml = [m for r_, (m, _) in x["prov"].items() if _origen(r_) == "loc" and m]
            if ml:
                d["loc_pierde"].append(min(ml) / mw - 1)
    med = lambda xs: st.median(xs) if xs else None  # noqa: E731
    return {
        "ca": d["ca"], "adjudicado": d["adj"], "ofertas_mediana": med(d["ofertas"]) or 0,
        "sin_ofertas": d["cero"] / max(1, d["ca"]), "una_oferta": d["una"] / max(1, d["ca"]),
        "un_producto": d["un_producto"] / max(1, d["ca"]), "seleccionado": d["sel"] / max(1, d["ca"]),
        "desierta_cancelada": d["desierta"] / max(1, d["ca"]),
        "gana_barato": sum(d["barato"]) / len(d["barato"]) if d["barato"] else None,
        "gap_mediana": med(d["gap"]), "ganador_vs_disponible": med(d["vs_disp"]),
        "ofertas_local": d["of_loc"] / max(1, d["of_loc"] + d["of_for"]),
        "ofertas_sin_dato": d["of_nd"] / max(1, d["of_loc"] + d["of_for"] + d["of_nd"]),
        "victoria_local": d["gan_loc"] / max(1, d["of_loc"]), "victoria_foraneo": d["gan_for"] / max(1, d["of_for"]),
        "local_pierde_por": med(d["loc_pierde"]), "n_local_pierde": len(d["loc_pierde"]),
    }


def archivos_cot() -> dict[str, Path]:
    """Un archivo de cotizaciones por mes: cot_api_* (F-merc-0b, con precio por producto) gana a cot_nuble_* (COT_)."""
    por_mes: dict[str, Path] = {}
    for p in sorted(OUT.glob("cot_nuble_*.csv.gz")):
        if p.stat().st_size > 2000:
            por_mes[p.name[10:17]] = p
    for p in sorted(OUT.glob("cot_api_*.csv.gz")):
        if p.stat().st_size > 0:
            por_mes[p.name[8:15]] = p
    return dict(sorted(por_mes.items()))


def _es_alimento(cod: str, nombre: str) -> bool:
    return grupo_de(cod, nombre) in G_ALIM or cod.startswith("1012")


def _precios_unitarios(ca: dict) -> list[dict]:
    """Por producto de alimentos (UNSPSC): precio unitario neto ganador vs. perdedores, solo filas de la API."""
    prod = defaultdict(lambda: {"nombre": Counter(), "ca": 0, "gan": [], "per": [], "gap": []})
    for x in ca.values():
        for cod, por_rut in x["pu"].items():
            nombre = x["pu_nombre"][cod]
            if not _es_alimento(cod, nombre):
                continue
            d = prod[cod]
            d["nombre"][nombre] += 1
            d["ca"] += 1
            pw = por_rut.get(x["sel"]) if x["sel"] else None
            otros = [p for r_, p in por_rut.items() if r_ != x["sel"]]
            if pw:
                d["gan"].append(pw)
            d["per"] += otros
            if pw and otros:
                d["gap"].append(min(otros) / pw - 1)
    med = lambda xs: st.median(xs) if xs else None  # noqa: E731
    filas = [{"codigo": cod, "producto": d["nombre"].most_common(1)[0][0], "familia": familia_alimento(cod),
              "ca": d["ca"], "n_ganador": len(d["gan"]), "mediana_ganador": med(d["gan"]),
              "n_perdedores": len(d["per"]), "mediana_perdedores": med(d["per"]),
              "distancia_al_2o": med(d["gap"]), "n_distancia": len(d["gap"])} for cod, d in prod.items()]
    filas.sort(key=lambda f: -f["ca"])
    INF.mkdir(parents=True, exist_ok=True)
    with (INF / "precios_unitarios_cot_alimentos.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["codigo", "producto", "familia", "ca", "n_ganador", "mediana_ganador", "n_perdedores",
                    "mediana_perdedores", "distancia_al_2o_mediana", "n_distancia"])
        for x in filas:
            w.writerow([x["codigo"], x["producto"], x["familia"], x["ca"], x["n_ganador"],
                        "" if x["mediana_ganador"] is None else round(x["mediana_ganador"]), x["n_perdedores"],
                        "" if x["mediana_perdedores"] is None else round(x["mediana_perdedores"]),
                        "" if x["distancia_al_2o"] is None else round(x["distancia_al_2o"], 3), x["n_distancia"]])
    return filas[:40]


def analizar_cot() -> dict | None:
    por_mes = archivos_cot()
    if not por_mes:
        return None
    ca = defaultdict(lambda: {"g": Counter(), "fam": Counter(), "prov": {}, "sel": None, "prods": set(), "estado": "",
                              "mes": "", "disp": None, "pu": defaultdict(dict), "pu_nombre": {},
                              "monto_sel": None, "pu_sel": {}})
    # Monto por oferta usado en "ganador vs. segundo" y "ganador / disponible", por mes. En cot_api_* el
    # MontoTotal incluye IVA y despacho (guía) y se usa ValorNeto si viene; COT_ solo trae MontoTotal.
    monto_usado = defaultdict(Counter)
    for mes_archivo, p in por_mes.items():
        es_api = p.name.startswith("cot_api_")
        with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f, delimiter=";"):
                x = ca[r["CodigoCotizacion"]]
                cod = r.get("CodigoProducto", "")
                gr = grupo_de(cod, r.get("NombreProductoGenerico", "")) or "Fuera de alcance"
                x["g"][gr] += 1
                if gr in G_ALIM or cod.startswith("1012"):
                    x["fam"][familia_alimento(cod)] += 1
                x["prods"].add(cod + "|" + r.get("ProductoCotizado", "")[:40])
                x["estado"], x["mes"] = r.get("Estado", ""), r.get("archivo", "")
                x["disp"] = num(r.get("MontoTotalDisponble", "")) or x["disp"]
                rutp = r.get("RUTProveedor", "")
                if rutp and rutp != "NA":
                    neto = num(r.get("ValorNeto", "")) if es_api else None
                    monto_usado[mes_archivo]["ValorNeto" if neto is not None else "MontoTotal"] += 1
                    monto = neto if neto is not None else num(r.get("MontoTotal", ""))
                    seleccionada = norm(r.get("ProveedorSeleccionado", "")) in ("si", "1", "true")
                    # Un RUT puede mandar varias ofertas a una CA (F-merc-1c): el precio de un
                    # competidor es su oferta MÁS BARATA y el del ganador, la seleccionada.
                    # Con una oferta por RUT da lo mismo que antes. N.º de oferentes = RUT.
                    previo = x["prov"].get(rutp)
                    if previo is None or (monto is not None and (previo[0] is None or monto < previo[0])):
                        x["prov"][rutp] = (monto, r.get("Tamano", ""))
                    NOMBRE_COT[rutp] = norm(r.get("RazonSocialProveedor", ""))
                    if seleccionada:
                        x["sel"] = rutp
                        x["monto_sel"] = (monto, r.get("Tamano", ""))
                    # Solo cot_api_* trae PrecioUnitario (neto, por producto cotizado).
                    pu = num(r.get("PrecioUnitario", ""))
                    if pu and pu > 0 and cod:
                        x["pu"][cod][rutp] = min(pu, x["pu"][cod].get(rutp, pu))
                        if seleccionada:
                            x["pu_sel"][cod] = pu
                        x["pu_nombre"].setdefault(cod, r.get("NombreProductoGenerico", ""))
    for x in ca.values():
        # El ganador se mide con su oferta seleccionada, no con otra suya más barata.
        if x["sel"] and x.get("monto_sel"):
            x["prov"][x["sel"]] = x["monto_sel"]
        for cod, pu in x["pu_sel"].items():
            x["pu"][cod][x["sel"]] = pu
    por_g, por_f = defaultdict(list), defaultdict(list)
    for x in ca.values():
        gr = x["g"].most_common(1)[0][0]
        por_g[gr].append(x)
        if gr in G_ALIM and x["fam"]:
            por_f[x["fam"].most_common(1)[0][0]].append(x)
    meses = sorted({x["mes"] for x in ca.values()})
    out = {"meses": [meses[0], meses[-1]], "lista_meses": meses, "n_meses": len(meses), "ca_total": len(ca),
           "grupos": [], "familias_alimentos": [],
           "fuente_por_mes": {m: ("API" if p.name.startswith("cot_api_") else "COT_") for m, p in por_mes.items()},
           "monto_por_mes": {m: dict(c) for m, c in sorted(monto_usado.items())},
           "precios_alimentos": _precios_unitarios(ca),
           "alimentos_total": _stats_cot([x for g in G_ALIM for x in por_g.get(g, [])])}
    for gr, xs in por_g.items():
        out["grupos"].append({"grupo": gr, **_stats_cot(xs)})
    out["grupos"].sort(key=lambda g: -g["adjudicado"])
    for fam, xs in por_f.items():
        out["familias_alimentos"].append({"familia": fam, **_stats_cot(xs)})
    out["familias_alimentos"].sort(key=lambda g: -g["adjudicado"])
    return out


def analizar_alimentos() -> dict:
    """Detalle del giro principal: alimentos (envasados y preparados) de compradores de Ñuble."""
    meses = sorted({p.name[7:14] for p in OUT.glob("oc_ca2_*.csv.gz")})
    desde12 = _mes_menos(meses[-1], 11)
    serie = defaultdict(lambda: Counter())          # mes -> {envasados, preparados}
    serie_oc = defaultdict(set)
    fam = defaultdict(lambda: {"monto": 0.0, "local": 0.0, "oc": set(), "prov": set(), "comp": set()})
    prod = defaultdict(lambda: {"monto": 0.0, "local": 0.0, "oc": set(), "comp": set(), "precios": [], "base": Counter(),
                                "fam": "", "prep": False})
    nac = defaultdict(list)                          # clave normalizada -> precios nacionales 12 m
    comp = defaultdict(lambda: {"monto": 0.0, "oc": set(), "meses": set(), "comuna": "", "sector": "", "local": 0.0,
                                "organismo": ""})
    sector = Counter()
    comuna = Counter()
    prov = defaultdict(lambda: {"monto": 0.0, "oc": set(), "comp": set(), "comunas": set(), "local": False, "comuna": "",
                                "region": ""})
    oc_monto = defaultdict(float)
    tot12 = loc12 = 0.0
    for r in leer_oc():
        gr = r["grupo"]
        if gr not in G_ALIM:
            continue
        mes = r["archivo"]
        monto = num(r.get("totalLineaNeto", "")) or 0.0
        nz = normalizar(r)
        if mes >= desde12 and nz:
            nac[nz[0]].append(nz[1])
        if not es_nuble(r.get("RegionUnidadCompra", "")):
            continue
        prep = gr == G_PREP
        serie[mes]["preparados" if prep else "envasados"] += monto
        serie_oc[mes].add(r.get("Codigo"))
        if mes < desde12:
            continue
        reg = r.get("RegionProveedor", "")
        local = es_nuble(reg)
        cod = r.get("codigoProductoONU", "")
        tot12 += monto
        loc12 += monto if local else 0
        f = fam[familia_alimento(cod) if not prep else "Preparados (cócteles, empanadas, sándwiches)"]
        f["monto"] += monto
        f["local"] += monto if local else 0
        f["oc"].add(r.get("Codigo"))
        f["prov"].add(r.get("NombreProveedor"))
        f["comp"].add(r.get("RutUnidadCompra"))
        pk = r.get("NombreroductoGenerico", "") or cod
        pr = prod[pk]
        pr["monto"] += monto
        pr["local"] += monto if local else 0
        pr["oc"].add(r.get("Codigo"))
        pr["comp"].add(r.get("RutUnidadCompra"))
        pr["fam"] = familia_alimento(cod)
        pr["prep"] = prep
        if nz:
            pr["precios"].append(nz)
            pr["base"][nz[0].rsplit("|", 1)[1]] += 1
        ck = r.get("RutUnidadCompra", "") + "|" + r.get("UnidadCompra", "")
        c = comp[ck]
        c["monto"] += monto
        c["oc"].add(r.get("Codigo"))
        c["meses"].add(mes)
        c["comuna"] = r.get("CiudadUnidadCompra", "")
        c["sector"] = r.get("sector", "")
        c["organismo"] = r.get("OrganismoPublico", "")
        c["local"] += monto if local else 0
        sector[r.get("sector", "") or "Sin dato"] += monto
        comuna[r.get("CiudadUnidadCompra", "") or "?"] += monto
        pv = prov[r.get("NombreProveedor", "")]
        pv["monto"] += monto
        pv["oc"].add(r.get("Codigo"))
        pv["comp"].add(ck)
        pv["comunas"].add(r.get("CiudadUnidadCompra", ""))
        pv["local"] = local
        pv["comuna"] = r.get("ComunaProveedor", "")
        pv["region"] = reg
        oc_monto[r.get("Codigo")] += monto
    A = {"desde12": desde12, "total12": tot12, "local12": loc12 / max(1, tot12), "oc12": len(oc_monto)}
    A["serie"] = [[m, serie[m]["envasados"], serie[m]["preparados"], len(serie_oc[m])] for m in sorted(serie)]
    A["familias"] = sorted(({"familia": k, "monto": v["monto"], "local": v["local"] / max(1, v["monto"]), "oc": len(v["oc"]),
                             "proveedores": len(v["prov"]), "compradores": len(v["comp"])} for k, v in fam.items()),
                           key=lambda x: -x["monto"])
    canasta = []
    for k, v in prod.items():
        ref_p = None
        if v["precios"]:
            base = v["base"].most_common(1)[0][0]
            ps = [p for kk, p in v["precios"] if kk.endswith("|" + base)]
            claves = Counter(kk for kk, _ in v["precios"] if kk.endswith("|" + base))
            nacs = nac.get(claves.most_common(1)[0][0], [])
            ref_p = {"base": base, "mediana_nuble": st.median(ps), "n": len(ps),
                     "p25_nacional": pct(nacs, .25) if len(nacs) >= 10 else None,
                     "mediana_nacional": st.median(nacs) if len(nacs) >= 10 else None}
        canasta.append({"producto": k, "familia": v["fam"], "preparado": v["prep"], "monto": v["monto"],
                        "local": v["local"] / max(1, v["monto"]), "oc": len(v["oc"]), "compradores": len(v["comp"]),
                        "normalizable": sum(v["base"].values()), "precio": ref_p})
    canasta.sort(key=lambda x: -x["monto"])
    A["canasta"] = canasta[:40]
    A["compradores"] = sorted(({"comprador": v["organismo"], "unidad": k.split("|", 1)[1], "comuna": v["comuna"],
                                "sector": v["sector"], "monto": v["monto"], "oc": len(v["oc"]), "meses": len(v["meses"]),
                                "local": v["local"] / max(1, v["monto"])} for k, v in comp.items()),
                              key=lambda x: -x["monto"])[:25]
    A["n_compradores"] = len(comp)
    A["compradores_recurrentes"] = sum(1 for v in comp.values() if len(v["meses"]) >= 6)
    A["sector"] = [[k, v] for k, v in sector.most_common()]
    A["comunas"] = [[k, v] for k, v in comuna.most_common()]
    A["proveedores"] = sorted(({"proveedor": k, "comuna": v["comuna"], "region": v["region"], "local": v["local"],
                                "monto": v["monto"], "oc": len(v["oc"]), "compradores": len(v["comp"]),
                                "comunas": len(v["comunas"])} for k, v in prov.items()), key=lambda x: -x["monto"])
    A["n_proveedores"] = len(A["proveedores"])
    A["n_proveedores_locales"] = sum(1 for p in A["proveedores"] if p["local"])
    tot = sum(p["monto"] for p in A["proveedores"]) or 1
    A["hhi"] = sum((p["monto"] / tot * 100) ** 2 for p in A["proveedores"])
    A["top5_cuota"] = sum(p["monto"] for p in A["proveedores"][:5]) / tot
    A["proveedores"] = A["proveedores"][:20]
    bandas = [(0, 125e3, "< $125 mil"), (125e3, 500e3, "$125–500 mil"), (500e3, 1e6, "$500 mil–1 M"),
              (1e6, 3e6, "$1–3 M"), (3e6, 1e12, "> $3 M")]
    A["tickets"] = []
    for lo, hi, et in bandas:
        xs = [v for v in oc_monto.values() if lo <= v < hi]
        A["tickets"].append({"banda": et, "oc": len(xs), "monto": sum(xs)})
    return A


def M(x: float) -> str:
    return f"{x / 1e6:,.0f}".replace(",", ".")


def P(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.0f} %"


def informe() -> None:
    INF.mkdir(parents=True, exist_ok=True)
    R = analizar_oc()
    R["cot"] = analizar_cot()
    R["alimentos"] = analizar_alimentos()
    (INF / "resumen.json").write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")
    L = ["# Observatorio Compra Ágil — productos intermediables (Ñuble)\n",
         f"Órdenes de compra {R['meses'][0]} a {R['meses'][1]} ({R['lineas_leidas']:,} líneas de CA en alcance, país). "
         f"Últimos 12 meses desde {R['desde12']}.\n",
         f"Ñuble, 12 meses: ${M(R['total12'])} MM en {R['oc12']:,} OC; proveedor de Ñuble {P(R['local12'])}.\n",
         "## Grupos (12 meses, Ñuble)\n",
         "| Grupo | MM$ | OC | Ticket M$ | Local | Foráneo MM$ | Proveedores 24m | HHI | Normalizable |",
         "|---|---|---|---|---|---|---|---|---|"]
    for g in R["grupos"]:
        L.append(f"| {g['grupo']} | {M(g['monto12'])} | {g['oc12']:,} | {g['ticket'] / 1e3:,.0f} | {P(g['local'])} | "
                 f"{M(g['foraneo12'])} | {g['proveedores24']} | {g['hhi']:,.0f} | {P(g['normalizable'])} |")
    c = R["cot"]
    if c:
        L += [f"\n## Cotizaciones ({c['meses'][0]} a {c['meses'][1]}, {c['n_meses']} meses, {c['ca_total']:,} CA de Ñuble)\n",
              "| Grupo | CA | Adjudicado MM$ | Ofertas (mediana) | 1 oferta | 1 producto | Gana el más barato | Distancia al 2º |",
              "|---|---|---|---|---|---|---|---|"]
        for g in c["grupos"]:
            L.append(f"| {g['grupo']} | {g['ca']:,} | {M(g['adjudicado'])} | {g['ofertas_mediana']:.0f} | "
                     f"{P(g['una_oferta'])} | {P(g['un_producto'])} | {P(g['gana_barato'])} | {P(g['gap_mediana'])} |")
        api = [m for m, fte in c["fuente_por_mes"].items() if fte == "API"]
        L.append(f"\nMeses desde la API (precio por producto): {', '.join(api) or 'ninguno'}; el resto desde COT_.")
        notas = []
        for m, cnt in c["monto_por_mes"].items():
            usado = "ValorNeto (neto)" if cnt.get("ValorNeto") and not cnt.get("MontoTotal") else (
                "MontoTotal" if not cnt.get("ValorNeto") else
                f"mixto: ValorNeto {cnt['ValorNeto']:,} ofertas, MontoTotal {cnt['MontoTotal']:,}")
            notas.append(f"{m}: {usado}")
        L.append("Monto por oferta en «gana el más barato», «distancia al 2º» y «ganador / disponible» — "
                 + "; ".join(notas) + ". En la API, MontoTotal incluye IVA y despacho; en COT_ no se sabe.")
        if c["precios_alimentos"]:
            pesos = lambda v: "—" if v is None else f"${v:,.0f}".replace(",", ".")  # noqa: E731
            L += ["\n### Alimentos: precio unitario neto por producto (cotizaciones de la API)\n",
                  "| Producto | UNSPSC | CA | Ganador (mediana) | Perdedores (mediana) | Distancia al 2º (mediana, n) |",
                  "|---|---|---|---|---|---|"]
            L += [f"| {x['producto'][:35]} | {x['codigo']} | {x['ca']} | {pesos(x['mediana_ganador'])} | "
                  f"{pesos(x['mediana_perdedores'])} | {P(x['distancia_al_2o'])} (n={x['n_distancia']}) |"
                  for x in c["precios_alimentos"][:25]]
            L.append("\nOjo: el precio unitario depende del formato (1 L vs. 5 L, kg vs. unidad); la mediana mezcla "
                     "formatos hasta tener el catálogo canónico.")
    L += [f"\n## Proveedores locales por línea (12 meses): {R['proveedores_locales_activos']:,} locales de "
          f"{R['proveedores_activos']:,} activos\n", "| Grupo | Local | Nº locales | Nº foráneos | Origen foráneo principal | Top local |",
          "|---|---|---|---|---|---|"]
    for g in R["locales_grupo"]:
        orig = next((f"{k} {P(v)}" for k, v in g["origen"] if k != "Ñuble"), "—")
        top = g["top_locales"][0][0][:35] if g["top_locales"] else "—"
        L.append(f"| {g['grupo']} | {P(g['local'])} | {g['n_locales']} | {g['n_foraneos']} | {orig} | {top} |")
    L += ["\n### Líneas (familia UNSPSC) donde más gana gente de afuera\n", "| Familia | Grupo | Producto | MM$ | Local | Foráneo MM$ |",
          "|---|---|---|---|---|---|"]
    L += [f"| {x['familia']} | {x['grupo']} | {x['nombre'][:35]} | {M(x['monto12'])} | {P(x['local'])} | {M(x['foraneo12'])} |"
          for x in R["lineas_top_foraneo"][:15]]
    if c:
        L += ["\n### En las cotizaciones: ¿cómo les va a los locales?\n",
              "| Grupo | % ofertas locales | Victoria local | Victoria foráneo | Local pierde por (med.) | Sin región |",
              "|---|---|---|---|---|---|"]
        for g in c["grupos"]:
            L.append(f"| {g['grupo']} | {P(g['ofertas_local'])} | {P(g['victoria_local'])} | {P(g['victoria_foraneo'])} | "
                     f"{P(g['local_pierde_por'])} (n={g['n_local_pierde']}) | {P(g['ofertas_sin_dato'])} |")
    wf = R["walkforward"]
    if wf["total"]:
        L += [f"\n## Walk-forward ({wf['mes']})\n",
              f"Total: n={wf['total']['n']}, error mediano {P(wf['total']['mediana'])}, ±20 %: {P(wf['total']['dentro20'])}"]
        L += [f"- Base {b}: n={r['n']}, error mediano {P(r['mediana'])}, ±20 %: {P(r['dentro20'])}"
              for b, r in sorted(wf["por_base"].items())]
    L.append("\n*Fuente: Dirección ChileCompra.*")
    (INF / "observatorio_nuble.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\n-> {INF}")


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bajar")
    b.add_argument("--meses", type=int, default=24, help="meses cerrados de oc-da (def. 24)")
    b.add_argument("--meses-cot", type=int, default=12, help="meses cerrados de COT_ (def. 12; 0 = no bajar)")
    b.add_argument("--conservar-zip", action="store_true")
    sub.add_parser("informe")
    dg = sub.add_parser("diagnostico-cot")
    dg.add_argument("mes", help="AAAA-MM, por ejemplo 2026-03")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.cmd == "bajar":
        for y, m in meses_cerrados(a.meses):
            bajar_oc(y, m, a.conservar_zip)
        for y, m in meses_cerrados(a.meses_cot) if a.meses_cot else []:
            bajar_cot(y, m, a.conservar_zip)
    elif a.cmd == "diagnostico-cot":
        y, m = (int(x) for x in a.mes.split("-"))
        diagnostico_cot(y, m)
    else:
        informe()


if __name__ == "__main__":
    main()
