"""Tests F-ca-ventana — la sonda de ventanas del listado de Compra Ágil.

Nada pega a la red: la sonda recibe un cliente falso o un cliente real detrás
de respx, y el tiempo (pausas y reloj) va inyectado. `scripts/` no es un
paquete: el módulo se carga por ruta.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import create_engine

from app.clients.base import (
    MPConcurrencyError,
    MPRateLimitError,
    MPServerError,
    reset_rate_limiter_compartido,
)
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.clients.types import CompraAgilBasica
from app.core.settings import Settings

_V2_LISTADO = "https://api2.mercadopublico.cl/v2/compra-agil"
_CURSOR = datetime(2026, 9, 20, 12, 0, 0)
_AHORA = datetime(2026, 9, 22, 11, 0, 0)


def _cargar_smoke():
    ruta = Path(__file__).parent.parent / "scripts" / "smoke_test.py"
    spec = importlib.util.spec_from_file_location("smoke_test_ca_ventana", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


smoke = _cargar_smoke()


def _resp_vacia() -> dict[str, object]:
    return {
        "success": "OK",
        "payload": {"convocatorias": [], "paginacion": {"total_paginas": 1, "total_resultados": 0}},
    }


class _V2Falso:
    """Responde según un guion: una excepción o un JSON por llamada a `_get`,
    que es por donde pide la sonda."""

    def __init__(self, guion: list[object]) -> None:
        self._guion = list(guion)
        self.llamadas: list[dict[str, object]] = []

    def _get(self, url: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.llamadas.append(dict(params or {}))
        paso = self._guion.pop(0)
        if isinstance(paso, BaseException):
            raise paso
        assert isinstance(paso, dict)
        return paso


def _correr(v2: object, pausas: list[float]) -> list[str]:
    return smoke.correr_sonda(
        v2,
        smoke.variantes_sonda(_CURSOR, _AHORA),
        ["cerrada", "proveedor_seleccionado", "publicada"],
        dormir=pausas.append,
        reloj=lambda: 0.0,
    )


# ---------------------------------------------------------------------------
# Las ventanas de la sonda
# ---------------------------------------------------------------------------


def test_las_cinco_variantes_tienen_las_ventanas_del_prompt() -> None:
    v = {x.letra: x for x in smoke.variantes_sonda(_CURSOR, _AHORA)}
    desde = _CURSOR - timedelta(minutes=5)

    # De menor a mayor ventana, con A (lo de hoy) al final.
    assert [x.letra for x in smoke.variantes_sonda(_CURSOR, _AHORA)] == list("BDCEA")
    assert (v["A"].cambio_desde, v["A"].cambio_hasta) == (desde, None)
    assert (v["B"].cambio_desde, v["B"].cambio_hasta) == (_AHORA - timedelta(hours=2), None)
    assert (v["C"].cambio_desde, v["C"].cambio_hasta) == (desde, _CURSOR + timedelta(hours=6))
    assert (v["D"].cambio_desde, v["D"].cambio_hasta) == (desde, _CURSOR + timedelta(hours=2))
    assert (v["E"].cambio_desde, v["E"].cambio_hasta) == (desde, _CURSOR + timedelta(hours=12))


# ---------------------------------------------------------------------------
# Control de flujo: pausas y cortes (regla 3)
# ---------------------------------------------------------------------------


def test_pide_solo_la_pagina_1_con_los_estados_y_pausa_60_s_entre_variantes() -> None:
    v2 = _V2Falso([_resp_vacia() for _ in range(5)])
    pausas: list[float] = []

    assert _correr(v2, pausas) == list("BDCEA")
    assert pausas == [60.0] * 4
    assert all(c["numero_pagina"] == 1 and c["tamano_pagina"] == 50 for c in v2.llamadas)
    assert all(c["estado"] == "cerrada,proveedor_seleccionado,publicada" for c in v2.llamadas)


def test_un_504_se_informa_y_la_sonda_sigue() -> None:
    v2 = _V2Falso([MPServerError("Error del servidor (504)", 504)] + [_resp_vacia()] * 4)
    assert _correr(v2, []) == list("BDCEA")


def test_un_10500_espera_120_s_extra_y_sigue() -> None:
    v2 = _V2Falso(
        [_resp_vacia(), MPConcurrencyError("10500", retry_after_seconds=900)] + [_resp_vacia()] * 3
    )
    pausas: list[float] = []

    assert _correr(v2, pausas) == list("BDCEA")
    assert pausas == [60.0, 120.0, 60.0, 60.0, 60.0]


def test_cualquier_otro_429_detiene_la_sonda(capsys: pytest.CaptureFixture[str]) -> None:
    v2 = _V2Falso([_resp_vacia(), MPRateLimitError("429", retry_after_seconds=1)])

    assert _correr(v2, []) == ["B", "D"]
    assert len(v2.llamadas) == 2
    assert "DETENIDA" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Con el cliente real detrás de respx
# ---------------------------------------------------------------------------


@pytest.fixture()
def v2_real(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MP_TICKET", "ticket-de-test-1234")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@x/x")
    monkeypatch.setenv("SECRET_KEY", "clave-de-test-32bytesxxxxxxxxxx")
    monkeypatch.setenv("JOBS_TOKEN", "token-de-test-jobs-abcdefgh1234")
    reset_rate_limiter_compartido()
    engine = create_engine("sqlite:///:memory:")
    yield MercadoPublicoV2Client(Settings(_env_file=None), engine)  # type: ignore[call-arg]
    engine.dispose()
    reset_rate_limiter_compartido()


@respx.mock
def test_distingue_total_no_informado_y_no_imprime_el_ticket(
    v2_real, capsys: pytest.CaptureFixture[str]
) -> None:
    con_total = {
        "success": "OK",
        "payload": {
            "convocatorias": [],
            "paginacion": {"total_paginas": 7, "total_resultados": 321},
        },
    }
    sin_total = {"success": "OK", "payload": {"convocatorias": [], "paginacion": {}}}
    ruta = respx.get(_V2_LISTADO).mock(
        side_effect=[httpx.Response(200, json=con_total)]
        + [httpx.Response(200, json=sin_total)] * 4
    )

    smoke.correr_sonda(
        v2_real,
        smoke.variantes_sonda(_CURSOR, _AHORA),
        ["publicada"],
        dormir=lambda _s: None,
    )
    salida = capsys.readouterr().out

    assert "total_paginas=7" in salida and "total_resultados=321" in salida
    assert "total_resultados=(no informado)" in salida
    assert "ticket-de-test-1234" not in salida
    # B (primera) no lleva cambio_hasta; D (segunda) sí.
    assert "cambio_hasta" not in ruta.calls[0].request.url.params
    assert "cambio_hasta" in ruta.calls[1].request.url.params


# ---------------------------------------------------------------------------
# Fechas de la página: máximo fecha_ultimo_cambio y respeto de cambio_hasta
# ---------------------------------------------------------------------------

_HASTA = datetime(2026, 9, 20, 18, 0, 0)


def _ca(codigo: str, cambio: datetime | None) -> CompraAgilBasica:
    return CompraAgilBasica(
        codigo=codigo,
        nombre="x",
        estado="publicada",
        fecha_publicacion=None,
        fecha_cierre=None,
        fecha_ultimo_cambio=cambio,
        monto_clp=None,
        region=None,
        organismo_nombre=None,
        organismo_rut=None,
        total_ofertas=0,
    )


def test_cambio_hasta_respetado_si() -> None:
    items = [_ca("1", datetime(2026, 9, 20, 13, 0)), _ca("2", datetime(2026, 9, 20, 17, 59))]
    assert smoke.describir_pagina(items, _HASTA) == [
        "max_fecha_ultimo_cambio=2026-09-20T17:59:00 UTC",
        "cambio_hasta respetado: sí",
    ]


def test_cambio_hasta_respetado_no_si_algun_item_lo_pasa() -> None:
    items = [_ca("1", datetime(2026, 9, 20, 13, 0)), _ca("2", datetime(2026, 9, 21, 9, 30))]
    assert smoke.describir_pagina(items, _HASTA) == [
        "max_fecha_ultimo_cambio=2026-09-21T09:30:00 UTC",
        "cambio_hasta respetado: no (1 ítems posteriores)",
    ]


def test_pagina_vacia_no_se_puede_verificar() -> None:
    assert smoke.describir_pagina([], _HASTA) == [
        "max_fecha_ultimo_cambio=(sin fechas)",
        "cambio_hasta respetado: sin ítems para verificar",
    ]


def test_sin_cambio_hasta_solo_informa_el_maximo() -> None:
    items = [_ca("1", datetime(2026, 9, 21, 9, 30)), _ca("2", None)]
    assert smoke.describir_pagina(items, None) == [
        "max_fecha_ultimo_cambio=2026-09-21T09:30:00 UTC",
    ]


@respx.mock
def test_la_salida_de_una_variante_200_incluye_la_verificacion(
    v2_real, capsys: pytest.CaptureFixture[str]
) -> None:
    """De punta a punta por el parser real: la fecha llega en hora de Chile sin
    offset y se compara ya convertida a UTC."""
    item = {
        "codigo": "1234-5-COT26",
        "nombre": "x",
        "estado": "publicada",
        # 21:00 Chile (UTC-3) = 00:00 UTC del 21 → posterior al cambio_hasta de D.
        "fechas": {"fecha_ultimo_cambio": "2026-09-20T21:00:00"},
    }
    cuerpo = {
        "success": "OK",
        "payload": {"convocatorias": [item], "paginacion": {"total_paginas": 1}},
    }
    respx.get(_V2_LISTADO).mock(return_value=httpx.Response(200, json=cuerpo))
    d = [v for v in smoke.variantes_sonda(_CURSOR, _AHORA) if v.letra == "D"]

    smoke.correr_sonda(v2_real, d, ["publicada"], dormir=lambda _s: None)
    salida = capsys.readouterr().out

    assert "max_fecha_ultimo_cambio=2026-09-21T00:00:00 UTC" in salida
    assert "cambio_hasta respetado: no (1 ítems posteriores)" in salida


# ---------------------------------------------------------------------------
# Prueba de huso: enviar_utc, fechas crudas y --solo
# ---------------------------------------------------------------------------


_G_DESDE = datetime(2026, 9, 21, 15, 0, 0)
_G_HASTA = datetime(2026, 9, 21, 18, 0, 0)


def _huso(letra: str):
    return next(v for v in smoke.variantes_huso(_AHORA) if v.letra == letra)


@respx.mock
def test_sin_enviar_utc_los_params_son_los_mismos_que_arma_el_cliente(v2_real) -> None:
    """La sonda arma sus params; este test impide que se aparten de
    listar_compra_agil en la variante con la conversión actual."""
    ruta = respx.get(_V2_LISTADO).mock(return_value=httpx.Response(200, json=_resp_vacia()))
    g2 = _huso("G2")
    estados = ["cerrada", "proveedor_seleccionado", "publicada"]

    v2_real.listar_compra_agil(
        cambio_desde=g2.cambio_desde,
        cambio_hasta=g2.cambio_hasta,
        estados=estados,
        tamano_pagina=50,
        numero_pagina=1,
    )

    esperado = {k: str(v) for k, v in smoke.params_variante(g2, estados).items()}
    assert dict(ruta.calls.last.request.url.params) == esperado


def test_g1_viaja_en_utc_y_g2_con_la_conversion_actual() -> None:
    g1 = smoke.params_variante(_huso("G1"), ["publicada"])
    g2 = smoke.params_variante(_huso("G2"), ["publicada"])

    assert (g1["cambio_desde"], g1["cambio_hasta"]) == (
        "2026-09-21T15:00:00",
        "2026-09-21T18:00:00",
    )
    # 21-sep: Chile en horario de verano, UTC-3.
    assert (g2["cambio_desde"], g2["cambio_hasta"]) == (
        "2026-09-21T12:00:00",
        "2026-09-21T15:00:00",
    )


def test_b2_y_b3_tienen_las_ventanas_del_prompt() -> None:
    b2, b3 = _huso("B2"), _huso("B3")

    assert (b2.cambio_desde, b2.cambio_hasta, b2.enviar_utc) == (
        _AHORA - timedelta(hours=2),
        _AHORA - timedelta(minutes=10),
        True,
    )
    assert (b3.cambio_desde, b3.cambio_hasta, b3.enviar_utc) == (
        _AHORA - timedelta(hours=2),
        None,
        False,
    )


def _item_crudo(fecha: object) -> dict[str, object]:
    return {"codigo": "x", "fechas": {"fecha_ultimo_cambio": fecha}}


def test_describir_crudo_muestra_los_strings_tal_cual_y_cuenta_los_de_la_ventana() -> None:
    """La Z es falsa (F-ca-ventana): '12:05Z' es 12:05 Chile = 15:05 UTC."""
    items = [
        _item_crudo("2026-09-21T14:55:00.12Z"),  # 17:55 UTC
        _item_crudo("2026-09-21T11:55:00.5Z"),  # 14:55 UTC: fuera, antes de 15:00Z
        _item_crudo("2026-09-21T12:00:00.3Z"),  # 15:00 UTC
        _item_crudo(None),
    ]

    assert smoke.describir_crudo(items, _G_DESDE, _G_HASTA) == [
        "fecha_ultimo_cambio cruda: min='2026-09-21T11:55:00.5Z'  max='2026-09-21T14:55:00.12Z'",
        "  en UTC (Chile → UTC):    min=2026-09-21T14:55:00.500000Z  max=2026-09-21T17:55:00.120000Z",
        "dentro de la ventana pedida [2026-09-21T15:00:00Z, 2026-09-21T18:00:00Z]: 2 de 3",
    ]


def test_describir_crudo_sin_fechas() -> None:
    assert smoke.describir_crudo([_item_crudo(None)], _G_DESDE, None) == [
        "fecha_ultimo_cambio cruda: (ningún ítem la trae)"
    ]


def test_solo_elige_las_variantes_en_el_orden_pedido_y_sin_cursor() -> None:
    elegidas = smoke.seleccionar_variantes(["G1", "G2", "B2", "B3"], None, _AHORA)
    assert [v.letra for v in elegidas] == ["G1", "G2", "B2", "B3"]


def test_solo_rechaza_letras_desconocidas_y_las_que_piden_cursor_sin_tenerlo() -> None:
    with pytest.raises(ValueError, match="desconocidas: Z9"):
        smoke.seleccionar_variantes(["G1", "Z9"], _CURSOR, _AHORA)
    with pytest.raises(ValueError, match="C no aplican"):
        smoke.seleccionar_variantes(["G1", "C"], None, _AHORA)
    with pytest.raises(ValueError):
        smoke.seleccionar_variantes(None, None, _AHORA)


def test_sin_solo_corren_las_cinco_originales() -> None:
    elegidas = smoke.seleccionar_variantes(None, _CURSOR, _AHORA)
    assert [v.letra for v in elegidas] == list("BDCEA")


def test_leer_solo() -> None:
    assert smoke._leer_solo(["ventana-ca"]) is None
    assert smoke._leer_solo(["ventana-ca", "--solo", "g1, G2,B2,B3"]) == ["G1", "G2", "B2", "B3"]
    with pytest.raises(ValueError):
        smoke._leer_solo(["ventana-ca", "--solo"])


@respx.mock
def test_g1_de_punta_a_punta_imprime_lo_que_viajo_y_las_fechas_crudas(
    v2_real, capsys: pytest.CaptureFixture[str]
) -> None:
    cuerpo = {
        "success": "OK",
        "payload": {
            "convocatorias": [
                _item_crudo("2026-09-21T15:05:00.86Z"),
                _item_crudo("2026-09-21T17:55:00.92Z"),
            ],
            "paginacion": {"total_paginas": 1, "total_resultados": 2},
        },
    }
    ruta = respx.get(_V2_LISTADO).mock(return_value=httpx.Response(200, json=cuerpo))

    smoke.correr_sonda(v2_real, [_huso("G1")], ["publicada"], dormir=lambda _s: None)
    salida = capsys.readouterr().out

    params = ruta.calls.last.request.url.params
    assert (params["cambio_desde"], params["cambio_hasta"]) == (
        "2026-09-21T15:00:00",
        "2026-09-21T18:00:00",
    )
    assert "viajó (UTC sin convertir): cambio_desde=2026-09-21T15:00:00" in salida
    assert "min='2026-09-21T15:05:00.86Z'  max='2026-09-21T17:55:00.92Z'" in salida
    # La Z es falsa: 15:05Z y 17:55Z son hora de Chile, 18:05 y 20:55 UTC. G1 mandó
    # 15:00–18:00 "en UTC" y la API lo leyó en Chile: quedan fuera de lo pedido.
    assert (
        "dentro de la ventana pedida [2026-09-21T15:00:00Z, 2026-09-21T18:00:00Z]: 0 de 2" in salida
    )


# ---------------------------------------------------------------------------
# Latencia por ítems: tamano_pagina por variante (H10, H20, A10)
# ---------------------------------------------------------------------------


def test_h10_h20_usan_la_ventana_de_g2_con_otro_tamano_y_a10_la_de_a() -> None:
    elegidas = smoke.seleccionar_variantes(["H10", "H20", "A10"], _CURSOR, _AHORA)
    h10, h20, a10 = elegidas
    g2 = smoke.params_variante(_huso("G2"), ["publicada"])

    p10 = smoke.params_variante(h10, ["publicada"])
    p20 = smoke.params_variante(h20, ["publicada"])
    assert p10["tamano_pagina"] == 10 and p20["tamano_pagina"] == 20
    for p in (p10, p20):
        assert {k: v for k, v in p.items() if k != "tamano_pagina"} == {
            k: v for k, v in g2.items() if k != "tamano_pagina"
        }

    a = next(v for v in smoke.variantes_sonda(_CURSOR, _AHORA) if v.letra == "A")
    assert (a10.cambio_desde, a10.cambio_hasta, a10.enviar_utc) == (a.cambio_desde, None, False)
    assert smoke.params_variante(a10, ["publicada"])["tamano_pagina"] == 10


def test_el_default_sigue_en_50() -> None:
    assert smoke.params_variante(_huso("G2"), ["publicada"])["tamano_pagina"] == 50


def test_a10_necesita_cursor_y_h10_no() -> None:
    with pytest.raises(ValueError, match="A10 no aplican"):
        smoke.seleccionar_variantes(["H10", "A10"], None, _AHORA)
    assert [v.letra for v in smoke.seleccionar_variantes(["H10", "H20"], None, _AHORA)] == [
        "H10",
        "H20",
    ]


@respx.mock
def test_h10_manda_tamano_10_e_imprime_paginas_y_fechas_crudas(
    v2_real, capsys: pytest.CaptureFixture[str]
) -> None:
    fechas = {"fecha_ultimo_cambio": "2026-09-21T13:05:00.1Z", "fecha_cierre": "2026-09-23 15:00"}
    cuerpo = {
        "success": "OK",
        "payload": {
            "convocatorias": [{"codigo": "x", "fechas": fechas}],
            "paginacion": {"total_paginas": 4, "total_resultados": 31},
        },
    }
    ruta = respx.get(_V2_LISTADO).mock(return_value=httpx.Response(200, json=cuerpo))
    h10 = smoke.seleccionar_variantes(["H10"], None, _AHORA)[0]

    smoke.correr_sonda(v2_real, [h10], ["publicada"], dormir=lambda _s: None)
    salida = capsys.readouterr().out

    assert ruta.calls.last.request.url.params["tamano_pagina"] == "10"
    assert "total_paginas=4  (tamano_pagina=10)" in salida
    assert f"fechas crudas del 1er ítem: {fechas!r}" in salida
