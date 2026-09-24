"""Tests F-raw-json: raw_json serializable.

El bug: ``run_match`` guardaba ``raw_json = asdict(det)`` y el detalle trae
``datetime``; el commit contra JSONB fallaba y se deshacía todo el detalle.
El camino detalle → commit JSONB contra Postgres, la cola y el tope por tiempo
viven ahora en tests/test_detalles_match.py (F-detalles-match).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from app.clients.types import (
    CompraAgilDetalle,
    CompraAgilItem,
    ItemLicitacion,
    LicitacionDetalle,
)
from app.core.serializacion import a_json, dataclass_a_json
from app.core.tiempo import ahora_utc

_VALID_ENV = {
    "MP_TICKET": "ticket-test-raw-json",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-raw-json-32bytesxxxxx",
    "JOBS_TOKEN": "token-test-raw-json-jobs-xxxxxx",
}


# ---------------------------------------------------------------------------
# 1. Helper puro
# ---------------------------------------------------------------------------


class _Color(Enum):
    ROJO = "rojo"


class _Nivel(Enum):
    ALTO = 3


@dataclass
class _Hijo:
    cuando: date
    color: _Color


@dataclass
class _Padre:
    nombre: str
    instante: datetime | None
    monto: Decimal
    hijos: list[_Hijo] = field(default_factory=list)
    par: tuple[int, date] = (1, date(2026, 1, 2))
    extra: dict[str, Any] = field(default_factory=dict)


class TestSerializacion:
    def test_escalares_nativos_quedan_igual(self) -> None:
        for v in (None, True, False, 0, 7, 1.5, "texto", ""):
            assert a_json(v) == v

    def test_datetime_naive_en_isoformat_sin_zona(self) -> None:
        # Se guarda tal cual lo tiene el dataclass: naive UTC, sin agregar zona.
        assert a_json(datetime(2026, 9, 23, 16, 0, 0)) == "2026-09-23T16:00:00"

    def test_date_y_time(self) -> None:
        assert a_json(date(2026, 9, 23)) == "2026-09-23"
        assert a_json(time(13, 5)) == "13:05:00"

    def test_decimal_a_str_sin_perder_precision(self) -> None:
        assert a_json(Decimal("1234567.89")) == "1234567.89"

    def test_enum_a_su_value(self) -> None:
        assert a_json(_Color.ROJO) == "rojo"
        assert a_json(_Nivel.ALTO) == 3

    def test_float_no_finito_a_none(self) -> None:
        assert a_json(float("nan")) is None
        assert a_json(float("inf")) is None

    def test_anidados(self) -> None:
        obj = _Padre(
            nombre="p",
            instante=None,
            monto=Decimal("10.5"),
            hijos=[_Hijo(date(2026, 3, 1), _Color.ROJO)],
            extra={"en": datetime(2026, 1, 1, 8, 30), 5: [Decimal("1")]},
        )
        assert dataclass_a_json(obj) == {
            "nombre": "p",
            "instante": None,
            "monto": "10.5",
            "hijos": [{"cuando": "2026-03-01", "color": "rojo"}],
            "par": [1, "2026-01-02"],
            "extra": {"en": "2026-01-01T08:30:00", "5": ["1"]},
        }

    def test_tipo_desconocido_a_str_sin_romper(self) -> None:
        class _Raro:
            def __str__(self) -> str:
                return "raro!"

        assert a_json({"x": _Raro()}) == {"x": "raro!"}

    def test_resultado_pasa_por_json_dumps(self) -> None:
        det = _lic_detalle("LIC-SER", ahora_utc() + timedelta(days=3))
        json.dumps(dataclass_a_json(det))  # no lanza

    def test_rechaza_lo_que_no_es_instancia_de_dataclass(self) -> None:
        with pytest.raises(TypeError):
            dataclass_a_json({"a": 1})
        with pytest.raises(TypeError):
            dataclass_a_json(_Padre)


# ---------------------------------------------------------------------------
# Constructores de detalles con fechas reales
# ---------------------------------------------------------------------------


def _lic_detalle(codigo: str, cierre: datetime, nombre: str = "Licitación de prueba") -> LicitacionDetalle:
    return LicitacionDetalle(
        codigo=codigo,
        nombre=nombre,
        estado=5,
        fecha_publicacion=cierre - timedelta(days=10),
        fecha_cierre=cierre,
        tipo="L1",
        codigo_organismo="ORG-RAWJSON",
        descripcion=f"Descripción del detalle {codigo}",
        moneda="CLP",
        monto_estimado=1_500_000.0,
        items=[ItemLicitacion("43211500", "Notebook", 3.0, "Unidad")],
    )


def _ca_detalle(codigo: str, cierre: datetime, nombre: str = "Compra ágil de prueba") -> CompraAgilDetalle:
    return CompraAgilDetalle(
        codigo=codigo,
        nombre=nombre,
        estado="publicada",
        fecha_publicacion=cierre - timedelta(days=2),
        fecha_cierre=cierre,
        fecha_ultimo_cambio=cierre - timedelta(days=1),
        monto_clp=800_000.0,
        region=13,
        organismo_nombre="Organismo de prueba",
        organismo_rut="61.000.000-0",
        total_ofertas=0,
        descripcion=f"Descripción del detalle {codigo}",
        productos=[CompraAgilItem("44121600", "Resmas", 10.0, "Caja")],
        id_orden_compra="1234-56-SE26",
        estado_convocatoria=1,
    )
