"""Tests F-merc-1: cotizaciones de Compra Ágil por región, en vivo (`mercado-ca`) y hacia
atrás (`mercado-backfill`), retención y exportación.

Red siempre mockeada (respx). Fixtures SINTÉTICAS con las claves de la forma real del
detalle [V, sonda 31-oct]: sin RUT ni datos reales. SQLite en archivo con el esquema
completo: la pausa de 429 y los cursores viven en sync_state.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.clients.mp_v2 import MercadoPublicoV2Client, _parse_ca_detalle
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.ingest.mercado import (
    CURSOR_COMPLETO,
    FUENTE_MERCADO_CA,
    PREFIJO_BACKFILL,
    cola_mercado_ca,
    guarda_productos,
    guardar_detalle_mercado,
    meses_backfill,
    run_backfill,
    sync_mercado_ca,
)
from app.models.tables import (
    CaCotizacion,
    CaCotizacionProducto,
    CaProducto,
    CompraAgil,
    SyncState,
)

_TICKET = "ticket-secreto-de-test-mercado-4321"
_LISTADO = "https://api2.mercadopublico.cl/v2/compra-agil"
_DETALLE = _LISTADO + "/{codigo}"
_429 = {"errors": [{"codigo": "429", "mensaje": "límite de tasa"}]}

# 23:00 Chile del 31-oct-2026: dentro de la ventana nocturna.
_NOCHE = datetime(2026, 10, 31, 23, 0, tzinfo=TZ_CHILE)
_DIA = datetime(2026, 10, 31, 15, 0, tzinfo=TZ_CHILE)


def _reloj_chile(instante: datetime) -> Any:
    return lambda tz=TZ_CHILE: instante.astimezone(tz)


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine(f"sqlite:///{tmp_path / 'mercado.db'}")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.clients.base.time.sleep", lambda _s: None)


def _settings(monkeypatch: pytest.MonkeyPatch, **extra: Any) -> Settings:
    monkeypatch.setenv("MP_TICKET", _TICKET)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SECRET_KEY", "clave-de-test-32bytesxxxxxxxxxx")
    monkeypatch.setenv("JOBS_TOKEN", "token-de-test-jobs-abcdefgh1234")
    return Settings(_env_file=None, rate_limit_rps=1000.0, **extra)  # type: ignore[call-arg]


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    return _settings(monkeypatch)


@pytest.fixture()
def v2(settings: Settings, engine: Engine) -> MercadoPublicoV2Client:
    return MercadoPublicoV2Client(settings, engine, reserva=0)


# ---------------------------------------------------------------------------
# Payloads sintéticos
# ---------------------------------------------------------------------------


def _oferta(rut: str, ganador: bool | None, precio: float, codigo: Any = 50131700) -> dict[str, Any]:
    o: dict[str, Any] = {
        "rut_proveedor": rut,
        "razon_social": f"Proveedor {rut}",
        "es_emt": True,
        "estado": {"id": 3, "glosa": "Enviada"},
        "activo": 1,
        "id_cotizacion": 100,
        "id_oc": 200 if ganador else None,
        "valor_neto": precio * 10,
        "total_impuesto": precio * 1.9,
        "monto_despacho": 0,
        "monto_total": precio * 11.9,
        "descripcion_cotizacion": "Entrega en 48 h",
        "productos_cotizados": [
            {
                "codigo_producto": codigo,
                "nombre_producto": "Leche",
                "descripcion": "Leche entera 1 L",
                "cantidad": 10,
                "precio_unitario": precio,
                "monto_total_producto": precio * 10,
            }
        ],
    }
    if ganador is not None:
        o["proveedor_seleccionado"] = ganador
    return o


def _payload(
    codigo: str,
    *,
    ofertas: list[dict[str, Any]] | None = None,
    llamado: int | None = 1,
    codigo_producto: Any = 50131700,
    estado: str = "proveedor_seleccionado",
    publicada: str = "2026-08-01 09:00",
    region: int = 16,
) -> dict[str, Any]:
    if ofertas is None:
        ofertas = [
            _oferta("11111111-1", True, 950, codigo_producto),
            _oferta("22222222-2", False, 1100, codigo_producto),
        ]
    return {
        "codigo": codigo,
        "nombre": "Leche para jardín",
        "estado": {"codigo": estado},
        "fechas": {
            "fecha_publicacion": publicada,
            "fecha_cierre": "2026-08-05 15:00",
            "fecha_ultimo_cambio": "2026-08-06T10:00:00Z",
        },
        "convocatoria": {"estado_convocatoria": llamado} if llamado is not None else {},
        "presupuesto": {"monto_disponible_clp": 20000, "tipo_presupuesto": "Neto", "moneda": "CLP"},
        "institucion": {
            "region": region,
            "organismo_comprador": "Municipalidad Ficticia",
            "rut": "69.000.000-0",
            "unidad_compra": "Educación",
        },
        "entrega": {"direccion_entrega": "Calle Falsa 123", "plazo_entrega_dias": 5},
        "motivos": {"motivo_desierta": None},
        "documentos": [{"id": 1, "nombre": "anexo.pdf"}],
        "productos_solicitados": [
            {"codigo_producto": codigo_producto, "nombre": "Leche", "cantidad": 10}
        ],
        "proveedores_cotizando": ofertas,
    }


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {"success": "OK", "payload": payload, "errors": []}


def _listado(items: list[tuple[str, str]], publicada: str = "2026-08-01 09:00") -> dict[str, Any]:
    return _envelope(
        {
            "convocatorias": [
                {
                    "codigo": c,
                    "nombre": f"CA {c}",
                    "estado": {"codigo": estado, "glosa": estado},
                    "fechas": {
                        "fecha_publicacion": publicada,
                        "fecha_cierre": "2026-08-05 15:00",
                        "fecha_ultimo_cambio": "2026-08-06T10:00:00Z",
                    },
                    "institucion": {"region": 16, "organismo_comprador": "Muni Ficticia"},
                    "montos": {"monto_disponible_clp": 20000},
                }
                for c, estado in items
            ],
            "paginacion": {
                "total_paginas": 1,
                "total_resultados": len(items),
                "numero_pagina": 1,
                "tamano_pagina": 50,
            },
        }
    )


def _contar(engine: Engine, modelo: Any) -> int:
    with Session(engine) as s:
        return int(s.execute(select(func.count()).select_from(modelo)).scalar_one())


def _guardar(engine: Engine, payload: dict[str, Any], solo_grupos: bool = True) -> tuple[int, int]:
    with Session(engine) as s:
        r = guardar_detalle_mercado(s, _parse_ca_detalle(payload), solo_grupos=solo_grupos)
        s.commit()
    return r


# ---------------------------------------------------------------------------
# Escritura idempotente
# ---------------------------------------------------------------------------


def test_guarda_ofertas_productos_y_campos_nuevos(engine):
    assert _guardar(engine, _payload("CA-1")) == (2, 2)
    with Session(engine) as s:
        ca = s.get(CompraAgil, "CA-1")
        assert ca is not None
        assert ca.unidad_compra == "Educación"
        assert ca.direccion_entrega == "Calle Falsa 123"
        assert ca.plazo_entrega_dias == 5
        assert ca.tipo_presupuesto == "Neto"
        assert ca.cotizaciones_obtenidas_en is not None
        assert ca.raw_json is None  # regla 11: sin raw_json
        cots = {c.rut_proveedor: c for c in s.execute(select(CaCotizacion)).scalars()}
        assert cots["11111111-1"].seleccionado is True
        assert cots["22222222-2"].seleccionado is False
        assert cots["11111111-1"].llamado == 1
        assert cots["11111111-1"].id_oc == "200"
        precios = sorted(p.precio_unitario for p in s.execute(select(CaCotizacionProducto)).scalars())
        assert precios == [950, 1100]


def test_reejecutar_no_duplica(engine):
    _guardar(engine, _payload("CA-1"))
    _guardar(engine, _payload("CA-1"))
    assert _contar(engine, CaCotizacion) == 2
    assert _contar(engine, CaCotizacionProducto) == 2
    assert _contar(engine, CaProducto) == 1


def test_seleccionado_ausente_queda_none(engine):
    _guardar(engine, _payload("CA-1", ofertas=[_oferta("11111111-1", None, 950)]))
    with Session(engine) as s:
        assert s.execute(select(CaCotizacion.seleccionado)).scalar_one() is None


def test_arreglo_vacio_no_borra_lo_guardado(engine):
    _guardar(engine, _payload("CA-1"))
    assert _guardar(engine, _payload("CA-1", ofertas=[])) == (0, 0)
    assert _contar(engine, CaCotizacion) == 2


def test_otro_llamado_no_pisa_el_anterior(engine):
    _guardar(engine, _payload("CA-1", llamado=1))
    _guardar(engine, _payload("CA-1", llamado=2, ofertas=[_oferta("33333333-3", True, 900)]))
    with Session(engine) as s:
        pares = sorted(s.execute(select(CaCotizacion.llamado, CaCotizacion.rut_proveedor)).all())
    assert pares == [(1, "11111111-1"), (1, "22222222-2"), (2, "33333333-3")]


def test_oferta_repetida_del_mismo_rut_se_guarda_una_vez(engine):
    ofertas = [_oferta("11111111-1", True, 950), _oferta("11111111-1", False, 990)]
    assert _guardar(engine, _payload("CA-1", ofertas=ofertas)) == (1, 1)


def test_productos_fuera_de_los_grupos_no_se_guardan(engine):
    # 72101500 (servicios de construcción) no está en los grupos del observatorio.
    assert _guardar(engine, _payload("CA-1", codigo_producto=72101500)) == (2, 0)
    assert _contar(engine, CaCotizacion) == 2
    assert _contar(engine, CaCotizacionProducto) == 0
    # Sin el filtro (MERCADO_PRODUCTOS_SOLO_GRUPOS=false) se guardan todos.
    assert _guardar(engine, _payload("CA-2", codigo_producto=72101500), solo_grupos=False) == (2, 2)


def test_basta_un_producto_ofertado_en_los_grupos():
    det = _parse_ca_detalle(
        _payload("CA-1", codigo_producto=72101500, ofertas=[_oferta("1-9", True, 9, 50131700)])
    )
    assert guarda_productos(det, solo_grupos=True)
    assert not guarda_productos(_parse_ca_detalle(_payload("CA-2", codigo_producto=72101500)), True)


# ---------------------------------------------------------------------------
# mercado-ca
# ---------------------------------------------------------------------------


@respx.mock
def test_mercado_ca_lista_region_sin_estado_y_baja_cotizaciones(settings, engine, v2):
    listado = respx.get(_LISTADO).mock(
        return_value=httpx.Response(
            200, json=_listado([("CA-1", "proveedor_seleccionado"), ("CA-2", "publicada")])
        )
    )
    detalle = respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-1")))
    )
    # CA-2 publicada con cierre 2026-08-05 ya pasado: también pide detalle.
    respx.get(_DETALLE.format(codigo="CA-2")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-2", ofertas=[])))
    )

    r = sync_mercado_ca(settings, engine, v2)

    req = listado.calls[0].request
    assert req.url.params["region"] == "16"
    assert "estado" not in req.url.params
    assert req.headers["ticket"] == _TICKET
    assert _TICKET not in str(req.url)
    assert detalle.called
    assert r["detalles_guardados"] == 2
    assert r["cotizaciones"] == 2
    with Session(engine) as s:
        cursor = s.get(SyncState, FUENTE_MERCADO_CA)
        assert cursor is not None and cursor.cursor
        assert datetime.fromisoformat(cursor.cursor) > ahora_utc() - timedelta(hours=1)

    # Re-ejecutar: la cola queda vacía, no se piden detalles ni se duplica nada.
    llamadas = detalle.call_count
    r2 = sync_mercado_ca(settings, engine, v2)
    assert r2["cola"] == 0
    assert detalle.call_count == llamadas
    assert _contar(engine, CaCotizacion) == 2


@respx.mock
def test_mercado_ca_429_corta_sin_pedir_detalles(settings, engine, v2):
    respx.get(_LISTADO).mock(return_value=httpx.Response(429, json=_429))
    detalle = respx.get(url__regex=_LISTADO + "/.+").mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-1")))
    )
    with Session(engine) as s:
        _ca(s, "CA-1", estado="proveedor_seleccionado")
        s.commit()
    r = sync_mercado_ca(settings, engine, v2)
    assert r["cortado_por"] == "MPTasaError"
    assert not detalle.called
    with Session(engine) as s:
        assert s.get(SyncState, "api_pausa") is not None  # pausa persistida (regla 3)


def _ca(
    s: Session,
    codigo: str,
    *,
    estado: str = "cerrada",
    region: int = 16,
    cierre: datetime | None = None,
    obtenidas: datetime | None = None,
    cambio: datetime | None = None,
    **extra: Any,
) -> None:
    s.add(
        CompraAgil(
            codigo=codigo,
            nombre=codigo,
            estado=estado,
            region=region,
            fecha_cierre=cierre or datetime(2026, 8, 5, 18, 0),
            fecha_ultimo_cambio=cambio,
            cotizaciones_obtenidas_en=obtenidas,
            **extra,
        )
    )


def test_cola_respeta_tope_orden_region_y_frescura(engine):
    ahora = datetime(2026, 11, 3, 12, 0)
    with Session(engine) as s:
        _ca(s, "VIEJA", cierre=datetime(2026, 8, 1))
        _ca(s, "MEDIA", cierre=datetime(2026, 9, 1), estado="desierta")
        _ca(s, "NUEVA", cierre=datetime(2026, 10, 1), estado="proveedor_seleccionado")
        _ca(s, "OTRA-REGION", cierre=datetime(2026, 10, 2), region=13)
        _ca(s, "FRESCA", cierre=datetime(2026, 10, 3), obtenidas=ahora, cambio=ahora - timedelta(days=1))
        _ca(s, "CAMBIO", cierre=datetime(2026, 7, 1), obtenidas=ahora - timedelta(days=2), cambio=ahora)
        _ca(s, "ABIERTA", estado="publicada", cierre=ahora + timedelta(days=1))
        _ca(s, "CERRO-SIN-AVISO", estado="publicada", cierre=ahora - timedelta(hours=1))
        s.commit()

        cola = cola_mercado_ca(s, [16], ahora, limite=10, fallos_max=3, espera_horas=6)
        assert cola == ["CERRO-SIN-AVISO", "NUEVA", "MEDIA", "VIEJA", "CAMBIO"]
        assert cola_mercado_ca(s, [16], ahora, limite=2, fallos_max=3, espera_horas=6) == [
            "CERRO-SIN-AVISO",
            "NUEVA",
        ]


def test_cola_deja_en_espera_a_la_que_falla_seguido(engine):
    ahora = datetime(2026, 11, 3, 12, 0)
    with Session(engine) as s:
        _ca(s, "FALLA", cierre=datetime(2026, 10, 9), detalle_fallos=3, detalle_ultimo_fallo=ahora)
        _ca(s, "OK", cierre=datetime(2026, 10, 1))
        s.commit()
        assert cola_mercado_ca(s, [16], ahora, 10, 3, 6) == ["OK"]
        assert cola_mercado_ca(s, [16], ahora + timedelta(hours=7), 10, 3, 6) == ["OK", "FALLA"]


@respx.mock
def test_mercado_ca_detalle_que_falla_suma_fallo_y_sigue(settings, engine, v2):
    respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    respx.get(_DETALLE.format(codigo="CA-MALA")).mock(return_value=httpx.Response(500))
    respx.get(_DETALLE.format(codigo="CA-BUENA")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-BUENA")))
    )
    with Session(engine) as s:
        _ca(s, "CA-MALA", cierre=datetime(2026, 8, 9))
        _ca(s, "CA-BUENA", cierre=datetime(2026, 8, 1))
        s.commit()
    r = sync_mercado_ca(settings, engine, v2)
    assert r["detalles_fallidos"] == 1
    assert r["detalles_guardados"] == 1
    with Session(engine) as s:
        mala = s.get(CompraAgil, "CA-MALA")
        assert mala is not None and mala.detalle_fallos == 1


# ---------------------------------------------------------------------------
# mercado-backfill
# ---------------------------------------------------------------------------


def test_meses_por_defecto_en_orden():
    class _S:
        mercado_backfill_meses = ""
        mercado_backfill_primer_mes = "2024-11"

    meses = meses_backfill(_S(), date(2026, 11, 3))  # type: ignore[arg-type]
    assert meses[:6] == ["2026-03", "2026-04", "2026-06", "2026-07", "2026-08", "2026-10"]
    assert meses[-1] == "2024-11"
    assert len(meses) == len(set(meses)) == 24


def test_meses_explicitos_mandan(monkeypatch):
    s = _settings(monkeypatch, mercado_backfill_meses="2026-08, 2025-01")
    assert meses_backfill(s, date(2026, 11, 3)) == ["2026-08", "2025-01"]


def test_primer_mes_acota_el_horizonte(monkeypatch):
    s = _settings(monkeypatch, mercado_backfill_primer_mes="2026-04")
    meses = meses_backfill(s, date(2026, 11, 3))
    assert meses == ["2026-04", "2026-06", "2026-07", "2026-08", "2026-10", "2026-09", "2026-05"]
    # También filtra una lista explícita: un mes anterior nunca se pide.
    s2 = _settings(
        monkeypatch, mercado_backfill_primer_mes="2026-04", mercado_backfill_meses="2026-08,2026-03"
    )
    assert meses_backfill(s2, date(2026, 11, 3)) == ["2026-08"]


@respx.mock
def test_primer_mes_no_pide_meses_anteriores(settings, engine, v2):
    listado = respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    s = _con_meses(settings, "2026-07", mercado_backfill_primer_mes="2026-08")
    r = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert not listado.called
    assert r["requests"] == 0


def test_backfill_fuera_de_ventana_sale_omitido(settings, engine, v2):
    assert run_backfill(settings, engine, v2, now_fn=_reloj_chile(_DIA)) == {
        "omitido_fuera_de_ventana": True
    }


@pytest.mark.parametrize(
    ("instante_utc", "dentro"),
    [
        # Cambio de horario de Chile del 6-sep-2026 (UTC−4 → UTC−3).
        (datetime(2026, 9, 6, 2, 30, tzinfo=UTC), True),  # 22:30 del 5-sep, UTC−4
        (datetime(2026, 9, 6, 10, 30, tzinfo=UTC), False),  # 07:30 UTC−3 (06:30 si fuera UTC−4)
        (datetime(2026, 4, 5, 10, 30, tzinfo=UTC), True),  # 06:30 tras volver a UTC−4
    ],
)
def test_ventana_nocturna_con_cambio_de_horario(settings, engine, v2, instante_utc, dentro):
    with respx.mock:
        respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
        r = run_backfill(
            _con_meses(settings, "2026-08"), engine, v2, now_fn=_reloj_chile(instante_utc)
        )
    assert ("omitido_fuera_de_ventana" in r) is not dentro


def _con_meses(settings: Settings, meses: str, **extra: Any) -> Settings:
    return settings.model_copy(update={"mercado_backfill_meses": meses, **extra})


def _listado_por_dia(dias: dict[str, list[tuple[str, str]]]) -> Any:
    """side_effect: el listado del día `publicado_desde` (hora de Chile) o vacío."""

    def responder(request: httpx.Request) -> httpx.Response:
        dia = request.url.params["publicado_desde"][:10]
        items = dias.get(dia, [])
        if items == "429":  # type: ignore[comparison-overlap]
            return httpx.Response(429, json=_429)
        return httpx.Response(200, json=_listado(items, publicada=f"{dia} 09:00"))

    return responder


@respx.mock
def test_backfill_completa_el_mes_y_no_lo_repite(settings, engine, v2):
    listado = respx.get(_LISTADO).mock(
        side_effect=_listado_por_dia(
            {"2026-08-01": [("CA-1", "proveedor_seleccionado"), ("CA-D", "desierta")]}
        )
    )
    detalle = respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-1")))
    )
    s = _con_meses(settings, "2026-08")

    r = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))

    assert listado.call_count == 31  # un día por request: ventanas de un día
    req = listado.calls[0].request
    assert req.url.params["ordenar_por"] == "FechaPublicacion"
    assert "estado" not in req.url.params
    assert detalle.call_count == 1  # solo proveedor_seleccionado pide detalle
    assert r["meses_completos"] == ["2026-08"]
    assert r["cotizaciones"] == 2 and r["productos"] == 2
    assert r["ca_listadas"] == 2
    with Session(engine) as ses:
        assert ses.get(SyncState, PREFIJO_BACKFILL + "2026-08").cursor == CURSOR_COMPLETO  # type: ignore[union-attr]
        assert ses.get(CompraAgil, "CA-D") is not None  # upsert básico de todo lo listado

    r2 = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert r2["requests"] == 0
    assert listado.call_count == 31


@respx.mock
def test_backfill_429_guarda_avance_y_reanuda(settings, engine, v2):
    dias: dict[str, Any] = {
        "2026-08-01": [("CA-1", "proveedor_seleccionado")],
        "2026-08-02": "429",
        "2026-08-03": [("CA-3", "proveedor_seleccionado")],
    }
    listado = respx.get(_LISTADO).mock(side_effect=_listado_por_dia(dias))
    d1 = respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-1")))
    )
    respx.get(_DETALLE.format(codigo="CA-3")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-3", publicada="2026-08-03 09:00")))
    )
    s = _con_meses(settings, "2026-08")

    r = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert r["cortado_por"] == "MPTasaError"
    with Session(engine) as ses:
        assert ses.get(SyncState, PREFIJO_BACKFILL + "2026-08").cursor == "2026-08-02|1"  # type: ignore[union-attr]

    # Vence la pausa y el día 2 ya responde: retoma en el día 2, sin repetir el detalle del 1.
    with Session(engine) as ses:
        ses.execute(text("DELETE FROM sync_state WHERE fuente = 'api_pausa'"))
        ses.commit()
    dias["2026-08-02"] = []
    antes = listado.call_count
    r2 = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert r2["meses_completos"] == ["2026-08"]
    assert d1.call_count == 1
    assert listado.call_count - antes == 30  # días 2 a 31
    assert _contar(engine, CaCotizacion) == 4


@respx.mock
def test_backfill_detalle_fallido_deja_el_mes_abierto_y_se_reintenta(settings, engine, v2):
    respx.get(_LISTADO).mock(
        side_effect=_listado_por_dia({"2026-08-01": [("CA-1", "proveedor_seleccionado")]})
    )
    det = respx.get(_DETALLE.format(codigo="CA-1")).mock(
        side_effect=[httpx.Response(500)] * 4
        + [httpx.Response(200, json=_envelope(_payload("CA-1")))]
    )
    s = _con_meses(settings, "2026-08")

    r = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert r["meses_completos"] == []
    assert any("pendientes" in a for a in r["avisos"])
    with Session(engine) as ses:
        assert ses.get(SyncState, PREFIJO_BACKFILL + "2026-08").cursor == "listado"  # type: ignore[union-attr]

    r2 = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert r2["meses_completos"] == ["2026-08"]
    assert r2["requests"] == 1  # solo el pendiente: los días ya estaban listados
    assert det.call_count == 5


@respx.mock
def test_backfill_para_por_presupuesto(settings, engine):
    s = _con_meses(settings, "2026-08", api_daily_budget=350)
    v2 = MercadoPublicoV2Client(s, engine, reserva=0)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO quota_log (fecha, requests_usadas) VALUES (:f, 60)"),
            {"f": datetime.now(TZ_CHILE).date().isoformat()},
        )
    listado = respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    r = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert "presupuesto" in r["motivo_fin"]
    assert not listado.called


def _cursor(engine: Engine, mes: str) -> str | None:
    with Session(engine) as ses:
        st = ses.get(SyncState, PREFIJO_BACKFILL + mes)
        return st.cursor if st is not None else None


@respx.mock
def test_mes_vacio_se_marca_y_el_backfill_sigue(settings, engine, v2):
    # Julio vacío va primero: queda en espera hasta que agosto muestra datos (filtros sanos).
    listado = respx.get(_LISTADO).mock(
        side_effect=_listado_por_dia({"2026-08-01": [("CA-1", "desierta")]})
    )
    s = _con_meses(settings, "2026-07,2026-08,2026-06")

    r = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))

    assert listado.call_count == 31 + 31 + 30  # no corta en julio
    assert _cursor(engine, "2026-07") == "vacio"
    assert _cursor(engine, "2026-06") == "vacio"
    assert _cursor(engine, "2026-08") == CURSOR_COMPLETO
    assert sum("marcado vacío" in a for a in r["avisos"]) == 2
    assert r["motivo_fin"] == "sin meses pendientes"

    # Un mes `vacio` no se vuelve a pedir.
    r2 = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert r2["requests"] == 0


@respx.mock
def test_un_solo_mes_vacio_al_final_de_la_lista_queda_vacio(settings, engine, v2):
    respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    r = run_backfill(_con_meses(settings, "2026-08"), engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert r["meses_completos"] == []
    assert _cursor(engine, "2026-08") == "vacio"


def test_primer_mes_por_defecto_es_el_de_la_sonda(settings):
    assert settings.mercado_backfill_primer_mes == "2024-11"


@respx.mock
def test_meses_completos_previos_cuentan_como_datos(settings, engine, v2):
    """F-merc-1-fix: desde la segunda noche los meses recientes vienen `completo`; tres
    meses viejos vacíos después de ellos son vacíos de verdad, no un filtro roto."""
    with Session(engine) as ses:
        for mes in ("2026-08", "2026-07", "2026-06"):
            ses.add(SyncState(fuente=PREFIJO_BACKFILL + mes, cursor=CURSOR_COMPLETO))
        ses.commit()
    listado = respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    s = _con_meses(settings, "2026-08,2026-07,2026-06,2026-05,2026-04,2026-03")

    r = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))

    assert "revisar filtros" not in r["motivo_fin"]
    assert listado.call_count == 31 + 30 + 31  # mayo, abril y marzo
    for mes in ("2026-05", "2026-04", "2026-03"):
        assert _cursor(engine, mes) == "vacio"

    # La noche siguiente no pide ninguno.
    r2 = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert r2["requests"] == 0
    assert listado.call_count == 92


@respx.mock
def test_tres_meses_vacios_al_partir_cortan_por_filtros(settings, engine, v2):
    listado = respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    s = _con_meses(settings, "2026-08,2026-07,2026-06,2026-05")

    r = run_backfill(s, engine, v2, now_fn=_reloj_chile(_NOCHE))

    assert "revisar filtros" in r["motivo_fin"]
    assert listado.call_count == 31 + 31 + 30  # mayo no se pide
    for mes in ("2026-08", "2026-07", "2026-06"):
        assert _cursor(engine, mes) is None  # se vuelven a listar, no quedan `vacio`
    assert _cursor(engine, "2026-05") is None


@respx.mock
def test_backfill_mes_en_curso_solo_dias_terminados(settings, engine, v2):
    listado = respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    r = run_backfill(_con_meses(settings, "2026-10"), engine, v2, now_fn=_reloj_chile(_NOCHE))
    assert listado.call_count == 30  # 1–30 de octubre; el 31 es "hoy"
    assert r["meses_completos"] == []
    with Session(engine) as ses:
        assert ses.get(SyncState, PREFIJO_BACKFILL + "2026-10").cursor == "2026-10-31|1"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Retención (§1.7)
# ---------------------------------------------------------------------------


def test_retencion_no_purga_ca_de_mercado_regiones(settings, engine):
    from app.ingest.orchestrator import run_retencion

    hace_200 = ahora_utc() - timedelta(days=200)
    with Session(engine) as s:
        for codigo, region in (("NUBLE", 16), ("RM", 13)):
            s.add(
                CompraAgil(
                    codigo=codigo,
                    nombre=codigo,
                    estado="desierta",
                    region=region,
                    raw_json={"x": 1},
                    actualizado_en=hace_200,
                )
            )
            s.add(CaProducto(ca_codigo=codigo, codigo_producto="50131700", nombre="Leche"))
        s.flush()
        s.add(CaCotizacion(ca_codigo="NUBLE", llamado=1, rut_proveedor="1-9"))
        s.commit()

    r = run_retencion(engine, settings)

    assert r["ca_purgadas"] == 1
    with Session(engine) as s:
        assert s.get(CompraAgil, "NUBLE").raw_json == {"x": 1}  # type: ignore[union-attr]
        assert s.get(CompraAgil, "RM").raw_json is None  # type: ignore[union-attr]
        assert s.execute(select(CaProducto.ca_codigo)).scalars().all() == ["NUBLE"]
    assert _contar(engine, CaCotizacion) == 1


# ---------------------------------------------------------------------------
# CLI, exportación y tamaño
# ---------------------------------------------------------------------------


@respx.mock
def test_sonda_mercado_barre_el_horizonte(v2, capsys):
    smoke = _cargar_script("smoke_test")
    listado = respx.get(_LISTADO).mock(
        return_value=httpx.Response(200, json=_listado([("CA-1", "proveedor_seleccionado")]))
    )

    smoke.sonda_mercado(v2, pausa_s=0)

    assert listado.call_count == 6
    desde = [c.request.url.params["publicado_desde"][:10] for c in listado.calls]
    assert desde[2:] == ["2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01"]
    for c in listado.calls:
        assert c.request.url.params["region"] == "16"
        assert c.request.url.params["tamano_pagina"] == "10"
        assert "estado" not in c.request.url.params
    salida = capsys.readouterr().out
    assert "2025-07: total_resultados=1" in salida
    assert _TICKET not in salida


def _cargar_script(nombre: str) -> Any:
    ruta = Path(__file__).parent.parent / "scripts" / f"{nombre}.py"
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_jobs_mercado_en_el_cli_y_con_api():
    from app.ingest.__main__ import _JOBS, _JOBS_CON_API

    for job in ("mercado-ca", "mercado-backfill"):
        assert job in _JOBS
        assert job in _JOBS_CON_API


def _cargar_exportador() -> Any:
    ruta = Path(__file__).parent.parent / "scripts" / "mercado" / "exportar_cot_api.py"
    spec = importlib.util.spec_from_file_location("exportar_cot_api", ruta)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_exportar_cot_api_escribe_las_columnas_del_observatorio(engine, tmp_path):
    _guardar(engine, _payload("CA-1"))
    ex = _cargar_exportador()

    escritas = ex.exportar(engine, region=16, meses=None, dir_salida=tmp_path, sobrescribir=False)

    assert escritas == {"2026-08": 2}
    with gzip.open(tmp_path / "cot_api_2026-08.csv.gz", "rt", encoding="utf-8") as f:
        filas = list(csv.DictReader(f, delimiter=";"))
    assert {f["RUTProveedor"] for f in filas} == {"11111111-1", "22222222-2"}
    ganador = next(f for f in filas if f["ProveedorSeleccionado"] == "si")
    assert ganador["PrecioUnitario"] == "950"
    assert ganador["MontoTotalProducto"] == "9500"
    assert ganador["CantidadSolicitada"] == "10"
    # No pisa un archivo existente sin --sobrescribir.
    assert ex.exportar(engine, region=16, meses=None, dir_salida=tmp_path, sobrescribir=False) == {}


def _bytes_postgres(valores: list[Any]) -> int:
    """Estimado grueso de una tupla de Postgres: cabecera + item + datos (varlena 1 B)."""
    datos = 0
    for v in valores:
        if v is None:
            continue
        if isinstance(v, bool):
            datos += 1
        elif isinstance(v, int | float):
            datos += 8
        else:
            datos += len(str(v).encode()) + 1
    return 24 + 4 + datos


def test_tamano_estimado_por_ca_con_la_fixture(engine):
    """§1.5: bytes por CA con la fixture (2 ofertas × 1 producto). El estimado con el volumen
    real de agosto (6,1 ofertas y 19,3 filas de producto por CA) va en el commit y en
    docs/decisiones.md: pasa de 120 MB a 24 meses, por eso `mercado_productos_solo_grupos`."""
    _guardar(engine, _payload("CA-1"))
    with Session(engine) as s:
        ofertas = [
            _bytes_postgres([c.id, c.ca_codigo, c.llamado, c.rut_proveedor, c.razon_social, c.es_emt,
                             c.codigo_empresa, c.codigo_sucursal, c.id_cotizacion, c.id_oc,
                             c.estado_glosa, c.activo, c.seleccionado, c.valor_neto, c.total_impuesto,
                             c.monto_despacho, c.monto_total, c.fecha_creacion, c.fecha_vigencia,
                             c.descripcion])
            for c in s.execute(select(CaCotizacion)).scalars()
        ]
        productos = [
            _bytes_postgres([p.id, p.cotizacion_id, p.codigo_producto, p.nombre_producto,
                             p.descripcion, p.cantidad, p.precio_unitario, p.monto_total_producto])
            for p in s.execute(select(CaCotizacionProducto)).scalars()
        ]
    # Índices (pkey, UNIQUE, rut, FK) ≈ 90 B por oferta y 36 B por producto.
    por_ca = sum(ofertas) + 90 * len(ofertas) + sum(productos) + 36 * len(productos)
    assert 400 < por_ca < 1200
