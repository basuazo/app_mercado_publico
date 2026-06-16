"""Tests F1 — clientes de la API de Mercado Público."""

from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import patch

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import create_engine, text

from app.clients.base import (
    BaseClient,
    MPAuthError,
    MPParseError,
    MPRateLimitError,
    MPServerError,
    QuotaExceededError,
    QuotaTracker,
    RateLimiter,
)
from app.clients.mp_v1 import MercadoPublicoV1Client
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.clients.types import CompraAgilBasica, LicitacionBasica, parse_binario, parse_fecha_v1
from app.core.settings import Settings

# ---------------------------------------------------------------------------
# Fixtures compartidos
# ---------------------------------------------------------------------------

_FAKE_TICKET = "ticket-de-test-1234"
_FAKE_DB = "postgresql://x:x@x/x"

_V1_BASE = "https://api.mercadopublico.cl/servicios/v1/publico/"
_V2_BASE = "https://api2.mercadopublico.cl"


@pytest.fixture()
def mem_engine():
    engine = create_engine("sqlite:///:memory:")
    yield engine
    engine.dispose()


@pytest.fixture()
def settings_fake(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MP_TICKET", _FAKE_TICKET)
    monkeypatch.setenv("DATABASE_URL", _FAKE_DB)
    monkeypatch.setenv("SECRET_KEY", "clave-de-test-32bytesxxxxxxxxxx")
    monkeypatch.setenv("JOBS_TOKEN", "token-de-test-jobs-abcdefgh1234")
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture()
def quota(mem_engine) -> QuotaTracker:
    return QuotaTracker(mem_engine, budget=100)


@pytest.fixture()
def rate_limiter() -> RateLimiter:
    rl = RateLimiter(rps=100.0)
    return rl


@pytest.fixture()
def base_client(rate_limiter, quota) -> BaseClient:
    return BaseClient(
        ticket=_FAKE_TICKET,
        rate_limiter=rate_limiter,
        quota=quota,
    )


def _v1_client(settings_fake, mem_engine) -> MercadoPublicoV1Client:
    c = MercadoPublicoV1Client.__new__(MercadoPublicoV1Client)
    from app.clients.base import BaseClient, QuotaTracker, RateLimiter

    rl = RateLimiter(rps=100.0)
    qt = QuotaTracker(mem_engine, budget=100)
    c._ticket = settings_fake.mp_ticket
    c._client = BaseClient(ticket=settings_fake.mp_ticket, rate_limiter=rl, quota=qt)
    return c


def _v2_client(settings_fake, mem_engine) -> MercadoPublicoV2Client:
    c = MercadoPublicoV2Client.__new__(MercadoPublicoV2Client)
    from app.clients.base import BaseClient, QuotaTracker, RateLimiter

    rl = RateLimiter(rps=100.0)
    qt = QuotaTracker(mem_engine, budget=100)
    c._ticket = settings_fake.mp_ticket
    c._client = BaseClient(
        ticket=settings_fake.mp_ticket,
        rate_limiter=rl,
        quota=qt,
        default_headers={"ticket": settings_fake.mp_ticket},
    )
    return c


# ---------------------------------------------------------------------------
# Tests de helpers de parsing
# ---------------------------------------------------------------------------


def test_parse_binario_variantes():
    assert parse_binario(0) is False
    assert parse_binario(1) is True
    assert parse_binario(2) is True
    assert parse_binario("NO") is False
    assert parse_binario("SI") is True
    assert parse_binario(None) is None
    assert parse_binario(True) is True


def test_parse_fecha_v1_valida():
    d = parse_fecha_v1("12062026")
    assert d is not None
    assert d.day == 12
    assert d.month == 6
    assert d.year == 2026


def test_parse_fecha_v1_invalida():
    assert parse_fecha_v1(None) is None
    assert parse_fecha_v1("") is None
    assert parse_fecha_v1("corta") is None


# ---------------------------------------------------------------------------
# Tests de QuotaTracker
# ---------------------------------------------------------------------------


def test_quota_remaining_inicial(quota):
    assert quota.remaining() == 100


def test_quota_consume_decrementa(quota):
    quota.consume(10)
    assert quota.remaining() == 90


def test_quota_exceeded(quota):
    quota.consume(100)
    with pytest.raises(QuotaExceededError):
        quota.check_budget()


def test_quota_persiste_dia_correcto(mem_engine):
    """Verifica que se usa la fecha de Santiago, no UTC."""
    # Congela el reloj a medianoche UTC (es aún ayer en Santiago UTC-4)
    with freeze_time("2026-06-13 03:00:00"):  # UTC = 2026-06-13 03:00 → Santiago = 2026-06-12 23:00
        qt = QuotaTracker(mem_engine, budget=50)
        qt.consume(5)
        assert qt.remaining() == 45
        # La fecha usada debe ser la de Santiago
        with mem_engine.connect() as conn:
            row = conn.execute(text("SELECT fecha FROM quota_log")).fetchone()
        assert row is not None
        # En Santiago (UTC-4/-3), a las 03:00 UTC es aún el día 12 (no el 13)
        assert str(row[0]).startswith("2026-06-12")


# ---------------------------------------------------------------------------
# Tests de la API v1
# ---------------------------------------------------------------------------


@respx.mock
def test_v1_licitaciones_activas_ok(settings_fake, mem_engine):
    respx.get(_V1_BASE + "licitaciones.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "Cantidad": 2,
                "Listado": [
                    {
                        "CodigoExterno": "1234-5-L114",
                        "Nombre": "Test Licitacion",
                        "CodigoEstado": 5,
                        "FechaCierre": "30062026",
                        "FechaPublicacion": "01062026",
                        "Tipo": "L1",
                        "CodigoOrganismo": "6945",
                    },
                    {
                        "CodigoExterno": "9999-1-LE14",
                        "Nombre": "Otra Licitacion",
                        "CodigoEstado": 5,
                        "FechaCierre": None,
                        "FechaPublicacion": None,
                        "Tipo": "LE",
                        "CodigoOrganismo": None,
                    },
                ],
            },
        )
    )
    client = _v1_client(settings_fake, mem_engine)
    result = client.licitaciones_activas()
    assert len(result) == 2
    assert isinstance(result[0], LicitacionBasica)
    assert result[0].codigo == "1234-5-L114"
    assert result[0].estado == 5


