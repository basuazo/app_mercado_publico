"""Juez de F-plat-1: walk-forward por producto canónico (solo lectura; no escribe ni llama a la API).

Para el último mes cerrado con cotizaciones: por línea `ok` con `cantidad_base`, compara el precio
GANADOR por unidad base con la mediana de los 3 meses anteriores del mismo producto (comuna →
provincia → región; cada nivel necesita ≥ 3 observaciones).

Reporta dos coberturas, el error mediano, el % dentro de ±20 % y los 10 productos que más fallan:
  (a) cobertura con detalle propio: % del monto de CA de alimentos con líneas propias (sin las
      líneas de anexo ni los kits/servicios) cuya línea quedó `ok`;
  (b) cobertura total: % del monto de alimentos de 12 meses con línea `ok` (sin meta).
Meta de la fase: (a) ≥ 70 % y error mediano ≤ 25 %. Si no pasa, se reportan los números reales.
Monto de una línea = monto disponible de su CA repartido entre sus líneas de alimentos.

Uso (desde la raíz, con DATABASE_URL del entorno; no usar la de producción sin saberlo):
    python scripts/mercado/walk_forward_canonico.py [--mes AAAA-MM] [--region 16]

Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalogos.canasta_alimentos import KITS
from app.catalogos.mercado import norm
from app.models.tables import (
    CaCotizacion,
    CaCotizacionProducto,
    CaLinea,
    CompraAgil,
    ProductoCanonico,
)

META_COBERTURA = 0.70
META_ERROR = 0.25
TOL = 0.20
MIN_OBS = 3  # observaciones mínimas para usar un nivel geográfico

# Ñuble (16): comuna → provincia (la CA no trae comuna: se deduce del texto de la unidad y el organismo).
PROVINCIAS: dict[str, str] = {}
for _prov, _comunas in {
    "Diguillín": [
        "Chillán",
        "Chillán Viejo",
        "Bulnes",
        "El Carmen",
        "Pemuco",
        "Pinto",
        "Quillón",
        "San Ignacio",
        "Yungay",
    ],
    "Punilla": ["San Carlos", "Coihueco", "Ñiquén", "San Fabián", "San Nicolás"],
    "Itata": ["Quirihue", "Cobquecura", "Coelemu", "Ninhue", "Portezuelo", "Ránquil", "Treguaco"],
}.items():
    for _c in _comunas:
        PROVINCIAS[_c] = _prov
# Más largas primero: "Chillán Viejo" antes que "Chillán".
_COMUNAS = sorted(PROVINCIAS, key=len, reverse=True)
_KITS_C = [re.compile(k.patron) for k in KITS if k.categoria != "Anexo"]


def comuna_de(ca: CompraAgil) -> str | None:
    texto = norm(f"{ca.unidad_compra or ''} {ca.organismo_nombre or ''}")
    for c in _COMUNAS:
        if re.search(rf"\b{re.escape(norm(c))}\b", texto):
            return c
    return None


@dataclass
class Obs:
    producto_id: int
    mes: str
    precio_base: float
    comuna: str | None
    provincia: str | None
    region: int | None


def _mes(d: datetime | None) -> str | None:
    return d.strftime("%Y-%m") if d else None


def _meses_antes(mes: str, n: int) -> list[str]:
    a, m = map(int, mes.split("-"))
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            a, m = a - 1, 12
        out.append(f"{a:04d}-{m:02d}")
    return out


def observaciones(session: Session, regiones: list[int], desde: date) -> list[Obs]:
    """Precio ganador por unidad base de cada línea `ok` con cantidad base, desde `desde`."""
    lineas = session.execute(
        select(CaLinea, CompraAgil)
        .join(CompraAgil, CompraAgil.codigo == CaLinea.ca_codigo)
        .where(
            CompraAgil.region.in_(regiones),
            CaLinea.estado == "ok",
            CaLinea.producto_id.isnot(None),
            CaLinea.cantidad_base > 0,
            CaLinea.cantidad > 0,
            CompraAgil.fecha_publicacion >= datetime(desde.year, desde.month, 1),
        )
    ).all()
    if not lineas:
        return []
    codigos = sorted({ln.ca_codigo for ln, _ in lineas})
    ganadores: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for i in range(0, len(codigos), 500):
        lote = codigos[i : i + 500]
        for ca_codigo, cod, nombre, precio in session.execute(
            select(
                CaCotizacion.ca_codigo,
                CaCotizacionProducto.codigo_producto,
                CaCotizacionProducto.nombre_producto,
                CaCotizacionProducto.precio_unitario,
            )
            .join(CaCotizacion, CaCotizacion.id == CaCotizacionProducto.cotizacion_id)
            .where(
                CaCotizacion.ca_codigo.in_(lote),
                CaCotizacion.seleccionado.is_(True),
                CaCotizacionProducto.precio_unitario > 0,
            )
        ):
            ganadores[(ca_codigo, cod or "")].append((norm(nombre), float(precio)))
    por_codigo: dict[tuple[str, str], int] = defaultdict(int)
    for ln, _ in lineas:
        por_codigo[(ln.ca_codigo, ln.codigo_unspsc or "")] += 1
    out: list[Obs] = []
    for ln, ca in lineas:
        clave = (ln.ca_codigo, ln.codigo_unspsc or "")
        cands = ganadores.get(clave, [])
        if not cands:
            continue
        if por_codigo[clave] > 1:
            # Varias líneas con el mismo UNSPSC: solo se asigna si el texto de la oferta calza.
            t = norm(ln.texto_origen)
            cands = [c for c in cands if c[0] and (c[0] in t or t in c[0])]
        if len(cands) != 1:
            continue  # ambiguo o sin oferta ganadora: no se inventa
        factor = ln.cantidad_base / ln.cantidad
        comuna = comuna_de(ca)
        out.append(
            Obs(
                ln.producto_id,  # type: ignore[arg-type]
                _mes(ca.fecha_publicacion) or "",
                cands[0][1] / factor,
                comuna,
                PROVINCIAS.get(comuna) if comuna else None,
                ca.region,
            )
        )
    return out


def prediccion(o: Obs, previas: list[Obs]) -> tuple[float, str] | None:
    """Mediana de las observaciones previas del mismo producto: comuna → provincia → región."""
    mismo = [p for p in previas if p.producto_id == o.producto_id]
    for nivel, filtro in (
        ("comuna", lambda p: o.comuna is not None and p.comuna == o.comuna),
        ("provincia", lambda p: o.provincia is not None and p.provincia == o.provincia),
        ("region", lambda p: p.region == o.region),
    ):
        pool = [p.precio_base for p in mismo if filtro(p)]
        if len(pool) >= MIN_OBS:
            return statistics.median(pool), nivel
    return None


def walk_forward(obs: list[Obs], mes: str) -> dict:
    """Error de predecir el mes `mes` con los 3 anteriores."""
    previos = set(_meses_antes(mes, 3))
    previas = [o for o in obs if o.mes in previos]
    del_mes = [o for o in obs if o.mes == mes]
    errores: list[float] = []
    por_producto: dict[int, list[float]] = defaultdict(list)
    niveles: dict[str, int] = defaultdict(int)
    sin_base = 0
    for o in del_mes:
        p = prediccion(o, previas)
        if p is None:
            sin_base += 1
            continue
        err = abs(p[0] - o.precio_base) / o.precio_base
        errores.append(err)
        por_producto[o.producto_id].append(err)
        niveles[p[1]] += 1
    n = len(errores)
    return {
        "mes": mes,
        "lineas_mes": len(del_mes),
        "evaluadas": n,
        "sin_base": sin_base,
        "error_mediano": statistics.median(errores) if errores else None,
        "dentro_20": sum(e <= TOL for e in errores) / n if n else None,
        "niveles": dict(niveles),
        "por_producto": {
            pid: (len(es), statistics.median(es), sum(e > TOL for e in es))
            for pid, es in por_producto.items()
        },
    }


def coberturas(session: Session, regiones: list[int], desde: date) -> dict[str, float | int | None]:
    """(a) y (b): ver el encabezado. Monto de línea = monto de la CA / líneas de la CA."""
    filas = session.execute(
        select(
            CaLinea.ca_codigo,
            CaLinea.estado,
            CaLinea.origen,
            CaLinea.texto_origen,
            CompraAgil.monto_disponible_clp,
        )
        .join(CompraAgil, CompraAgil.codigo == CaLinea.ca_codigo)
        .where(
            CompraAgil.region.in_(regiones),
            CompraAgil.fecha_publicacion >= datetime(desde.year, desde.month, 1),
        )
    ).all()
    n_por_ca: dict[str, int] = defaultdict(int)
    for ca_codigo, *_ in filas:
        n_por_ca[ca_codigo] += 1
    total = ok = elegible = elegible_ok = 0.0
    for ca_codigo, estado, origen, texto, monto in filas:
        peso = float(monto or 0) / n_por_ca[ca_codigo]
        total += peso
        ok += peso if estado == "ok" else 0
        t = norm(texto)
        if origen == "anexo" or any(k.search(t) for k in _KITS_C):
            continue
        elegible += peso
        elegible_ok += peso if estado == "ok" else 0
    return {
        "cobertura_a": elegible_ok / elegible if elegible else None,
        "cobertura_b": ok / total if total else None,
        "monto_total": round(total),
        "monto_elegible": round(elegible),
        "lineas": len(filas),
    }


def ultimo_mes_cerrado(
    session: Session, regiones: list[int], hoy: date | None = None
) -> str | None:
    """Último mes anterior al actual con alguna línea `ok` y una oferta ganadora."""
    hoy = hoy or date.today()
    actual = f"{hoy.year:04d}-{hoy.month:02d}"
    meses = {
        _mes(f)
        for (f,) in session.execute(
            select(CompraAgil.fecha_publicacion)
            .join(CaLinea, CaLinea.ca_codigo == CompraAgil.codigo)
            .join(CaCotizacion, CaCotizacion.ca_codigo == CompraAgil.codigo)
            .where(
                CompraAgil.region.in_(regiones),
                CaLinea.estado == "ok",
                CaCotizacion.seleccionado.is_(True),
            )
            .distinct()
        )
        if f
    }
    cerrados = sorted(m for m in meses if m and m < actual)
    return cerrados[-1] if cerrados else None


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f} %"


def informe(session: Session, regiones: list[int], mes: str | None, hoy: date | None = None) -> str:
    hoy = hoy or date.today()
    mes = mes or ultimo_mes_cerrado(session, regiones, hoy)
    desde = date(hoy.year - 1, hoy.month, 1)
    cob = coberturas(session, regiones, desde)
    out = [
        "Juez walk-forward por producto canónico (F-plat-1) — Fuente: Dirección ChileCompra",
        f"Regiones: {regiones} · cobertura sobre 12 meses desde {desde:%Y-%m}",
        "",
        f"(a) Cobertura con detalle propio: {_pct(cob['cobertura_a'])}   (meta ≥ {META_COBERTURA * 100:.0f} %)",
        f"(b) Cobertura total:              {_pct(cob['cobertura_b'])}   (sin meta)",
        f"    líneas: {cob['lineas']} · monto elegible ${cob['monto_elegible']:,} de ${cob['monto_total']:,}".replace(
            ",", "."
        ),
    ]
    if mes is None:
        out += ["", "Sin mes cerrado con líneas `ok` y oferta ganadora: no hay error que medir."]
        return "\n".join(out)
    a3, m3 = map(int, _meses_antes(mes, 3)[-1].split("-"))
    obs = observaciones(session, regiones, date(a3, m3, 1))
    wf = walk_forward(obs, mes)
    nombres: dict[int, str] = {
        i: n for i, n in session.execute(select(ProductoCanonico.id, ProductoCanonico.nombre)).tuples()
    }
    out += [
        "",
        f"Mes evaluado: {mes} — líneas con precio ganador: {wf['lineas_mes']}, evaluadas: {wf['evaluadas']}, "
        f"sin base (< {MIN_OBS} obs.): {wf['sin_base']}",
        f"Error mediano: {_pct(wf['error_mediano'])}   (meta ≤ {META_ERROR * 100:.0f} %)",
        f"Dentro de ±{TOL * 100:.0f} %: {_pct(wf['dentro_20'])} · niveles usados: {wf['niveles']}",
        "",
        "Los 10 productos que más fallan (n, error mediano, fuera de ±20 %):",
    ]
    peores = sorted(wf["por_producto"].items(), key=lambda kv: (-kv[1][2], -kv[1][1]))[:10]
    for pid, (n, med, fallas) in peores:
        out.append(f"  {nombres.get(pid, pid)}: n={n}, error mediano {med:.0%}, fallas {fallas}")
    ok_a = cob["cobertura_a"] is not None and cob["cobertura_a"] >= META_COBERTURA
    ok_e = wf["error_mediano"] is not None and wf["error_mediano"] <= META_ERROR
    out += [
        "",
        f"Meta (a) ≥ 70 %: {'CUMPLE' if ok_a else 'NO cumple'} · error ≤ 25 %: {'CUMPLE' if ok_e else 'NO cumple'}",
    ]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--mes", default=None, help="AAAA-MM (default: último mes cerrado)")
    parser.add_argument("--region", type=int, action="append", default=None)
    args = parser.parse_args()

    from sqlalchemy import create_engine

    from app.core.db import normalizar_url_driver
    from app.core.settings import get_settings
    from app.mercado.territorio import regiones_activas

    settings = get_settings()
    engine = create_engine(normalizar_url_driver(settings.database_url), pool_pre_ping=True)
    with Session(engine) as session:
        regiones = args.region or regiones_activas(session, settings)
        print(informe(session, regiones, args.mes))


if __name__ == "__main__":
    main()
