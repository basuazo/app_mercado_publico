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
import itertools
import logging
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
    cola_vigentes,
    contar_repetidas,
    guarda_productos,
    guardar_detalle_mercado,
    guardar_detalle_vigente,
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
from tests import pg_url

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


_IDS_COTIZACION = itertools.count(570_000_000)


def _oferta(
    rut: str,
    ganador: bool | None,
    precio: float,
    codigo: Any = 50131700,
    id_cotizacion: Any = "nuevo",
) -> dict[str, Any]:
    """Una oferta; cada una con su `id_cotizacion` (como la API) salvo que se pida otro."""
    if id_cotizacion == "nuevo":
        id_cotizacion = next(_IDS_COTIZACION)
    o: dict[str, Any] = {
        "rut_proveedor": rut,
        "razon_social": f"Proveedor {rut}",
        "es_emt": True,
        "estado": {"id": 3, "glosa": "Enviada"},
        "activo": 1,
        "id_cotizacion": id_cotizacion,
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
        precios = sorted(
            p.precio_unitario for p in s.execute(select(CaCotizacionProducto)).scalars()
        )
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


# --- F-merc-1c §3: un RUT puede mandar varias ofertas a una CA ------------------------


def test_dos_ofertas_del_mismo_rut_con_distinto_id_son_dos_filas(engine, caplog):
    ofertas = [_oferta("11111111-1", True, 950), _oferta("11111111-1", False, 990)]
    with caplog.at_level(logging.WARNING):
        assert _guardar(engine, _payload("CA-1", ofertas=ofertas)) == (2, 2)
    assert "repetid" not in caplog.text
    with Session(engine) as s:
        precios = sorted(s.execute(select(CaCotizacion.valor_neto)).scalars())
    assert precios == [9500, 9900]


def test_id_cotizacion_repetido_se_guarda_una_vez_con_un_aviso_por_ca(engine, caplog):
    ofertas = [
        _oferta("11111111-1", True, 950, id_cotizacion=7),
        _oferta("11111111-1", True, 950, id_cotizacion=7),
        _oferta("11111111-1", True, 950, id_cotizacion=7),
        _oferta("22222222-2", False, 990),
    ]
    with caplog.at_level(logging.WARNING):
        assert _guardar(engine, _payload("CA-1", ofertas=ofertas)) == (2, 2)
    avisos = [r for r in caplog.records if "id_cotizacion repetido" in r.getMessage()]
    assert len(avisos) == 1


def test_id_cotizacion_nulo_usa_la_clave_sustituta(engine):
    """Sin id, dos ofertas del mismo RUT chocan y se guarda la primera (comportamiento previo)."""
    ofertas = [
        _oferta("11111111-1", True, 950, id_cotizacion=None),
        _oferta("11111111-1", False, 990, id_cotizacion=None),
        _oferta("22222222-2", False, 1100, id_cotizacion=None),
    ]
    assert _guardar(engine, _payload("CA-1", ofertas=ofertas)) == (2, 2)
    with Session(engine) as s:
        filas = sorted(s.execute(select(CaCotizacion.id_cotizacion, CaCotizacion.valor_neto)).all())
    assert filas == [("rut:11111111-1", 9500), ("rut:22222222-2", 11000)]


def test_reingestar_ofertas_del_mismo_rut_no_duplica(engine):
    ofertas = [
        _oferta("11111111-1", True, 950, id_cotizacion=1),
        _oferta("11111111-1", False, 990, id_cotizacion=2),
    ]
    _guardar(engine, _payload("CA-1", ofertas=ofertas))
    _guardar(engine, _payload("CA-1", ofertas=ofertas))
    assert _contar(engine, CaCotizacion) == 2
    assert _contar(engine, CaCotizacionProducto) == 2


def test_contar_ofertas_del_mismo_rut_e_identicas():
    ofertas = [
        _oferta("11111111-1", True, 950),
        _oferta("11111111-1", False, 990),  # mismo RUT, otra oferta
        _oferta("11111111-1", False, 990),  # idéntica a la anterior salvo el id
        _oferta("22222222-2", False, 990),  # mismo precio, otro RUT: no es idéntica
        _oferta("33333333-3", False, 800, id_cotizacion=5),
        _oferta("33333333-3", False, 800, id_cotizacion=5),  # id repetido: no se cuenta
    ]
    det = _parse_ca_detalle(_payload("CA-1", ofertas=ofertas))
    assert contar_repetidas(det.cotizaciones) == (2, 1)


@respx.mock
def test_mercado_ca_resume_ofertas_del_mismo_rut(settings, engine, v2):
    ofertas = [
        _oferta("11111111-1", True, 950),
        _oferta("11111111-1", False, 990),
        _oferta("11111111-1", False, 990),
    ]
    respx.get(_LISTADO).mock(
        return_value=httpx.Response(200, json=_listado([("CA-1", "proveedor_seleccionado")]))
    )
    respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-1", ofertas=ofertas)))
    )
    r = sync_mercado_ca(settings, engine, v2)
    assert r["cotizaciones"] == 3
    assert r["ofertas_mismo_rut"] == 2
    assert r["ofertas_identicas"] == 1


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
        _ca(
            s,
            "FRESCA",
            cierre=datetime(2026, 10, 3),
            obtenidas=ahora,
            cambio=ahora - timedelta(days=1),
        )
        _ca(
            s,
            "CAMBIO",
            cierre=datetime(2026, 7, 1),
            obtenidas=ahora - timedelta(days=2),
            cambio=ahora,
        )
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
# F-merc-3: detalle de las CA vigentes
# ---------------------------------------------------------------------------

