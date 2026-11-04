"""Ingesta de mercado: cotizaciones de Compra Ágil por región, en vivo y hacia atrás (F-merc-1).

Por cada CA de las regiones activas (F-plat-3) guarda quién cotizó, a qué precio por producto y quién
ganó (`ca_cotizaciones`, `ca_cotizacion_productos`). Dos jobs, ambos con `MP_TICKET`, el
`QuotaTracker` y la pausa `api_pausa` de siempre, sin reserva, y el advisory lock de
siempre (corrección 31-oct: una sola key):

- `mercado-ca` (cada 15 min): listado por `cambio_desde` con su propio cursor, filtrado
  por región y sin `estado`, y después el detalle de las CA que lo necesitan.
- `mercado-backfill` (de noche): mes a mes, ventanas de un día de publicación, con un
  cursor por mes en `sync_state`, reanudable.

El listado va SIN `estado`: el filtro `estado=proveedor_seleccionado` devuelve 0 [V,
31-oct, `01-analisis` §10]; se filtra localmente. El parser es el de F-merc-0b
(`app/clients/mp_v2.py`): el ganador es `proveedor_seleccionado` de cada oferta.
Regla 24: nada de esto pide licitaciones.

Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import calendar
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, and_, case, delete, func, or_, select
from sqlalchemy.orm import Session

from app.catalogos.mercado import grupo_de
from app.clients.base import (
    ERRORES_DE_PAUSA,
    MPAuthError,
    MPParseError,
    MPRateLimitError,
    MPServerError,
    MPTasaError,
    MPTransportError,
    QuotaExceededError,
    leer_pausa,
)
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.clients.types import CompraAgilDetalle, CotizacionCA
from app.core.logging import get_logger
from app.core.retencion import tamano_bd
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc, borde_del_dia_utc_naive, en_ventana_nocturna
from app.ingest.compra_agil import (
    _TOPE_RESULTADOS,
    _leer_cursor,
    _procesar_pagina,
    _registrar_intento,
    _sync_en_ventanas,
    _Totales,
    upsert_ca_detalle,
)
from app.ingest.orchestrator import _FUENTE_CA, _espera_detalle, _registrar_fallo_detalle
from app.mercado.territorio import regiones_activas_engine
from app.models.tables import CaCotizacion, CaCotizacionProducto, CompraAgil, SyncState

_log = get_logger(__name__)

FUENTE_MERCADO_CA = "mercado_ca"
PREFIJO_BACKFILL = "mercado_backfill:"
CURSOR_COMPLETO = "completo"
# Todos los días del mes ya listados; quedan detalles pendientes por fallos.
CURSOR_LISTADO = "listado"
# Mes terminado, dentro del horizonte, con 0 CA: no se vuelve a pedir (borrar a mano
# el cursor para repetirlo, ver docs/operacion-disparos.md).
CURSOR_VACIO = "vacio"
# Meses vacíos seguidos, sin ningún mes con datos antes en la corrida, que se leen como
# filtro roto y no como meses vacíos (protección de F-merc-0b-fix2).
_VACIOS_SOSPECHOSOS = 3

# Estados en que una CA ya tiene (o puede tener) ofertas que guardar.
ESTADOS_TERMINALES_CA = ("cerrada", "proveedor_seleccionado", "desierta", "cancelada")
# Meses que `COT_` trae sin Ñuble [V marzo; I el resto] (prompt F-merc-1 §1.6).
MESES_PRIORITARIOS = ("2026-03", "2026-04", "2026-06", "2026-07", "2026-08")

# Clave sustituta de `id_cotizacion` nulo (misma que la migración b8d4f1a6c2e9).
PREFIJO_CLAVE_SUSTITUTA = "rut:"

_LARGO_DESCRIPCION_COTIZACION = 2000
_LARGO_DESCRIPCION_PRODUCTO = 1000
# Hora de Chile en que el backfill deja de pedir: margen antes de las 07:00 (regla 5).
_FIN_NOCHE = (6, 45)
# Detalles fallidos seguidos (5xx, timeout, JSON roto) antes de cortar el backfill.
_MAX_FALLOS_SEGUIDOS = 3
# Cada cuántas requests el backfill mira el tamaño de la base (pg_database_size).
_CADA_CUANTO_TAMANO = 50
# F-merc-1c: margen sobre el fin de la pausa antes del tramo siguiente (como el puente).
_JITTER_PAUSA_S = (30.0, 90.0)
# Holgura del tope de minutos: el tramo que sigue a una pausa necesita tiempo para avanzar.
_HOLGURA_TOPE_S = 5 * 60

_ERRORES_DE_CORTE = (MPRateLimitError, QuotaExceededError)
# Cortes del listado que no tocan el canal: la cola de detalles sigue.
_CORTES_DE_SERVIDOR = frozenset({"MPServerError", "MPTransportError"})


def _recortar(v: str | None, largo: int) -> str | None:
    return v[:largo] if v else v


# ---------------------------------------------------------------------------
# Escritura: detalle + cotizaciones, idempotente
# ---------------------------------------------------------------------------


def clave_oferta(c: CotizacionCA) -> str:
    """Clave de la oferta en el UNIQUE (ca, llamado, id_cotizacion) (F-merc-1c §3.1).

    `id_cotizacion` si viene; si no, la sustituta estable "rut:<rut>": sin id, dos
    ofertas del mismo RUT chocan y se guarda una, como antes de F-merc-1c.
    """
    if c.id_cotizacion:
        return c.id_cotizacion
    return PREFIJO_CLAVE_SUSTITUTA + c.rut_proveedor[:20]


def _unicas_por_clave(cotizaciones: list[CotizacionCA]) -> tuple[list[CotizacionCA], int]:
    """(ofertas con clave distinta, la primera de cada una; cuántas se descartaron)."""
    vistas: set[str] = set()
    unicas: list[CotizacionCA] = []
    for c in cotizaciones:
        clave = clave_oferta(c)
        if clave in vistas:
            continue
        vistas.add(clave)
        unicas.append(c)
    return unicas, len(cotizaciones) - len(unicas)


def _sin_repetir(cotizaciones: list[CotizacionCA], codigo: str) -> list[CotizacionCA]:
    """Una fila por oferta (`clave_oferta`), la primera; un WARNING por CA si hubo repetidas.

    Un mismo RUT puede mandar varias ofertas distintas [V, sonda 4-nov-2026]: todas se
    guardan. Solo se descarta un `id_cotizacion` repetido dentro de la misma respuesta.
    """
    unicas, descartadas = _unicas_por_clave(cotizaciones)
    if descartadas:
        _log.warning(
            "CA %s: %d ofertas con id_cotizacion repetido en la respuesta; se guarda la primera",
            codigo,
            descartadas,
        )
    return unicas


def _firma(c: CotizacionCA) -> tuple[Any, ...]:
    """Lo que hace dos ofertas idénticas salvo el `id_cotizacion`: RUT, montos y productos."""
    productos = sorted(
        (str(p.codigo_producto), p.cantidad or 0.0, p.precio_unitario or 0.0) for p in c.productos
    )
    return (c.rut_proveedor, c.valor_neto, c.monto_total, c.monto_despacho, tuple(productos))


def contar_repetidas(cotizaciones: list[CotizacionCA]) -> tuple[int, int]:
    """(ofertas de un RUT que ya ofertó en la CA, ofertas idénticas a una anterior).

    Sobre las ofertas que se guardan (ya sin claves repetidas). Una idéntica con distinto
    `id_cotizacion` se guarda igual: para la API son dos ofertas (F-merc-1c §3.2).
    """
    unicas, _ = _unicas_por_clave(cotizaciones)
    ruts: set[str] = set()
    firmas: set[tuple[Any, ...]] = set()
    mismo_rut = identicas = 0
    for c in unicas:
        mismo_rut += c.rut_proveedor in ruts
        ruts.add(c.rut_proveedor)
        firma = _firma(c)
        identicas += firma in firmas
        firmas.add(firma)
    return mismo_rut, identicas


def guarda_productos(det: CompraAgilDetalle, solo_grupos: bool) -> bool:
    """¿Se guardan los `productos_cotizados` de esta CA? (prompt F-merc-1 §1.5).

    El estimado pasa de 120 MB a 24 meses (ver docs/decisiones.md, retención): con
    `solo_grupos`, solo los de CA con algún producto —pedido u ofertado— en los
    grupos del observatorio (`app/catalogos/mercado.py`). Las ofertas se guardan siempre.
    """
    if not solo_grupos:
        return True
    if any(grupo_de(p.codigo_producto, p.nombre) for p in det.productos):
        return True
    return any(
        grupo_de(p.codigo_producto, p.nombre_producto) for c in det.cotizaciones for p in c.productos
    )


def _reemplazar_cotizaciones(
    session: Session, det: CompraAgilDetalle, *, con_productos: bool
) -> tuple[int, int]:
    """Borra y reinserta las cotizaciones del llamado recibido. Devuelve (ofertas, productos).

    Un arreglo vacío no borra lo ya guardado: según la guía refleja solo el llamado
    vigente y puede venir vacío o parcial antes de "cerrada en segundo llamado" [I].
    Los productos se borran explícitos además del ON DELETE CASCADE (SQLite de los
    tests no aplica FK por defecto).
    """
    if not det.cotizaciones:
        return 0, 0
    llamado = det.estado_convocatoria
    del_llamado = and_(
        CaCotizacion.ca_codigo == det.codigo,
        CaCotizacion.llamado.is_(None) if llamado is None else CaCotizacion.llamado == llamado,
    )
    session.execute(
        delete(CaCotizacionProducto).where(
            CaCotizacionProducto.cotizacion_id.in_(select(CaCotizacion.id).where(del_llamado))
        )
    )
    session.execute(delete(CaCotizacion).where(del_llamado))

    n_productos = 0
    unicas = _sin_repetir(det.cotizaciones, det.codigo)
    for c in unicas:
        cot = CaCotizacion(
            ca_codigo=det.codigo,
            llamado=llamado,
            rut_proveedor=c.rut_proveedor[:20],
            razon_social=_recortar(c.razon_social, 500),
            es_emt=c.es_emt,
            codigo_empresa=_recortar(c.codigo_empresa, 50),
            codigo_sucursal=_recortar(c.codigo_sucursal, 50),
            id_cotizacion=clave_oferta(c)[:50],
            id_oc=_recortar(c.id_oc, 50),
            estado_glosa=_recortar(c.estado_glosa, 100),
            activo=c.activo,
            seleccionado=c.seleccionado,
            valor_neto=c.valor_neto,
            total_impuesto=c.total_impuesto,
            monto_despacho=c.monto_despacho,
            monto_total=c.monto_total,
            fecha_creacion=c.fecha_creacion,
            fecha_vigencia=c.fecha_vigencia,
            descripcion=_recortar(c.descripcion, _LARGO_DESCRIPCION_COTIZACION),
        )
        cot.productos = [
            CaCotizacionProducto(
                codigo_producto=_recortar(p.codigo_producto, 100),
                nombre_producto=_recortar(p.nombre_producto, 500),
                descripcion=_recortar(p.descripcion, _LARGO_DESCRIPCION_PRODUCTO),
                cantidad=p.cantidad,
                precio_unitario=p.precio_unitario,
                monto_total_producto=p.monto_total_producto,
            )
            for p in (c.productos if con_productos else [])
        ]
        n_productos += len(cot.productos)
        session.add(cot)
    return len(unicas), n_productos


def guardar_detalle_mercado(
    session: Session, det: CompraAgilDetalle, *, solo_grupos: bool = True
) -> tuple[int, int]:
    """Guarda el detalle de una CA con sus cotizaciones. No commitea. Devuelve (ofertas, productos).

    Campos y productos solicitados con la misma función que el resto de la ingesta
    (`upsert_ca_detalle`); los campos de F-merc-1 no se pisan con None. Sin raw_json
    (regla 11). Re-ejecutar no duplica: ver :func:`_reemplazar_cotizaciones`.
    """
    upsert_ca_detalle(session, det)
    ca = session.get(CompraAgil, det.codigo)
    assert ca is not None  # upsert_ca_detalle la crea si no existía
    nuevos: dict[str, object] = {
        "unidad_compra": _recortar(det.unidad_compra, 500),
        "direccion_entrega": _recortar(det.direccion_entrega, 1000),
        "plazo_entrega_dias": det.plazo_entrega_dias,
        "tipo_presupuesto": _recortar(det.tipo_presupuesto, 50),
        "fecha_cierre_segundo_llamado": det.fecha_cierre_segundo_llamado,
        "motivo_desierta": det.motivo_desierta,
        "motivo_cancelacion": det.motivo_cancelacion,
        "moneda": _recortar(det.moneda, 10),
    }
    for campo, valor in nuevos.items():
        if valor is not None:
            setattr(ca, campo, valor)
    session.flush()
    resultado = _reemplazar_cotizaciones(
        session, det, con_productos=guarda_productos(det, solo_grupos)
    )
    ca.cotizaciones_obtenidas_en = ahora_utc()
    # Un detalle que llegó resetea el contador de fallos (mismo criterio que detalles-match).
    ca.detalle_fallos = 0
    ca.detalle_ultimo_fallo = None
    return resultado


def _bajar_y_guardar(
    settings: Settings,
    engine: Engine,
    v2: MercadoPublicoV2Client,
    codigo: str,
    *,
    reintentar: bool,
) -> tuple[int, int, int, int]:
    """Pide el detalle y lo guarda en UN commit, en una sesión propia.

    Devuelve (ofertas, productos, ofertas del mismo RUT, ofertas idénticas)."""
    det = v2.detalle_compra_agil(codigo, reintentar_transitorios=reintentar)
    with Session(engine) as session:
        ofertas, productos = guardar_detalle_mercado(
            session, det, solo_grupos=settings.mercado_productos_solo_grupos
        )
        session.commit()
    mismo_rut, identicas = contar_repetidas(det.cotizaciones) if ofertas else (0, 0)
    return ofertas, productos, mismo_rut, identicas


@dataclass
class _Conteo:
    detalles_guardados: int = 0
    detalles_fallidos: int = 0
    cotizaciones: int = 0
    productos: int = 0
    # F-merc-1c: ofertas de un RUT que ya ofertó en esa CA, e idénticas salvo el id.
    ofertas_mismo_rut: int = 0
    ofertas_identicas: int = 0

    def sumar(self, guardado: tuple[int, int, int, int]) -> None:
        self.detalles_guardados += 1
        self.cotizaciones += guardado[0]
        self.productos += guardado[1]
        self.ofertas_mismo_rut += guardado[2]
        self.ofertas_identicas += guardado[3]


# ---------------------------------------------------------------------------
# mercado-ca: en vivo
# ---------------------------------------------------------------------------


def cola_mercado_ca(
    session: Session,
    regiones: list[int],
    ahora: datetime,
    limite: int,
    fallos_max: int,
    espera_horas: int,
) -> list[str]:
    """CA de `regiones` cuyas cotizaciones faltan o quedaron viejas, `fecha_cierre` desc.

    Entran las terminales con `cotizaciones_obtenidas_en` nulo o anterior a
    `fecha_ultimo_cambio`, y las `publicada` con el cierre ya pasado (fija el primer
    llamado). Las que llegaron a `fallos_max` esperan como en `detalles-match`
    (:func:`app.ingest.orchestrator._espera_detalle`) y van al final.
    """
    pendiente = or_(
        CompraAgil.cotizaciones_obtenidas_en.is_(None),
        CompraAgil.cotizaciones_obtenidas_en < CompraAgil.fecha_ultimo_cambio,
    )
    candidata = or_(
        CompraAgil.estado.in_(ESTADOS_TERMINALES_CA),
        and_(CompraAgil.estado == "publicada", CompraAgil.fecha_cierre < ahora),
    )
    stmt = (
        select(CompraAgil.codigo, CompraAgil.detalle_fallos, CompraAgil.detalle_ultimo_fallo)
        .where(CompraAgil.region.in_(regiones), candidata, pendiente)
        .order_by(
            case((CompraAgil.detalle_fallos > 0, 1), else_=0),
            CompraAgil.fecha_cierre.desc().nulls_last(),
            CompraAgil.codigo,
        )
        # Holgura para las que están en espera; el corte fino va abajo.
        .limit(limite * 3)
    )
    cola: list[str] = []
    for codigo, fallos, ultimo in session.execute(stmt):
        espera = _espera_detalle(fallos, fallos_max, espera_horas)
        if espera is not None and ultimo is not None and ahora < ultimo + espera:
            continue
        cola.append(codigo)
        if len(cola) >= limite:
            break
    return cola


def sync_mercado_ca(
    settings: Settings,
    engine: Engine,
    v2: MercadoPublicoV2Client,
    reloj: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Job `mercado-ca`: listado incremental de las regiones y detalle de la cola.

    1. Listado por `cambio_desde` con el cursor `mercado_ca` en ventanas de hasta
       `ca_ventana_horas` (misma función que `ca`, :func:`_sync_en_ventanas`), región
       en las regiones activas (F-plat-3) y sin `estado`. Sin cursor arranca en "ahora − ventana":
       lo anterior lo cubre `mercado-backfill`.
    2. Cola (:func:`cola_mercado_ca`) con tope de `mercado_detalles_max` detalles y
       `mercado_detalles_minutos`. Sin reintento de 5xx: lo que falla suma un fallo y
       vuelve a la cola de la corrida siguiente.

    Un 429, la API en pausa o la cuota cortan la corrida guardando el avance (regla 3)
    y dejan `cortado_por`; un 401 se re-lanza.
    """
    regiones = regiones_activas_engine(engine, settings)
    resultado: dict[str, Any] = {"regiones": regiones}
    with Session(engine) as session:
        cursor = _leer_cursor(session, FUENTE_MERCADO_CA)
        inicio_cursor = (
            cursor.replace(tzinfo=None)
            if cursor is not None
            else ahora_utc() - timedelta(hours=settings.ca_ventana_horas)
        )
        try:
            listado = _sync_en_ventanas(
                session,
                v2,
                settings,
                inicio_cursor,
                fuente=FUENTE_MERCADO_CA,
                regiones=regiones,
                estados=[],
            )
        except MPAuthError:
            _registrar_intento(session, ok=False, fuente=FUENTE_MERCADO_CA)
            raise
        except _ERRORES_DE_CORTE as exc:
            _registrar_intento(session, ok=False, fuente=FUENTE_MERCADO_CA)
            _log.warning("mercado-ca: listado cortado por %s — %s", type(exc).__name__, exc)
            resultado["cortado_por"] = type(exc).__name__
            return resultado
        except MPServerError as exc:
            # Sin ventana cerrada: el cursor no avanzó. La cola de detalles sigue igual.
            _registrar_intento(session, ok=False, fuente=FUENTE_MERCADO_CA)
            _log.warning("mercado-ca: listado sin avance (%s); sigo con la cola", exc)
            listado = {"cortado_por_error": type(exc).__name__}
    resultado["listado"] = listado
    if listado.get("cortado_por_error"):
        resultado["cortado_por"] = listado["cortado_por_error"]
        if listado["cortado_por_error"] not in _CORTES_DE_SERVIDOR:
            return resultado

    with Session(engine) as session:
        cola = cola_mercado_ca(
            session,
            regiones,
            ahora_utc(),
            settings.mercado_detalles_max,
            settings.detalles_fallos_max,
            settings.detalles_espera_horas,
        )

    conteo = _Conteo()
    presupuesto_s = settings.mercado_detalles_minutos * 60
    inicio = reloj()
    intentados = 0
    for codigo in cola:
        if reloj() - inicio >= presupuesto_s:
            resultado["cortado_por_tiempo"] = True
            break
        intentados += 1
        try:
            conteo.sumar(_bajar_y_guardar(settings, engine, v2, codigo, reintentar=False))
        except MPAuthError:
            raise
        except _ERRORES_DE_CORTE as exc:
            conteo.detalles_fallidos += 1
            _log.warning("mercado-ca: detalles cortados por %s — %s", type(exc).__name__, exc)
            resultado["cortado_por"] = type(exc).__name__
            break
        except MPTransportError as exc:
            conteo.detalles_fallidos += 1
            _log.warning("mercado-ca: error de transporte en %s — %s; sin sumar fallo", codigo, exc)
        except Exception:
            conteo.detalles_fallidos += 1
            _log.error("mercado-ca: error con el detalle de %s", codigo, exc_info=True)
            _registrar_fallo_detalle(engine, _FUENTE_CA, codigo)

    resultado.update(
        {
            "cola": len(cola),
            "detalles_intentados": intentados,
            "detalles_pendientes": len(cola) - intentados,
            **conteo.__dict__,
        }
    )
    return resultado


