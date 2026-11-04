"""Genera app/catalogos/canasta_alimentos.py desde el Excel de la canasta y las reglas (F-plat-1).

Solo lectura de `data/mercado/canasta_borrador_candidatos.xlsx` y `scripts/mercado/canasta_reglas.py`.
Semilla = filas con `decision_boris = entra`; si Boris aún no llenó la columna, las de
`propuesta = entra`. Nombre = `nombre_final_boris` si existe, si no `producto`. Las reglas (regex)
se copian junto a cada producto: `app/` no importa de `scripts/`.

Uso (desde la raíz): python scripts/mercado/generar_canasta_alimentos.py

Fuente: Dirección ChileCompra (datos abiertos).
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from canasta_reglas import K, R
from openpyxl import load_workbook

from app.catalogos.mercado import norm

XLSX = "data/mercado/canasta_borrador_candidatos.xlsx"
SALIDA = "app/catalogos/canasta_alimentos.py"
# Mismo orden de prioridad que scripts/mercado/canasta_borrador.py.
PRI = {"Nutrición": 0, "Preparados": 1, "Pastelería": 2, "Panadería": 3}

hoja = load_workbook(XLSX, read_only=True)["candidatos"]
filas = list(hoja.iter_rows(values_only=True))
cab = filas[0]
cand = [dict(zip(cab, f, strict=False)) for f in filas[1:] if f[0] is not None]
hay_decision = any(c.get("decision_boris") for c in cand)


def _entra(c: dict) -> bool:
    if hay_decision:
        return str(c.get("decision_boris") or "").strip().lower() == "entra"
    return str(c.get("propuesta") or "").strip().lower() == "entra"


def clave_de(nombre: str) -> str:
    """Clave estable de la regla: slug del nombre del borrador (no cambia con `nombre_final_boris`)."""
    return re.sub(r"[^a-z0-9]+", "-", norm(nombre)).strip("-")[:100]


por_nombre = {c["producto"]: c for c in cand}
reglas = sorted(R, key=lambda x: PRI.get(x[0], 9))
nombres_regla = [r[1] for r in reglas]
assert len(set(nombres_regla)) == len(nombres_regla), "producto repetido en las reglas"
assert len({clave_de(n) for n in nombres_regla}) == len(nombres_regla), "clave repetida"

out: list[str] = [
    '"""Canasta de alimentos: semilla de `producto_canonico` y reglas por palabra clave (F-plat-1).',
    "",
    "GENERADO por scripts/mercado/generar_canasta_alimentos.py desde el borrador de canasta",
    f"({'decision_boris = entra' if hay_decision else 'propuesta = entra, top 80 por monto'}) y",
    "scripts/mercado/canasta_reglas.py. No editar a mano: lo que se corrija en `/catalogo` manda y la",
    "semilla solo inserta lo que falta. `REGLAS` va en orden de prioridad (gana la primera que calza);",
    "`sembrar=False` = producto que no entra a la canasta (reserva o genérico): sirve para que su",
    "texto no caiga en otro producto, pero no se crea.",
    "",
    "Fuente: Dirección ChileCompra (datos abiertos).",
    '"""',
    "",
    "from __future__ import annotations",
    "",
    "from typing import NamedTuple",
    "",
    "",
    "class ReglaProducto(NamedTuple):",
    "    categoria: str",
    "    nombre: str",
    "    formato: str",
    "    unidad_base: str  # kg | L | u",
    "    patron: str  # regex sobre texto normalizado (minúsculas, sin tildes)",
    "    sembrar: bool",
    "    unspsc: str",
    "    clave: str  # slug del nombre ORIGINAL de la regla: estable aunque se renombre el producto",
    "",
    "",
    "class ReglaKit(NamedTuple):",
    "    categoria: str  # Kit | Servicio | Anexo",
    "    descripcion: str",
    "    patron: str",
    "",
    "",
    "REGLAS: tuple[ReglaProducto, ...] = (",
]
sembrados = 0
for cat, nombre, formato, base, patron in reglas:
    c = por_nombre.get(nombre)
    sembrar = bool(c) and _entra(c)
    final = (c.get("nombre_final_boris") or "").strip() if c else ""
    if sembrar:
        sembrados += 1
        nombre_sem = final or nombre
    else:
        nombre_sem = nombre
    unspsc = str((c or {}).get("unspsc") or "")
    out.append(
        f"    ReglaProducto({cat!r}, {nombre_sem!r}, {formato!r}, {base!r}, {patron!r}, "
        f"{sembrar}, {unspsc!r}, {clave_de(nombre)!r}),"
    )
out += [")", "", "KITS: tuple[ReglaKit, ...] = ("]
for cat, desc, patron in K:
    out.append(f"    ReglaKit({cat!r}, {desc!r}, {patron!r}),")
out += [")", ""]
with open(SALIDA, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(out))
print(f"{SALIDA}: {len(reglas)} reglas, {sembrados} para sembrar, {len(K)} de kits/anexos")