_CAMBIO_VIEJO = datetime(2026, 8, 6, 10, 0)


def _vigente(s: Session, codigo: str, dias: float = 2, **extra: Any) -> None:
    _ca(
        s,
        codigo,
        estado="publicada",
        cierre=ahora_utc() + timedelta(days=dias),
        cambio=extra.pop("cambio", _CAMBIO_VIEJO),
        **extra,
    )


def _payload_vigente(codigo: str, dias: float = 2) -> dict[str, Any]:
    p = _payload(codigo, ofertas=[], estado="publicada")
    p["fechas"]["fecha_cierre"] = (ahora_utc() + timedelta(days=dias)).strftime("%Y-%m-%d %H:%M")
    return p


def test_cola_vigentes_entran_sin_detalle_y_cambiadas_en_orden(engine):
    ahora = datetime(2026, 11, 3, 12, 0)
    with Session(engine) as s:
        _ca(s, "LEJOS", estado="publicada", cierre=ahora + timedelta(days=5), cambio=_CAMBIO_VIEJO)
        _ca(s, "CERCA", estado="publicada", cierre=ahora + timedelta(days=1), cambio=_CAMBIO_VIEJO)
        _ca(
            s,
            "CAMBIADA",
            estado="publicada",
            cierre=ahora + timedelta(hours=1),
            cambio=ahora,
            detalle_vigente_en=ahora - timedelta(days=1),
        )
        _ca(
            s,
            "AL-DIA",
            estado="publicada",
            cierre=ahora + timedelta(days=1),
            cambio=_CAMBIO_VIEJO,
            detalle_vigente_en=ahora,
        )
        _ca(s, "CERRADA", estado="cerrada", cierre=ahora + timedelta(days=1))
        _ca(
            s,
            "YA-PASO",
            estado="publicada",
            cierre=ahora - timedelta(hours=1),
            cambio=_CAMBIO_VIEJO,
        )
        _ca(s, "OTRA-REGION", estado="publicada", region=13, cierre=ahora + timedelta(days=1))
        s.commit()
        # Primero las que nunca tuvieron detalle (por cierre), después las cambiadas.
        assert cola_vigentes(s, [16], ahora, 10, 3, 6) == ["CERCA", "LEJOS", "CAMBIADA"]
        assert cola_vigentes(s, [16], ahora, 1, 3, 6) == ["CERCA"]


def test_cola_vigentes_deja_en_espera_a_la_que_falla_seguido(engine):
    ahora = datetime(2026, 11, 3, 12, 0)
    with Session(engine) as s:
        _ca(
            s,
            "FALLA",
            estado="publicada",
            cierre=ahora + timedelta(days=1),
            detalle_fallos=3,
            detalle_ultimo_fallo=ahora,
        )
        _ca(s, "OK", estado="publicada", cierre=ahora + timedelta(days=2))
        s.commit()
        assert cola_vigentes(s, [16], ahora, 10, 3, 6) == ["OK"]
        assert cola_vigentes(s, [16], ahora + timedelta(hours=7), 10, 3, 6) == ["OK", "FALLA"]