# ---------------------------------------------------------------------------
# mercado-backfill: hacia atrás, de noche
# ---------------------------------------------------------------------------


def _mes_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _mes_anterior(mes: str) -> str:
    y, m = (int(x) for x in mes.split("-"))
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def meses_backfill(settings: Settings, hoy: date) -> list[str]:
    """Meses a recorrer, en orden. `MERCADO_BACKFILL_MESES` manda si viene.

    Por defecto: los cinco que `COT_` trae sin Ñuble y luego desde el mes anterior al
    actual hasta `mercado_backfill_primer_mes`, del más reciente al más antiguo, sin
    repetir. Un mes anterior a `mercado_backfill_primer_mes` nunca se pide, venga de
    donde venga (la sonda del 3-nov dio 0 CA en oct-2024 [V]).
    """
    primer = settings.mercado_backfill_primer_mes.strip()
    if settings.mercado_backfill_meses.strip():
        explicitos = [m.strip() for m in settings.mercado_backfill_meses.split(",") if m.strip()]
        return [m for m in explicitos if m >= primer]
    actual = _mes_str(hoy)
    meses = [m for m in MESES_PRIORITARIOS if primer <= m < actual]
    mes = _mes_anterior(actual)
    while mes >= primer:
        if mes not in meses:
            meses.append(mes)
        mes = _mes_anterior(mes)
    return meses


