"""Tests F-merc-0b: `scripts/backfill_cot_nuble.py` (red mockeada con respx).

`scripts/` no es un paquete: el módulo se carga por ruta. SQLite en memoria con el
esquema completo, porque la pausa de 429 vive en sync_state. El reloj de la ventana
se inyecta; ningún test duerme.
"""

from __future__ import annotations

import copy
import csv
import gzip
import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.clients.mp_v2 import MercadoPublicoV2Client
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE

_TICKET = "ticket-secreto-de-test-9876"
_LISTADO = "https://api2.mercadopublico.cl/v2/compra-agil"
_DETALLE = _LISTADO + "/{codigo}"
_NOCHE = datetime(2026, 10, 31, 23, 0, tzinfo=TZ_CHILE)
_DIA = datetime(2026, 10, 31, 15, 0, tzinfo=TZ_CHILE)


def _cargar() -> ModuleType:
    ruta = Path(__file__).parent.parent / "scripts" / "backfill_cot_nuble.py"
    spec = importlib.util.spec_from_file_location("backfill_cot_nuble", ruta)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # @dataclass busca el módulo en sys.modules al procesar las anotaciones.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bf = _cargar()


@pytest.fixture()
def engine() -> Iterator[Engine]:
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.clients.base.time.sleep", lambda _s: None)


