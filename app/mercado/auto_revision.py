"""Confirmación automática de líneas de anexo por excepción (F-plat-auto-revision).

Hasta esta fase toda línea leída de un anexo quedaba `dudosa` y la confirmaba una persona. Ahora
cada línea recibe un puntaje (0–1) con señales independientes del modelo que la leyó; si alcanza
`AUTO_CONFIRMAR_UMBRAL` pasa sola a `ok` con `confirmada_por = 'auto'`, y el equipo revisa solo
la cola de excepciones (`/catalogo/revision`).

- :func:`puntaje_linea` es PURA (sin base ni red): suma pesos de las señales de
  :class:`Senales`. Se prueba con casos de cada señal y la calibra
  `scripts/mercado/calibrar_auto_revision.py` sobre el set de evaluación revisado.
- :func:`preparar` lee lo que hace falta (catálogo, líneas de la API de la CA, historial de
  cantidades) ANTES de escribir; si a alguna línea le falta solo el acuerdo de dos lecturas para
  llegar al umbral, :func:`completar_con_segunda_lectura` llama una vez al modelo de respaldo
  (controla el costo: solo cuando las señales 1–3 no bastan; la caché de `llm_cache` evita pagar
  dos veces).
- :func:`aplicar` deja `ok` las que alcanzan el umbral; si TODAS las líneas del anexo quedan
  `ok`, la CA queda «anexo resuelto» (las de la API pasan a `reemplazada`, igual que con
  confirmación humana: F-plat-anexos-2).
- Control: :func:`muestrear_control` manda a la cola el 5 % de lo auto-confirmado de cada
  semana (determinista: orden por sha256 de semilla + id). Lo que una persona corrige ahí cuenta
  como error del auto (:func:`registrar_control`, tabla `auto_control_semana`). Si la precisión
  observada baja de 95 % dos semanas seguidas, :func:`evaluar_subida` sube el umbral (persistido
  en `sync_state`, regla 10) y avisa por log WARNING y en /salud. Nunca lo baja.

Nada de esto toca líneas ya `ok` por una persona ni las de la API: solo las recién leídas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.catalogos.canonico import (
    Catalogo,
    _productos_en_texto,
    normalizar_texto,
    sugerencia_unica,
    sugerir_producto,
)
from app.catalogos.mercado import norm
from app.clients.types import ExtraccionAnexo, LineaExtraida
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.models.tables import (
    AutoControlSemana,
    CaLinea,
    CaProducto,
    CompraAgil,
    ProductoCanonico,
    SyncState,
)

_log = logging.getLogger(__name__)

AUTO = "auto"
HUMANO = "humano"
FUENTE_ESTADO = "auto_revision"  # fila de `sync_state` con el umbral subido por el job
PCT_CONTROL = 0.05  # §3: el 5 % de lo auto-confirmado de la semana va a la cola de control
SEMILLA_CONTROL = "F-plat-auto-revision"
PRECISION_OBJETIVO = 0.98  # §2: la calibración elige el umbral más bajo que la cumple
PRECISION_MINIMA = 0.95  # §3: dos semanas seguidas bajo esto → el job sube el umbral
PASO_SUBIDA = 0.05
MIN_AUTO_CALIBRACION = 20  # bajo esto, un 100 % de precisión no dice nada

# Señal 3: cantidad plausible si cae en [p05 / 3, p95 × 3] de las cantidades base confirmadas del
# mismo producto (con al menos `MIN_HISTORIAL` observaciones; si no, la señal no opina).
MIN_HISTORIAL = 5
HOLGURA_HISTORIAL = 3.0
# Señal 2: «cantidad del mismo orden» que la línea de la API del mismo producto.
RAZON_API = 3.0
# Señal 2: el monto del anexo (Σ precio × cantidad) cuadra con el presupuesto ± 15 %.
TOLERANCIA_MONTO = 0.15
# Señal 4: dos lecturas «coinciden» si el texto se parece (Jaccard de tokens) y la cantidad y la
# unidad son las mismas.
SIMILITUD_ACUERDO = 0.5

# Pesos de :func:`puntaje_linea`. La suma de los positivos supera 1 (se recorta): ninguna señal
# sola basta para un umbral alto, y las contradicciones restan. Los calibra el script del §2:
# lo que se fija en settings es el umbral, no los pesos.
PESOS: dict[str, float] = {
    # 1. Catálogo canónico
    "cat_alias": 0.35,
    "cat_contenido": 0.25,
    "cat_regla": 0.10,
    "cat_unica": 0.05,
    "cat_formato": 0.05,
    # 3. Cantidad y unidad
    "unidad": 0.05,
    "plausible": 0.05,
    "implausible": -0.20,
    # 2. Coherencia con la API
    "api_calza": 0.10,
    "api_cantidad": 0.05,
    "api_no_calza": -0.15,
    "monto_cuadra": 0.05,
    "monto_descuadra": -0.10,
    # 4. Dos lecturas
    "acuerdo": 0.20,
    "desacuerdo": -0.30,
    # 5. Tipo de documento y calidad de la extracción
    "doc_docx-xlsx": 0.10,
    "doc_texto": 0.07,
    "doc_escaneado": 0.0,
    "paginas_omitidas": -0.10,
    "respaldo": -0.05,
}

TIPOS_DOC = ("texto", "escaneado", "docx-xlsx")
# Unidades de pedido que se entienden aunque no sean kg/L/u (envases).
_ENVASES = frozenset(
    ["caja", "bolsa", "paquete", "botella", "tarro", "frasco", "malla", "saco", "bandeja", "display", "pack", "docena", "sobre", "lata", "bidon", "pote", "galon", "envase", "rollo", "set", "kit", "fardo", "tableta", "barra", "porcion", "racion"]
)


# ---------------------------------------------------------------------------
# Puntaje (puro)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Senales:
    """Lo que se sabe de UNA línea leída de un anexo. `None` = la señal no opina."""

    producto_id: int | None = None
    confianza: float | None = None  # de `sugerir_producto`
    via: str | None = None  # alias | contenido | regla
    sugerencia_unica: bool = False
    formato_coherente: bool = False
    cantidad: float | None = None
    unidad_reconocida: bool = False
    plausible: bool | None = None
    api_calza: bool | None = None
    api_cantidad_orden: bool | None = None
    monto_cuadra: bool | None = None
    acuerdo: bool | None = None
    tipo_doc: str = "texto"
    paginas_omitidas: bool = False
    respaldo: bool = False


@dataclass(frozen=True)
class Puntaje:
    valor: float
    motivos: tuple[str, ...] = ()


def puntaje_linea(s: Senales) -> Puntaje:
    """Puntaje 0–1 de una línea. Sin producto o sin cantidad → 0: una línea así no se puede
    confirmar sola (no sirve a la oferta ni al lote)."""
    if s.producto_id is None:
        return Puntaje(0.0, ("sin producto",))
    if s.cantidad is None or s.cantidad <= 0:
        return Puntaje(0.0, ("sin cantidad",))
    motivos: list[str] = []
    total = 0.0

    def suma(clave: str) -> None:
        nonlocal total
        total += PESOS[clave]
        motivos.append(clave)

    if s.via in ("alias", "contenido", "regla"):
        suma(f"cat_{s.via}")
    if s.sugerencia_unica:
        suma("cat_unica")
    if s.formato_coherente:
        suma("cat_formato")
    if s.unidad_reconocida:
        suma("unidad")
    if s.plausible is True:
        suma("plausible")
    elif s.plausible is False:
        suma("implausible")
    if s.api_calza is True:
        suma("api_calza")
    elif s.api_calza is False:
        suma("api_no_calza")
    if s.api_cantidad_orden is True:
        suma("api_cantidad")
    if s.monto_cuadra is True:
        suma("monto_cuadra")
    elif s.monto_cuadra is False:
        suma("monto_descuadra")
    if s.acuerdo is True:
        suma("acuerdo")
    elif s.acuerdo is False:
        suma("desacuerdo")
    suma(f"doc_{s.tipo_doc if s.tipo_doc in TIPOS_DOC else 'escaneado'}")
    if s.paginas_omitidas:
        suma("paginas_omitidas")
    if s.respaldo:
        suma("respaldo")
    return Puntaje(round(min(1.0, max(0.0, total)), 4), tuple(motivos))


def alcanza_con_acuerdo(s: Senales, umbral: float) -> bool:
    """¿Le falta SOLO el acuerdo de dos lecturas para llegar al umbral? (§1.4: el segundo modelo
    se llama solo entonces.)"""
    if s.acuerdo is not None:
        return False
    base = puntaje_linea(s).valor
    return base < umbral <= puntaje_linea(replace(s, acuerdo=True)).valor


def tipo_documento(mime: str | None, escaneado: bool) -> str:
    """texto | escaneado | docx-xlsx (las mismas clases que `evaluar_extraccion.py`)."""
    from app.mercado.anexos import DOCX, XLSX

    if mime in (DOCX, XLSX):
        return "docx-xlsx"
    return "escaneado" if escaneado else "texto"


def unidad_reconocida(unidad: str | None) -> bool:
    from app.mercado.proveedores import normalizar_unidad

    if normalizar_unidad(unidad) is not None:
        return True
    t = norm(unidad).strip().rstrip(".")
    return bool(t) and (t in _ENVASES or t.rstrip("s") in _ENVASES or t[:-2] in _ENVASES)


def _unidad_comparable(unidad: str | None) -> str:
    from app.mercado.proveedores import normalizar_unidad

    return normalizar_unidad(unidad) or norm(unidad).strip().rstrip(".s") or "u"


def _tokens(s: str) -> set[str]:
    return set(normalizar_texto(s).split())


def similitud(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _misma_cantidad(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 0.01 * max(1.0, abs(a))


def acuerdo_lecturas(
    principal: Sequence[LineaExtraida], segunda: Sequence[LineaExtraida]
) -> list[bool]:
    """Por cada línea de la lectura principal: ¿la segunda lectura trae una (no usada) con texto
    parecido, la misma cantidad y la misma unidad? Puro."""
    from app.mercado.anexos import texto_de_linea

    textos2 = [texto_de_linea(x) for x in segunda]
    usadas: set[int] = set()
    out: list[bool] = []
    for ln in principal:
        t1 = texto_de_linea(ln)
        mejor, idx = 0.0, -1
        for i, t2 in enumerate(textos2):
            if i in usadas:
                continue
            s = similitud(t1, t2)
            if s > mejor:
                mejor, idx = s, i
        if idx < 0 or mejor < SIMILITUD_ACUERDO:
            out.append(False)
            continue
        otra = segunda[idx]
        ok = _misma_cantidad(ln.cantidad, otra.cantidad) and _unidad_comparable(
            ln.unidad
        ) == _unidad_comparable(otra.unidad)
        if ok:
            usadas.add(idx)
        out.append(ok)
    return out


def monto_cuadra(lineas: Sequence[LineaExtraida], presupuesto: float | None) -> bool | None:
    """Si TODAS las líneas traen precio y cantidad, ¿Σ precio × cantidad cuadra con el
    presupuesto de la CA (± `TOLERANCIA_MONTO`)? Si no se puede saber, None."""
    if not presupuesto or presupuesto <= 0 or not lineas:
        return None
    suma = 0.0
    for ln in lineas:
        if ln.precio is None or ln.cantidad is None or ln.precio <= 0 or ln.cantidad <= 0:
            return None
        suma += ln.precio * ln.cantidad
    return abs(suma - presupuesto) <= TOLERANCIA_MONTO * presupuesto


# ---------------------------------------------------------------------------
# Contexto de la CA (lecturas) y señales
# ---------------------------------------------------------------------------


@dataclass
class ContextoCA:
    """Lo que la API y el historial dicen de la CA, para las señales 2 y 3."""

    presupuesto: float | None = None
    hay_api: bool = False  # la API trae productos reales (no solo «según anexo»)
    # producto → (cantidad base, cantidad) de sus líneas de la API (sumadas; None si falta)
    api_productos: dict[int, tuple[float | None, float | None]] = field(default_factory=dict)
    api_familias: set[str] = field(default_factory=set)  # UNSPSC a 4 dígitos
    familia_producto: dict[int, str] = field(default_factory=dict)  # producto → UNSPSC 4 díg.
    rangos: dict[int, tuple[float, float]] = field(default_factory=dict)  # producto → (lo, hi)


def _sumar(a: float | None, b: float | None) -> float | None:
    if b is None or b <= 0:
        return a
    return (a or 0.0) + b


def _familia(unspsc: str | None) -> str | None:
    t = re.sub(r"\D", "", unspsc or "")
    return t[:4] if len(t) >= 4 else None


def contexto_ca(session: Session, codigo: str, productos: set[int]) -> ContextoCA:
    """Lee la CA, sus líneas de la API y el historial de cantidades de `productos`. Solo SELECT."""
    from app.catalogos.canonico import detectar_sin_detalle

    ctx = ContextoCA()
    ca = session.get(CompraAgil, codigo)
    ctx.presupuesto = ca.monto_disponible_clp if ca is not None else None
    for p in session.execute(select(CaProducto).where(CaProducto.ca_codigo == codigo)).scalars():
        texto = f"{p.nombre} {p.descripcion}".strip()
        if detectar_sin_detalle(texto, p.cantidad, ctx.presupuesto):
            continue
        ctx.hay_api = True
        fam = _familia(p.codigo_producto)
        if fam:
            ctx.api_familias.add(fam)
    for pid, base, cant in session.execute(
        select(CaLinea.producto_id, CaLinea.cantidad_base, CaLinea.cantidad).where(
            CaLinea.ca_codigo == codigo,
            CaLinea.origen == "api",
            CaLinea.producto_id.isnot(None),
            CaLinea.estado != "reemplazada",
        )
    ).tuples():
        if pid is not None:
            b0, c0 = ctx.api_productos.get(pid, (None, None))
            ctx.api_productos[pid] = (_sumar(b0, base), _sumar(c0, cant))
    if not productos:
        return ctx
    for pid, unspsc in session.execute(
        select(ProductoCanonico.id, ProductoCanonico.unspsc_sugerido).where(
            ProductoCanonico.id.in_(productos)
        )
    ).tuples():
        fam = _familia(unspsc)
        if fam:
            ctx.familia_producto[pid] = fam
    valores: dict[int, list[float]] = {}
    for pid, base in session.execute(
        select(CaLinea.producto_id, CaLinea.cantidad_base).where(
            CaLinea.producto_id.in_(productos),
            CaLinea.estado == "ok",
            CaLinea.cantidad_base.isnot(None),
            CaLinea.cantidad_base > 0,
            # El historial no se alimenta de sí mismo: fuera lo auto-confirmado.
            or_(CaLinea.confirmada_por.is_(None), CaLinea.confirmada_por != AUTO),
        )
    ).tuples():
        if pid is not None and base is not None:
            valores.setdefault(pid, []).append(float(base))
    for pid, vs in valores.items():
        if len(vs) < MIN_HISTORIAL:
            continue
        vs.sort()
        p05 = vs[int(0.05 * (len(vs) - 1))]
        p95 = vs[math.ceil(0.95 * (len(vs) - 1))]
        ctx.rangos[pid] = (p05 / HOLGURA_HISTORIAL, p95 * HOLGURA_HISTORIAL)
    return ctx


def _cantidad_base(cantidad: float | None, factor: float | None) -> float | None:
    if cantidad is None or factor is None:
        return None
    return cantidad * factor


def senales_de_linea(
    ext: LineaExtraida,
    catalogo: Catalogo,
    ctx: ContextoCA,
    *,
    tipo_doc: str,
    paginas_omitidas: bool = False,
    respaldo: bool = False,
    monto: bool | None = None,
    acuerdo: bool | None = None,
) -> Senales:
    """Señales 1, 2, 3 y 5 de una línea extraída (la 4 la agrega la segunda lectura). Pura dado
    el catálogo y el contexto. El producto y el formato son los mismos que asigna
    `anexos.crear_lineas_de_anexo` (misma función de sugerencia)."""
    from app.mercado.anexos import _sugerir, texto_de_linea
    from app.mercado.proveedores import normalizar_unidad

    texto = texto_de_linea(ext)
    producto_id, formato_id = _sugerir(ext, catalogo)
    cantidad = ext.cantidad if ext.cantidad is not None and ext.cantidad > 0 else None
    base = dict(
        cantidad=cantidad,
        unidad_reconocida=unidad_reconocida(ext.unidad),
        monto_cuadra=monto,
        acuerdo=acuerdo,
        tipo_doc=tipo_doc,
        paginas_omitidas=paginas_omitidas,
        respaldo=respaldo,
    )
    if producto_id is None:
        return Senales(**base)  # type: ignore[arg-type]
    sug = sugerir_producto(texto, None, catalogo)
    producto = catalogo.productos[producto_id]
    unica = sugerencia_unica(texto, None, catalogo) is not None and not (
        sug is not None
        and sug.via == "contenido"
        and _productos_en_texto(texto, catalogo) - {producto_id}
    )
    formato_coherente = formato_id is not None or normalizar_unidad(ext.unidad) == (
        producto.unidad_base
    )
    factor = next((f for i, f in producto.formatos if i == formato_id), None)
    cant_base = _cantidad_base(cantidad, factor)
    plausible: bool | None = None
    rango = ctx.rangos.get(producto_id)
    if rango is not None and cant_base is not None:
        plausible = rango[0] <= cant_base <= rango[1]
    api_calza: bool | None = None
    api_orden: bool | None = None
    if ctx.hay_api:
        fam = ctx.familia_producto.get(producto_id)
        api_calza = producto_id in ctx.api_productos or (
            fam is not None and fam in ctx.api_familias
        )
        api_base, api_cant = ctx.api_productos.get(producto_id, (None, None))
        # En la unidad base si las dos la tienen; si no, las cantidades tal cual (la holgura
        # ×3 absorbe envases parecidos; unidades muy distintas no calzan y la señal no suma).
        if api_base and cant_base:
            api_orden = 1 / RAZON_API <= cant_base / api_base <= RAZON_API
        elif api_cant and cantidad:
            api_orden = 1 / RAZON_API <= cantidad / api_cant <= RAZON_API
    return Senales(
        producto_id=producto_id,
        confianza=sug.confianza if sug else None,
        via=sug.via if sug else None,
        sugerencia_unica=unica,
        formato_coherente=formato_coherente,
        plausible=plausible,
        api_calza=api_calza,
        api_cantidad_orden=api_orden,
        **base,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Configuración y aplicación al extraer
# ---------------------------------------------------------------------------

SegundaLectura = Callable[[str | None, Sequence[tuple[bytes, str]]], ExtraccionAnexo]


@dataclass
class ConfigAuto:
    """`umbral` None = auto-confirmación apagada (todas `dudosa`, como antes de la fase).
    `segunda`: lee el anexo con el modelo de respaldo (señal 4); None = sin segunda lectura."""

    umbral: float | None = None
    segunda: SegundaLectura | None = None
    modelo_segunda: str = ""


def _leer_estado(session: Session) -> dict[str, Any]:
    fila = session.get(SyncState, FUENTE_ESTADO)
    if fila is None or not fila.notas:
        return {}
    try:
        data = json.loads(fila.notas)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _guardar_estado(session: Session, estado: dict[str, Any]) -> None:
    fila = session.get(SyncState, FUENTE_ESTADO)
    if fila is None:
        fila = SyncState(fuente=FUENTE_ESTADO)
        session.add(fila)
    fila.notas = json.dumps(estado, ensure_ascii=False)
    fila.ultima_ejecucion = ahora_utc()


def umbral_efectivo(session: Session, umbral_base: float | None) -> float | None:
    """El de settings, o el que subió el job si es mayor. None si la función está apagada."""
    if umbral_base is None:
        return None
    subido = _leer_estado(session).get("umbral")
    if isinstance(subido, int | float) and not isinstance(subido, bool):
        return max(umbral_base, float(subido))
    return umbral_base


def resolver_config(session: Session, extractor: Any) -> ConfigAuto:
    """Configuración desde el entorno (`AUTO_CONFIRMAR_UMBRAL`, `LLM_MODELO_RESPALDO`) para los
    caminos que no la pasan (carga masiva, API externa, descarga automática). La segunda
    lectura solo existe si el extractor es el cliente real de OpenRouter y hay respaldo."""
    from app.core.settings import get_settings

    try:
        settings = get_settings()
    except Exception:  # settings inválidos: la extracción sigue, sin auto-confirmación
        _log.warning("auto-revisión: no pude leer la configuración; queda apagada", exc_info=True)
        return ConfigAuto()
    return config_desde_settings(session, settings, extractor)


def config_desde_settings(session: Session, settings: Any, extractor: Any) -> ConfigAuto:
    from app.clients.openrouter import OpenRouterClient

    umbral = umbral_efectivo(session, settings.auto_confirmar_umbral)
    if umbral is None:
        return ConfigAuto()
    respaldo = (settings.llm_modelo_respaldo or "").strip()
    if (
        not respaldo
        or not isinstance(extractor, OpenRouterClient)
        or not extractor.habilitado
        or respaldo == extractor.modelo
    ):
        return ConfigAuto(umbral=umbral)
    bind = session.get_bind()
    cfg = settings.model_copy(
        update={"llm_modelo_extraccion": respaldo, "llm_modelo_respaldo": ""}
    )
    cliente = OpenRouterClient(cfg, bind)  # type: ignore[arg-type]

    def segunda(texto: str | None, imagenes: Sequence[tuple[bytes, str]]) -> ExtraccionAnexo:
        return cliente.extraer_lineas(texto, imagenes)

    return ConfigAuto(umbral=umbral, segunda=segunda, modelo_segunda=respaldo)


@dataclass
class Revision:
    """Señales de las líneas útiles de UNA extracción, en el orden en que
    `crear_lineas_de_anexo` crea las `CaLinea`."""

    umbral: float | None
    senales: list[Senales]
    lineas: list[LineaExtraida]
    segunda_leida: bool = False
    # Quedaron páginas sin leer: aunque todo lo leído quede `ok`, faltan filas → la CA NO se
    # marca «anexo resuelto» sola.
    paginas_omitidas: bool = False


def preparar(
    session: Session,
    codigo: str,
    ext: ExtraccionAnexo,
    *,
    mime: str | None,
    escaneado: bool,
    paginas_omitidas: bool,
    config: ConfigAuto,
) -> Revision | None:
    """Señales 1, 2, 3 y 5 de cada línea útil. Solo lecturas: quien llama hace `rollback()`
    después, antes de la segunda lectura (puede tardar). None si la función está apagada."""
    if config.umbral is None:
        return None
    from app.mercado.catalogo import cargar_catalogo

    utiles = [ln for ln in ext.lineas if (ln.texto or "").strip()]
    if not utiles:
        return None
    catalogo = cargar_catalogo(session)
    from app.mercado.anexos import _sugerir

    productos = {p for p, _ in (_sugerir(ln, catalogo) for ln in utiles) if p is not None}
    ctx = contexto_ca(session, codigo, productos)
    tipo = tipo_documento(mime, escaneado)
    monto = monto_cuadra(utiles, ctx.presupuesto)
    senales = [
        senales_de_linea(
            ln,
            catalogo,
            ctx,
            tipo_doc=tipo,
            paginas_omitidas=paginas_omitidas,
            respaldo=ext.respaldo,
            monto=monto,
        )
        for ln in utiles
    ]
    return Revision(
        umbral=config.umbral, senales=senales, lineas=utiles, paginas_omitidas=paginas_omitidas
    )


def completar_con_segunda_lectura(
    rev: Revision | None,
    config: ConfigAuto,
    ext: ExtraccionAnexo,
    texto: str | None,
    imagenes: Sequence[tuple[bytes, str]],
) -> None:
    """Señal 4: si a alguna línea le falta solo el acuerdo para llegar al umbral, lee el anexo
    UNA vez con el modelo de respaldo y marca acuerdo/desacuerdo en todas. Nunca rompe: si la
    segunda lectura falla, la señal no opina. Sin sesión abierta (puede tardar)."""
    from app.clients.openrouter import LLMError

    if rev is None or rev.umbral is None or config.segunda is None:
        return
    if ext.respaldo or (config.modelo_segunda and ext.modelo == config.modelo_segunda):
        return  # la principal ya la leyó el respaldo: «acordar» consigo mismo no prueba nada
    umbral = rev.umbral
    if not any(alcanza_con_acuerdo(s, umbral) for s in rev.senales):
        return
    try:
        otra = config.segunda(texto, imagenes)
    except LLMError as exc:
        _log.warning("auto-revisión: la segunda lectura falló (%s); sin señal de acuerdo", exc)
        return
    if otra.invalida:
        return
    acuerdos = acuerdo_lecturas(rev.lineas, otra.lineas)
    rev.senales = [replace(s, acuerdo=a) for s, a in zip(rev.senales, acuerdos, strict=True)]
    rev.segunda_leida = True


def aplicar(session: Session, ca: CompraAgil, lineas: list[CaLinea], rev: Revision | None) -> int:
    """Guarda el puntaje de cada línea recién creada y deja `ok` (`confirmada_por = 'auto'`) las
    que alcanzan el umbral. Si todas las líneas del anexo quedan `ok`, la CA queda «anexo
    resuelto» (las de la API pasan a `reemplazada`), salvo que hayan quedado páginas sin leer.
    Devuelve cuántas confirmó. Flush, sin commit."""
    if rev is None or rev.umbral is None or len(lineas) != len(rev.senales):
        return 0
    from app.mercado.anexos import resolver_anexo
    from app.mercado.catalogo import recalcular_detalle_lineas

    autos = 0
    for ln, sen in zip(lineas, rev.senales, strict=True):
        p = puntaje_linea(sen)
        ln.puntaje_auto = p.valor
        if (
            p.valor >= rev.umbral
            and ln.producto_id is not None
            and ln.producto_id == sen.producto_id  # el puntaje es de ESE producto
            and ln.cantidad is not None
        ):
            ln.estado = "ok"
            ln.confirmada_por = AUTO
            autos += 1
    session.flush()
    if autos and autos == len(lineas) and not rev.paginas_omitidas:
        resolver_anexo(session, ca)
    elif autos:
        recalcular_detalle_lineas(session, [ca.codigo])
    return autos


# ---------------------------------------------------------------------------
# Semanas, muestreo de control y precisión observada
# ---------------------------------------------------------------------------


def semana_iso(momento: datetime) -> str:
    """«AAAA-Www» de la semana ISO en Chile de un instante UTC naive."""
    local = momento.replace(tzinfo=UTC).astimezone(TZ_CHILE)
    a, s, _ = local.isocalendar()
    return f"{a}-W{s:02d}"


def limites_semana(momento: datetime) -> tuple[datetime, datetime]:
    """[lunes 00:00, lunes siguiente 00:00) en Chile de la semana de `momento`, en UTC naive."""
    local = momento.replace(tzinfo=UTC).astimezone(TZ_CHILE)
    lunes = (local - timedelta(days=local.weekday())).date()
    ini = datetime(lunes.year, lunes.month, lunes.day, tzinfo=TZ_CHILE)
    fin_dia = lunes + timedelta(days=7)
    fin = datetime(fin_dia.year, fin_dia.month, fin_dia.day, tzinfo=TZ_CHILE)
    return (
        ini.astimezone(UTC).replace(tzinfo=None),
        fin.astimezone(UTC).replace(tzinfo=None),
    )


def _orden_control(semilla: str, linea_id: int) -> str:
    return hashlib.sha256(f"{semilla}:{linea_id}".encode()).hexdigest()


def muestrear_control(
    session: Session,
    momento: datetime,
    *,
    ahora: datetime | None = None,
    pct: float = PCT_CONTROL,
    semilla: str = SEMILLA_CONTROL,
) -> int:
    """Manda a la cola de control el `pct` de las líneas auto-confirmadas de la semana de
    `momento` (redondeo hacia arriba). Incremental e idempotente: cuenta las ya muestreadas y
    agrega solo las que faltan, en un orden fijo por sha256(semilla:id) (determinista). Devuelve
    cuántas agregó. Flush, sin commit."""
    ini, fin = limites_semana(momento)
    filas = session.execute(
        select(CaLinea.id, CaLinea.control_en, CaLinea.confirmada_por).where(
            CaLinea.creado_en >= ini,
            CaLinea.creado_en < fin,
            or_(CaLinea.confirmada_por == AUTO, CaLinea.control_en.isnot(None)),
        )
    ).all()
    if not filas:
        return 0
    objetivo = math.ceil(pct * len(filas))
    ya = sum(1 for f in filas if f.control_en is not None)
    faltan = objetivo - ya
    if faltan <= 0:
        return 0
    candidatas = sorted(
        (f.id for f in filas if f.control_en is None and f.confirmada_por == AUTO),
        key=lambda i: _orden_control(semilla, i),
    )[:faltan]
    if not candidatas:
        return 0
    session.execute(
        update(CaLinea)
        .where(CaLinea.id.in_(candidatas))
        .values(control_en=ahora or ahora_utc(), actualizado_en=CaLinea.actualizado_en)
        .execution_options(synchronize_session="fetch")
    )
    session.flush()
    return len(candidatas)


def es_control_pendiente(linea: CaLinea) -> bool:
    return linea.confirmada_por == AUTO and linea.control_en is not None


def registrar_control(session: Session, linea: CaLinea, *, error: bool) -> None:
    """Una persona revisó una línea de control: acierto (sin cambios) o error del auto
    (corregida o eliminada). Cuenta en la semana en que se auto-confirmó. Flush, sin commit."""
    semana = semana_iso(linea.creado_en or ahora_utc())
    fila = session.get(AutoControlSemana, semana, with_for_update=True)
    if fila is None:
        fila = AutoControlSemana(semana=semana, controles=0, errores=0)
        session.add(fila)
    fila.controles = (fila.controles or 0) + 1
    fila.errores = (fila.errores or 0) + (1 if error else 0)
    session.flush()


def precision_semana(fila: AutoControlSemana | None) -> float | None:
    if fila is None or not fila.controles:
        return None
    return 1.0 - fila.errores / fila.controles


def _semana_anterior(momento: datetime, n: int = 1) -> datetime:
    return momento - timedelta(days=7 * n)


def evaluar_subida(
    session: Session, umbral_base: float | None, ahora: datetime | None = None
) -> dict[str, Any]:
    """Si la precisión observada de las DOS últimas semanas cerradas bajó de
    `PRECISION_MINIMA`, sube el umbral `PASO_SUBIDA` (tope 1,0), lo persiste en `sync_state` y
    avisa (log WARNING; /salud lo muestra). Una vez por par de semanas: la próxima subida pide
    dos semanas nuevas. Nunca baja el umbral. Flush, sin commit."""
    ahora = ahora or ahora_utc()
    if umbral_base is None:
        return {"umbral": None}
    estado = _leer_estado(session)
    efectivo = umbral_efectivo(session, umbral_base) or umbral_base
    s1 = semana_iso(_semana_anterior(ahora, 1))
    s2 = semana_iso(_semana_anterior(ahora, 2))
    p1 = precision_semana(session.get(AutoControlSemana, s1))
    p2 = precision_semana(session.get(AutoControlSemana, s2))
    salida: dict[str, Any] = {"umbral": efectivo, "subido": False}
    ultima = estado.get("ultima_semana")
    if (
        p1 is not None
        and p2 is not None
        and p1 < PRECISION_MINIMA
        and p2 < PRECISION_MINIMA
        and (not isinstance(ultima, str) or s2 > ultima)
        and efectivo < 1.0
    ):
        nuevo = round(min(1.0, efectivo + PASO_SUBIDA), 4)
        _guardar_estado(
            session,
            {
                "umbral": nuevo,
                "ultima_semana": s1,
                "subido_en": ahora.isoformat(timespec="seconds"),
                "desde": efectivo,
                "precisiones": {s2: round(p2, 4), s1: round(p1, 4)},
            },
        )
        session.flush()
        _log.warning(
            "auto-revisión: precisión observada %.1f %% (%s) y %.1f %% (%s), bajo %.0f %%: el "
            "umbral sube de %.2f a %.2f. Revisar la cola de control y recalibrar.",
            p2 * 100, s2, p1 * 100, s1, PRECISION_MINIMA * 100, efectivo, nuevo,
        )  # fmt: skip
        salida.update(umbral=nuevo, subido=True, desde=efectivo)
    return salida


def ciclo_semanal(
    session: Session, umbral_base: float | None, ahora: datetime | None = None
) -> dict[str, Any]:
    """Lo que corre en `mercado-catalogo`: muestrea la semana en curso y la anterior (las líneas
    que llegaron tarde) y evalúa la subida del umbral. Commit de quien llama."""
    ahora = ahora or ahora_utc()
    muestreadas = muestrear_control(session, ahora, ahora=ahora) + muestrear_control(
        session, _semana_anterior(ahora), ahora=ahora
    )
    return {"control_muestreadas": muestreadas, **evaluar_subida(session, umbral_base, ahora)}


# ---------------------------------------------------------------------------
# /salud
# ---------------------------------------------------------------------------

SEMANAS_SALUD = 4


def resumen_salud(
    session: Session, umbral_base: float | None, ahora: datetime | None = None
) -> dict[str, Any]:
    """Umbral (configurado y efectivo), auto-confirmadas, controles y precisión observada de las
    últimas semanas. La alerta se enciende si la última semana cerrada quedó bajo 95 %."""
    ahora = ahora or ahora_utc()
    semanas = [semana_iso(_semana_anterior(ahora, i)) for i in range(SEMANAS_SALUD)]
    desde, _ = limites_semana(_semana_anterior(ahora, SEMANAS_SALUD - 1))
    por: dict[str, dict[str, int]] = {
        s: {"auto": 0, "control": 0, "control_pendiente": 0} for s in semanas
    }
    for creado, conf, control in session.execute(
        select(CaLinea.creado_en, CaLinea.confirmada_por, CaLinea.control_en).where(
            CaLinea.creado_en >= desde,
            or_(CaLinea.confirmada_por == AUTO, CaLinea.control_en.isnot(None)),
        )
    ).tuples():
        d = por.get(semana_iso(creado))
        if d is None:
            continue
        d["auto"] += 1
        if control is not None:
            d["control"] += 1
            if conf == AUTO:
                d["control_pendiente"] += 1
    filas = {
        f.semana: f
        for f in session.execute(
            select(AutoControlSemana).where(AutoControlSemana.semana.in_(semanas))
        ).scalars()
    }
    detalle = []
    for s in semanas:
        f = filas.get(s)
        p = precision_semana(f)
        detalle.append(
            {
                "semana": s,
                **por[s],
                "revisados": f.controles if f else 0,
                "errores": f.errores if f else 0,
                "precision": round(p * 100, 1) if p is not None else None,
            }
        )
    estado = _leer_estado(session)
    prec_cerrada = detalle[1]["precision"] if len(detalle) > 1 else None
    return {
        "umbral_config": umbral_base,
        "umbral_efectivo": umbral_efectivo(session, umbral_base),
        "subida": estado if estado.get("umbral") else None,
        "semanas": detalle,
        "alerta": isinstance(prec_cerrada, float) and prec_cerrada < PRECISION_MINIMA * 100,
    }


# ---------------------------------------------------------------------------
# Calibración (pura; la usa `scripts/mercado/calibrar_auto_revision.py`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PuntoCurva:
    umbral: float
    total: int
    auto: int
    correctas: int

    @property
    def precision(self) -> float | None:
        return self.correctas / self.auto if self.auto else None

    @property
    def cobertura(self) -> float:
        return self.auto / self.total if self.total else 0.0


def umbrales_candidatos(paso: float = 0.025) -> list[float]:
    n = int(round(1 / paso))
    return [round(i * paso, 4) for i in range(1, n + 1)]


@dataclass(frozen=True)
class DocumentoCalibracion:
    """Un anexo del set de evaluación: señales base de sus líneas, el acuerdo con la segunda
    lectura (None si no hay segunda lectura en caché) y si cada línea está bien leída."""

    tipo: str
    senales: tuple[Senales, ...]
    acuerdos: tuple[bool, ...] | None
    correctas: tuple[bool, ...]


def puntajes_simulados(doc: DocumentoCalibracion, umbral: float) -> list[float]:
    """Los puntajes que daría producción con ese umbral: la segunda lectura se usa solo si a
    alguna línea le falta solo el acuerdo (§1.4), y entonces cuenta para todas."""
    senales = list(doc.senales)
    if doc.acuerdos is not None and any(alcanza_con_acuerdo(s, umbral) for s in senales):
        senales = [replace(s, acuerdo=a) for s, a in zip(senales, doc.acuerdos, strict=True)]
    return [puntaje_linea(s).valor for s in senales]


def curva(docs: Sequence[DocumentoCalibracion], umbrales: Sequence[float]) -> list[PuntoCurva]:
    """Por umbral: cuántas líneas se auto-confirmarían y cuántas de esas están bien leídas."""
    total = sum(len(d.senales) for d in docs)
    out = []
    for u in umbrales:
        auto = correctas = 0
        for d in docs:
            for p, ok in zip(puntajes_simulados(d, u), d.correctas, strict=True):
                if p >= u:
                    auto += 1
                    correctas += int(ok)
        out.append(PuntoCurva(umbral=u, total=total, auto=auto, correctas=correctas))
    return out


def elegir_umbral(
    puntos: Sequence[PuntoCurva],
    objetivo: float = PRECISION_OBJETIVO,
    min_auto: int = MIN_AUTO_CALIBRACION,
) -> float | None:
    """El umbral más bajo cuya precisión ≥ `objetivo` con al menos `min_auto` líneas
    auto-confirmadas, y que lo siga cumpliendo en todos los umbrales mayores con muestra
    suficiente (una curva que sube y baja no se corta en el primer pico). None si ninguno."""
    validos = [p for p in puntos if p.auto >= min_auto and p.precision is not None]
    for i, p in enumerate(sorted(validos, key=lambda x: x.umbral)):
        resto = sorted(validos, key=lambda x: x.umbral)[i:]
        if all(q.precision is not None and q.precision >= objetivo for q in resto):
            return p.umbral
    return None