def _dias_del_mes(mes: str) -> list[date]:
    y, m = (int(x) for x in mes.split("-"))
    return [date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]


@dataclass
class ResumenBackfill:
    requests: int = 0
    ca_listadas: int = 0
    conteo: _Conteo = field(default_factory=_Conteo)
    meses_completos: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    motivo_fin: str = "sin meses pendientes"
    cortado_por: str | None = None
    # F-merc-1c: pausas de 429 esperadas, minutos dormidos y requests de cada tramo que
    # terminó en 429 (incluida la que lo recibió): el N_A de la sonda H1.
    pausas: int = 0
    minutos_esperados: float = 0.0
    requests_por_tramo: list[int] = field(default_factory=list)

    def como_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "ca_listadas": self.ca_listadas,
            **self.conteo.__dict__,
            "meses_completos": self.meses_completos,
            "avisos": self.avisos,
            "motivo_fin": self.motivo_fin,
            "cortado_por": self.cortado_por,
            "pausas": self.pausas,
            "minutos_esperados": round(self.minutos_esperados, 1),
            "requests_por_tramo": self.requests_por_tramo,
        }


class _Corte(Exception):
    """Fin limpio del backfill: el avance ya quedó guardado."""


def _leer_cursor_mes(session: Session, mes: str) -> str | None:
    state = session.get(SyncState, PREFIJO_BACKFILL + mes)
    return state.cursor if state is not None else None


