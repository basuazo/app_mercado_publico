"""Catálogo canónico y líneas 1:1 por Compra Ágil (F-plat-1): semilla, backfill y curación.

El catálogo es COMÚN a todas las empresas (como `oc_lineas_ca`): las correcciones son
compartidas y `revisado_por` deja la huella. Sin commit en las funciones de curación: lo hace
quien llama (la ruta). El job `mercado-catalogo` (`procesar_catalogo`) sí confirma por lotes.

Sin API ni cuota: solo lee `ca_productos` y `ca_cotizacion_productos` ya ingestados.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.catalogos.canasta_alimentos import REGLAS
from app.catalogos.canonico import (
    LARGO_TEXTO_ORIGEN,
    AliasInfo,
    Catalogo,
    ProductoInfo,
    detectar_sin_detalle,
    estado_por_confianza,
    extraer_formato,
    mejor_sugerencia,
    normalizar_texto,
)
from app.catalogos.mercado import FAMILIAS_ALIMENTOS, G_ALIM, grupo_de
from app.core.tiempo import ahora_utc
from app.mercado.proveedores import normalizar_unidad
from app.models.tables import (
    CaCotizacion,
    CaCotizacionProducto,
    CaLinea,
    CaProducto,
    CompraAgil,
    ProductoAlias,
    ProductoCanonico,
    ProductoFormato,
    ProveedorProducto,
    SyncState,
)

_log = logging.getLogger(__name__)

FUENTE_CURSOR = "mercado_catalogo"
LOTE_CA = 500  # CA por lote: ~2.000 productos (regla 12: nunca todo en memoria)
N_MANUAL_DESDE = 1000  # `n` de las líneas manuales: nunca choca con la posición de la API
# F-plat-anexos-2: línea de la API que un anexo (o una persona) reemplazó. Se conserva como
# evidencia de lo que dijo la API; no cuenta para la bandera, la mesa, el índice ni la oferta.
REEMPLAZADA = "reemplazada"
BASE_NOMBRE = {"kg": "kilo", "L": "litro", "u": "unidad"}
UNIDADES_BASE = ("kg", "L", "u")
_LARGO_NOMBRE = 200


class CatalogoError(ValueError):
    """Dato inválido en una operación de curación (la ruta lo muestra al usuario)."""


# ---------------------------------------------------------------------------
# Catálogo en memoria
# ---------------------------------------------------------------------------


def cargar_catalogo(session: Session) -> Catalogo:
    """Productos activos con sus formatos y todos los alias (a productos activos)."""
    formatos: dict[int, list[tuple[int, float]]] = {}
    base: dict[int, int] = {}
    for f in session.execute(select(ProductoFormato)).scalars():
        formatos.setdefault(f.producto_id, []).append((f.id, f.factor_base))
        if abs(f.factor_base - 1.0) < 1e-9 and f.producto_id not in base:
            base[f.producto_id] = f.id
    productos: dict[int, ProductoInfo] = {}
    for p in session.execute(
        select(ProductoCanonico).where(ProductoCanonico.activo.is_(True))
    ).scalars():
        productos[p.id] = ProductoInfo(
            id=p.id,
            nombre=p.nombre,
            unidad_base=p.unidad_base,
            clave_semilla=p.clave_semilla,
            formato_base_id=base.get(p.id),
            formatos=tuple(formatos.get(p.id, ())),
        )
    alias = {
        a.texto_normalizado: AliasInfo(a.producto_id, a.formato_id, a.confianza)
        for a in session.execute(select(ProductoAlias)).scalars()
        if a.producto_id in productos
    }
    return Catalogo(productos=productos, alias=alias)


# ---------------------------------------------------------------------------
# Semilla
# ---------------------------------------------------------------------------


def _formato_inicial(nombre: str, base: str) -> float | None:
    """Factor del formato sugerido si es parseable y de la misma base; si no, None."""
    f = extraer_formato(nombre)
    if f is None or f[3] != base or abs(f[2] - 1.0) < 1e-9:
        return None
    return float(f[2])


def sembrar_canasta(session: Session) -> dict[str, int]:
    """Crea los productos de la canasta que faltan (con su formato base y el sugerido).

    Solo inserta: nunca renombra, fusiona, reactiva ni desactiva un producto existente (lo
    que se corrija en `/catalogo` manda). Existe = hay una fila con la `clave_semilla` de la
    regla, esté activa o no y se llame como se llame. Un producto SIN clave con el mismo nombre
    (ignorando mayúsculas; lo creó una persona) se adopta: recibe la clave y no se duplica.
    """
    claves = set(
        session.execute(
            select(ProductoCanonico.clave_semilla).where(ProductoCanonico.clave_semilla.isnot(None))
        ).scalars()
    )
    sin_clave: dict[str, ProductoCanonico] = {
        p.nombre.lower(): p
        for p in session.execute(
            select(ProductoCanonico).where(ProductoCanonico.clave_semilla.is_(None))
        ).scalars()
    }
    creados = formatos = adoptados = 0
    for regla in REGLAS:
        if not regla.sembrar or regla.clave in claves:
            continue
        propio = sin_clave.pop(regla.nombre.lower(), None)
        if propio is not None:
            propio.clave_semilla = regla.clave
            claves.add(regla.clave)
            adoptados += 1
            continue
        familia = FAMILIAS_ALIMENTOS.get(regla.unspsc[:4]) or regla.categoria
        producto = ProductoCanonico(
            nombre=regla.nombre[:_LARGO_NOMBRE],
            clave_semilla=regla.clave,
            familia=familia,
            unspsc_sugerido=regla.unspsc or None,
            unidad_base=regla.unidad_base,
            activo=True,
        )
        session.add(producto)
        session.flush()
        session.add(
            ProductoFormato(
                producto_id=producto.id, nombre=BASE_NOMBRE[regla.unidad_base], factor_base=1.0
            )
        )
        formatos += 1
        factor = _formato_inicial(regla.formato, regla.unidad_base)
        if factor is not None:
            session.add(
                ProductoFormato(
                    producto_id=producto.id, nombre=regla.formato[:100], factor_base=factor
                )
            )
            formatos += 1
        claves.add(regla.clave)
        creados += 1
    session.flush()
    return {
        "productos_creados": creados,
        "formatos_creados": formatos,
        "productos_adoptados": adoptados,
    }


# ---------------------------------------------------------------------------
# Líneas: cálculo
# ---------------------------------------------------------------------------


def texto_de_producto(nombre: str | None, descripcion: str | None) -> str:
    """Texto de una línea solicitada: nombre y descripción (sin repetir uno dentro del otro)."""
    n, d = (nombre or "").strip(), (descripcion or "").strip()
    if d and n and d.lower().startswith(n.lower()):
        n = ""
    return " ".join(x for x in (n, d) if x)[:LARGO_TEXTO_ORIGEN]


def _es_alimento(unspsc: str | None, nombre: str | None) -> bool:
    return grupo_de((unspsc or "").strip(), nombre) in G_ALIM


def _factor(session_formatos: dict[int, float], formato_id: int | None) -> float | None:
    return session_formatos.get(formato_id) if formato_id is not None else None


def _cantidad_base(cantidad: float | None, factor: float | None) -> float | None:
    if cantidad is None or factor is None:
        return None
    return round(cantidad * factor, 9)


def _formato_por_unidad(producto: ProductoInfo, unidad_ca: str | None) -> int | None:
    """Sin formato en el texto: si la CA pide en la unidad base del producto, el formato base."""
    return (
        producto.formato_base_id if normalizar_unidad(unidad_ca) == producto.unidad_base else None
    )


def recalcular_detalle_lineas(session: Session, ca_codigos: Iterable[str]) -> None:
    """Bandera de la CA («la CA no trae el detalle»):

    - `sin_detalle`: sin líneas, o con el placeholder «sin detalle (anexo)» (`origen = 'anexo'`
      y `estado = 'pendiente'`);
    - `incompleto`: alguna línea `pendiente` o `dudosa` (sin placeholder). Las líneas que se
      leyeron de un anexo esperan confirmación como `dudosa`: «líneas por revisar» (F-plat-2);
    - `completo`: todas las líneas `ok`.

    Las `reemplazada` (F-plat-anexos-2) no cuentan: una CA con solo esas es `sin_detalle`.
    Una línea `pendiente` normal (las reglas no la reconocieron) NO es «sin detalle»: la CA sí
    trae sus líneas. F-plat-2 descarga anexos solo de las CA `sin_detalle`."""
    codigos = sorted(set(ca_codigos))
    if not codigos:
        return
    estados: dict[str, set[str]] = {c: set() for c in codigos}
    anexo_sin_resolver: set[str] = set()
    for ca_codigo, estado, origen in session.execute(
        select(CaLinea.ca_codigo, CaLinea.estado, CaLinea.origen)
        .where(CaLinea.ca_codigo.in_(codigos), CaLinea.estado != REEMPLAZADA)
        .distinct()
    ):
        estados[ca_codigo].add(estado)
        if origen == "anexo" and estado == "pendiente":
            anexo_sin_resolver.add(ca_codigo)
    for ca_codigo, es in estados.items():
        if not es or ca_codigo in anexo_sin_resolver:
            valor = "sin_detalle"
        elif "pendiente" in es or "dudosa" in es:
            valor = "incompleto"
        else:
            valor = "completo"
        # Core UPDATE con `actualizado_en` fijo: la bandera no cuenta como "la CA cambió" (el
        # cursor del job y el resto de la plataforma leen `actualizado_en`).
        session.execute(
            update(CompraAgil)
            .where(CompraAgil.codigo == ca_codigo, CompraAgil.detalle_lineas != valor)
            .values(detalle_lineas=valor, actualizado_en=CompraAgil.actualizado_en)
            .execution_options(synchronize_session=False)
        )
    # F-plat-lote: cambiaron las líneas de estas CA → se recalcula el agregado de los lotes que
    # las tienen (una consulta; casi siempre ninguno). Import local: lote → oferta → catálogo.
    from app.mercado.lote import recalcular_por_cas

    recalcular_por_cas(session, codigos)


# ---------------------------------------------------------------------------
# Backfill / job `mercado-catalogo`
# ---------------------------------------------------------------------------


def _textos_de_ofertas(session: Session, ca_codigos: list[str]) -> dict[tuple[str, str], list[str]]:
    """Textos de las ofertas por (CA, código de producto), el más repetido primero (máx. 3).

    Suelen ser más específicos que lo solicitado ("Nescafé 170 g")."""
    txt = func.coalesce(CaCotizacionProducto.nombre_producto, "")
    filas = session.execute(
        select(
            CaCotizacion.ca_codigo,
            CaCotizacionProducto.codigo_producto,
            txt,
            func.count().label("n"),
        )
        .join(CaCotizacion, CaCotizacion.id == CaCotizacionProducto.cotizacion_id)
        .where(
            CaCotizacion.ca_codigo.in_(ca_codigos), CaCotizacionProducto.codigo_producto.isnot(None)
        )
        .group_by(CaCotizacion.ca_codigo, CaCotizacionProducto.codigo_producto, txt)
        .order_by(func.count().desc())
    ).all()
    out: dict[tuple[str, str], list[str]] = {}
    for ca_codigo, codigo, texto, _n in filas:
        if not codigo or not texto:
            continue
        lista = out.setdefault((ca_codigo, codigo), [])
        if len(lista) < 3:
            lista.append(texto)
    return out


def _procesar_lote(
    session: Session, cas: list[CompraAgil], catalogo: Catalogo, stats: dict[str, int]
) -> None:
    codigos = [c.codigo for c in cas]
    productos_ca: dict[str, list[CaProducto]] = {c: [] for c in codigos}
    for p in session.execute(
        select(CaProducto).where(CaProducto.ca_codigo.in_(codigos)).order_by(CaProducto.id)
    ).scalars():
        productos_ca[p.ca_codigo].append(p)
    existentes: dict[tuple[str, int], CaLinea] = {
        (ln.ca_codigo, ln.n): ln
        for ln in session.execute(select(CaLinea).where(CaLinea.ca_codigo.in_(codigos))).scalars()
    }
    ofertas = _textos_de_ofertas(session, codigos)
    factores: dict[int, float] = {
        fid: fac
        for fid, fac in session.execute(
            select(ProductoFormato.id, ProductoFormato.factor_base)
        ).tuples()
    }

    vigentes_por_ca: dict[str, set[int]] = {}
    for ca in cas:
        vigentes: set[int] = set()
        for pos, prod in enumerate(productos_ca[ca.codigo], start=1):
            if pos >= N_MANUAL_DESDE or not _es_alimento(prod.codigo_producto, prod.nombre):
                continue
            vigentes.add(pos)
            texto = texto_de_producto(prod.nombre, prod.descripcion)
            norm_t = normalizar_texto(texto)
            ln = existentes.get((ca.codigo, pos))
            if ln is not None and ln.origen == "manual":
                continue
            if ln is not None and ln.estado == REEMPLAZADA:
                # El anexo manda (F-plat-anexos-2): se actualiza la evidencia, nunca el estado.
                if ln.texto_origen != texto or ln.cantidad != prod.cantidad:
                    ln.texto_origen, ln.texto_norm = texto, norm_t
                    ln.cantidad = prod.cantidad
                    stats["lineas_actualizadas"] += 1
                continue

            reviewed = ln is not None and ln.revisado_por is not None
            if ln is not None and reviewed:
                # Revisada por una persona: nunca se pisa en silencio. Si cambió el texto
                # queda `dudosa` (con su producto) para que se revise de nuevo.
                if ln.texto_origen != texto:
                    ln.texto_origen, ln.texto_norm = texto, norm_t
                    ln.estado = "dudosa" if ln.producto_id is not None else "pendiente"
                    stats["lineas_a_revisar_de_nuevo"] += 1
                if ln.cantidad != prod.cantidad:
                    ln.cantidad = prod.cantidad
                    ln.cantidad_base = _cantidad_base(
                        prod.cantidad, _factor(factores, ln.formato_id)
                    )
                continue

            sin_detalle = detectar_sin_detalle(texto, prod.cantidad, ca.monto_disponible_clp)
            if sin_detalle and ca.anexo_resuelto_en is not None:
                # F-plat-2: el anexo ya se resolvió (extracción o «Anexo resuelto»): el
                # placeholder no vuelve. Fuera de `vigentes`, el que quede sin revisar se quita.
                continue
            sug = None
            if not sin_detalle:
                otros = ofertas.get((ca.codigo, prod.codigo_producto), [])
                sug = mejor_sugerencia([texto, *otros], prod.codigo_producto, catalogo)
            producto_id = sug.producto_id if sug else None
            formato_id = sug.formato_id if sug else None
            if sug and formato_id is None:
                formato_id = _formato_por_unidad(catalogo.productos[sug.producto_id], prod.unidad)
            valores: dict[str, Any] = {
                "producto_id": producto_id,
                "formato_id": formato_id,
                "cantidad": prod.cantidad,
                "cantidad_base": _cantidad_base(prod.cantidad, _factor(factores, formato_id)),
                "texto_origen": texto,
                "texto_norm": norm_t,
                "codigo_unspsc": (prod.codigo_producto or "")[:20] or None,
                "origen": "anexo" if sin_detalle else "api",
                "estado": estado_por_confianza(sug.confianza if sug else None),
            }
            if ln is None:
                session.add(CaLinea(ca_codigo=ca.codigo, n=pos, **valores))
                stats["lineas_nuevas"] += 1
            else:
                cambios = {k: v for k, v in valores.items() if getattr(ln, k) != v}
                if cambios:
                    for k, v in cambios.items():
                        setattr(ln, k, v)
                    stats["lineas_actualizadas"] += 1

        vigentes_por_ca[ca.codigo] = vigentes

    # La CA ya no trae esa posición (o ya no es alimento): se quita la línea sin revisar.
    # Las de `n ≥ N_MANUAL_DESDE` no son posiciones de la API (manuales y leídas de un anexo,
    # F-plat-2): el job nunca las toca.
    for (ca_codigo, n), ln in existentes.items():
        if (
            n < N_MANUAL_DESDE
            and ln.origen != "manual"
            and ln.revisado_por is None
            and n not in vigentes_por_ca[ca_codigo]
        ):
            session.delete(ln)
            stats["lineas_quitadas"] += 1
    session.flush()
    recalcular_detalle_lineas(session, codigos)


def _leer_cursor(session: Session) -> datetime | None:
    estado = session.get(SyncState, FUENTE_CURSOR)
    if estado is None or not estado.cursor:
        return None
    try:
        return datetime.fromisoformat(estado.cursor)
    except ValueError:
        return None


def _guardar_cursor(session: Session, valor: datetime) -> None:
    estado = session.get(SyncState, FUENTE_CURSOR)
    if estado is None:
        estado = SyncState(fuente=FUENTE_CURSOR)
        session.add(estado)
    estado.cursor = valor.isoformat()
    estado.ultima_ejecucion = estado.ultimo_ok = ahora_utc()


def procesar_catalogo(
    session: Session, regiones: list[int], *, inicial: bool = False, lote: int = LOTE_CA
) -> dict[str, Any]:
    """Job `mercado-catalogo`: siembra lo que falta y crea/actualiza las líneas de las CA de
    `regiones` (las activas, F-plat-3).

    Incremental desde el cursor `sync_state` `mercado_catalogo` (CA cuyo detalle o cotizaciones
    cambiaron); `inicial=True` ignora el cursor y recorre todas. Idempotente: re-ejecutar no
    duplica (upsert por `(ca_codigo, n)`). Por lotes de CA con commit por lote (regla 12). El
    cursor avanza al inicio de la corrida solo si terminó: un corte se reanuda sin perder nada.
    """
    inicio = ahora_utc()
    stats: dict[str, int] = {
        "lineas_nuevas": 0,
        "lineas_actualizadas": 0,
        "lineas_quitadas": 0,
        "lineas_a_revisar_de_nuevo": 0,
        "cas": 0,
    }
    semilla = sembrar_canasta(session)
    session.commit()
    if not regiones:
        return {**stats, **semilla, "sin_regiones": 1}
    catalogo = cargar_catalogo(session)
    cursor = None if inicial else _leer_cursor(session)

    ultimo = ""
    while True:
        cond = [CompraAgil.region.in_(regiones), CompraAgil.codigo > ultimo]
        if cursor is not None:
            cond.append(
                or_(
                    CompraAgil.actualizado_en > cursor,
                    and_(
                        CompraAgil.cotizaciones_obtenidas_en.isnot(None),
                        CompraAgil.cotizaciones_obtenidas_en > cursor,
                    ),
                    # F-merc-3: el detalle de una vigente reemplaza `ca_productos`.
                    and_(
                        CompraAgil.detalle_vigente_en.isnot(None),
                        CompraAgil.detalle_vigente_en > cursor,
                    ),
                )
            )
        cas = list(
            session.execute(
                select(CompraAgil).where(*cond).order_by(CompraAgil.codigo).limit(lote)
            ).scalars()
        )
        if not cas:
            break
        _procesar_lote(session, cas, catalogo, stats)
        session.commit()
        stats["cas"] += len(cas)
        ultimo = cas[-1].codigo
    _guardar_cursor(session, inicio)
    session.commit()
    _log.info("mercado-catalogo: %s", {**semilla, **stats})
    return {**semilla, **stats}


# ---------------------------------------------------------------------------
# Curación
# ---------------------------------------------------------------------------


def crear_producto(
    session: Session,
    *,
    nombre: str,
    unidad_base: str,
    familia: str | None = None,
    unspsc: str | None = None,
    usuario_id: int | None = None,
) -> ProductoCanonico:
    nombre = " ".join((nombre or "").split())
    if not nombre:
        raise CatalogoError("El nombre es obligatorio")
    if unidad_base not in UNIDADES_BASE:
        raise CatalogoError("La unidad base debe ser kg, L o u")
    if session.execute(
        select(ProductoCanonico.id).where(func.lower(ProductoCanonico.nombre) == nombre.lower())
    ).first():
        raise CatalogoError("Ya existe un producto con ese nombre")
    p = ProductoCanonico(
        nombre=nombre[:_LARGO_NOMBRE],
        unidad_base=unidad_base,
        familia=(familia or "").strip()[:100] or None,
        unspsc_sugerido=(unspsc or "").strip()[:20] or None,
        activo=True,
        creado_por=usuario_id,
    )
    session.add(p)
    session.flush()
    session.add(ProductoFormato(producto_id=p.id, nombre=BASE_NOMBRE[unidad_base], factor_base=1.0))
    session.flush()
    return p


def agregar_formato(
    session: Session, producto: ProductoCanonico, nombre: str, factor_base: float
) -> ProductoFormato:
    nombre = " ".join((nombre or "").split())[:100]
    if not nombre:
        raise CatalogoError("El nombre del formato es obligatorio")
    if not factor_base or factor_base <= 0:
        raise CatalogoError("El factor debe ser mayor que cero")
    if session.execute(
        select(ProductoFormato.id).where(
            ProductoFormato.producto_id == producto.id, ProductoFormato.nombre == nombre
        )
    ).first():
        raise CatalogoError("Ese producto ya tiene un formato con ese nombre")
    f = ProductoFormato(producto_id=producto.id, nombre=nombre, factor_base=float(factor_base))
    session.add(f)
    session.flush()
    return f


def _formato_valido(
    session: Session, producto_id: int, formato_id: int | None
) -> ProductoFormato | None:
    if formato_id is None:
        return None
    f = session.get(ProductoFormato, formato_id)
    if f is None or f.producto_id != producto_id:
        raise CatalogoError("El formato no pertenece a ese producto")
    return f


def _producto_activo(session: Session, producto_id: int) -> ProductoCanonico:
    p = session.get(ProductoCanonico, producto_id)
    if p is None or not p.activo:
        raise CatalogoError("Producto no encontrado")
    return p


def _upsert_alias(
    session: Session,
    texto_norm: str,
    producto_id: int,
    formato_id: int | None,
    usuario_id: int | None,
) -> None:
    if not texto_norm:
        return
    ahora = ahora_utc()
    a = session.execute(
        select(ProductoAlias).where(ProductoAlias.texto_normalizado == texto_norm)
    ).scalar_one_or_none()
    if a is None:
        session.add(
            ProductoAlias(
                texto_normalizado=texto_norm,
                producto_id=producto_id,
                formato_id=formato_id,
                fuente="humano",
                confianza=1.0,
                revisado_por=usuario_id,
                revisado_en=ahora,
                veces_visto=1,
            )
        )
    else:
        a.producto_id, a.formato_id = producto_id, formato_id
        a.fuente, a.confianza = "humano", 1.0
        a.revisado_por, a.revisado_en = usuario_id, ahora
        a.veces_visto += 1
    session.flush()


MUESTRA_HISTORICAS = 200  # §2.4: CA históricas que se listan en «líneas por revisar»
POR_REVISAR = ("pendiente", "dudosa")


def _ca_vigente(ahora: datetime) -> Any:
    """Cierre futuro: la CA todavía se puede ofertar (la cola las pone primero)."""
    return and_(CompraAgil.fecha_cierre.isnot(None), CompraAgil.fecha_cierre > ahora)


def muestra_historicas(regiones: list[int], ahora: datetime) -> Any:
    """Subconsulta con los códigos de las `MUESTRA_HISTORICAS` CA históricas (cierre pasado o
    sin cierre) con líneas por revisar, de mayor a menor monto. Todas las líneas de `ca_lineas`
    son de alimentos (F-plat-1 solo crea esas: es el «dentro de G_ALIM» del prompt)."""
    return (
        select(CompraAgil.codigo)
        .join(CaLinea, CaLinea.ca_codigo == CompraAgil.codigo)
        .where(
            CompraAgil.region.in_(regiones),
            CaLinea.estado.in_(POR_REVISAR),
            ~_ca_vigente(ahora),
        )
        .group_by(CompraAgil.codigo, CompraAgil.monto_disponible_clp)
        .order_by(CompraAgil.monto_disponible_clp.desc().nulls_last(), CompraAgil.codigo)
        .limit(MUESTRA_HISTORICAS)
    )


def conteo_por_revisar(session: Session, regiones: list[int], ahora: datetime) -> dict[str, int]:
    """«Líneas por revisar» partidas (F-plat-anexos-2 §2.4): líneas de CA vigentes, líneas de la
    muestra de históricas, y cuántas CA históricas quedan fuera de la muestra."""
    if not regiones:
        return {"vigentes": 0, "historicas_muestra": 0, "historicas_resto_cas": 0}
    base = (
        select(func.count())
        .select_from(CaLinea)
        .join(CompraAgil, CompraAgil.codigo == CaLinea.ca_codigo)
        .where(CompraAgil.region.in_(regiones), CaLinea.estado.in_(POR_REVISAR))
    )
    vigentes = session.execute(base.where(_ca_vigente(ahora))).scalar_one()
    muestra = session.execute(
        base.where(CaLinea.ca_codigo.in_(muestra_historicas(regiones, ahora)))
    ).scalar_one()
    cas_hist = session.execute(
        select(func.count(func.distinct(CaLinea.ca_codigo)))
        .join(CompraAgil, CompraAgil.codigo == CaLinea.ca_codigo)
        .where(
            CompraAgil.region.in_(regiones),
            CaLinea.estado.in_(POR_REVISAR),
            ~_ca_vigente(ahora),
        )
    ).scalar_one()
    return {
        "vigentes": int(vigentes),
        "historicas_muestra": int(muestra),
        "historicas_resto_cas": max(0, int(cas_hist) - MUESTRA_HISTORICAS),
    }


def reemplazar_lineas_api(session: Session, ca_codigo: str) -> int:
    """El anexo manda sobre la API (acuerdo del 5-nov, F-plat-anexos-2 §2.1): las líneas
    `origen = 'api'` de la CA pasan a `reemplazada`. No se borran (son la evidencia de lo que
    dijo la API); las manuales y las de anexo no se tocan. Devuelve cuántas cambió. Sin commit."""
    res = session.execute(
        update(CaLinea)
        .where(
            CaLinea.ca_codigo == ca_codigo,
            CaLinea.origen == "api",
            CaLinea.estado != REEMPLAZADA,
        )
        .values(estado=REEMPLAZADA)
        .execution_options(synchronize_session="fetch")
    )
    return int(getattr(res, "rowcount", 0) or 0)


def confirmar_linea(
    session: Session,
    linea: CaLinea,
    *,
    producto_id: int,
    formato_id: int | None,
    usuario_id: int,
    cantidad: float | None = None,
) -> int:
    """Confirma una línea: `ok` + revisión, crea o refuerza el alias (`humano`, 1,0) y lo
    re-aplica a las demás líneas `pendiente|dudosa` con el mismo texto (misma transacción).
    Devuelve cuántas OTRAS líneas se resolvieron.

    `cantidad` solo se acepta en las líneas leídas de un anexo (F-plat-2): en las de la API
    manda la API. Una línea de anexo confirmada sigue con `origen = 'anexo'`; el placeholder
    confirmado pasa a `api` (la persona le dio producto al texto de la API).

    Confirmar una línea leída de un anexo deja `reemplazada` las líneas de la API de la CA
    (F-plat-anexos-2 §2.1: el anexo manda)."""
    if linea.estado == REEMPLAZADA:
        raise CatalogoError("Esa línea fue reemplazada por el anexo: no se confirma")
    producto = _producto_activo(session, producto_id)
    formato = _formato_valido(session, producto.id, formato_id)
    if cantidad is not None:
        if linea.anexo_id is None:
            raise CatalogoError("La cantidad solo se corrige en las líneas leídas de un anexo")
        if cantidad <= 0:
            raise CatalogoError("La cantidad debe ser mayor que cero")
        linea.cantidad = cantidad
    ahora = ahora_utc()
    linea.producto_id = producto.id
    linea.formato_id = formato.id if formato else None
    linea.cantidad_base = _cantidad_base(linea.cantidad, formato.factor_base if formato else None)
    linea.estado = "ok"
    if linea.origen == "anexo" and linea.anexo_id is None:
        linea.origen = "api"
    linea.revisado_por, linea.revisado_en = usuario_id, ahora
    session.flush()
    if linea.anexo_id is not None:
        reemplazar_lineas_api(session, linea.ca_codigo)
    clave = linea.texto_norm or normalizar_texto(linea.texto_origen)
    _upsert_alias(session, clave, producto.id, linea.formato_id, usuario_id)
    reaplicadas = reaplicar_alias(session, clave, exceptuar_id=linea.id)
    recalcular_detalle_lineas(session, {linea.ca_codigo, *reaplicadas})
    return len(reaplicadas)


def reaplicar_alias(
    session: Session, texto_norm: str, exceptuar_id: int | None = None
) -> list[str]:
    """Aplica el alias de `texto_norm` a las líneas `pendiente|dudosa` con ese texto (salvo las
    que una persona marcó como anexo). Devuelve los códigos de CA tocadas (con repetidos)."""
    if not texto_norm:
        return []
    a = session.execute(
        select(ProductoAlias).where(ProductoAlias.texto_normalizado == texto_norm)
    ).scalar_one_or_none()
    if a is None:
        return []
    factor = (
        session.execute(
            select(ProductoFormato.factor_base).where(ProductoFormato.id == a.formato_id)
        ).scalar_one_or_none()
        if a.formato_id is not None
        else None
    )
    cond = [
        CaLinea.texto_norm == texto_norm,
        CaLinea.estado.in_(("pendiente", "dudosa")),
        CaLinea.origen != "anexo",
    ]
    if exceptuar_id is not None:
        cond.append(CaLinea.id != exceptuar_id)
    lineas = list(session.execute(select(CaLinea).where(*cond)).scalars())
    for ln in lineas:
        ln.producto_id, ln.formato_id, ln.estado = a.producto_id, a.formato_id, "ok"
        ln.cantidad_base = _cantidad_base(ln.cantidad, factor)
    session.flush()
    return [ln.ca_codigo for ln in lineas]


def marcar_sin_detalle(session: Session, linea: CaLinea, usuario_id: int) -> None:
    """ "Sin detalle (anexo)": la línea queda `pendiente` con `origen=anexo` y la CA en
    `sin_detalle`. La revisión evita que el reproceso la vuelva a calcular."""
    linea.producto_id = linea.formato_id = linea.cantidad_base = None
    linea.origen, linea.estado = "anexo", "pendiente"
    linea.anexo_id = None  # es un placeholder, aunque venga de una fila leída de un anexo
    linea.revisado_por, linea.revisado_en = usuario_id, ahora_utc()
    session.flush()
    recalcular_detalle_lineas(session, [linea.ca_codigo])


def agregar_linea_manual(
    session: Session,
    ca: CompraAgil,
    *,
    producto_id: int,
    formato_id: int | None,
    cantidad: float | None,
    usuario_id: int,
    reemplaza_api: bool = False,
) -> CaLinea:
    """ "Agregar líneas a mano": `origen=manual`, `ok`.

    `reemplaza_api` (casilla «la API no describe bien esta CA», F-plat-anexos-2 §2.1 caso B: el
    detalle está en la ficha): las líneas de la API pasan a `reemplazada`."""
    producto = _producto_activo(session, producto_id)
    formato = _formato_valido(session, producto.id, formato_id)
    if cantidad is not None and cantidad <= 0:
        raise CatalogoError("La cantidad debe ser mayor que cero")
    maximo = session.execute(
        select(func.max(CaLinea.n)).where(CaLinea.ca_codigo == ca.codigo)
    ).scalar_one_or_none()
    ahora = ahora_utc()
    texto = f"{producto.nombre}" + (f" — {formato.nombre}" if formato else "")
    ln = CaLinea(
        ca_codigo=ca.codigo,
        n=max(N_MANUAL_DESDE, (maximo or 0) + 1),
        producto_id=producto.id,
        formato_id=formato.id if formato else None,
        cantidad=cantidad,
        cantidad_base=_cantidad_base(cantidad, formato.factor_base if formato else None),
        texto_origen=texto[:LARGO_TEXTO_ORIGEN],
        texto_norm=normalizar_texto(texto),
        origen="manual",
        estado="ok",
        revisado_por=usuario_id,
        revisado_en=ahora,
    )
    session.add(ln)
    session.flush()
    if reemplaza_api:
        reemplazar_lineas_api(session, ca.codigo)
    recalcular_detalle_lineas(session, [ca.codigo])
    return ln


def quitar_linea_manual(session: Session, linea: CaLinea) -> None:
    """Quita una línea agregada a mano o leída de un anexo (F-plat-2, «Eliminar fila»)."""
    if linea.origen != "manual" and linea.anexo_id is None:
        raise CatalogoError(
            "Solo se pueden quitar las líneas agregadas a mano o leídas de un anexo"
        )
    ca_codigo = linea.ca_codigo
    session.delete(linea)
    session.flush()
    recalcular_detalle_lineas(session, [ca_codigo])


def fusionar_productos(session: Session, origen_id: int, destino_id: int) -> dict[str, int]:
    """Fusiona `origen` en `destino` (solo admin, lo valida la ruta): mueve líneas, alias,
    costos de proveedores, inventario de las empresas y formatos; el origen queda INACTIVO (no se borra: así la semilla no
    lo resucita). Los formatos con el mismo factor se unifican con los del destino."""
    if origen_id == destino_id:
        raise CatalogoError("Elige dos productos distintos")
    origen = session.get(ProductoCanonico, origen_id)
    destino = session.get(ProductoCanonico, destino_id)
    if origen is None or destino is None or not destino.activo:
        raise CatalogoError("Producto no encontrado")
    if origen.unidad_base != destino.unidad_base:
        raise CatalogoError("No se pueden fusionar productos con distinta unidad base")
    mapa: dict[int, int] = {}
    destino_f = list(
        session.execute(
            select(ProductoFormato).where(ProductoFormato.producto_id == destino.id)
        ).scalars()
    )
    nombres = {f.nombre for f in destino_f}
    for f in session.execute(
        select(ProductoFormato).where(ProductoFormato.producto_id == origen.id)
    ).scalars():
        igual = next((d for d in destino_f if abs(d.factor_base - f.factor_base) < 1e-9), None)
        if igual is not None:
            mapa[f.id] = igual.id
            continue
        nombre = f.nombre if f.nombre not in nombres else f"{f.nombre} ({origen.nombre})"[:100]
        nuevo = ProductoFormato(producto_id=destino.id, nombre=nombre, factor_base=f.factor_base)
        session.add(nuevo)
        session.flush()
        nombres.add(nombre)
        mapa[f.id] = nuevo.id
    cas: set[str] = set(
        session.execute(select(CaLinea.ca_codigo).where(CaLinea.producto_id == origen.id)).scalars()
    )
    n_lineas = len(
        session.execute(select(CaLinea.id).where(CaLinea.producto_id == origen.id)).all()
    )
    for tabla in (CaLinea, ProductoAlias, ProveedorProducto):
        session.execute(
            update(tabla).where(tabla.producto_id == origen.id).values(producto_id=destino.id)
        )
        for viejo, nuevo_id in mapa.items():
            session.execute(
                update(tabla).where(tabla.formato_id == viejo).values(formato_id=nuevo_id)
            )
    from app.mercado.oferta_propia import fusionar_inventario  # lazy: importa este módulo

    fusionar_inventario(session, origen.id, destino.id, mapa)  # F-plat-capas: capa «Inventario»
    factores: dict[int, float] = {
        fid: fac
        for fid, fac in session.execute(
            select(ProductoFormato.id, ProductoFormato.factor_base).where(
                ProductoFormato.producto_id == destino.id
            )
        ).tuples()
    }
    for ln in session.execute(
        select(CaLinea).where(CaLinea.producto_id == destino.id, CaLinea.ca_codigo.in_(cas))
    ).scalars():
        ln.cantidad_base = _cantidad_base(ln.cantidad, _factor(factores, ln.formato_id))
    origen.activo = False
    session.flush()
    recalcular_detalle_lineas(session, cas)
    return {"lineas": n_lineas, "formatos": len(mapa)}