@respx.mock
def test_v1_licitacion_detalle_ok(settings_fake, mem_engine):
    respx.get(_V1_BASE + "licitaciones.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "Listado": [
                    {
                        "CodigoExterno": "1234-5-L114",
                        "Nombre": "Detalle Test",
                        "CodigoEstado": 5,
                        "FechaCierre": "30062026",
                        "FechaPublicacion": "01062026",
                        "Tipo": "L1",
                        "CodigoOrganismo": "6945",
                        "Descripcion": "Una descripcion",
                        "Moneda": "CLP",
                        "MontoEstimado": 1000000,
                        "Informada": 1,
                        "Contrato": "NO",
                        "Obras": 0,
                        "Items": {
                            "Listado": [
                                {
                                    "CodigoProducto": "123",
                                    "NombreProducto": "Producto A",
                                    "Cantidad": 5,
                                    "UnidadMedida": "UN",
                                }
                            ]
                        },
                    }
                ]
            },
        )
    )
    client = _v1_client(settings_fake, mem_engine)
    detalle = client.licitacion_detalle("1234-5-L114")
    assert detalle.descripcion == "Una descripcion"
    assert detalle.informada is True
    assert detalle.contrato is False
    assert detalle.obras is False
    assert len(detalle.items) == 1
    assert detalle.items[0].nombre == "Producto A"


@respx.mock
def test_v1_401(settings_fake, mem_engine):
    respx.get(_V1_BASE + "licitaciones.json").mock(return_value=httpx.Response(401))
    client = _v1_client(settings_fake, mem_engine)
    with pytest.raises(MPAuthError):
        client.licitaciones_activas()