def _guardar_cursor_mes(session: Session, mes: str, valor: str | None) -> None:
    state = session.get(SyncState, PREFIJO_BACKFILL + mes)
    if state is None:
        state = SyncState(fuente=PREFIJO_BACKFILL + mes)
        session.add(state)
    ahora = ahora_utc()
    state.cursor = valor
    state.ultima_ejecucion = ahora
    state.ultimo_ok = ahora


def _parsear_cursor_dia(valor: str | None, mes: str) -> tuple[date, int]:
    """'AAAA-MM-DD|página' = siguiente día y página por pedir. Ilegible → inicio del mes."""
    if valor and "|" in valor:
        dia_txt, pagina_txt = valor.split("|", 1)
        try:
            return date.fromisoformat(dia_txt), max(1, int(pagina_txt))
        except ValueError:
            _log.warning("mercado-backfill: cursor de %s ilegible (%r); parte de cero", mes, valor)
    return _dias_del_mes(mes)[0], 1


def _tras_fin_noche(ahora: datetime) -> bool:
    """¿Ya pasaron las 06:45 Chile de esta madrugada? (antes de las 22:00)."""
    local = ahora.astimezone(TZ_CHILE)
    return local.hour < 22 and (local.hour, local.minute) >= _FIN_NOCHE


