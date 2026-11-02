"""Backfill de cotizaciones de Compra Ágil de Ñuble con la key actual, a CSV (F-merc-0b).

Puente hasta tener la segunda key (F-merc-1). Lo ejecuta SOLO el humano, desde su PC,
como `smoke_test.py`: NO en Actions ni en tests automáticos. Usa `MP_TICKET`, el
`QuotaTracker` de producción y respeta y persiste la pausa de 429 de `api_pausa`.
Corrección 31-oct: la ingesta de la app está en pausa mientras corre este puente, así que
no compite con producción: `reserva=0` y `--rps 0.5` por defecto (el ritmo de producción).

Solo entre 22:00 y 07:00 de Chile (regla 5), salvo `--diurno` (excepción 2-nov-2026, Boris,
mientras la ingesta de la app esté en pausa): corre de día con `--rps` <= 0.5 y
`--max-requests` <= 2000 (default 2000; de noche, 6000). El modo se fija al arrancar: una
corrida diurna que entra a la ventana nocturna sigue con los límites diurnos. Un 429 cierra la corrida guardando el avance
(regla 3); de noche, con `--seguir-tras-pausa` (default), el script duerme hasta el fin de la
pausa de `api_pausa` + 30–90 s y empieza otra corrida con el mismo estado, hasta
`--max-pausas` (15), `--max-requests` (6000) o el fin de la ventana.

Uso (PowerShell, desde la raíz del repo):
    $env:PYTHONIOENCODING = "utf-8"
    python scripts\\backfill_cot_nuble.py                                    # noche
    python scripts\\backfill_cot_nuble.py --diurno                           # de dia, 0.5 req/s, 2000 req
    python scripts\\backfill_cot_nuble.py --fuera-de-ventana --meses 2026-08 --max-requests 30

Por mes: listado por ventanas de un día de publicación SIN parámetro `estado` (el filtro
`estado=proveedor_seleccionado` devuelve 0 [V, 31-oct]) y filtro local por `estado.codigo`:
solo las CA en `--estados-detalle` (default `proveedor_seleccionado`) piden detalle. Avance
en `data/mercado/_backfill_estado.json`: reanudable, nunca pide dos veces un detalle guardado.
Un día con 0 resultados no se guarda como listado; un mes cuyos días terminados dan todos 0
no se marca completo y corta con aviso, igual que un día con `total_resultados` >= 10.000.
Salidas:
  - `cot_api_{AAAA-MM}.csv.gz`: una fila por (CA × proveedor × producto cotizado); una CA
    sin ofertas deja una fila por producto solicitado con el proveedor vacío.
  - `ca_api_listado_{AAAA-MM}.csv.gz`: TODAS las CA listadas, de cualquier estado, sin gastar
    detalles (sirve para medir desiertas, canceladas y CA sin ofertas). Si una noche se corta
    entre escribir un día y guardar el avance, ese día puede repetirse: deduplicar por `codigo`.

Nunca imprime ni escribe el ticket (regla 1). No escribe en la base salvo `quota_log` y la
pausa, que ya escriben los jobs.

Fuente: Dirección ChileCompra (API de Mercado Público).
"""

from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import json
import random
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402

from app.clients.base import (  # noqa: E402
    APIPausadaError,
    MPAuthError,
    MPParseError,
    MPRateLimitError,
    MPServerError,
    MPTasaError,
    QuotaExceededError,
)
from app.clients.mp_v2 import MercadoPublicoV2Client  # noqa: E402
from app.clients.types import CompraAgilBasica, CompraAgilDetalle  # noqa: E402
from app.core.tiempo import (  # noqa: E402
    TZ_CHILE,
    a_naive_como_la_api,
    borde_del_dia_utc_naive,
    en_ventana_nocturna,
)