def test_detalle_vigente_no_toca_cotizaciones_ni_su_marca(engine):
    # Una abierta con ofertas ya guardadas y su marca vieja: el detalle de vigente no las
    # borra ni mueve `cotizaciones_obtenidas_en`, así la cola de cerradas la sigue pidiendo.
    obtenidas = datetime(2026, 8, 1, 10, 0)
    _guardar(engine, _payload("CA-V", estado="publicada"))
    with Session(engine) as s:
        ca = s.get(CompraAgil, "CA-V")
        assert ca is not None
        ca.cotizaciones_obtenidas_en = obtenidas
        s.commit()
    n_cot = _contar(engine, CaCotizacion)
    assert n_cot == 2
    ahora = datetime(2026, 11, 3, 12, 0)
    det = _parse_ca_detalle(_payload("CA-V", ofertas=[], estado="publicada"))
    with Session(engine) as s:
        guardar_detalle_vigente(s, det, ahora)
        s.commit()
    assert _contar(engine, CaCotizacion) == n_cot
    with Session(engine) as s:
        ca = s.get(CompraAgil, "CA-V")
        assert ca is not None
        assert ca.cotizaciones_obtenidas_en == obtenidas
        assert ca.detalle_vigente_en == ahora
        assert ca.n_documentos == 1
        assert len(ca.productos) == 1


def test_n_documentos_se_guarda_en_las_dos_rutas(engine):
    _guardar(engine, _payload("CA-CERRADA"))
    det = _parse_ca_detalle(_payload("CA-ABIERTA", ofertas=[], estado="publicada"))
    with Session(engine) as s:
        guardar_detalle_vigente(s, det, ahora_utc())
        s.commit()
        cerrada = s.get(CompraAgil, "CA-CERRADA")
        abierta = s.get(CompraAgil, "CA-ABIERTA")
        assert cerrada is not None and cerrada.n_documentos == 1
        assert abierta is not None and abierta.n_documentos == 1


@respx.mock
def test_mercado_ca_baja_vigentes_antes_que_cerradas_y_es_idempotente(settings, engine, v2):
    respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    abierta = respx.get(_DETALLE.format(codigo="CA-ABIERTA")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload_vigente("CA-ABIERTA")))
    )
    cerrada = respx.get(_DETALLE.format(codigo="CA-CERRADA")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-CERRADA")))
    )
    with Session(engine) as s:
        _vigente(s, "CA-ABIERTA")
        _ca(s, "CA-CERRADA", cierre=datetime(2026, 8, 1))
        s.commit()

    r = sync_mercado_ca(settings, engine, v2)
    assert (r["vigentes_cola"], r["vigentes_detalles"], r["vigentes_pendientes"]) == (1, 1, 0)
    assert r["detalles_guardados"] == 1
    assert abierta.call_count == 1 and cerrada.call_count == 1
    with Session(engine) as s:
        ca = s.get(CompraAgil, "CA-ABIERTA")
        assert ca is not None
        assert ca.detalle_vigente_en is not None and ca.cotizaciones_obtenidas_en is None
        assert len(ca.productos) == 1

    r2 = sync_mercado_ca(settings, engine, v2)
    assert (r2["vigentes_cola"], r2["cola"]) == (0, 0)
    assert abierta.call_count == 1 and cerrada.call_count == 1


@respx.mock
def test_mercado_ca_tope_de_cantidad_de_vigentes(monkeypatch, engine):
    cfg = _settings(monkeypatch, mercado_vigentes_max=2)
    cliente = MercadoPublicoV2Client(cfg, engine, reserva=0)
    respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    for c in ("CA-1", "CA-2", "CA-3"):
        respx.get(_DETALLE.format(codigo=c)).mock(
            return_value=httpx.Response(200, json=_envelope(_payload_vigente(c)))
        )
    with Session(engine) as s:
        for i, c in enumerate(("CA-1", "CA-2", "CA-3")):
            _vigente(s, c, dias=1 + i)
        s.commit()
    r = sync_mercado_ca(cfg, engine, cliente)
    assert (r["vigentes_cola"], r["vigentes_detalles"]) == (2, 2)
    r2 = sync_mercado_ca(cfg, engine, cliente)
    assert (r2["vigentes_cola"], r2["vigentes_detalles"]) == (1, 1)