@pytest.fixture()
def cliente(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> MercadoPublicoV2Client:
    monkeypatch.setenv("MP_TICKET", _TICKET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@x/x")
    monkeypatch.setenv("SECRET_KEY", "clave-de-test-32bytesxxxxxxxxxx")
    monkeypatch.setenv("JOBS_TOKEN", "token-de-test-jobs-abcdefgh1234")
    settings = Settings(_env_file=None, rate_limit_rps=1000.0)  # type: ignore[call-arg]
    return MercadoPublicoV2Client(settings, engine, reserva=0)


def _detalle(codigo: str, *, con_ofertas: bool = True) -> dict[str, Any]:
    ofertas = [
        {
            "rut_proveedor": "11111111-1",
            "razon_social": "Uno SpA",
            "es_emt": True,
            "proveedor_seleccionado": True,
            "monto_total": 11305,
            "valor_neto": 9000,
            "total_impuesto": 1710,
            "monto_despacho": 595,
            "productos_cotizados": [
                {"codigo_producto": 50131700, "nombre_producto": "Leche", "cantidad": 10,
                 "precio_unitario": 950, "monto_total_producto": 9500},
                {"codigo_producto": 50221100, "nombre_producto": "Arroz", "cantidad": 5,
                 "precio_unitario": "1.200", "monto_total_producto": 6000},
            ],
        },
        {
            "rut_proveedor": "22222222-2",
            "razon_social": "Dos Ltda",
            "es_emt": False,
            "proveedor_seleccionado": False,
            "monto_total": 13000,
            "productos_cotizados": [
                {"codigo_producto": 50131700, "nombre_producto": "Leche", "cantidad": 10,
                 "precio_unitario": 1100, "monto_total_producto": 11000},
            ],
        },
    ]
    payload = {
        "codigo": codigo,
        "nombre": "Alimentos",
        "estado": {"codigo": "proveedor_seleccionado"},
        "institucion": {"region": 16, "organismo_comprador": "Muni Ficticia", "rut": "69.000.000-0"},
        "presupuesto": {"monto_disponible_clp": 20000},
        "entrega": {"direccion_entrega": "Calle 1", "plazo_entrega_dias": 5},
        "productos_solicitados": [
            {"codigo_producto": 50131700, "nombre": "Leche", "cantidad": 10},
            {"codigo_producto": 50221100, "nombre": "Arroz", "cantidad": 5},
        ],
        "proveedores_cotizando": ofertas if con_ofertas else [],
    }
    return {"success": "OK", "payload": payload, "errors": []}


def _listado(
    items: list[tuple[str, str]], total_resultados: int | None = None
) -> dict[str, Any]:
    return {
        "success": "OK",
        "payload": {
            "convocatorias": [
                {
                    "codigo": c,
                    "nombre": f"CA {c}",
                    "estado": {"codigo": estado, "glosa": estado},
                    "fechas": {"fecha_publicacion": "2026-08-01 09:00",
                               "fecha_cierre": "2026-08-05 15:00"},
                    "institucion": {"region": 16, "organismo_comprador": "Muni Ficticia",
                                    "unidad_compra": "Educación"},
                    "montos": {"monto_disponible_clp": 20000},
                    "resumen": {"total_ofertas_recibidas": 2},
                }
                for c, estado in items
            ],
            "paginacion": {
                "total_paginas": 1,
                "total_resultados": len(items) if total_resultados is None else total_resultados,
            },
        },
        "errors": [],
    }


def _mock_listado(
    por_dia: dict[Any, list[Any]], total_resultados: int | None = None
) -> respx.Route:
    """`por_dia[AAAA-MM-DD]` = CA de ese día; el resto de días, vacío.

    Cada CA es un código (estado `proveedor_seleccionado`) o `(código, estado)`. La clave
    `(día, estado)` de antes también vale y pone ese estado a sus códigos. El listado NO
    recibe `estado` (el filtro devuelve 0 [V]): si llegara, la respuesta sería vacía.
    """

    def normalizar(clave: Any, cas: list[Any]) -> tuple[str, list[tuple[str, str]]]:
        dia, estado = clave if isinstance(clave, tuple) else (clave, "proveedor_seleccionado")
        return dia, [ca if isinstance(ca, tuple) else (ca, estado) for ca in cas]

    dias: dict[str, list[tuple[str, str]]] = {}
    for clave, cas in por_dia.items():
        dia, items = normalizar(clave, cas)
        dias.setdefault(dia, []).extend(items)

    def responder(request: httpx.Request) -> httpx.Response:
        if "estado" in request.url.params:
            return httpx.Response(200, json=_listado([]))
        dia = request.url.params["publicado_desde"][:10]
        return httpx.Response(200, json=_listado(dias.get(dia, []), total_resultados))

    return respx.get(_LISTADO).mock(side_effect=responder)


def _backfill(cliente: MercadoPublicoV2Client, tmp_path: Path, **kw: Any) -> Any:
    opciones: dict[str, Any] = {
        "meses": ["2026-08"],
        "region": 16,
        "estados_detalle": ["proveedor_seleccionado"],
        "max_requests": 500,
        "tamano_pagina": 50,
        "dir_salida": tmp_path,
        "now_fn": lambda tz=TZ_CHILE: _NOCHE.astimezone(tz),
    }
    opciones.update(kw)
    return bf.Backfill(cliente, **opciones)


def _leer_csv(ruta: Path) -> tuple[list[str], list[dict[str, str]]]:
    with gzip.open(ruta, "rt", encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f, delimiter=";")
        filas = list(rd)
        return list(rd.fieldnames or []), filas


# ---------------------------------------------------------------------------


@respx.mock
def test_fuera_de_ventana_no_emite_nada(cliente, tmp_path):
    ruta = _mock_listado({})
    r = _backfill(cliente, tmp_path, now_fn=lambda tz=TZ_CHILE: _DIA.astimezone(tz)).correr()

    assert "fuera de la ventana" in r.motivo_fin
    assert r.requests == 0
    assert not ruta.called


@respx.mock
def test_ventana_respeta_el_cambio_de_horario(cliente, tmp_path):
    """06:30 Chile en invierno y en verano: dentro; 07:30: fuera (reloj en UTC)."""
    _mock_listado({})
    # 2026-07-15 10:30 UTC = 06:30 Chile (UTC−4); 2026-10-31 10:30 UTC = 07:30 Chile (UTC−3).
    from datetime import UTC

    dentro = datetime(2026, 7, 15, 10, 30, tzinfo=UTC)
    fuera = datetime(2026, 10, 31, 10, 30, tzinfo=UTC)
    r = _backfill(cliente, tmp_path, now_fn=lambda tz=TZ_CHILE: fuera.astimezone(tz)).correr()
    assert "fuera de la ventana" in r.motivo_fin
    assert bf.en_ventana_nocturna(lambda tz: dentro.astimezone(tz))


def test_fuera_de_ventana_exige_max_requests_chico():
    with pytest.raises(SystemExit):
        bf.parsear_args(["--fuera-de-ventana"])
    a = bf.parsear_args(["--fuera-de-ventana", "--max-requests", "30", "--meses", "2026-08"])
    assert a.max_requests == 30
    assert a.rps == 0.5
    assert a.seguir_tras_pausa is False  # apagada con --fuera-de-ventana
    a = bf.parsear_args(["--fuera-de-ventana", "--max-requests", "30", "--seguir-tras-pausa"])
    assert a.seguir_tras_pausa is False


def test_defaults_de_noche():
    a = bf.parsear_args([])
    assert a.seguir_tras_pausa is True
    assert a.max_pausas == 15
    assert a.max_requests == 6000
    assert bf.parsear_args(["--no-seguir-tras-pausa"]).seguir_tras_pausa is False
    assert bf.parsear_args([]).diurno is False


def test_diurno_defaults_y_topes(capsys):
    a = bf.parsear_args(["--diurno"])
    assert a.diurno is True
    assert a.max_requests == 2000
    assert a.rps == 0.5
    assert a.seguir_tras_pausa is True
    assert a.max_pausas == 15
    assert capsys.readouterr().out == ""  # sin pedir de más no hay aviso


def test_diurno_baja_el_rps_a_0_5_con_aviso(capsys):
    a = bf.parsear_args(["--diurno", "--rps", "2"])
    assert a.rps == 0.5
    assert "limita --rps a 0.5" in capsys.readouterr().out
    # Un ritmo menor se respeta.
    assert bf.parsear_args(["--diurno", "--rps", "0.2"]).rps == 0.2


def test_diurno_baja_max_requests_a_2000_con_aviso(capsys):
    a = bf.parsear_args(["--diurno", "--max-requests", "5000"])
    assert a.max_requests == 2000
    assert "limita --max-requests a 2000" in capsys.readouterr().out
    assert bf.parsear_args(["--diurno", "--max-requests", "300"]).max_requests == 300


def test_sin_diurno_nada_cambia(capsys):
    a = bf.parsear_args(["--rps", "2", "--max-requests", "5000"])
    assert (a.rps, a.max_requests) == (2.0, 5000)  # de noche no hay tope de rps
    assert bf.parsear_args([]).max_requests == 6000
    assert capsys.readouterr().out == ""


def test_diurno_y_fuera_de_ventana_son_excluyentes():
    with pytest.raises(SystemExit):
        bf.parsear_args(["--diurno", "--fuera-de-ventana", "--max-requests", "30"])


def test_main_sin_diurno_fuera_de_ventana_no_emite(monkeypatch, capsys):
    monkeypatch.setattr(bf, "en_ventana_nocturna", lambda *a, **k: False)
    assert bf.main([]) == 0
    assert "No se emite nada" in capsys.readouterr().out


@respx.mock
def test_corrida_completa_csv_con_las_columnas(cliente, tmp_path):
    _mock_listado({"2026-08-01": ["CA-1"], "2026-08-02": [("CA-2", "desierta")]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )
    d2 = respx.get(_DETALLE.format(codigo="CA-2")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-2", con_ofertas=False))
    )

    r = _backfill(cliente, tmp_path).correr()

    assert r.motivo_fin == "completo"
    assert r.detalles == 1
    assert not d2.called  # desierta: se lista, no pide detalle
    # 31 días de listado (una sola pasada, sin pases) + 1 detalle.
    assert r.requests == 32
    columnas, filas = _leer_csv(tmp_path / "cot_api_2026-08.csv.gz")
    assert columnas == bf.COLUMNAS
    ca1 = [f for f in filas if f["CodigoCotizacion"] == "CA-1"]
    assert len(ca1) == 3  # 2 productos del ganador + 1 del perdedor
    leche_gana = next(f for f in ca1 if f["RUTProveedor"] == "11111111-1"
                      and f["CodigoProducto"] == "50131700")
    assert leche_gana["PrecioUnitario"] == "950"
    assert leche_gana["ProveedorSeleccionado"] == "si"
    assert leche_gana["Tamano"] == "MiPyme"
    assert leche_gana["CantidadSolicitada"] == "10"
    assert leche_gana["Estado"] == "Proveedor seleccionado"
    assert leche_gana["archivo"] == "2026-08"
    assert (leche_gana["MontoTotal"], leche_gana["ValorNeto"]) == ("11305", "9000")
    assert (leche_gana["TotalImpuesto"], leche_gana["MontoDespacho"]) == ("1710", "595")
    arroz = next(f for f in ca1 if f["CodigoProducto"] == "50221100")
    assert arroz["PrecioUnitario"] == "1200"
    estado = json.loads((tmp_path / "_backfill_estado.json").read_text(encoding="utf-8"))
    assert estado["meses_completos"] == ["2026-08"]


@respx.mock
def test_lista_sin_estado_y_filtra_localmente(cliente, tmp_path):
    ruta = _mock_listado({
        "2026-08-01": ["CA-1", ("CA-2", "desierta"), ("CA-3", "cancelada"), ("CA-4", "cerrada")],
    })
    detalle = respx.get(url__regex=r".*/v2/compra-agil/CA-.*").mock(
        side_effect=lambda req: httpx.Response(
            200, json=_detalle(req.url.path.rsplit("/", 1)[1])
        )
    )

    r = _backfill(cliente, tmp_path).correr()

    assert all("estado" not in c.request.url.params for c in ruta.calls)
    assert [c.request.url.path.rsplit("/", 1)[1] for c in detalle.calls] == ["CA-1"]
    assert r.ca_listadas == 4
    # Con --estados-detalle ampliado, las otras también piden detalle.
    r2 = _backfill(cliente, tmp_path / "otra",
                   estados_detalle=["proveedor_seleccionado", "desierta"]).correr()
    assert r2.detalles == 2


@respx.mock
def test_listado_completo_a_csv_con_todos_los_estados(cliente, tmp_path):
    _mock_listado({"2026-08-01": ["CA-1", ("CA-2", "desierta"), ("CA-3", "cancelada")]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )

    _backfill(cliente, tmp_path).correr()

    columnas, filas = _leer_csv(tmp_path / "ca_api_listado_2026-08.csv.gz")
    assert columnas == bf.COLUMNAS_LISTADO
    assert {f["codigo"]: f["estado"] for f in filas} == {
        "CA-1": "proveedor_seleccionado", "CA-2": "desierta", "CA-3": "cancelada"}
    ca2 = next(f for f in filas if f["codigo"] == "CA-2")
    assert ca2["organismo"] == "Muni Ficticia"
    assert ca2["unidad"] == "Educación"
    assert ca2["monto_disponible"] == "20000"
    assert ca2["total_ofertas"] == "2"
    assert ca2["fecha_publicacion"] == "2026-08-01 09:00"
    assert ca2["fecha_cierre"] == "2026-08-05 15:00"


@respx.mock
def test_dia_vacio_no_se_guarda_como_listado(cliente, tmp_path):
    _mock_listado({"2026-08-01": ["CA-1"]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )

    _backfill(cliente, tmp_path).correr()

    estado = json.loads((tmp_path / "_backfill_estado.json").read_text(encoding="utf-8"))
    assert list(estado["listados"]) == ["2026-08|2026-08-01"]


@respx.mock
def test_mes_sin_resultados_corta_y_no_se_marca_completo(cliente, tmp_path):
    ruta = _mock_listado({})

    r = _backfill(cliente, tmp_path, meses=["2026-08", "2026-07"]).correr()

    assert r.motivo_fin == "mes sin resultados: revisar filtros"
    assert r.requests == 31  # los 31 días de agosto; julio ni se toca
    assert ruta.call_count == 31
    estado = json.loads((tmp_path / "_backfill_estado.json").read_text(encoding="utf-8"))
    assert estado["meses_completos"] == []
    assert estado["listados"] == {}


@respx.mock
def test_dia_con_10000_resultados_corta_con_aviso(cliente, tmp_path):
    _mock_listado({"2026-08-01": ["CA-1"]}, total_resultados=10_000)

    r = _backfill(cliente, tmp_path).correr()

    assert "total_resultados >= 10000" in r.motivo_fin
    assert r.requests == 1
    estado = json.loads((tmp_path / "_backfill_estado.json").read_text(encoding="utf-8"))
    assert estado["listados"] == {}
    assert not (tmp_path / "ca_api_listado_2026-08.csv.gz").exists()


@respx.mock
def test_control_de_calidad_de_ganadores(cliente, tmp_path, capsys):
    _mock_listado({"2026-08-01": ["CA-1", "CA-0", "CA-N", "CA-X"]})
    ninguno = _detalle("CA-0")
    for o in ninguno["payload"]["proveedores_cotizando"]:
        o["proveedor_seleccionado"] = False
    sin_ofertas = _detalle("CA-N", con_ofertas=False)
    varios = _detalle("CA-X")
    for o in varios["payload"]["proveedores_cotizando"]:
        o["proveedor_seleccionado"] = True
    for codigo, cuerpo in (("CA-1", _detalle("CA-1")), ("CA-0", ninguno),
                           ("CA-N", sin_ofertas), ("CA-X", varios)):
        respx.get(_DETALLE.format(codigo=codigo)).mock(
            return_value=httpx.Response(200, json=cuerpo)
        )

    r = _backfill(cliente, tmp_path).correr()
    r.imprimir()

    assert (r.ganador_1, r.ganador_0, r.ganador_varios) == (1, 2, 1)
    assert "exactamente 1 = 1, 0 = 2, más de 1 = 1" in capsys.readouterr().out


@respx.mock
def test_reanuda_desde_el_json_sin_repetir_detalles(cliente, tmp_path):
    (tmp_path / "_backfill_estado.json").write_text(json.dumps({
        "listados": {
            "2026-08|2026-08-01": {"CA-1": "proveedor_seleccionado", "CA-2": "proveedor_seleccionado"},
            # Formato viejo (por "pase"): se descarta, no rompe.
            "2026-08|resto|2026-08-02": ["CA-9"],
        },
        "hechos": ["CA-1"],
    }), encoding="utf-8")
    listado = _mock_listado({})
    d1 = respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )
    d2 = respx.get(_DETALLE.format(codigo="CA-2")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-2"))
    )

    r = _backfill(cliente, tmp_path).correr()

    # El día 1 está en el estado: no se vuelve a listar. Los otros 30 sí (1 request c/u).
    pedidos = {c.request.url.params["publicado_desde"][:10] for c in listado.calls}
    assert "2026-08-01" not in pedidos
    assert len(pedidos) == 30
    assert not d1.called
    assert d2.call_count == 1
    assert r.requests == 31


@respx.mock
def test_corta_ante_429_de_tasa_con_pausa_persistida(cliente, tmp_path, engine):
    _mock_listado({("2026-08-01", "proveedor_seleccionado"): ["CA-1", "CA-2", "CA-3"]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )
    respx.get(_DETALLE.format(codigo="CA-2")).mock(
        return_value=httpx.Response(
            429, json={"errors": [{"codigo": "429", "mensaje": "límite de tasa"}]}
        )
    )
    d3 = respx.get(_DETALLE.format(codigo="CA-3")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-3"))
    )

    r = _backfill(cliente, tmp_path).correr()

    assert "429" in r.motivo_fin
    assert len(r.horas_429) == 1
    assert r.minutos_pausa == 20
    assert not d3.called
    with engine.connect() as conn:
        pausa = conn.execute(
            text("SELECT notas FROM sync_state WHERE fuente = 'api_pausa'")
        ).scalar()
    assert pausa == "429/429"
    estado = json.loads((tmp_path / "_backfill_estado.json").read_text(encoding="utf-8"))
    assert estado["hechos"] == ["CA-1"]
    assert estado["meses_completos"] == []

    # La corrida siguiente, aún en pausa, no emite nada y corta limpio.
    r2 = _backfill(cliente, tmp_path).correr()
    assert r2.requests == 0
    assert "pausa" in r2.motivo_fin


@respx.mock
def test_corta_en_max_requests_guardando_avance(cliente, tmp_path):
    _mock_listado({("2026-08-01", "proveedor_seleccionado"): ["CA-1", "CA-2"]})
    for c in ("CA-1", "CA-2"):
        respx.get(_DETALLE.format(codigo=c)).mock(
            return_value=httpx.Response(200, json=_detalle(c))
        )

    r = _backfill(cliente, tmp_path, max_requests=2).correr()

    assert "--max-requests" in r.motivo_fin
    assert r.requests == 2  # 1 listado + 1 detalle
    estado = json.loads((tmp_path / "_backfill_estado.json").read_text(encoding="utf-8"))
    assert estado["hechos"] == ["CA-1"]


@respx.mock
def test_detalle_con_5xx_queda_pendiente_y_sigue(cliente, tmp_path):
    _mock_listado({("2026-08-01", "proveedor_seleccionado"): ["CA-1", "CA-2"]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(return_value=httpx.Response(500, text="x"))
    respx.get(_DETALLE.format(codigo="CA-2")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-2"))
    )

    r = _backfill(cliente, tmp_path).correr()

    assert r.fallidos == 1
    assert r.detalles == 1
    estado = json.loads((tmp_path / "_backfill_estado.json").read_text(encoding="utf-8"))
    assert estado["hechos"] == ["CA-2"]
    # El mes queda abierto: un detalle pendiente no se pierde.
    assert estado["meses_completos"] == []


@respx.mock
def test_detalle_fallido_se_vuelve_a_pedir_en_la_corrida_siguiente(cliente, tmp_path):
    _mock_listado({("2026-08-01", "proveedor_seleccionado"): ["CA-1", "CA-2"]})
    d1 = respx.get(_DETALLE.format(codigo="CA-1")).mock(
        side_effect=[httpx.Response(500, text="x"), httpx.Response(500, text="x"),
                     httpx.Response(200, json=_detalle("CA-1"))]
    )
    respx.get(_DETALLE.format(codigo="CA-2")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-2"))
    )

    _backfill(cliente, tmp_path).correr()
    r2 = _backfill(cliente, tmp_path).correr()

    assert d1.call_count == 3  # 2 intentos de la primera corrida + 1 de la segunda
    assert r2.detalles == 1
    estado = json.loads((tmp_path / "_backfill_estado.json").read_text(encoding="utf-8"))
    assert estado["hechos"] == ["CA-1", "CA-2"]
    assert estado["meses_completos"] == ["2026-08"]


class _Reloj:
    """Reloj inyectado que avanza al dormir; maneja también el QuotaTracker."""

    def __init__(self, inicio: datetime) -> None:
        self.t = inicio
        self.dormido: list[float] = []

    def __call__(self, tz: Any = TZ_CHILE) -> datetime:
        return self.t.astimezone(tz)

    def dormir(self, s: float) -> None:
        self.dormido.append(s)
        self.t += timedelta(seconds=s)


_429 = httpx.Response(429, json={"errors": [{"codigo": "429", "mensaje": "límite de tasa"}]})


def _con_reloj(cliente: MercadoPublicoV2Client, tmp_path: Path, reloj: _Reloj, **kw: Any) -> Any:
    # La pausa se persiste y se compara con el mismo reloj que la ventana.
    cliente._client._quota._now_fn = reloj
    return _backfill(cliente, tmp_path, now_fn=reloj, sleep_fn=reloj.dormir,
                     jitter_fn=lambda: 60.0, seguir_tras_pausa=True, **kw)


@respx.mock
def test_tras_la_pausa_se_reanuda_con_el_mismo_estado(cliente, tmp_path):
    reloj = _Reloj(_NOCHE)
    _mock_listado({("2026-08-01", "proveedor_seleccionado"): ["CA-1", "CA-2", "CA-3"]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )
    d2 = respx.get(_DETALLE.format(codigo="CA-2")).mock(
        side_effect=[_429, httpx.Response(200, json=_detalle("CA-2"))]
    )
    respx.get(_DETALLE.format(codigo="CA-3")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-3"))
    )

    r = _con_reloj(cliente, tmp_path, reloj).correr()

    assert r.motivo_fin == "completo"
    assert (r.corridas, r.pausas) == (2, 1)
    assert reloj.dormido == [20 * 60 + 60.0]  # fin de la pausa + jitter
    assert r.minutos_esperados == 21
    assert d2.call_count == 2
    assert r.detalles == 3
    estado = json.loads((tmp_path / "_backfill_estado.json").read_text(encoding="utf-8"))
    assert estado["meses_completos"] == ["2026-08"]


@respx.mock
def test_no_se_reanuda_si_la_pausa_termina_fuera_de_ventana(cliente, tmp_path):
    reloj = _Reloj(datetime(2026, 10, 31, 6, 50, tzinfo=TZ_CHILE))
    _mock_listado({("2026-08-01", "proveedor_seleccionado"): ["CA-1"]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(return_value=_429)

    r = _con_reloj(cliente, tmp_path, reloj).correr()

    assert (r.corridas, r.pausas) == (1, 0)
    assert reloj.dormido == []
    assert "fuera de la ventana" in r.motivo_fin


@respx.mock
def test_respeta_max_pausas(cliente, tmp_path):
    reloj = _Reloj(_NOCHE)
    _mock_listado({("2026-08-01", "proveedor_seleccionado"): ["CA-1"]})
    d1 = respx.get(_DETALLE.format(codigo="CA-1")).mock(return_value=_429)

    r = _con_reloj(cliente, tmp_path, reloj, max_pausas=2).correr()

    assert (r.corridas, r.pausas) == (3, 2)
    assert d1.call_count == 3
    assert "--max-pausas 2" in r.motivo_fin


@respx.mock
def test_sin_seguir_tras_pausa_el_429_cierra_la_noche(cliente, tmp_path):
    reloj = _Reloj(_NOCHE)
    _mock_listado({("2026-08-01", "proveedor_seleccionado"): ["CA-1"]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(return_value=_429)

    cliente._client._quota._now_fn = reloj
    r = _backfill(cliente, tmp_path, now_fn=reloj, sleep_fn=reloj.dormir).correr()

    assert (r.corridas, r.pausas) == (1, 0)
    assert reloj.dormido == []


@respx.mock
def test_el_ticket_no_aparece_en_ningun_archivo_de_salida(cliente, tmp_path, capsys):
    _mock_listado({("2026-08-01", "proveedor_seleccionado"): ["CA-1"]})
    detalle = respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )

    _backfill(cliente, tmp_path).correr().imprimir()

    # El ticket sí viajó, en el header: la prueba no es trivial.
    assert detalle.calls.last.request.headers["ticket"] == _TICKET
    for archivo in tmp_path.iterdir():
        crudo = archivo.read_bytes()
        contenido = gzip.decompress(crudo) if archivo.suffix == ".gz" else crudo
        assert _TICKET.encode() not in contenido, archivo.name
    assert _TICKET not in capsys.readouterr().out


def test_filas_de_detalle_sin_productos_ni_ofertas():
    from app.clients.mp_v2 import _parse_ca_detalle

    payload = copy.deepcopy(_detalle("CA-9", con_ofertas=False)["payload"])
    payload["productos_solicitados"] = []
    filas = bf.filas_de_detalle(_parse_ca_detalle(payload), "2026-08")
    assert len(filas) == 1
    assert filas[0]["CodigoCotizacion"] == "CA-9"
    assert filas[0]["RUTProveedor"] == ""


@respx.mock
def test_diurno_fuera_de_ventana_corre(cliente, tmp_path):
    _mock_listado({"2026-08-01": ["CA-1"]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )

    r = _backfill(cliente, tmp_path, now_fn=lambda tz=TZ_CHILE: _DIA.astimezone(tz),
                  diurno=True).correr()

    assert r.motivo_fin == "completo"
    assert r.modo == "diurno"
    assert r.detalles == 1
    assert r.requests == 32


@respx.mock
def test_sin_diurno_fuera_de_ventana_no_emite_y_dice_nocturno(cliente, tmp_path):
    ruta = _mock_listado({"2026-08-01": ["CA-1"]})

    r = _backfill(cliente, tmp_path, now_fn=lambda tz=TZ_CHILE: _DIA.astimezone(tz)).correr()

    assert "fuera de la ventana" in r.motivo_fin
    assert r.modo == "nocturno"
    assert not ruta.called


@respx.mock
def test_el_resumen_indica_el_modo(cliente, tmp_path, capsys):
    _mock_listado({})
    _backfill(cliente, tmp_path, diurno=True).correr().imprimir()
    assert "Modo:              diurno" in capsys.readouterr().out
    _backfill(cliente, tmp_path).correr().imprimir()
    assert "Modo:              nocturno" in capsys.readouterr().out


@respx.mock
def test_diurno_espera_la_pausa_igual_que_de_noche(cliente, tmp_path):
    reloj = _Reloj(_DIA)  # 15:00, fuera de la ventana nocturna
    _mock_listado({"2026-08-01": ["CA-1", "CA-2"]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )
    respx.get(_DETALLE.format(codigo="CA-2")).mock(
        side_effect=[_429, httpx.Response(200, json=_detalle("CA-2"))]
    )

    r = _con_reloj(cliente, tmp_path, reloj, diurno=True).correr()

    assert r.motivo_fin == "completo"
    assert (r.corridas, r.pausas) == (2, 1)
    assert reloj.dormido == [20 * 60 + 60.0]


@respx.mock
def test_diurno_que_entra_a_la_noche_sigue_con_los_limites_diurnos(cliente, tmp_path):
    # 21:50 Chile: la pausa de 20 min + jitter despierta a las 22:11, ya en la ventana nocturna.
    reloj = _Reloj(datetime(2026, 10, 31, 21, 50, tzinfo=TZ_CHILE))
    _mock_listado({"2026-08-01": ["CA-1", "CA-2", "CA-3"]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )
    respx.get(_DETALLE.format(codigo="CA-2")).mock(
        side_effect=[_429, httpx.Response(200, json=_detalle("CA-2"))]
    )
    respx.get(_DETALLE.format(codigo="CA-3")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-3"))
    )

    # max_requests fijo de la corrida (el tope diurno): se respeta aunque ya sea de noche.
    r = _con_reloj(cliente, tmp_path, reloj, diurno=True, max_requests=4).correr()

    assert r.modo == "diurno"
    assert reloj.t.astimezone(TZ_CHILE).hour == 22  # despertó dentro de la ventana nocturna
    assert (r.corridas, r.pausas) == (2, 1)
    assert "--max-requests 4" in r.motivo_fin
    assert r.requests == 4