DIR_SALIDA = Path(__file__).resolve().parent.parent / "data" / "mercado"
MESES_DEFAULT = "2026-08,2026-07,2026-06,2026-04,2026-03"
ESTADOS_DETALLE_DEFAULT = "proveedor_seleccionado"
# Tope del modo diurno (regla 5, excepción 2-nov-2026): ritmo de producción y requests por corrida.
_RPS_DIURNO_MAX = 0.5
_MAX_REQUESTS_DIURNO = 2000
_MAX_REQUESTS_NOCTURNO = 6000
# La API topa `total_resultados` en 10.000 [V, 31-oct]: más allá la ventana ya no es confiable.
_TOPE_RESULTADOS = 10_000
# Fallos seguidos de detalle (5xx, timeout, JSON roto) antes de cortar la noche.
_MAX_FALLOS_SEGUIDOS = 3
# Margen sobre el fin de la pausa antes de la corrida siguiente (F-merc-0b-fix).
_JITTER_PAUSA_S = (30.0, 90.0)

# Columnas de COT_ que lee scripts/mercado/mercado_observatorio.py, más las que solo da la API.
COLUMNAS = [
    "CodigoCotizacion", "Region", "RUTUnidaddeCompra", "NombreUnidaddeCompra",
    "MontoTotalDisponble", "CodigoProducto", "NombreProductoGenerico", "ProductoCotizado",
    "CantidadSolicitada", "Estado", "RazonSocialProveedor", "RUTProveedor", "Tamano",
    "ProveedorSeleccionado", "MontoTotal", "archivo",
    "PrecioUnitario", "MontoTotalProducto", "CantidadCotizada", "DireccionEntrega",
    "PlazoEntrega",
    # Por oferta: según la guía MontoTotal incluye IVA y despacho; el neto va aparte.
    "ValorNeto", "TotalImpuesto", "MontoDespacho",
]

# Listado completo de CA (cualquier estado), una fila por CA.
COLUMNAS_LISTADO = [
    "codigo", "estado", "fecha_publicacion", "fecha_cierre", "organismo", "unidad",
    "monto_disponible", "total_ofertas",
]

# Glosas como las escribe COT_ (el observatorio compara "desierta"/"cancelada" normalizado).
_GLOSA_ESTADO = {
    "proveedor_seleccionado": "Proveedor seleccionado",
    "desierta": "Desierta",
    "cancelada": "Cancelada",
    "cerrada": "Cerrada",
}


class Corte(Exception):
    """Fin limpio de la corrida: el avance ya quedó guardado."""


# ---------------------------------------------------------------------------
# Estado reanudable
# ---------------------------------------------------------------------------


class Estado:
    """`_backfill_estado.json`: días ya listados (con su estado por código) y detalles hechos.

    `listados["AAAA-MM|AAAA-MM-DD"]` = {código: estado} de ese día (nunca un día vacío);
    `hechos` = códigos cuyo detalle ya está en el CSV. Escritura atómica (tmp + replace).
    """

    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta
        datos: dict[str, Any] = {}
        if ruta.exists():
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        # Solo el formato actual: las claves por "pase" de F-merc-0b se descartan.
        self.listados: dict[str, dict[str, str]] = {
            k: dict(v) for k, v in datos.get("listados", {}).items() if isinstance(v, dict)
        }
        self.hechos: set[str] = set(datos.get("hechos", []))
        self.meses_completos: set[str] = set(datos.get("meses_completos", []))

    def guardar(self) -> None:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ruta.with_suffix(".tmp")
        datos = {
            "listados": self.listados,
            "hechos": sorted(self.hechos),
            "meses_completos": sorted(self.meses_completos),
        }
        tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.ruta)


# ---------------------------------------------------------------------------
# Filas del CSV
# ---------------------------------------------------------------------------