@respx.mock
def test_mercado_ca_tope_de_minutos_de_vigentes_deja_tiempo_a_las_cerradas(monkeypatch, engine):
    cfg = _settings(monkeypatch, mercado_vigentes_minutos=1, mercado_detalles_minutos=10)
    cliente = MercadoPublicoV2Client(cfg, engine, reserva=0)
    respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    respx.get(url__regex=_LISTADO + r"/CA-V\d").mock(
        side_effect=lambda req: httpx.Response(
            200, json=_envelope(_payload_vigente(req.url.path.rsplit("/", 1)[1]))
        )
    )
    cerrada = respx.get(_DETALLE.format(codigo="CA-CERRADA")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-CERRADA")))
    )
    with Session(engine) as s:
        for i in (1, 2, 3):
            _vigente(s, f"CA-V{i}", dias=i)
        _ca(s, "CA-CERRADA", cierre=datetime(2026, 8, 1))
        s.commit()
    # Cada lectura del reloj avanza 25 s: el tope de 60 s de vigentes alcanza para dos.
    tics = itertools.count(0, 25)
    r = sync_mercado_ca(cfg, engine, cliente, reloj=lambda: float(next(tics)))
    assert r["vigentes_cortado_por_tiempo"] is True
    assert r["vigentes_detalles"] == 2 and r["vigentes_pendientes"] == 1
    assert cerrada.called  # sigue con las cerradas: les queda tiempo del total


@respx.mock
def test_mercado_ca_429_en_vigentes_corta_sin_pedir_cerradas(settings, engine, v2):
    respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    respx.get(_DETALLE.format(codigo="CA-ABIERTA")).mock(
        return_value=httpx.Response(429, json=_429)
    )
    cerrada = respx.get(_DETALLE.format(codigo="CA-CERRADA")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-CERRADA")))
    )
    with Session(engine) as s:
        _vigente(s, "CA-ABIERTA")
        _ca(s, "CA-CERRADA", cierre=datetime(2026, 8, 1))
        s.commit()
    r = sync_mercado_ca(settings, engine, v2)
    assert r["cortado_por"] == "MPTasaError"
    assert r["vigentes_detalles"] == 0
    assert not cerrada.called
    with Session(engine) as s:
        assert s.get(SyncState, "api_pausa") is not None


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
        return_value=httpx.Response(
            200, json=_envelope(_payload("CA-3", publicada="2026-08-03 09:00"))
        )
    )
    # F-merc-1c: con 0 pausas, el comportamiento previo (cortar al primer 429).
    s = _con_meses(settings, "2026-08", mercado_backfill_max_pausas=0)

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


# --- F-merc-1c: el backfill espera la pausa del 429 y sigue --------------------------

_JITTER = 45.0
_PAUSA_S = 20 * 60  # PAUSA_TASA_MIN por defecto


class _Reloj:
    """Hora de Chile, reloj monotónico y `sleep` falsos y compartidos: dormir los adelanta.

    El QuotaTracker del cliente usa la misma hora, así la pausa que persiste el 429 y la
    que lee el backfill hablan del mismo tiempo. Al dormir anota las conexiones del pool
    que siguen prestadas (§2.7) y corre el gancho pendiente, si hay.
    """

    def __init__(self, inicio: datetime, engine: Engine, v2: MercadoPublicoV2Client) -> None:
        self.t = inicio
        self.mono = 0.0
        self.engine = engine
        self.v2 = v2
        self.dormidas: list[float] = []
        self.prestadas_al_dormir: list[int] = []
        self.ganchos: list[Any] = []
        v2._client._quota._now_fn = lambda: self.t

    def now(self, tz: Any = TZ_CHILE) -> datetime:
        return self.t.astimezone(tz)

    def reloj(self) -> float:
        return self.mono

    def sleep(self, segundos: float) -> None:
        self.prestadas_al_dormir.append(self.engine.pool.checkedout())  # type: ignore[attr-defined]
        self.dormidas.append(segundos)
        if self.ganchos:
            self.ganchos.pop(0)()
        self.t += timedelta(seconds=segundos)
        self.mono += segundos

    def correr(self, s: Settings) -> dict[str, Any]:
        return run_backfill(
            s,
            self.engine,
            self.v2,
            now_fn=self.now,
            reloj=self.reloj,
            sleep_fn=self.sleep,
            jitter_fn=lambda: _JITTER,
        )


def _dias_con_429(dias: dict[str, Any], veces_429: dict[str, int]) -> Any:
    """Como `_listado_por_dia`, pero el día de `veces_429` responde 429 esas veces y luego OK."""
    base = _listado_por_dia(dias)

    def responder(request: httpx.Request) -> httpx.Response:
        dia = request.url.params["publicado_desde"][:10]
        if veces_429.get(dia, 0) > 0:
            veces_429[dia] -= 1
            return httpx.Response(429, json=_429)
        return base(request)  # type: ignore[no-any-return]

    return responder