@respx.mock
@freeze_time("2026-06-12T15:00:00", tz_offset=0)
def test_v1_429_retry_after(settings_fake, mem_engine):
    respx.get(_V1_BASE + "licitaciones.json").mock(return_value=httpx.Response(429))
    client = _v1_client(settings_fake, mem_engine)
    with pytest.raises(MPRateLimitError) as exc_info:
        client.licitaciones_activas()
    # retry_after debe ser positivo y apuntar al día siguiente en Santiago
    assert exc_info.value.retry_after_seconds > 0


@respx.mock
def test_v1_5xx_retry(settings_fake, mem_engine):
    """5xx → tenacity reintenta hasta 3 veces y luego lanza MPServerError."""
    respx.get(_V1_BASE + "licitaciones.json").mock(return_value=httpx.Response(503))
    client = _v1_client(settings_fake, mem_engine)
    with patch("tenacity.wait_exponential.__call__", return_value=0), pytest.raises(MPServerError):
        client.licitaciones_activas()


@respx.mock
def test_v1_json_malformado(settings_fake, mem_engine):
    respx.get(_V1_BASE + "licitaciones.json").mock(
        return_value=httpx.Response(200, content=b"esto no es json{{{")
    )
    client = _v1_client(settings_fake, mem_engine)
    with pytest.raises(MPParseError):
        client.licitaciones_activas()


# ---------------------------------------------------------------------------
# Tests de la API v2
# ---------------------------------------------------------------------------

_LISTADO_RESP = {
    "success": "OK",
    "payload": {
        "convocatorias": [
            {
                "codigo": "CA-001",
                "nombre": "Compra Test",
                "estado": {"codigo": "publicada"},
                "fechas": {
                    "fecha_publicacion": "2026-06-01T10:00:00",
                    "fecha_cierre": "2026-06-30T18:00:00",
                    "fecha_ultimo_cambio": "2026-06-01T10:00:00",
                },
                "montos": {"monto_disponible_clp": 500000},
                "institucion": {
                    "organismo_comprador": "MINSAL",
                    "rut": "61.001.000-0",
                    "region": 13,
                },
                "resumen": {"total_ofertas_recibidas": 3},
            }
        ],
        "paginacion": {
            "total_paginas": 1,
            "total_resultados": 1,
            "numero_pagina": 1,
            "tamano_pagina": 50,
        },
    },
    "errors": [],
}


@respx.mock
def test_v2_listar_ok(settings_fake, mem_engine):
    respx.get(_V2_BASE + "/v2/compra-agil").mock(
        return_value=httpx.Response(200, json=_LISTADO_RESP)
    )
    client = _v2_client(settings_fake, mem_engine)
    result = client.listar_compra_agil()
    assert len(result.items) == 1
    assert isinstance(result.items[0], CompraAgilBasica)
    assert result.items[0].codigo == "CA-001"
    assert result.items[0].monto_clp == 500000
    assert result.items[0].region == 13
    assert result.paginacion.total_paginas == 1


@respx.mock
def test_v2_detalle_ok(settings_fake, mem_engine):
    """Verifica el gotcha: id_orden_compra=null aunque exista OC."""
    respx.get(_V2_BASE + "/v2/compra-agil/CA-001").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": "OK",
                "payload": {
                    "codigo": "CA-001",
                    "nombre": "Compra Detalle",
                    "descripcion": "Descripcion completa",
                    "estado": {"codigo": "cerrada"},
                    "fechas": {
                        "fecha_publicacion": "2026-06-01T10:00:00",
                        "fecha_cierre": "2026-06-30T18:00:00",
                        "fecha_ultimo_cambio": "2026-06-02T10:00:00",
                    },
                    "montos": {"monto_disponible_clp": 500000},
                    "institucion": {
                        "region": 13,
                        "organismo_comprador": "MINSAL",
                        "rut": "61.001.000-0",
                    },
                    "resumen": {"total_ofertas_recibidas": 2},
                    "orden_compra": {"codigo_orden_compra": None, "id_orden_compra": None},
                    "productos_solicitados": [
                        {
                            "codigo_producto": "ABC",
                            "nombre": "Silla ergonómica",
                            "cantidad": 10,
                            "unidad_medida": "UN",
                        }
                    ],
                },
                "errors": [],
            },
        )
    )
    client = _v2_client(settings_fake, mem_engine)
    detalle = client.detalle_compra_agil("CA-001")
    assert detalle.id_orden_compra is None
    assert len(detalle.productos) == 1
    assert detalle.productos[0].nombre == "Silla ergonómica"