def _txt(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _si_no(v: bool | None) -> str:
    return "" if v is None else ("si" if v else "no")


def filas_de_detalle(d: CompraAgilDetalle, mes: str) -> list[dict[str, str]]:
    """Una fila por (CA × proveedor × producto cotizado); sin ofertas, una por producto pedido."""
    comun = {
        "CodigoCotizacion": d.codigo,
        "Region": _txt(d.region),
        "RUTUnidaddeCompra": d.organismo_rut or "",
        "NombreUnidaddeCompra": d.unidad_compra or d.organismo_nombre or "",
        "MontoTotalDisponble": _txt(d.monto_clp),
        "Estado": _GLOSA_ESTADO.get(d.estado, d.estado),
        "archivo": mes,
        "DireccionEntrega": d.direccion_entrega or "",
        "PlazoEntrega": _txt(d.plazo_entrega_dias),
    }
    pedidos = {p.codigo_producto: p for p in d.productos if p.codigo_producto}
    filas: list[dict[str, str]] = []
    for c in d.cotizaciones:
        oferta = {
            **comun,
            "RazonSocialProveedor": c.razon_social or "",
            "RUTProveedor": c.rut_proveedor,
            "Tamano": "" if c.es_emt is None else ("MiPyme" if c.es_emt else "Grande"),
            "ProveedorSeleccionado": _si_no(c.seleccionado),
            "MontoTotal": _txt(c.monto_total),
            "ValorNeto": _txt(c.valor_neto),
            "TotalImpuesto": _txt(c.total_impuesto),
            "MontoDespacho": _txt(c.monto_despacho),
        }
        for p in c.productos or [None]:  # type: ignore[list-item]
            pedido = pedidos.get(p.codigo_producto or "") if p is not None else None
            filas.append({
                **oferta,
                "CodigoProducto": (p.codigo_producto or "") if p else "",
                "NombreProductoGenerico": (
                    (p.nombre_producto if p else None) or (pedido.nombre if pedido else "")
                ),
                "ProductoCotizado": (p.descripcion or "") if p else "",
                "CantidadSolicitada": _txt(pedido.cantidad) if pedido else "",
                "PrecioUnitario": _txt(p.precio_unitario) if p else "",
                "MontoTotalProducto": _txt(p.monto_total_producto) if p else "",
                "CantidadCotizada": _txt(p.cantidad) if p else "",
            })
    if not d.cotizaciones:
        for pedido in d.productos or [None]:  # type: ignore[list-item]
            filas.append({
                **comun,
                "CodigoProducto": pedido.codigo_producto if pedido else "",
                "NombreProductoGenerico": pedido.nombre if pedido else "",
                "ProductoCotizado": pedido.descripcion if pedido else "",
                "CantidadSolicitada": _txt(pedido.cantidad) if pedido else "",
            })
    return [{col: f.get(col, "") for col in COLUMNAS} for f in filas]


def filas_de_listado(items: Iterable[CompraAgilBasica]) -> list[dict[str, str]]:
    """Una fila por CA del listado, de cualquier estado. Fechas en hora de Chile, como la API."""

    def fecha(dt: datetime | None) -> str:
        return "" if dt is None else a_naive_como_la_api(dt).isoformat(sep=" ", timespec="minutes")

    return [
        {
            "codigo": ca.codigo,
            "estado": ca.estado,
            "fecha_publicacion": fecha(ca.fecha_publicacion),
            "fecha_cierre": fecha(ca.fecha_cierre),
            "organismo": ca.organismo_nombre or "",
            "unidad": ca.unidad_compra or "",
            "monto_disponible": _txt(ca.monto_clp),
            "total_ofertas": _txt(ca.total_ofertas),
        }
        for ca in items
    ]


def escribir_filas(
    ruta: Path, filas: Iterable[dict[str, str]], columnas: list[str] = COLUMNAS
) -> None:
    """Agrega filas al .csv.gz del mes (gzip multi-miembro: lo lee cualquier lector)."""
    nuevo = not ruta.exists()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(ruta, "at", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columnas, delimiter=";")
        if nuevo:
            w.writeheader()
        w.writerows(filas)


# ---------------------------------------------------------------------------
# Corrida
# ---------------------------------------------------------------------------


@dataclass
class Resumen:
    requests: int = 0
    ca_listadas: int = 0
    detalles: int = 0
    filas: int = 0
    fallidos: int = 0
    horas_429: list[str] = field(default_factory=list)
    # Pausa que pidió la API en el último 429 de tasa.
    minutos_pausa: float = 0.0
    corridas: int = 0
    pausas: int = 0
    minutos_esperados: float = 0.0
    # Control de calidad: ofertas con proveedor_seleccionado=true por CA con detalle.
    ganador_1: int = 0
    ganador_0: int = 0
    ganador_varios: int = 0
    modo: str = "nocturno"
    motivo_fin: str = "completo"

    def imprimir(self) -> None:
        print("\n=== Resumen backfill COT Ñuble ===")
        print(f"Modo:              {self.modo}")
        print(f"Requests emitidas: {self.requests}")
        print(f"CA listadas:       {self.ca_listadas}")
        print(f"Detalles:          {self.detalles} (fallidos, quedan pendientes: {self.fallidos})")
        print(f"Filas escritas:    {self.filas}")
        print(
            "Ganadores por CA con detalle: "
            f"exactamente 1 = {self.ganador_1}, 0 = {self.ganador_0}, más de 1 = {self.ganador_varios}"
        )
        print(f"429 recibidos:     {len(self.horas_429)} {self.horas_429 or ''}".rstrip())
        print(f"Corridas:          {self.corridas} ({self.pausas} pausas esperadas)")
        print(f"Minutos esperados: {self.minutos_esperados:.0f}")
        print(f"Fin:               {self.motivo_fin}")
        print("Fuente: Dirección ChileCompra.")


def _dias_del_mes(mes: str, hoy: date) -> list[date]:
    y, m = (int(x) for x in mes.split("-"))
    ultimo = calendar.monthrange(y, m)[1]
    return [date(y, m, d) for d in range(1, ultimo + 1) if date(y, m, d) <= hoy]


class Backfill:
    def __init__(
        self,
        cliente: MercadoPublicoV2Client,
        *,
        meses: list[str],
        region: int,
        estados_detalle: list[str],
        max_requests: int,
        tamano_pagina: int,
        dir_salida: Path,
        fuera_de_ventana: bool = False,
        diurno: bool = False,
        seguir_tras_pausa: bool = False,
        max_pausas: int = 15,
        now_fn: Callable[..., datetime] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        jitter_fn: Callable[[], float] = lambda: random.uniform(*_JITTER_PAUSA_S),
    ) -> None:
        self.cliente = cliente
        self.meses = meses
        self.region = region
        self.estados_detalle = {e.strip().lower() for e in estados_detalle}
        self.max_requests = max_requests
        self.tamano_pagina = tamano_pagina
        self.dir_salida = dir_salida
        self.fuera_de_ventana = fuera_de_ventana
        # El modo queda fijo toda la corrida: entrar a la ventana nocturna no cambia los límites.
        self.diurno = diurno
        self.resumen = Resumen(modo="diurno" if diurno else "nocturno")
        if fuera_de_ventana:
            self.resumen.modo = "prueba fuera de ventana"
        self.seguir_tras_pausa = seguir_tras_pausa
        self.max_pausas = max_pausas
        self.now_fn = now_fn or (lambda tz=TZ_CHILE: datetime.now(tz))
        self.sleep_fn = sleep_fn
        self.jitter_fn = jitter_fn
        self.estado = Estado(dir_salida / "_backfill_estado.json")
        self._fallos_seguidos = 0
        # Cuenta CADA request emitida (también los reintentos del cliente) y
        # anota la hora de cada 429. Solo lee status y hora: el header `ticket`
        # de la request no se toca (regla 1).
        hooks = cliente._client._http.event_hooks
        hooks["request"] = [*hooks.get("request", []), self._al_emitir]
        hooks["response"] = [*hooks.get("response", []), self._al_responder]

    # -- instrumentación -----------------------------------------------------

    def _al_emitir(self, _request: httpx.Request) -> None:
        self.resumen.requests += 1

    def _al_responder(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            self.resumen.horas_429.append(f"{self.now_fn(TZ_CHILE):%H:%M:%S}")

    # -- cortes --------------------------------------------------------------

    def _revisar(self) -> None:
        if not (self.fuera_de_ventana or self.diurno) and not en_ventana_nocturna(self.now_fn):
            raise Corte("fuera de la ventana 22:00–07:00 Chile")
        if self.resumen.requests >= self.max_requests:
            raise Corte(f"--max-requests {self.max_requests} alcanzado")

    # -- pasos ---------------------------------------------------------------

    def _listar_dia(self, dia: date) -> list[CompraAgilBasica]:
        """Todas las CA publicadas ese día en la región, de cualquier estado (sin `estado`)."""
        items: list[CompraAgilBasica] = []
        pagina = 1
        while True:
            self._revisar()
            resp = self.cliente.listar_compra_agil(
                publicado_desde=borde_del_dia_utc_naive(dia, fin_de_dia=False),
                publicado_hasta=borde_del_dia_utc_naive(dia, fin_de_dia=True),
                regiones=[self.region],
                tamano_pagina=self.tamano_pagina,
                numero_pagina=pagina,
                ordenar_por="FechaPublicacion",
            )
            if resp.paginacion.total_resultados >= _TOPE_RESULTADOS:
                raise Corte(
                    f"{dia}: total_resultados >= {_TOPE_RESULTADOS}; la API topa ahí, "
                    "ventana demasiado grande: revisar filtros"
                )
            items += resp.items
            if pagina >= resp.paginacion.total_paginas:
                return items
            pagina += 1

    def _dia(self, mes: str, dia: date) -> dict[str, str]:
        """{código: estado} del día, de la caché del estado o listándolo.

        Un día con 0 resultados no se guarda: la próxima corrida lo vuelve a listar
        (1 request), así un filtro roto no deja un mes "listado" y vacío.
        """
        clave = f"{mes}|{dia.isoformat()}"
        if clave in self.estado.listados:
            return self.estado.listados[clave]
        items = self._listar_dia(dia)
        if not items:
            return {}
        escribir_filas(
            self.dir_salida / f"ca_api_listado_{mes}.csv.gz", filas_de_listado(items), COLUMNAS_LISTADO
        )
        por_codigo = {ca.codigo: ca.estado for ca in items if ca.codigo}
        self.estado.listados[clave] = por_codigo
        self.estado.guardar()
        self.resumen.ca_listadas += len(por_codigo)
        return por_codigo

    def _detalle(self, codigo: str, mes: str) -> None:
        self._revisar()
        try:
            d = self.cliente.detalle_compra_agil(codigo)
        except (MPServerError, MPParseError) as exc:
            # 5xx, timeout o JSON roto: queda pendiente para la próxima corrida.
            self.resumen.fallidos += 1
            self._fallos_seguidos += 1
            print(f"  detalle {codigo}: {type(exc).__name__}; queda pendiente")
            if self._fallos_seguidos >= _MAX_FALLOS_SEGUIDOS:
                raise Corte(f"{_MAX_FALLOS_SEGUIDOS} detalles fallidos seguidos") from exc
            return
        self._fallos_seguidos = 0
        ganadores = sum(1 for c in d.cotizaciones if c.seleccionado is True)
        if ganadores == 1:
            self.resumen.ganador_1 += 1
        elif ganadores == 0:
            self.resumen.ganador_0 += 1
        else:
            self.resumen.ganador_varios += 1
        filas = filas_de_detalle(d, mes)
        escribir_filas(self.dir_salida / f"cot_api_{mes}.csv.gz", filas)
        self.estado.hechos.add(codigo)
        self.estado.guardar()
        self.resumen.detalles += 1
        self.resumen.filas += len(filas)

    def _pendientes(self, mes: str) -> set[str]:
        """Códigos del mes en `--estados-detalle` cuyo detalle aún no está guardado."""
        return {
            codigo
            for k, por_codigo in self.estado.listados.items()
            if k.startswith(f"{mes}|")
            for codigo, estado in por_codigo.items()
            if estado.strip().lower() in self.estados_detalle and codigo not in self.estado.hechos
        }

    def _mes(self, mes: str) -> None:
        hoy = self.now_fn(TZ_CHILE).date()
        terminados = [d for d in _dias_del_mes(mes, hoy) if d < hoy]
        con_resultados = 0
        for dia in _dias_del_mes(mes, hoy):
            por_codigo = self._dia(mes, dia)
            con_resultados += bool(por_codigo) and dia < hoy
            for codigo, estado in por_codigo.items():
                if estado.strip().lower() in self.estados_detalle and codigo not in self.estado.hechos:
                    self._detalle(codigo, mes)
        print(f"{mes}: listo")
        if terminados and not con_resultados:
            raise Corte("mes sin resultados: revisar filtros")
        dias = _dias_del_mes(mes, date.max)
        # Completo solo si el mes ya terminó y no quedan detalles pendientes
        # (un 5xx deja el código fuera de `hechos` y la corrida siguiente lo pide).
        pendientes = self._pendientes(mes)
        if pendientes:
            print(f"{mes}: {len(pendientes)} detalles pendientes; el mes sigue abierto")
        elif dias and dias[-1] < hoy:  # el mes en curso nunca queda completo
            self.estado.meses_completos.add(mes)
            self.estado.guardar()

    def _una_corrida(self) -> bool:
        """Una corrida sobre los meses pendientes. True si terminó por una pausa de la API.

        Ante un 429 de tasa o una pausa vigente guarda el avance y cierra la
        corrida, como pide la regla 3; reanudar es decisión de `correr`.
        """
        self.resumen.corridas += 1
        self.resumen.motivo_fin = "completo"
        try:
            for mes in self.meses:
                if mes in self.estado.meses_completos:
                    print(f"{mes}: completo en una corrida anterior, salto")
                    continue
                print(f"{mes}: empezando")
                self._mes(mes)
        except Corte as exc:
            self.resumen.motivo_fin = str(exc)
        except MPTasaError as exc:
            # El cliente ya persistió la pausa en api_pausa.
            self.resumen.minutos_pausa = exc.retry_after_seconds / 60
            self.resumen.motivo_fin = f"429 ({exc.causa}): API en pausa"
            return True
        except APIPausadaError as exc:
            self.resumen.motivo_fin = f"sin emitir: {exc}"
            return True
        except QuotaExceededError as exc:
            self.resumen.motivo_fin = f"sin emitir: {exc}"
        except MPRateLimitError as exc:  # 10500 con los reintentos agotados
            self.resumen.motivo_fin = f"429 de concurrencia: {exc}"
        finally:
            self.estado.guardar()
        return False

    def _fin_de_pausa(self) -> datetime | None:
        return self.cliente._client._quota.pausada_hasta()

    def _esperar_pausa(self) -> bool:
        """Duerme hasta el fin de la pausa + jitter si se puede seguir. True si durmió.

        Mismo patrón que F-429-pausa-ca: el fin se lee de sync_state (api_pausa),
        así que vale también una pausa que puso otro proceso.
        """
        if self.resumen.pausas >= self.max_pausas:
            self.resumen.motivo_fin += f"; --max-pausas {self.max_pausas} alcanzado"
            return False
        if self.resumen.requests >= self.max_requests:
            self.resumen.motivo_fin += f"; --max-requests {self.max_requests} alcanzado"
            return False
        ahora = self.now_fn(TZ_CHILE)
        hasta = self._fin_de_pausa()
        espera = max(0.0, (hasta - ahora).total_seconds()) if hasta is not None else 0.0
        espera += self.jitter_fn()
        despertar = ahora + timedelta(seconds=espera)
        if not self.diurno and not en_ventana_nocturna(lambda tz: despertar.astimezone(tz)):
            self.resumen.motivo_fin += "; la pausa termina fuera de la ventana 22–07, no se reanuda"
            return False
        print(f"Pausa: espero {espera / 60:.1f} min, hasta {despertar.astimezone(TZ_CHILE):%H:%M} Chile")
        self.resumen.pausas += 1
        self.resumen.minutos_esperados += espera / 60
        self.sleep_fn(espera)
        return True

    def correr(self) -> Resumen:
        try:
            while self._una_corrida() and self.seguir_tras_pausa and self._esperar_pausa():
                pass
        except KeyboardInterrupt:
            self.resumen.motivo_fin = "interrumpido a mano"
        finally:
            self.estado.guardar()
        return self.resumen


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parsear_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--meses", default=MESES_DEFAULT, help=f"AAAA-MM separados por coma (def. {MESES_DEFAULT})")
    ap.add_argument("--region", type=int, default=16)
    ap.add_argument(
        "--estados-detalle",
        default=ESTADOS_DETALLE_DEFAULT,
        help="estados (estado.codigo) cuyas CA piden detalle; el listado trae todos (def. "
        f"{ESTADOS_DETALLE_DEFAULT})",
    )
    ap.add_argument("--rps", type=float, default=0.5, help="requests por segundo (def. 0.5)")
    ap.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help=f"tope de requests por corrida (def. {_MAX_REQUESTS_NOCTURNO}; "
        f"{_MAX_REQUESTS_DIURNO} con --diurno)",
    )
    ap.add_argument("--tamano-pagina", type=int, default=50)
    ap.add_argument(
        "--fuera-de-ventana",
        action="store_true",
        help="solo para la prueba corta de día; exige --max-requests ≤ 30",
    )
    ap.add_argument(
        "--diurno",
        action="store_true",
        help=f"correr fuera de 22:00–07:00 con rps <= {_RPS_DIURNO_MAX} y <= {_MAX_REQUESTS_DIURNO} "
        "requests (regla 5, excepción 2-nov-2026: solo con la ingesta de la app en pausa)",
    )
    ap.add_argument(
        "--seguir-tras-pausa",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="tras un 429, esperar la pausa y seguir (def.: sí de noche, no con --fuera-de-ventana)",
    )
    ap.add_argument("--max-pausas", type=int, default=15, help="pausas esperadas por noche (def. 15)")
    a = ap.parse_args(argv)
    if a.diurno and a.fuera_de_ventana:
        ap.error("--diurno y --fuera-de-ventana son excluyentes")
    if a.max_requests is None:
        a.max_requests = _MAX_REQUESTS_DIURNO if a.diurno else _MAX_REQUESTS_NOCTURNO
    if a.fuera_de_ventana and a.max_requests > 30:
        ap.error("--fuera-de-ventana solo se permite con --max-requests 30 o menos")
    if a.diurno:
        # Los topes diurnos no se pueden superar: se bajan con aviso.
        if a.rps > _RPS_DIURNO_MAX:
            print(f"Aviso: --diurno limita --rps a {_RPS_DIURNO_MAX} (pidieron {a.rps}).")
            a.rps = _RPS_DIURNO_MAX
        if a.max_requests > _MAX_REQUESTS_DIURNO:
            print(
                f"Aviso: --diurno limita --max-requests a {_MAX_REQUESTS_DIURNO} "
                f"(pidieron {a.max_requests})."
            )
            a.max_requests = _MAX_REQUESTS_DIURNO
    # Con --fuera-de-ventana nunca se espera una pausa: la prueba de día es corta.
    a.seguir_tras_pausa = not a.fuera_de_ventana and a.seguir_tras_pausa is not False
    return a


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    from sqlalchemy import create_engine

    from app.core.db import normalizar_url_driver
    from app.core.logging import setup_logging
    from app.core.settings import Settings

    a = parsear_args(argv)
    if not (a.fuera_de_ventana or a.diurno) and not en_ventana_nocturna():
        print("Fuera de la ventana 22:00–07:00 de Chile (regla 5). No se emite nada; "
              "con la ingesta de la app en pausa se puede usar --diurno.")
        return 0

    load_dotenv()
    setup_logging()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    settings = Settings()  # type: ignore[call-arg]
    # El limiter del proceso se crea acá, con el ritmo de --rps.
    settings = settings.model_copy(update={"rate_limit_rps": a.rps})
    engine = create_engine(normalizar_url_driver(settings.database_url), pool_pre_ping=True)
    # reserva=0 (corrección 31-oct): la ingesta de la app está en pausa, no hay `ca` que proteger.
    cliente = MercadoPublicoV2Client(settings, engine, reserva=0)

    bf = Backfill(
        cliente,
        meses=[m.strip() for m in a.meses.split(",") if m.strip()],
        region=a.region,
        estados_detalle=[e.strip() for e in a.estados_detalle.split(",") if e.strip()],
        max_requests=a.max_requests,
        tamano_pagina=a.tamano_pagina,
        dir_salida=DIR_SALIDA,
        fuera_de_ventana=a.fuera_de_ventana,
        diurno=a.diurno,
        seguir_tras_pausa=a.seguir_tras_pausa,
        max_pausas=a.max_pausas,
    )
    try:
        bf.correr().imprimir()
    except MPAuthError:
        print("401: ticket inválido. Revisar MP_TICKET en .env.")
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