def _mocks_dias_1_y_3() -> tuple[Any, Any, dict[str, int]]:
    """Día 1: CA-1 (detalle); día 2: 429 una vez; día 3: CA-3. Devuelve (listado, d1, veces)."""
    veces = {"2026-08-02": 1}
    listado = respx.get(_LISTADO).mock(
        side_effect=_dias_con_429(
            {
                "2026-08-01": [("CA-1", "proveedor_seleccionado")],
                "2026-08-03": [("CA-3", "proveedor_seleccionado")],
            },
            veces,
        )
    )
    d1 = respx.get(_DETALLE.format(codigo="CA-1")).mock(
        return_value=httpx.Response(200, json=_envelope(_payload("CA-1")))
    )
    respx.get(_DETALLE.format(codigo="CA-3")).mock(
        return_value=httpx.Response(
            200, json=_envelope(_payload("CA-3", publicada="2026-08-03 09:00"))
        )
    )
    return listado, d1, veces


def _llamadas_del_dia(listado: Any, dia: str) -> int:
    return sum(c.request.url.params["publicado_desde"][:10] == dia for c in listado.calls)


@respx.mock
def test_backfill_espera_la_pausa_y_completa_el_mes(settings, engine, v2, caplog):
    """§2.1, §2.7 y §2.8: 429 en el request 3 → duerme fin de pausa + jitter → completa."""
    listado, d1, _ = _mocks_dias_1_y_3()
    reloj = _Reloj(_NOCHE, engine, v2)
    dia2_al_dormir: list[int] = []
    reloj.ganchos.append(lambda: dia2_al_dormir.append(_llamadas_del_dia(listado, "2026-08-02")))

    with caplog.at_level(logging.DEBUG):
        r = reloj.correr(_con_meses(settings, "2026-08"))

    assert r["pausas"] == 1
    assert r["requests_por_tramo"] == [3]  # listado día 1, detalle CA-1, listado día 2 (429)
    assert reloj.dormidas == [pytest.approx(_PAUSA_S + _JITTER)]
    assert r["minutos_esperados"] == round((_PAUSA_S + _JITTER) / 60, 1)
    # El request del 429 no se reintenta: el día 2 se vuelve a listar recién tras la pausa,
    # en el tramo 2, y el detalle ya guardado del día 1 no se pide otra vez.
    assert dia2_al_dormir == [1]
    assert _llamadas_del_dia(listado, "2026-08-02") == 2
    assert d1.call_count == 1
    assert listado.call_count == 32
    assert r["meses_completos"] == ["2026-08"]
    assert r["cortado_por"] is None
    assert r["motivo_fin"] == "sin meses pendientes"
    assert _contar(engine, CaCotizacion) == 4
    # §2.7: ninguna Session ni conexión del pool viva mientras duerme.
    assert reloj.prestadas_al_dormir == [0]
    # §2.8: el log dice cuánto espera, sin ticket (regla 1).
    assert (
        "429 tras 3 requests del tramo; espero 20.8 min, hasta 23:20 Chile (pausa 1/12)"
        in caplog.text
    )
    assert _TICKET not in caplog.text


@respx.mock
def test_backfill_no_espera_una_pausa_que_termina_despues_de_las_0645(settings, engine, v2):
    """§2.2: a las 06:30 la pausa (20 min) termina a las 06:50: corta guardando el avance."""
    listado, _, _ = _mocks_dias_1_y_3()
    reloj = _Reloj(datetime(2026, 11, 1, 6, 30, tzinfo=TZ_CHILE), engine, v2)

    r = reloj.correr(_con_meses(settings, "2026-08"))

    assert reloj.dormidas == []
    assert r["pausas"] == 0
    assert r["cortado_por"] == "MPTasaError"
    assert "la pausa termina después de las 06:45" in r["motivo_fin"]
    assert r["requests_por_tramo"] == [3]
    assert _cursor(engine, "2026-08") == "2026-08-02|1"
    assert listado.call_count == 2


@respx.mock
def test_backfill_no_espera_una_pausa_que_no_cabe_en_el_tope(settings, engine, v2):
    """§2.3: con 20 min de tope (15 útiles) no cabe una pausa de 20 min + jitter."""
    _mocks_dias_1_y_3()
    reloj = _Reloj(_NOCHE, engine, v2)

    r = reloj.correr(_con_meses(settings, "2026-08", mercado_backfill_minutos=20))

    assert reloj.dormidas == []
    assert r["cortado_por"] == "MPTasaError"
    assert "la pausa no cabe en el tope de 20 min" in r["motivo_fin"]