def _jitter_pausa() -> float:
    return random.uniform(*_JITTER_PAUSA_S)


class Backfill:
    """Una corrida de `mercado-backfill`: uno o más tramos separados por pausas de 429.

    `now_fn(tz)`, `reloj`, `sleep_fn` y `jitter_fn` son inyectables para tests.
    """

    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        v2: MercadoPublicoV2Client,
        now_fn: Callable[..., datetime] | None = None,
        reloj: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] | None = None,
        jitter_fn: Callable[[], float] = _jitter_pausa,
        regiones: list[int] | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.v2 = v2
        self.now_fn = now_fn or (lambda tz=TZ_CHILE: datetime.now(tz))
        self.reloj = reloj
        # Resuelto al dormir, no al importar: los tests parchean `time.sleep`.
        self.sleep_fn = sleep_fn or (lambda s: time.sleep(s))
        self.jitter_fn = jitter_fn
        self.inicio = reloj()
        # F-plat-3: las de las empresas activas (R-p2); `run_backfill` las resuelve y las pasa.
        self.regiones = (
            list(regiones) if regiones is not None else regiones_activas_engine(engine, settings)
        )
        self.estados_detalle = {e.strip().lower() for e in settings.mercado_backfill_estados_detalle}
        self.resumen = ResumenBackfill()
        self._fallos_seguidos = 0

    # -- cortes --------------------------------------------------------------

    def _revisar(self) -> None:
        """Antes de cada request: hora, minutos y cuota. Lanza _Corte para terminar limpio."""
        ahora = self.now_fn(TZ_CHILE)
        if not en_ventana_nocturna(lambda _tz: ahora) or _tras_fin_noche(ahora):
            raise _Corte(f"{_FIN_NOCHE[0]:02d}:{_FIN_NOCHE[1]:02d} Chile: fin de la ventana nocturna")
        if self.reloj() - self.inicio >= self.settings.mercado_backfill_minutos * 60:
            raise _Corte(f"tope de {self.settings.mercado_backfill_minutos} min por corrida")
        if self.v2.cuota_restante() <= self.settings.mercado_backfill_margen_cuota:
            raise _Corte(
                f"presupuesto del día menos {self.settings.mercado_backfill_margen_cuota} "
                "(margen para mercado-ca)"
            )
        if self.resumen.requests % _CADA_CUANTO_TAMANO == 0:
            self._revisar_tamano()

    def _revisar_tamano(self) -> None:
        """Neon free = 0,5 GB (regla 11): sobre `mercado_backfill_tope_bd_mb`, corta con aviso."""
        with Session(self.engine) as session:
            tamano = tamano_bd(session)  # None en SQLite
        tope = self.settings.mercado_backfill_tope_bd_mb
        if tamano is not None and tamano >= tope * 1024 * 1024:
            aviso = f"base en {tamano / 1024 / 1024:.0f} MB (tope {tope} MB): backfill detenido"
            self.resumen.avisos.append(aviso)
            raise _Corte(aviso)

    # -- pasos ---------------------------------------------------------------

    def _detalle(self, codigo: str, *, cortar_tras_fallos: bool = True) -> None:
        """Un detalle. Con `cortar_tras_fallos`, el tercer fallo seguido corta la corrida.

        El reintento de pendientes va sin corte: una CA que falla siempre no debe
        frenar cada noche los meses que vienen después.
        """
        self._revisar()
        self.resumen.requests += 1
        try:
            self.resumen.conteo.sumar(
                _bajar_y_guardar(self.settings, self.engine, self.v2, codigo, reintentar=True)
            )
        except (MPServerError, MPParseError) as exc:
            # 5xx, timeout o JSON roto (el cliente ya reintentó): queda pendiente.
            self.resumen.conteo.detalles_fallidos += 1
            self._fallos_seguidos += 1
            _log.warning("mercado-backfill: detalle %s falló (%s); queda pendiente", codigo, exc)
            if cortar_tras_fallos and self._fallos_seguidos >= _MAX_FALLOS_SEGUIDOS:
                raise _Corte(f"{_MAX_FALLOS_SEGUIDOS} detalles fallidos seguidos") from exc
            return
        self._fallos_seguidos = 0

    def _sin_cotizaciones(self, codigos: list[str]) -> set[str]:
        if not codigos:
            return set()
        with Session(self.engine) as session:
            return set(
                session.execute(
                    select(CompraAgil.codigo).where(
                        CompraAgil.codigo.in_(codigos),
                        CompraAgil.cotizaciones_obtenidas_en.is_(None),
                    )
                ).scalars()
            )

    def _pagina(self, mes: str, dia: date, pagina: int) -> int:
        """Lista una página del día, la guarda y pide sus detalles. Devuelve total de páginas."""
        self._revisar()
        self.resumen.requests += 1
        resp = self.v2.listar_compra_agil(
            publicado_desde=borde_del_dia_utc_naive(dia, fin_de_dia=False),
            publicado_hasta=borde_del_dia_utc_naive(dia, fin_de_dia=True),
            regiones=self.regiones,
            tamano_pagina=self.settings.mercado_backfill_tamano_pagina,
            numero_pagina=pagina,
            ordenar_por="FechaPublicacion",
        )
        if resp.paginacion.total_resultados >= _TOPE_RESULTADOS:
            aviso = f"{dia}: total_resultados >= {_TOPE_RESULTADOS}; ventana demasiado grande"
            self.resumen.avisos.append(aviso)
            raise _Corte(aviso)
        with Session(self.engine) as session:
            _procesar_pagina(
                session, resp, _Totales(), contexto=f"mercado-backfill {dia} pág {pagina}"
            )
        self.resumen.ca_listadas += len(resp.items)
        objetivo = [
            ca.codigo for ca in resp.items if ca.estado.strip().lower() in self.estados_detalle
        ]
        pendientes = self._sin_cotizaciones(objetivo)
        for codigo in objetivo:
            if codigo in pendientes:
                self._detalle(codigo)
        return resp.paginacion.total_paginas

    def _pendientes_del_mes(self, mes: str) -> list[str]:
        dias = _dias_del_mes(mes)
        with Session(self.engine) as session:
            return list(
                session.execute(
                    select(CompraAgil.codigo)
                    .where(
                        CompraAgil.region.in_(self.regiones),
                        CompraAgil.estado.in_(sorted(self.estados_detalle)),
                        CompraAgil.cotizaciones_obtenidas_en.is_(None),
                        CompraAgil.fecha_publicacion >= borde_del_dia_utc_naive(dias[0], fin_de_dia=False),
                        CompraAgil.fecha_publicacion <= borde_del_dia_utc_naive(dias[-1], fin_de_dia=True),
                    )
                    .order_by(CompraAgil.codigo)
                ).scalars()
            )

    def _ca_del_mes(self, mes: str) -> int:
        dias = _dias_del_mes(mes)
        with Session(self.engine) as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(CompraAgil)
                    .where(
                        CompraAgil.region.in_(self.regiones),
                        CompraAgil.fecha_publicacion >= borde_del_dia_utc_naive(dias[0], fin_de_dia=False),
                        CompraAgil.fecha_publicacion <= borde_del_dia_utc_naive(dias[-1], fin_de_dia=True),
                    )
                ).scalar_one()
            )

    def _guardar(self, mes: str, valor: str | None) -> None:
        with Session(self.engine) as session:
            _guardar_cursor_mes(session, mes, valor)
            session.commit()

    def _mes(self, mes: str, hoy: date) -> str:
        """Recorre un mes. Devuelve 'datos' (tiene CA, aunque esté `completo` de una noche
        anterior y no se pida nada), 'saltado' (`vacio` de antes, o mes en curso sin CA) o
        'vacio' (terminado y con 0 CA; lo marca `correr`).

        Un mes `completo` cuenta como 'datos' (F-merc-1-fix): desde la segunda noche los
        meses recientes ya están completos, y si no contaran, tres meses viejos vacíos
        seguidos cortarían con "revisar filtros" y se volverían a listar cada noche.
        """
        with Session(self.engine) as session:
            valor = _leer_cursor_mes(session, mes)
        if valor == CURSOR_COMPLETO:
            return "datos"
        if valor == CURSOR_VACIO:
            return "saltado"
        dias = _dias_del_mes(mes)
        terminado = dias[-1] < hoy
        if valor != CURSOR_LISTADO:
            dia, pagina = _parsear_cursor_dia(valor, mes)
            while dia <= dias[-1] and dia < hoy:
                total_paginas = self._pagina(mes, dia, pagina)
                if pagina >= total_paginas:
                    dia, pagina = dia + timedelta(days=1), 1
                else:
                    pagina += 1
                self._guardar(mes, f"{dia.isoformat()}|{pagina}")
            hay_ca = self._ca_del_mes(mes) > 0
            if not terminado:
                # El mes en curso nunca queda listado ni completo.
                return "datos" if hay_ca else "saltado"
            if not hay_ca:
                # Nunca "listado" sin CA (F-merc-0b-fix2): `correr` decide si es vacío
                # de verdad o un filtro roto. El cursor queda al final del mes: volver a
                # evaluarlo no gasta requests.
                return "vacio"
            self._guardar(mes, CURSOR_LISTADO)

        # Reintento de los que fallaron en el camino.
        for codigo in self._pendientes_del_mes(mes):
            self._detalle(codigo, cortar_tras_fallos=False)
        restantes = len(self._pendientes_del_mes(mes))
        if restantes:
            self.resumen.avisos.append(f"{mes}: {restantes} detalles pendientes; sigue abierto")
            return "datos"
        self._guardar(mes, CURSOR_COMPLETO)
        self.resumen.meses_completos.append(mes)
        return "datos"

    def _marcar_vacios(self, meses: list[str]) -> None:
        for mes in meses:
            self._guardar(mes, CURSOR_VACIO)
            aviso = f"{mes}: 0 CA en todo el mes; marcado vacío (no se vuelve a pedir)"
            _log.warning("mercado-backfill: %s", aviso)
            self.resumen.avisos.append(aviso)
        meses.clear()

    def correr(self) -> ResumenBackfill:
        """Tramos hasta terminar; entre tramo y tramo, la pausa de un 429 (F-merc-1c).

        Un tramo que termina por un 429 de tasa (el cliente ya persistió la pausa) o por
        una pausa vigente deja su avance guardado, como pide la regla 3; ningún request
        se repite. Si la pausa cabe en la noche (ver :meth:`_esperar_pausa`), el job
        duerme hasta su fin + jitter y empieza otro tramo: los cursores por mes dejan
        todo en su lugar (la página cortada se vuelve a listar y sus detalles ya
        guardados no se piden). Si no cabe, termina como antes de F-merc-1c.
        """
        while True:
            antes = self.resumen.requests
            pausa = self._tramo()
            if pausa is None:
                return self.resumen
            if isinstance(pausa, MPTasaError):
                self.resumen.requests_por_tramo.append(self.resumen.requests - antes)
            if not self._esperar_pausa(pausa, self.resumen.requests - antes):
                return self.resumen

    def _esperar_pausa(self, causa: Exception, requests_tramo: int) -> bool:
        """Duerme hasta el fin de la pausa + jitter si cabe en la noche. True si hay que seguir.

        El fin se lee de Postgres (`api_pausa`), no del error: otro proceso pudo
        alargarla (regla 10). Se sigue solo si quedan pausas
        (`mercado_backfill_max_pausas`), si el despertar cae antes de las 06:45 Chile
        dentro de la ventana 22–07 (regla 5) y antes del tope de minutos menos 5 (el
        reloj cuenta el sueño). Al despertar se relee: si otro proceso la alargó, se
        espera de nuevo (otra pausa). Mientras duerme no queda ninguna `Session`
        abierta: cada paso abre y cierra la suya, y `leer_pausa` devuelve su conexión.
        El advisory lock del job (otra conexión, de `_run_with_lock`) se retiene.
        """
        maximo = self.settings.mercado_backfill_max_pausas
        motivo = (
            f"429 tras {requests_tramo} requests del tramo"
            if isinstance(causa, MPTasaError)
            else f"API en pausa tras {requests_tramo} requests del tramo"
        )
        while True:
            if self.resumen.pausas >= maximo:
                self.resumen.motivo_fin += f"; max pausas ({maximo}) alcanzado"
                return False
            ahora = self.now_fn(TZ_CHILE)
            hasta = leer_pausa(self.engine)
            fin = max(hasta, ahora) if hasta is not None else ahora
            despertar = fin + timedelta(seconds=self.jitter_fn())
            espera = (despertar - ahora).total_seconds()
            if not en_ventana_nocturna(lambda tz, d=despertar: d.astimezone(tz)) or _tras_fin_noche(
                despertar
            ):
                self.resumen.motivo_fin += (
                    f"; la pausa termina después de las {_FIN_NOCHE[0]:02d}:{_FIN_NOCHE[1]:02d}"
                )
                return False
            tope_s = self.settings.mercado_backfill_minutos * 60 - _HOLGURA_TOPE_S
            if self.reloj() - self.inicio + espera >= tope_s:
                self.resumen.motivo_fin += (
                    f"; la pausa no cabe en el tope de {self.settings.mercado_backfill_minutos} min"
                )
                return False
            self.resumen.pausas += 1
            _log.info(
                "mercado-backfill: %s; espero %.1f min, hasta %s Chile (pausa %d/%d)",
                motivo,
                espera / 60,
                f"{despertar.astimezone(TZ_CHILE):%H:%M}",
                self.resumen.pausas,
                maximo,
            )
            self.resumen.minutos_esperados += espera / 60
            self.sleep_fn(espera)
            hasta = leer_pausa(self.engine)
            if hasta is None or self.now_fn(TZ_CHILE) >= hasta:
                return True
            motivo = "la pausa sigue vigente al despertar (otro proceso la alargó)"

    def _tramo(self) -> Exception | None:
        """Un tramo: lo que antes era la corrida entera. Devuelve el error de pausa que lo
        cortó (MPTasaError o APIPausadaError) o None si terminó por cualquier otra razón.

        Un mes vacío no corta: se marca y se sigue con el siguiente. Pero si los
        `_VACIOS_SOSPECHOSOS` primeros meses con requests dan 0 y no hubo ninguno con
        datos (ni ya completo ni listado), es más probable un filtro roto que tres meses
        sin CA: corta, deja esos cursores en blanco (se vuelven a listar) y avisa
        "revisar filtros".
        """
        # Por tramo: tras dormir pudo cambiar el día (y el mes en curso).
        hoy = self.now_fn(TZ_CHILE).date()
        self.resumen.cortado_por = None
        self.resumen.motivo_fin = "sin meses pendientes"
        hubo_datos = False
        vacios: list[str] = []
        try:
            for mes in meses_backfill(self.settings, hoy):
                estado = self._mes(mes, hoy)
                if estado == "datos":
                    hubo_datos = True
                    self._marcar_vacios(vacios)
                elif estado == "vacio":
                    vacios.append(mes)
                    if hubo_datos:
                        self._marcar_vacios(vacios)
                    elif len(vacios) >= _VACIOS_SOSPECHOSOS:
                        for m in vacios:
                            self._guardar(m, None)
                        aviso = f"{', '.join(vacios)}: 0 CA en todos; revisar filtros"
                        _log.warning("mercado-backfill: %s", aviso)
                        self.resumen.avisos.append(aviso)
                        raise _Corte(aviso)
            # Fin natural de la lista: los vacíos que quedaron son vacíos de verdad.
            self._marcar_vacios(vacios)
        except _Corte as exc:
            self.resumen.motivo_fin = str(exc)
        except MPAuthError:
            raise
        except _ERRORES_DE_CORTE as exc:
            # El cliente ya persistió la pausa (429 de tasa); el cursor quedó en la
            # última página completa (regla 3). 10500 agotado y cuota no son pausas:
            # terminan el job como antes.
            self.resumen.cortado_por = type(exc).__name__
            self.resumen.motivo_fin = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, ERRORES_DE_PAUSA):
                return exc
        return None