@respx.mock
def test_v2_401(settings_fake, mem_engine):
    respx.get(_V2_BASE + "/v2/compra-agil").mock(return_value=httpx.Response(401))
    client = _v2_client(settings_fake, mem_engine)
    with pytest.raises(MPAuthError):
        client.listar_compra_agil()


@respx.mock
@freeze_time("2026-06-12T20:00:00", tz_offset=0)
def test_v2_429(settings_fake, mem_engine):
    respx.get(_V2_BASE + "/v2/compra-agil").mock(return_value=httpx.Response(429))
    client = _v2_client(settings_fake, mem_engine)
    with pytest.raises(MPRateLimitError) as exc_info:
        client.listar_compra_agil()
    assert exc_info.value.retry_after_seconds > 0


@respx.mock
def test_v2_paginacion_multipagina(settings_fake, mem_engine):
    """iterar_compra_agil recorre 3 páginas."""

    def _pagina(n: int) -> dict:
        return {
            "success": "OK",
            "payload": {
                "convocatorias": [
                    {
                        "codigo": f"CA-{n:03d}",
                        "nombre": f"Compra {n}",
                        "estado": {"codigo": "publicada"},
                        "fechas": {},
                        "montos": {},
                        "institucion": {},
                        "resumen": {},
                    }
                ],
                "paginacion": {
                    "total_paginas": 3,
                    "total_resultados": 3,
                    "numero_pagina": n,
                    "tamano_pagina": 1,
                },
            },
            "errors": [],
        }

    route = respx.get(_V2_BASE + "/v2/compra-agil")
    route.side_effect = [
        httpx.Response(200, json=_pagina(1)),
        httpx.Response(200, json=_pagina(2)),
        httpx.Response(200, json=_pagina(3)),
    ]

    client = _v2_client(settings_fake, mem_engine)
    items = list(client.iterar_compra_agil(tamano_pagina=1))
    assert len(items) == 3
    assert [i.codigo for i in items] == ["CA-001", "CA-002", "CA-003"]


def test_v2_exclusion_mutua_ttl_cambio_desde(settings_fake, mem_engine):
    client = _v2_client(settings_fake, mem_engine)
    with pytest.raises(ValueError):
        client.listar_compra_agil(
            ttl_cambio_ms=5000,
            cambio_desde=datetime(2026, 6, 1),
        )


# ---------------------------------------------------------------------------
# Tests adicionales — cobertura de v1 (órdenes, proveedor, compradores)
# ---------------------------------------------------------------------------


@respx.mock
def test_v1_ordenes_por_fecha_ok(settings_fake, mem_engine):
    respx.get(_V1_BASE + "ordenesdecompra.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "Listado": [
                    {
                        "Codigo": "2097-241-SE14",
                        "Nombre": "OC Test",
                        "CodigoEstado": 6,
                        "Tipo": 1,
                        "FechaCreacion": "12062026",
                        "CodigoOrganismo": "6945",
                        "MontoTotal": 500000,
                        "Moneda": "CLP",
                    }
                ]
            },
        )
    )
    from datetime import date

    client = _v1_client(settings_fake, mem_engine)
    result = client.ordenes_por_fecha(date(2026, 6, 12))
    assert len(result) == 1
    assert result[0].codigo == "2097-241-SE14"
    assert result[0].estado == 6