@respx.mock
def test_backfill_corta_al_llegar_a_max_pausas(settings, engine, v2):
    """§2.4: el segundo 429 con `max_pausas=1` no se espera."""
    _, _, veces = _mocks_dias_1_y_3()
    veces["2026-08-02"] = 2
    reloj = _Reloj(_NOCHE, engine, v2)

    r = reloj.correr(_con_meses(settings, "2026-08", mercado_backfill_max_pausas=1))

    assert len(reloj.dormidas) == 1
    assert r["pausas"] == 1
    assert r["requests_por_tramo"] == [3, 1]
    assert r["cortado_por"] == "MPTasaError"
    assert "max pausas (1) alcanzado" in r["motivo_fin"]
    assert _cursor(engine, "2026-08") == "2026-08-02|1"


@respx.mock
def test_backfill_vuelve_a_esperar_si_otro_proceso_alargo_la_pausa(settings, engine, v2):
    """§2.5: al despertar la pausa sigue vigente (otro proceso la alargó): otra espera."""
    _mocks_dias_1_y_3()
    reloj = _Reloj(_NOCHE, engine, v2)
    quota = v2._client._quota
    reloj.ganchos.append(lambda: quota.pausar(reloj.t + timedelta(minutes=40), "429/429"))

    r = reloj.correr(_con_meses(settings, "2026-08"))

    assert r["pausas"] == 2
    assert len(reloj.dormidas) == 2
    # Despierta a las 23:20:45; la pausa ahora llega a las 23:40: espera 19:15 + jitter.
    assert reloj.dormidas[1] == pytest.approx(19 * 60 + 15 + _JITTER)
    assert r["requests_por_tramo"] == [3]
    assert r["meses_completos"] == ["2026-08"]
    assert r["cortado_por"] is None


@respx.mock
def test_backfill_10500_agotado_termina_sin_dormir(settings, engine, v2):
    """§2.6: el 429 de concurrencia (reintentos agotados) no es pausa: termina como antes."""
    respx.get(_LISTADO).mock(
        return_value=httpx.Response(429, json={"Codigo": 10500, "Mensaje": "simultáneas"})
    )
    reloj = _Reloj(_NOCHE, engine, v2)

    r = reloj.correr(_con_meses(settings, "2026-08"))

    assert reloj.dormidas == []
    assert r["cortado_por"] == "MPConcurrencyError"
    assert r["pausas"] == 0 and r["requests_por_tramo"] == []


@respx.mock
def test_backfill_corte_propio_termina_sin_dormir(settings, engine, v2):
    """§2.6: un `_Corte` (aquí, fin de la ventana a las 06:45) termina sin dormir."""
    listado = respx.get(_LISTADO).mock(return_value=httpx.Response(200, json=_listado([])))
    reloj = _Reloj(datetime(2026, 11, 1, 6, 46, tzinfo=TZ_CHILE), engine, v2)

    r = reloj.correr(_con_meses(settings, "2026-08"))

    assert reloj.dormidas == []
    assert not listado.called
    assert "fin de la ventana nocturna" in r["motivo_fin"]
    assert r["cortado_por"] is None


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
            _bytes_postgres(
                [
                    c.id,
                    c.ca_codigo,
                    c.llamado,
                    c.rut_proveedor,
                    c.razon_social,
                    c.es_emt,
                    c.codigo_empresa,
                    c.codigo_sucursal,
                    c.id_cotizacion,
                    c.id_oc,
                    c.estado_glosa,
                    c.activo,
                    c.seleccionado,
                    c.valor_neto,
                    c.total_impuesto,
                    c.monto_despacho,
                    c.monto_total,
                    c.fecha_creacion,
                    c.fecha_vigencia,
                    c.descripcion,
                ]
            )
            for c in s.execute(select(CaCotizacion)).scalars()
        ]
        productos = [
            _bytes_postgres(
                [
                    p.id,
                    p.cotizacion_id,
                    p.codigo_producto,
                    p.nombre_producto,
                    p.descripcion,
                    p.cantidad,
                    p.precio_unitario,
                    p.monto_total_producto,
                ]
            )
            for p in s.execute(select(CaCotizacionProducto)).scalars()
        ]
    # Índices (pkey, UNIQUE, rut, FK) ≈ 90 B por oferta y 36 B por producto.
    por_ca = sum(ofertas) + 90 * len(ofertas) + sum(productos) + 36 * len(productos)
    assert 400 < por_ca < 1200