def run_backfill(
    settings: Settings,
    engine: Engine,
    v2: MercadoPublicoV2Client,
    now_fn: Callable[..., datetime] | None = None,
    reloj: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] | None = None,
    jitter_fn: Callable[[], float] = _jitter_pausa,
) -> dict[str, Any]:
    """Job `mercado-backfill`. Solo entre 22:00 y 07:00 de Chile (regla 5); fuera, omitido.

    Por mes (ver :func:`meses_backfill`): ventanas de UN día de publicación,
    `ordenar_por=FechaPublicacion`, región en las regiones activas (F-plat-3), sin `estado`;
    upsert básico de todo lo listado y detalle de las CA en
    `mercado_backfill_estados_detalle` que aún no tienen cotizaciones. Cursor en
    `sync_state` `mercado_backfill:AAAA-MM`: 'día|página' siguiente, `listado`
    (quedan detalles fallidos) o `completo`. Reanudable; un mes completo no se
    vuelve a pedir. Para a las 06:45, a los `mercado_backfill_minutos` o con la
    cuota del día menos `mercado_backfill_margen_cuota`.

    Un 429 de tasa corta el tramo guardando el avance y, hasta
    `mercado_backfill_max_pausas` veces, el job duerme hasta el fin de la pausa y
    sigue (F-merc-1c, :meth:`Backfill.correr`). Mientras duerme RETIENE el advisory
    lock (regla 13): soltarlo y retomarlo abriría la puerta a dos backfills. Efecto
    aceptado: un `mercado-ca` disparado de noche espera el lock 10 min y sale omitido
    (código 0), igual que mientras el backfill pide; durante la pausa tampoco podría
    llamar a la API.
    """
    if not en_ventana_nocturna(now_fn):
        _log.warning("mercado-backfill: fuera de la ventana 22:00–07:00 Chile — omitido")
        return {"omitido_fuera_de_ventana": True}
    resumen = Backfill(
        settings,
        engine,
        v2,
        now_fn=now_fn,
        reloj=reloj,
        sleep_fn=sleep_fn,
        jitter_fn=jitter_fn,
        regiones=regiones_activas_engine(engine, settings),
    ).correr()
    _log.info(
        "mercado-backfill: %s",
        ", ".join(f"{k}={v}" for k, v in resumen.como_dict().items()),
    )
    return resumen.como_dict()
