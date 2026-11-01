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
from datetime import datetime
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
            "seleccion": {"proveedor_seleccionado": True},
            "monto_total": 11305,
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
            "seleccion": {"proveedor_seleccionado": False},
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


def _listado(codigos: list[str]) -> dict[str, Any]:
    return {
        "success": "OK",
        "payload": {
            "convocatorias": [{"codigo": c, "estado": "proveedor_seleccionado"} for c in codigos],
            "paginacion": {"total_paginas": 1, "total_resultados": len(codigos)},
        },
        "errors": [],
    }


def _mock_listado(por_dia: dict[tuple[str, str], list[str]]) -> respx.Route:
    """`por_dia[(AAAA-MM-DD, estado_param)]` = códigos; el resto de días, vacío."""

    def responder(request: httpx.Request) -> httpx.Response:
        dia = request.url.params["publicado_desde"][:10]
        estado = request.url.params["estado"]
        return httpx.Response(200, json=_listado(por_dia.get((dia, estado), [])))

    return respx.get(_LISTADO).mock(side_effect=responder)


def _backfill(cliente: MercadoPublicoV2Client, tmp_path: Path, **kw: Any) -> Any:
    opciones: dict[str, Any] = {
        "meses": ["2026-08"],
        "region": 16,
        "estados": ["proveedor_seleccionado", "desierta"],
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


@respx.mock
def test_corrida_completa_csv_con_las_columnas(cliente, tmp_path):
    _mock_listado({("2026-08-01", "proveedor_seleccionado"): ["CA-1"],
                   ("2026-08-02", "desierta"): ["CA-2"]})
    respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_detalle("CA-1"))
    )
    d2 = _detalle("CA-2", con_ofertas=False)
    d2["payload"]["estado"] = {"codigo": "desierta"}
    respx.get(_DETALLE.format(codigo="CA-2")).mock(return_value=httpx.Response(200, json=d2))

    r = _backfill(cliente, tmp_path).correr()

    assert r.motivo_fin == "completo"
    assert r.detalles == 2
    # 31 días × 2 pases de listado + 2 detalles.
    assert r.requests == 64
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
    arroz = next(f for f in ca1 if f["CodigoProducto"] == "50221100")
    assert arroz["PrecioUnitario"] == "1200"
    ca2 = [f for f in filas if f["CodigoCotizacion"] == "CA-2"]
    assert len(ca2) == 2  # sin ofertas: una fila por producto pedido
    assert all(f["RUTProveedor"] == "" for f in ca2)
    estado = json.loads((tmp_path / "_backfill_estado.json").read_text(encoding="utf-8"))
    assert estado["meses_completos"] == ["2026-08"]


@respx.mock
def test_seleccionadas_primero(cliente, tmp_path):
    _mock_listado({("2026-08-01", "desierta"): ["CA-D"],
                   ("2026-08-31", "proveedor_seleccionado"): ["CA-S"]})
    orden: list[str] = []
    for c in ("CA-D", "CA-S"):
        def responder(request: httpx.Request, c: str = c) -> httpx.Response:
            orden.append(c)
            return httpx.Response(200, json=_detalle(c))
        respx.get(_DETALLE.format(codigo=c)).mock(side_effect=responder)

    _backfill(cliente, tmp_path).correr()

    assert orden == ["CA-S", "CA-D"]


@respx.mock
def test_reanuda_desde_el_json_sin_repetir_detalles(cliente, tmp_path):
    (tmp_path / "_backfill_estado.json").write_text(json.dumps({
        "listados": {f"2026-08|{p}|2026-08-{d:02d}": [] for p in ("proveedor_seleccionado", "resto")
                     for d in range(1, 32)} | {
            "2026-08|proveedor_seleccionado|2026-08-01": ["CA-1", "CA-2"]},
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

    assert not listado.called
    assert not d1.called
    assert d2.call_count == 1
    assert r.requests == 1


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