# ---------------------------------------------------------------------------
# F-merc-1c §3.1: migración del UNIQUE por oferta (SQLite y Postgres)
# ---------------------------------------------------------------------------

_MIGRACION = (
    Path(__file__).parent.parent
    / "alembic"
    / "versions"
    / "b8d4f1a6c2e9_ofertas_por_id_cotizacion.py"
)


def _migracion() -> Any:
    spec = importlib.util.spec_from_file_location("migracion_ofertas", _MIGRACION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _aplicar(conn: Any, paso: str) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(conn)):
        getattr(_migracion(), paso)()


def _uniques(conn: Any) -> set[str]:
    from sqlalchemy import inspect

    return {u["name"] for u in inspect(conn).get_unique_constraints("ca_cotizaciones")}


def _ofertas_crudas(conn: Any, prefijo: str) -> list[tuple[str, str | None]]:
    return sorted(
        tuple(f)
        for f in conn.execute(
            text(
                "SELECT rut_proveedor, id_cotizacion FROM ca_cotizaciones WHERE ca_codigo LIKE :p"
            ),
            {"p": prefijo + "%"},
        )
    )


def _ida_y_vuelta(conn: Any, prefijo: str) -> None:
    """Baja y sube la migración sobre `conn` (una CA con dos RUT, uno con clave sustituta)."""
    # Insert de Core: aplica los defaults de Python de las columnas NOT NULL.
    conn.execute(
        CompraAgil.__table__.insert().values(codigo=prefijo + "1", nombre="x", estado="cerrada")
    )
    insertar = text(
        "INSERT INTO ca_cotizaciones (ca_codigo, llamado, rut_proveedor, id_cotizacion) "
        "VALUES (:c, 1, :r, :i)"
    )
    for rut, id_cot in (("1-9", "900"), ("2-7", "rut:2-7")):
        conn.execute(insertar, {"c": prefijo + "1", "r": rut, "i": id_cot})
    assert "uq_ca_cotizacion_oferta" in _uniques(conn)

    _aplicar(conn, "downgrade")
    assert "uq_ca_cotizacion" in _uniques(conn)
    assert "uq_ca_cotizacion_oferta" not in _uniques(conn)
    assert _ofertas_crudas(conn, prefijo) == [("1-9", "900"), ("2-7", None)]

    _aplicar(conn, "upgrade")
    assert "uq_ca_cotizacion_oferta" in _uniques(conn)
    assert "uq_ca_cotizacion" not in _uniques(conn)
    assert _ofertas_crudas(conn, prefijo) == [("1-9", "900"), ("2-7", "rut:2-7")]

    # Dos ofertas del mismo RUT: el downgrade falla claro y no borra nada.
    conn.execute(insertar, {"c": prefijo + "1", "r": "1-9", "i": "901"})
    with pytest.raises(RuntimeError, match="no borra"):
        _aplicar(conn, "downgrade")
    assert len(_ofertas_crudas(conn, prefijo)) == 3
    assert "uq_ca_cotizacion_oferta" in _uniques(conn)


def test_migracion_ofertas_sube_y_baja_en_sqlite(engine):
    with engine.begin() as conn:
        _ida_y_vuelta(conn, "MIG-")


@pytest.mark.skipif(not pg_url.TIENE_PG, reason=pg_url.MOTIVO)
def test_migracion_ofertas_sube_y_baja_en_postgres():
    """Dentro de una transacción que se deshace: el DDL de Postgres es transaccional."""
    pg = create_engine(pg_url.DB_URL)
    try:
        with pg.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text("SET LOCAL lock_timeout = '10s'"))
                # Base de tests compartida: se parte de la tabla vacía (se deshace al final).
                conn.execute(text("DELETE FROM ca_cotizaciones"))
                _ida_y_vuelta(conn, "ZZMIG-")
            finally:
                trans.rollback()
    finally:
        pg.dispose()


# ---------------------------------------------------------------------------
# F-merc-1c §3.3: reabrir las CA guardadas con ofertas de menos
# ---------------------------------------------------------------------------


def _cargar_reabrir() -> Any:
    ruta = Path(__file__).parent.parent / "scripts" / "mercado" / "reabrir_ofertas_faltantes.py"
    spec = importlib.util.spec_from_file_location("reabrir_ofertas_faltantes", ruta)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ca_con_filas(s: Session, codigo: str, total: int, filas: int, region: int = 16) -> None:
    _ca(s, codigo, region=region, obtenidas=datetime(2026, 11, 4, 3, 0), total_ofertas=total)
    s.flush()
    for i in range(filas):
        s.add(
            CaCotizacion(ca_codigo=codigo, llamado=1, rut_proveedor=f"{i}-K", id_cotizacion=str(i))
        )