@respx.mock
def test_v1_orden_detalle_ok(settings_fake, mem_engine):
    respx.get(_V1_BASE + "ordenesdecompra.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "Listado": [
                    {
                        "Codigo": "2097-241-SE14",
                        "Nombre": "OC Detalle",
                        "CodigoEstado": 6,
                        "Tipo": 9,
                        "FechaCreacion": "12062026",
                        "CodigoOrganismo": "1234",
                        "MontoTotal": 800000,
                        "Moneda": "CLP",
                    }
                ]
            },
        )
    )
    client = _v1_client(settings_fake, mem_engine)
    oc = client.orden_detalle("2097-241-SE14")
    assert oc.codigo == "2097-241-SE14"
    assert oc.tipo == 9


@respx.mock
def test_v1_buscar_proveedor_ok(settings_fake, mem_engine):
    respx.get(_V1_BASE + "Empresas/BuscarProveedor").mock(
        return_value=httpx.Response(
            200,
            json={
                "Listado": [
                    {
                        "RutProveedor": "70.017.820-k",
                        "NombreProveedor": "Empresa Test SpA",
                        "CodigoProveedor": "17793",
                    }
                ]
            },
        )
    )
    client = _v1_client(settings_fake, mem_engine)
    result = client.buscar_proveedor("70.017.820-k")
    assert len(result) == 1
    assert result[0].rut == "70.017.820-k"
    assert result[0].nombre == "Empresa Test SpA"


@respx.mock
def test_v1_listar_compradores_ok(settings_fake, mem_engine):
    respx.get(_V1_BASE + "Empresas/BuscarComprador").mock(
        return_value=httpx.Response(
            200,
            json={
                "Listado": [
                    {
                        "CodigoOrganismo": "6945",
                        "NombreOrganismo": "MINSAL",
                        "RutOrganismo": "61.001.000-0",
                    },
                    {"CodigoOrganismo": "1234", "NombreOrganismo": "MINEDUC", "RutOrganismo": None},
                ]
            },
        )
    )
    client = _v1_client(settings_fake, mem_engine)
    result = client.listar_compradores()
    assert len(result) == 2
    assert result[0].codigo == "6945"


@respx.mock
def test_v1_licitaciones_listado_vacio(settings_fake, mem_engine):
    respx.get(_V1_BASE + "licitaciones.json").mock(
        return_value=httpx.Response(200, json={"Cantidad": 0, "Listado": []})
    )
    client = _v1_client(settings_fake, mem_engine)
    result = client.licitaciones_activas()
    assert result == []


@respx.mock
def test_v2_envelope_error_generico(settings_fake, mem_engine):
    """Envelope con success=NOK sin error 401 → MPParseError."""
    respx.get(_V2_BASE + "/v2/compra-agil").mock(
        return_value=httpx.Response(
            200,
            json={"success": "NOK", "errors": [{"codigo": "500", "mensaje": "Error interno"}]},
        )
    )
    client = _v2_client(settings_fake, mem_engine)
    with pytest.raises(MPParseError):
        client.listar_compra_agil()


# ---------------------------------------------------------------------------
# Test de enmascaramiento de ticket en logs
# ---------------------------------------------------------------------------


@respx.mock
# ---------------------------------------------------------------------------
# Cobertura de ramas adicionales de mp_v1.py
# ---------------------------------------------------------------------------


@respx.mock
def test_v1_licitaciones_por_fecha_con_params(settings_fake, mem_engine):
    """licitaciones_por_fecha pasa parámetros opcionales (estado, organismo, proveedor)."""
    from datetime import date

    respx.get(_V1_BASE + "licitaciones.json").mock(
        return_value=httpx.Response(200, json={"Listado": []})
    )
    client = _v1_client(settings_fake, mem_engine)
    result = client.licitaciones_por_fecha(
        date(2026, 6, 12),
        estado="activas",
        codigo_organismo="ORG-1",
        codigo_proveedor="PROV-1",
    )
    assert result == []
    # Verifica que los params opcionales se incluyeron en la request
    req = respx.calls[0].request
    assert "estado" in str(req.url)


@respx.mock
def test_v1_licitaciones_activas_listado_no_lista(settings_fake, mem_engine):
    """Listado no lista en licitaciones_activas → []."""
    respx.get(_V1_BASE + "licitaciones.json").mock(
        return_value=httpx.Response(200, json={"Listado": "error_string"})
    )
    client = _v1_client(settings_fake, mem_engine)
    assert client.licitaciones_activas() == []


