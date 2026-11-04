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


# ---------------------------------------------------------------------------
# Alimentos (F-merc-2: movido desde el observatorio, que ahora lo importa)
# ---------------------------------------------------------------------------
FAMILIAS_ALIMENTOS = {
    "5010": "Frutas, verduras y frutos secos", "5011": "Carnes y aves", "5012": "Pescados y mariscos",
    "5013": "Lácteos y huevos", "5015": "Aceites y grasas", "5016": "Chocolates, azúcar y confites",
    "5017": "Condimentos y salsas", "5018": "Panadería y galletas", "5019": "Alimentos preparados y conservas",
    "5020": "Bebidas, jugos y agua", "5022": "Cereales, legumbres y harinas", "5030": "Frutas frescas",
    "5040": "Verduras frescas", "1012": "Alimento para mascotas",
}  # fmt: skip
G_ALIM = {GRUPOS["50"], G_PREP}


def familia_alimento(unspsc: str) -> str:
    f = (unspsc or "")[:4]
    return FAMILIAS_ALIMENTOS.get(f, f"Otros alimentos ({f})")


# ---------------------------------------------------------------------------
# Normalización de textos, RUT, números y precios (movido desde el observatorio)
# ---------------------------------------------------------------------------
def norm(s: str | None) -> str:
    return _norm(s or "")


def rut(s: str | None) -> str:
    """RUT normalizado sin puntos: 76942549-7. En oc-da el RUT del proveedor es RutSucursal [V 31-oct]."""
    s = (s or "").replace(".", "").replace(" ", "").upper()
    return s if "-" in s and s.split("-")[0].isdigit() else ""


def es_nuble(region: str | None) -> bool:
    return "uble" in norm(region)


def num(s: str | None) -> float | None:
    s = (s or "").strip()
    if not s or s.upper() == "NA":
        return None
    # 1.234,5 → 1234.5
    s = s.replace(".", "").replace(",", ".") if "," in s and "." in s else s.replace(",", ".")
    try:
        return float(s)  # acepta notación científica (7e+05), vista en COT_
    except ValueError:
        return None


_FMT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|kgs|kilo|kilos|gr|grs|g|gramos|ml|cc|lt|lts|l|litro|litros)\b")
_PACK = re.compile(r"\b(\d{1,3})\s*(?:x|un\b|u\b|unid|unidades)")
_A_KG = {"kg": 1, "kgs": 1, "kilo": 1, "kilos": 1, "gr": .001, "grs": .001, "g": .001, "gramos": .001}
_A_L = {"ml": .001, "cc": .001, "lt": 1, "lts": 1, "l": 1, "litro": 1, "litros": 1}
_U_FIS = {"kilogramo": ("kg", 1.0), "litro": ("L", 1.0), "gramo": ("kg", .001)}
_U_ENVASE = {"unidad", "paquete", "botella", "bolsa", "tarro", "pack", "frasco", "caja", "envase", "sobre", "lata"}


def normalizar(linea: dict[str, str]) -> tuple[str, float] | None:
    """(clave, precio por unidad base) o None si no se puede comparar.

    `linea` usa los nombres de columna de oc-da (`precioNeto`, `UnidadMedida`, ...).
    """
    precio = num(linea.get("precioNeto", ""))
    if not precio or precio <= 0:
        return None
    um = norm(linea.get("UnidadMedida", ""))
    unspsc = linea.get("codigoProductoONU", "")
    if um in _U_FIS:
        base, f = _U_FIS[um]
        return f"{unspsc}|{base}", precio / f
    crudo = " ".join([linea.get("EspecificacionComprador", ""), linea.get("EspecificacionProveedor", "")])
    if crudo.count("\n") >= 3 or (um in ("global", "unidad no definida") and len(crudo) > 300):
        return None  # canasta entera en una línea: no comparable
    txt = norm(crudo)
    if unspsc.startswith("50") and um in _U_ENVASE:
        m = _FMT.search(txt)
        if m:
            q, u = num(m.group(1)) or 0, m.group(2)
            base, cant = ("kg", q * _A_KG[u]) if u in _A_KG else ("L", q * _A_L[u])
            p = _PACK.search(txt)
            if um in ("caja", "pack") and p:
                cant *= int(p.group(1))
            if 0.005 <= cant <= 50:
                return f"{unspsc}|{base}", precio / cant
        return None
    if not unspsc.startswith("50") and um in ("unidad", "unidad no definida"):
        return f"{unspsc}|u", precio
    return None


def moneda_codigo(texto: str | None) -> str:
    """Código de moneda de `TipoMonedaOC`: CLP, CLF, UTM, USD o el texto en mayúsculas.

    El formato exacto de la columna es [I] hasta la primera carga real; vacío o "NA" = CLP.
    """
    t = norm(texto)
    if t in ("", "na", "clp") or "peso" in t:
        return "CLP"
    if t in ("clf", "uf") or "fomento" in t:
        return "CLF"
    if t == "utm" or "mensual" in t:
        return "UTM"
    if t == "usd" or "dolar" in t:
        return "USD"
    return t.upper()[:10]


def codigo_oc_normalizado(codigo: str | None) -> str:
    """Código de OC comparable entre `ca_cotizaciones.id_oc` (API) y `oc_lineas_ca.codigo_oc` (oc-da).

    Hoy solo recorta espacios y pasa a mayúsculas: el formato real de ambos [I] se contrasta
    con datos reales tras la primera noche del backfill (F-merc-2 §6). Único lugar donde
    cambiarlo si difieren.
    """
    return (codigo or "").strip().upper()