def _obtenidas(engine: Engine, codigo: str) -> datetime | None:
    with Session(engine) as s:
        return s.get(CompraAgil, codigo).cotizaciones_obtenidas_en  # type: ignore[union-attr]


def test_reabrir_dry_run_no_escribe_y_aplicar_reabre(engine):
    with Session(engine) as s:
        _ca_con_filas(s, "CA-FALTAN", total=5, filas=3)
        _ca_con_filas(s, "CA-COMPLETA", total=2, filas=2)
        _ca_con_filas(s, "CA-SIN-TOTAL", total=0, filas=1)
        _ca_con_filas(s, "CA-RM", total=5, filas=1, region=13)
        s.commit()
    mod = _cargar_reabrir()

    r = mod.reabrir(engine, [16], aplicar=False)
    assert r.ca == [("CA-FALTAN", 5, 3)]
    assert r.requests == 1 and r.ofertas_faltantes == 2
    assert not r.aplicado
    assert _obtenidas(engine, "CA-FALTAN") is not None  # dry-run: nada escrito

    r = mod.reabrir(engine, [16], aplicar=True)
    assert r.aplicado
    assert _obtenidas(engine, "CA-FALTAN") is None
    assert _obtenidas(engine, "CA-COMPLETA") is not None
    assert _obtenidas(engine, "CA-RM") is not None


def test_reabrir_no_aplica_si_total_ofertas_cuenta_rut(engine):
    """Si una CA de la sonda (16 ofertas de 13 RUT) trae total_ofertas=13, el criterio no sirve."""
    with Session(engine) as s:
        _ca_con_filas(s, "4158-26-COT26", total=13, filas=13)
        _ca_con_filas(s, "CA-FALTAN", total=5, filas=3)
        s.commit()
    mod = _cargar_reabrir()

    r = mod.reabrir(engine, [16], aplicar=True)

    assert r.cuenta_ofertas is False
    assert not r.aplicado
    assert _obtenidas(engine, "CA-FALTAN") is not None
    assert any("cuenta RUT" in linea for linea in r.verificacion)


def test_reabrir_con_total_que_cuenta_ofertas(engine):
    with Session(engine) as s:
        _ca_con_filas(s, "4158-26-COT26", total=16, filas=13)
        s.commit()
    r = _cargar_reabrir().reabrir(engine, [16], aplicar=False)
    assert r.cuenta_ofertas is True
    assert r.ca == [("4158-26-COT26", 16, 13)]


# ---------------------------------------------------------------------------
# F-merc-1c §3.4: consumidores con varias ofertas por RUT
# ---------------------------------------------------------------------------


def test_bandas_una_oferta_por_rut():
    una = _cargar_script_mercado("bandas_precio").una_por_rut
    # Una oferta por RUT: igual que antes, mismo orden.
    simples = [("B", 10.0, False, 1.0), ("A", 9.0, True, 1.0)]
    assert una(simples) == simples
    # Competidor con dos ofertas: cuenta la más barata. Ganador: la seleccionada, aunque
    # tenga otra suya más barata.
    varias = [
        ("A", 9.0, True, 1.0),
        ("A", 8.0, False, 1.0),
        ("B", 12.0, False, 1.0),
        ("B", 11.0, False, 1.0),
    ]
    assert una(varias) == [("A", 9.0, True, 1.0), ("B", 11.0, False, 1.0)]


def _cargar_script_mercado(nombre: str) -> Any:
    ruta = Path(__file__).parent.parent / "scripts" / "mercado" / f"{nombre}.py"
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_exportar_cot_api_escribe_todas_las_ofertas_del_mismo_rut(engine, tmp_path):
    ofertas = [_oferta("11111111-1", True, 950), _oferta("11111111-1", False, 990)]
    _guardar(engine, _payload("CA-1", ofertas=ofertas))

    escritas = _cargar_exportador().exportar(
        engine, region=16, meses=None, dir_salida=tmp_path, sobrescribir=False
    )

    assert escritas == {"2026-08": 2}
    with gzip.open(tmp_path / "cot_api_2026-08.csv.gz", "rt", encoding="utf-8") as f:
        filas = list(csv.DictReader(f, delimiter=";"))
    assert sorted(f["ValorNeto"] for f in filas) == ["9500", "9900"]
