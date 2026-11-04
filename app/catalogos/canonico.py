"""Normalización por reglas de textos de productos de Compra Ágil (F-plat-1). Puro: sin DB ni red.

Sin modelo de lenguaje: alias aprendidos + palabras clave (`app.catalogos.canasta_alimentos`).
Las funciones reciben el catálogo ya cargado (`Catalogo`) en vez de consultar la DB, así se
prueban con tablas de textos reales. La carga desde Postgres vive en `app.mercado.catalogo`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

from app.catalogos.canasta_alimentos import KITS, REGLAS
from app.catalogos.mercado import norm

# Umbrales del prompt F-plat-1 §1.2.
CONF_OK = 0.8
CONF_DUDOSA = 0.5
CONF_EXACTO = 1.0
CONF_CONTENIDO = 0.8
CONF_REGLA = 0.5

LARGO_ALIAS = 300  # producto_alias.texto_normalizado
LARGO_TEXTO_ORIGEN = 1000  # ca_lineas.texto_origen
MONTO_MIN_SIN_DETALLE = 100_000

# --------------------------------------------------------------------------------------
# Texto
# --------------------------------------------------------------------------------------
_UNIDAD_MEDIDA = r"kilogramos?|kilos?|kgs?|gramos?|grs?|gr|g|mililitros?|ml|cc|litros?|lts?|lt|l"
# Número que no es parte de otro ni va pegado a una unidad de medida. Tras `normalizar_texto` la
# única coma que queda es la decimal: `(?![\d,])` evita cortar "1,5" en "1" + ",5".
_CANT_SUELTA = re.compile(rf"(?<![\d,])\d+(?:,\d+)?(?![\d,])(?!\s*(?:{_UNIDAD_MEDIDA})\b)")


def normalizar_texto(s: str | None) -> str:
    """Clave de alias: minúsculas, sin tildes ni signos, espacios colapsados y sin cantidades
    sueltas ("50 tortas de chocolate" → "tortas de chocolate"). Se conservan las medidas
    pegadas a su unidad ("400 g", "1,5 l"): distinguen formatos. Tope `LARGO_ALIAS`."""
    t = norm(s)
    t = re.sub(r"(?<=\d)[.,](?=\d)", "_", t)  # protege el separador decimal
    t = re.sub(r"[^a-z0-9_]+", " ", t).replace("_", ",")
    t = _CANT_SUELTA.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()[:LARGO_ALIAS].strip()


# --------------------------------------------------------------------------------------
# Formato
# --------------------------------------------------------------------------------------
_PESO = {"kilogramo": 1.0, "kilo": 1.0, "kg": 1.0, "gramo": 0.001, "gr": 0.001, "g": 0.001}
_VOLUMEN = {"mililitro": 0.001, "ml": 0.001, "cc": 0.001, "litro": 1.0, "lt": 1.0, "l": 1.0}
_MEDIDA = re.compile(rf"(?<![a-z0-9])(\d+(?:[.,]\d+)*)\s*({_UNIDAD_MEDIDA})\b")
_MEDIDA_SOLA = re.compile(r"\b(kilos?|kilogramos?|litros?)\b")
_ENVASE = r"(?:pack|caja|bolsa|paquete|set|display|bandeja|estuche|malla|saco|bidon|tarro)s?"
# "6 x 200 ml": el multiplicador va pegado a la medida.
_MULT_ANTES = re.compile(
    r"(?<![a-z0-9])(\d{1,3})\s*x\s*(?=\d+(?:[.,]\d+)?\s*(?:" + _UNIDAD_MEDIDA + r")\b)"
)
_MULT_ENVASE = re.compile(
    rf"\b{_ENVASE}\s*(?:de|con|x)?\s*(\d{{1,3}})\b(?!\s*(?:[.,]\d|{_UNIDAD_MEDIDA}\b))"
)
_MULT_X = re.compile(r"(?<![a-z0-9])x\s*(\d{1,3})\b(?!\s*(?:[.,]\d|" + _UNIDAD_MEDIDA + r"\b))")
_DOCENA = re.compile(r"\bdocena\b")
_MAX_MULT = 500


def _numero(txt: str) -> float | None:
    """'1,5' / '1.5' → 1.5; '1.000' / '2.500' (miles) → 1000 / 2500."""
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", txt):
        txt = txt.replace(".", "")
    txt = txt.replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        return None


def _clase_unidad(u: str) -> tuple[str, float, str] | None:
    """(unidad canónica, factor a la base, base) de la unidad escrita en el texto."""
    raiz = u.rstrip("s") if u not in ("cc",) else u
    if raiz in ("kilogramo", "kilo", "kg"):
        return "kg", 1.0, "kg"
    if raiz in ("gramo", "gr", "g"):
        return "g", 0.001, "kg"
    if raiz in ("mililitro", "ml", "cc"):
        return "ml", 0.001, "L"
    if raiz in ("litro", "lt", "l"):
        return "L", 1.0, "L"
    return None


def extraer_formato(s: str | None) -> tuple[float, str, float, str] | None:
    """`(cantidad, unidad, factor_base, unidad_base)` del formato que nombra el texto, o None.

    "400 g" → (400, "g", 0.4, "kg"); "1,5 L" → (1.5, "L", 1.5, "L"); "caja de 12 unidades" →
    (12, "u", 12, "u"); "pack 6 x 200 ml" → (1200, "ml", 1.2, "L"); "docena" → (12, "u", 12, "u").
    `cantidad` es el total del envase en `unidad`; `factor_base` lo expresa en la unidad base.
    "o similar" y el resto del texto se ignoran. Un número sin unidad ("50 tortas") no es formato.
    """
    t = norm(s)
    if not t:
        return None
    mult = 1
    m_mult = _MULT_ANTES.search(t) or _MULT_ENVASE.search(t) or _MULT_X.search(t)
    if m_mult:
        mult = int(m_mult.group(1))
    elif _DOCENA.search(t):
        mult = 12
    if not 1 <= mult <= _MAX_MULT:
        mult = 1
        m_mult = None

    m = _MEDIDA.search(t)
    q: float | None = None
    clase: tuple[str, float, str] | None = None
    if m:
        q = _numero(m.group(1))
        clase = _clase_unidad(m.group(2))
    else:
        sola = _MEDIDA_SOLA.search(t)
        if sola:
            q, clase = 1.0, _clase_unidad(sola.group(1))
    if q and clase and q > 0:
        total = q * mult
        factor = total * clase[1]
        if 0.0005 <= factor <= 5000:
            return total, clase[0], round(factor, 9), clase[2]
    if (m_mult or _DOCENA.search(t)) and mult > 1:
        return float(mult), "u", float(mult), "u"
    return None


# --------------------------------------------------------------------------------------
# Compras sin detalle
# --------------------------------------------------------------------------------------
_SIN_DETALLE = re.compile(
    r"segun (anexo|detalle|listado|documento|adjunto|planilla|excel)"
    r"|\badjunt|\banexo\b|detalle en (excel|planilla|archivo|documento)"
    r"|ver (documento|archivo|adjunto|anexo|planilla)|\blistado\b|orden de pedido"
)


def detectar_sin_detalle(
    texto: str | None, cantidad: float | None = None, monto: float | None = None
) -> bool:
    """¿El texto remite a un anexo en vez de listar productos ("ALIMENTOS SEGÚN ANEXO ADJUNTO")?

    Heurística [I]: palabra clave Y cantidad 1 Y monto > $100.000. Si no se conoce la
    cantidad o el monto, esa condición no bloquea; si se conocen y no calzan (cantidad > 1 o
    monto bajo) no es anexo: una línea de "20 cajas según anexo" por $30.000 es una compra chica.
    """
    if not _SIN_DETALLE.search(norm(texto)):
        return False
    if cantidad is not None and cantidad > 1:
        return False
    return not (monto is not None and monto <= MONTO_MIN_SIN_DETALLE)


# --------------------------------------------------------------------------------------
# Catálogo en memoria y sugerencia
# --------------------------------------------------------------------------------------
class AliasInfo(NamedTuple):
    producto_id: int
    formato_id: int | None
    confianza: float


@dataclass(frozen=True)
class ProductoInfo:
    id: int
    nombre: str
    unidad_base: str
    clave_semilla: str | None = None  # la regla de la canasta se resuelve por esta clave
    formato_base_id: int | None = None  # el formato "unidad base" (factor 1)
    formatos: tuple[tuple[int, float], ...] = ()  # (formato_id, factor_base)


@dataclass
class Catalogo:
    """Foto del catálogo para sugerir: solo productos activos. Se arma en `app.mercado.catalogo`."""

    productos: dict[int, ProductoInfo] = field(default_factory=dict)
    alias: dict[str, AliasInfo] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._por_clave = {p.clave_semilla: p for p in self.productos.values() if p.clave_semilla}
        self._max_tokens = max((len(k.split()) for k in self.alias), default=0)

    def por_clave(self, clave: str) -> ProductoInfo | None:
        return self._por_clave.get(clave)


class Sugerencia(NamedTuple):
    producto_id: int
    formato_id: int | None
    confianza: float
    via: str  # alias | contenido | regla


_REGLAS_C = tuple((r, re.compile(r.patron)) for r in REGLAS)
_KITS_C = tuple((k, re.compile(k.patron)) for k in KITS)


def estado_por_confianza(confianza: float | None) -> str:
    """ok (≥ 0,8) | dudosa (0,5–0,8) | pendiente (< 0,5 o sin sugerencia)."""
    if confianza is None or confianza < CONF_DUDOSA:
        return "pendiente"
    return "ok" if confianza >= CONF_OK else "dudosa"


def formato_de_texto(producto: ProductoInfo, texto: str | None) -> int | None:
    """Formato del producto que calza con el que nombra el texto (misma base y factor)."""
    f = extraer_formato(texto)
    if f is None or f[3] != producto.unidad_base:
        return None
    for formato_id, factor in producto.formatos:
        if abs(factor - f[2]) <= 1e-6 * max(1.0, factor):
            return formato_id
    return None


def _alias_contenido(t: str, catalogo: Catalogo) -> AliasInfo | None:
    """El alias más largo que aparece como frase completa dentro del texto."""
    tokens = t.split()
    tope = min(catalogo._max_tokens, len(tokens))
    for n in range(tope, 0, -1):
        for i in range(len(tokens) - n + 1):
            frase = " ".join(tokens[i : i + n])
            if n == 1 and len(frase) < 4:
                continue
            info = catalogo.alias.get(frase)
            if info is not None and info.producto_id in catalogo.productos:
                return info
    return None


def sugerir_producto(
    texto: str | None, unspsc: str | None, catalogo: Catalogo
) -> Sugerencia | None:
    """Producto y formato más probables para un texto, o None.

    1. alias exacto (1,0); 2. alias contenido en el texto (0,8); 3. palabra clave de la
    canasta + familia de alimentos por UNSPSC (0,5). Con `estado_por_confianza`: solo ≥ 0,8
    pasa a `ok` solo; 0,5–0,8 queda `dudosa`. Anexos, kits y servicios no se sugieren.
    """
    t = normalizar_texto(texto)
    if not t:
        return None
    exacto = catalogo.alias.get(t)
    if exacto is not None and exacto.producto_id in catalogo.productos:
        return Sugerencia(exacto.producto_id, exacto.formato_id, CONF_EXACTO, "alias")
    contenido = _alias_contenido(t, catalogo)
    if contenido is not None:
        formato = contenido.formato_id
        if formato is None:
            formato = formato_de_texto(catalogo.productos[contenido.producto_id], texto)
        return Sugerencia(contenido.producto_id, formato, CONF_CONTENIDO, "contenido")

    crudo = norm(texto)
    if (unspsc or "").strip() and not (unspsc or "").strip().startswith("50"):
        return None  # fuera de la familia de alimentos de la canasta
    if any(p.search(crudo) for _, p in _KITS_C):
        return None  # kit, servicio o anexo: no es un producto
    categorias = {r.categoria for r, p in _REGLAS_C if p.search(crudo)}
    if len(categorias) >= 3:
        return None  # varios productos en un texto
    for regla, patron in _REGLAS_C:
        if patron.search(crudo):
            producto = catalogo.por_clave(regla.clave)
            if producto is None or not regla.sembrar:
                return None
            return Sugerencia(producto.id, formato_de_texto(producto, texto), CONF_REGLA, "regla")
    return None


def mejor_sugerencia(
    textos: Iterable[str | None], unspsc: str | None, catalogo: Catalogo
) -> Sugerencia | None:
    """La de mayor confianza entre varios textos de la misma línea (el solicitado y los de las
    ofertas, a menudo más específicos: "Nescafé 170 g"). Empata el primero."""
    mejor: Sugerencia | None = None
    for texto in textos:
        s = sugerir_producto(texto, unspsc, catalogo)
        if s is not None and (mejor is None or s.confianza > mejor.confianza):
            mejor = s
    return mejor