@respx.mock
def test_v1_ordenes_por_fecha_con_params(settings_fake, mem_engine):
    """ordenes_por_fecha pasa parámetros opcionales."""
    from datetime import date

    respx.get(_V1_BASE + "ordenesdecompra.json").mock(
        return_value=httpx.Response(200, json={"Listado": []})
    )
    client = _v1_client(settings_fake, mem_engine)
    result = client.ordenes_por_fecha(
        date(2026, 6, 12),
        estado="recepcionada",
        codigo_organismo="ORG-2",
        codigo_proveedor="PROV-2",
    )
    assert result == []


@respx.mock
def test_v1_ordenes_por_fecha_listado_no_lista(settings_fake, mem_engine):
    """Listado no lista en ordenes_por_fecha → []."""
    from datetime import date

    respx.get(_V1_BASE + "ordenesdecompra.json").mock(
        return_value=httpx.Response(200, json={"Listado": None})
    )
    client = _v1_client(settings_fake, mem_engine)
    assert client.ordenes_por_fecha(date(2026, 6, 12)) == []


@respx.mock
def test_v1_buscar_proveedor_listado_no_lista(settings_fake, mem_engine):
    """Listado no lista en buscar_proveedor → []."""
    respx.get(_V1_BASE + "Empresas/BuscarProveedor").mock(
        return_value=httpx.Response(200, json={"Listado": "bad"})
    )
    client = _v1_client(settings_fake, mem_engine)
    assert client.buscar_proveedor("12.345.678-9") == []


@respx.mock
def test_v1_listar_compradores_listado_no_lista(settings_fake, mem_engine):
    """Listado no lista en listar_compradores → []."""
    respx.get(_V1_BASE + "Empresas/BuscarComprador").mock(
        return_value=httpx.Response(200, json={"Listado": 42})
    )
    client = _v1_client(settings_fake, mem_engine)
    assert client.listar_compradores() == []


@respx.mock
def test_v1_licitacion_detalle_items_como_lista(settings_fake, mem_engine):
    """_parse_licitacion_detalle con Items como lista (no dict)."""
    respx.get(_V1_BASE + "licitaciones.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "Listado": [
                    {
                        "CodigoExterno": "LIC-LISTA",
                        "Nombre": "Lic con Items lista",
                        "CodigoEstado": 5,
                        "FechaCierre": "30062026",
                        "FechaPublicacion": "01062026",
                        "Items": [
                            {
                                "CodigoProducto": "P1",
                                "NombreProducto": "Producto Lista",
                                "Cantidad": 3,
                                "UnidadMedida": "UN",
                            }
                        ],
                    }
                ]
            },
        )
    )
    client = _v1_client(settings_fake, mem_engine)
    detalle = client.licitacion_detalle("LIC-LISTA")
    assert len(detalle.items) == 1
    assert detalle.items[0].nombre == "Producto Lista"


def test_ticket_nunca_en_logs(settings_fake, mem_engine, caplog, monkeypatch):
    """El ticket NO debe aparecer en ningún mensaje de log (incluyendo httpx)."""

    from app.core.logging import _SecretFilter, setup_logging

    monkeypatch.setenv("MP_TICKET", _FAKE_TICKET)
    setup_logging(logging.DEBUG)

    respx.get(_V1_BASE + "licitaciones.json").mock(
        return_value=httpx.Response(200, json={"Cantidad": 0, "Listado": []})
    )
    client = _v1_client(settings_fake, mem_engine)

    # caplog captura registros antes del filtro; verificamos directamente con el filtro
    filter_ = _SecretFilter()
    with caplog.at_level(logging.DEBUG):
        client.licitaciones_activas()

    for record in caplog.records:
        # Aplicar el filtro manualmente al record para simular lo que haría el logger
        import copy

        r = copy.copy(record)
        filter_.filter(r)
        masked_msg = r.getMessage()
        assert _FAKE_TICKET not in masked_msg, f"Ticket sin enmascarar: {masked_msg}"
