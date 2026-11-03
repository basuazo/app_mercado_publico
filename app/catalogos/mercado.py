"""Grupos de productos intermediables de Compra Ágil (piloto Ñuble), por prefijo UNSPSC.

Vienen del observatorio (`scripts/mercado/mercado_observatorio.py`, peldaño 1 del pivote),
que ahora los importa de acá: la ingesta de mercado (F-merc-1) los usa para decidir qué
`productos_cotizados` guarda (prompt F-merc-1 §1.5: sobre 120 MB estimados, solo los de CA
con algún producto en estos grupos). F-merc-2 mueve acá también la normalización.
"""

from __future__ import annotations

import re
import unicodedata

GRUPOS: dict[str, str] = {
    "50": "Alimentos envasados y abarrotes",
    "1012": "Alimento para mascotas",
    "43": "Computación y telecom", "44": "Oficina", "14": "Papel",
    "56": "Muebles", "52": "Electrodomésticos y menaje", "48": "Cocina comercial",
    "53": "Vestuario y cuidado personal", "47": "Aseo", "49": "Deporte y recreación",
    "46": "Seguridad y protección", "39": "Iluminación y eléctricos", "45": "Audiovisual",
    "27": "Herramientas", "60": "Material educativo y de arte",
    "31": "Ferretería y pintura", "4014": "Gasfitería", "4017": "Gasfitería",
    "2517": "Neumáticos y repuestos", "24": "Bodega y manejo de carga",
    "4219": "Ortopedia y ayudas técnicas", "4221": "Ortopedia y ayudas técnicas",
    "4224": "Ortopedia y ayudas técnicas",
}  # fmt: skip
EXCLUIR = ("5021",)  # tabaco
# Alimentos preparados (por nombre genérico): se reportan aparte para decidir si quedan fuera.
_PREPARADO = re.compile(
    r"empanada|sandwich|canape|coctel|tarta|pies? |comida|colacion|almuerzo|plato preparad"
)
G_PREP = "Alimentos preparados (revisar)"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower()).strip()


def grupo_de(unspsc: str | None, nombre_generico: str | None = "") -> str | None:
    """Grupo intermediable de un código UNSPSC de 8 dígitos, o None si queda fuera."""
    u = (unspsc or "").strip()
    if len(u) != 8 or not u.isdigit() or u.startswith(EXCLUIR):
        return None
    g = GRUPOS.get(u[:4]) or GRUPOS.get(u[:2])
    if g == GRUPOS["50"] and _PREPARADO.search(_norm(nombre_generico or "") + " "):
        return G_PREP
    return g
