"""Tests F-raw-json: raw_json serializable y tope de detalles por corrida en match.

El bug: ``run_match`` guardaba ``raw_json = asdict(det)`` y el detalle trae
``datetime``; el commit contra JSONB fallaba, se deshacía todo el detalle y la
corrida siguiente volvía a pedir los mismos, sin tope. Ningún test lo veía
porque el único que pasaba por ahí usaba SQLite, fechas en None y no revisaba
qué quedaba guardado.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.clients.types import (
    CompraAgilDetalle,
    CompraAgilItem,
    ItemLicitacion,
    LicitacionDetalle,
)
from app.core.serializacion import a_json, dataclass_a_json
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.models.tables import CaProducto, CompraAgil, Licitacion, LicitacionItem, Usuario

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


def _resultado_match(sin_lic: list[str], sin_ca: list[str]) -> dict[str, Any]:
    return {
        "perfiles_procesados": 1,
        "nuevos": 0,
        "actualizados": 0,
        "descartados": 0,
        "sin_detalle_licitaciones": sin_lic,
        "sin_detalle_ca": sin_ca,
    }


# ---------------------------------------------------------------------------
# 2. Tope y orden de prioridad (SQLite: solo se prueba la cola, no el guardado)
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None, match_max_detalles_por_corrida=40)  # type: ignore[call-arg]


@pytest.fixture()
def sqlite_engine():
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


class TestTopeDetalles:
    def _poblar(self, engine: Any) -> tuple[list[str], list[str], list[tuple[str, str]]]:
        """50 pendientes: 30 futuras (lic y CA intercaladas), 10 sin cierre, 10 cerradas.

        Devuelve (sin_lic, sin_ca, orden esperado completo).
        """
        ahora = ahora_utc()
        futuras: list[tuple[str, str]] = []
        sin_cierre: list[tuple[str, str]] = []
        cerradas: list[tuple[str, str]] = []
        with Session(engine) as s:
            for i in range(30):
                # Horas pares → licitación, impares → CA; el cierre crece con i.
                cierre = ahora + timedelta(hours=i + 1)
                if i % 2 == 0:
                    cod = f"LIC-F{i:02d}"
                    s.add(Licitacion(codigo=cod, nombre=cod, estado="publicada", fecha_cierre=cierre))
                    futuras.append(("licitaciones", cod))
                else:
                    cod = f"CA-F{i:02d}"
                    s.add(CompraAgil(codigo=cod, nombre=cod, estado="publicada", fecha_cierre=cierre))
                    futuras.append(("compras_agiles", cod))
            for i in range(10):
                cod = f"CA-N{i:02d}"
                s.add(CompraAgil(codigo=cod, nombre=cod, estado="publicada", fecha_cierre=None))
                sin_cierre.append(("compras_agiles", cod))
            for i in range(10):
                # La más antigua primero dentro de las cerradas.
                cod = f"LIC-C{i:02d}"
                cierre = ahora - timedelta(days=10 - i)
                s.add(Licitacion(codigo=cod, nombre=cod, estado="cerrada", fecha_cierre=cierre))
                cerradas.append(("licitaciones", cod))
            s.commit()

        todas = futuras + sin_cierre + cerradas
        sin_lic = [c for f, c in todas if f == "licitaciones"]
        sin_ca = [c for f, c in todas if f == "compras_agiles"]
        # Las listas del engine vienen de un set(): orden arbitrario.
        sin_lic.reverse()
        return sin_lic, sin_ca, todas

    def test_con_50_pendientes_y_tope_40_intenta_40_en_orden(self, settings, sqlite_engine) -> None:
        from app.ingest.orchestrator import run_match

        sin_lic, sin_ca, orden = self._poblar(sqlite_engine)
        assert len(sin_lic) + len(sin_ca) == 50
        llamadas: list[tuple[str, str]] = []

        def _fake_bajar(_s, _e, _v1, _v2, fuente: str, codigo: str) -> None:
            llamadas.append((fuente, codigo))

        with (
            patch("app.matching.engine.match_todos", return_value=_resultado_match(sin_lic, sin_ca)),
            patch("app.ingest.orchestrator._make_clients", return_value=(MagicMock(), MagicMock())),
            patch("app.ingest.orchestrator._bajar_detalle", side_effect=_fake_bajar),
        ):
            result = run_match(settings, sqlite_engine)

        # 30 futuras por cierre ascendente + 10 sin cierre; las cerradas quedan fuera.
        assert llamadas == orden[:40]
        assert result["detalles_intentados"] == 40
        assert result["detalles_guardados"] == 40
        assert result["detalles_fallidos"] == 0
        assert result["detalles_pendientes"] == 10

    def test_sin_tope_efectivo_las_cerradas_van_al_final(self, settings, sqlite_engine) -> None:
        from app.ingest.orchestrator import _priorizar_detalles

        sin_lic, sin_ca, orden = self._poblar(sqlite_engine)
        with Session(sqlite_engine) as s:
            cola = _priorizar_detalles(s, sin_lic, sin_ca, 100, ahora_utc())
        assert cola == orden

    def test_fallido_no_corta_la_cola_y_se_cuenta(self, settings, sqlite_engine) -> None:
        from app.clients.base import MPServerError
        from app.ingest.orchestrator import run_match

        sin_lic, sin_ca, orden = self._poblar(sqlite_engine)
        primero = orden[0]

        def _fake_bajar(_s, _e, _v1, _v2, fuente: str, codigo: str) -> None:
            if (fuente, codigo) == primero:
                raise MPServerError("504 persistente")

        with (
            patch("app.matching.engine.match_todos", return_value=_resultado_match(sin_lic, sin_ca)),
            patch("app.ingest.orchestrator._make_clients", return_value=(MagicMock(), MagicMock())),
            patch("app.ingest.orchestrator._bajar_detalle", side_effect=_fake_bajar),
        ):
            result = run_match(settings, sqlite_engine)

        assert result["detalles_intentados"] == 40
        assert result["detalles_guardados"] == 39
        assert result["detalles_fallidos"] == 1
        assert result["detalles_pendientes"] == 10

    def test_corte_de_canal_deja_el_resto_pendiente(self, settings, sqlite_engine) -> None:
        from app.clients.base import MPRateLimitError
        from app.ingest.orchestrator import run_match

        sin_lic, sin_ca, orden = self._poblar(sqlite_engine)
        n = 0

        def _fake_bajar(_s, _e, _v1, _v2, fuente: str, codigo: str) -> None:
            nonlocal n
            n += 1
            if n == 5:
                raise MPRateLimitError("tope diario", retry_after_seconds=3600)

        with (
            patch("app.matching.engine.match_todos", return_value=_resultado_match(sin_lic, sin_ca)),
            patch("app.ingest.orchestrator._make_clients", return_value=(MagicMock(), MagicMock())),
            patch("app.ingest.orchestrator._bajar_detalle", side_effect=_fake_bajar),
        ):
            result = run_match(settings, sqlite_engine)

        assert result["detalles_interrumpidos"] is True
        assert result["detalles_intentados"] == 5
        assert result["detalles_guardados"] == 4
        assert result["detalles_fallidos"] == 1
        assert result["detalles_pendientes"] == 45

    def test_sin_pendientes_no_crea_clientes(self, settings, sqlite_engine) -> None:
        from app.ingest.orchestrator import run_match

        with (
            patch("app.matching.engine.match_todos", return_value=_resultado_match([], [])),
            patch("app.ingest.orchestrator._make_clients") as mk,
        ):
            result = run_match(settings, sqlite_engine)

        mk.assert_not_called()
        assert result["detalles_intentados"] == 0
        assert result["detalles_pendientes"] == 0


# ---------------------------------------------------------------------------
# 3. Integración contra Postgres: el camino detalle → commit JSONB
# ---------------------------------------------------------------------------

# Keyword inventada: así el perfil del test solo matchea sus propias filas
# aunque la BD de dev tenga datos reales.
_KW = "zorglubrawjson"
_EMAIL = "raw_json_test@test.com"
_LIC = "LIC-RAWJSON-1"
_CA = "CA-RAWJSON-1"


@pytest.fixture()
def pg_engine(db_url: str):
    if not db_url.startswith("postgres"):
        pytest.skip("Requiere DATABASE_URL de Postgres (JSONB real)")
    from app.core.db import normalizar_url_driver

    e = create_engine(normalizar_url_driver(db_url), pool_pre_ping=True)
    yield e
    e.dispose()


@pytest.fixture()
def pg_settings(db_url: str) -> Settings:
    return Settings(match_max_detalles_por_corrida=40)  # type: ignore[call-arg]


@pytest.fixture()
def escenario(pg_engine):
    """Usuario + perfil + una licitación y una CA sin detalle, que matchean `_KW`.

    Limpia antes y después para autorepararse si una corrida anterior se cortó.
    """
    from app.matching.perfiles import crear_perfil

    def _limpiar() -> None:
        with Session(pg_engine) as s:
            s.execute(delete(Licitacion).where(Licitacion.codigo == _LIC))
            s.execute(delete(CompraAgil).where(CompraAgil.codigo == _CA))
            u = s.execute(select(Usuario).where(Usuario.email == _EMAIL)).scalar_one_or_none()
            if u:
                s.delete(u)
            s.commit()

    _limpiar()
    cierre = (ahora_utc() + timedelta(days=5)).replace(microsecond=0)
    with Session(pg_engine) as s:
        u = Usuario(email=_EMAIL, password_hash="$2b$12$fakehashforraw.json.test.xyz", activo=True)
        s.add(u)
        s.flush()
        perfil = crear_perfil(s, u.id, "Raw JSON (test)", keywords=[_KW])
        s.add(Licitacion(codigo=_LIC, nombre=f"Compra de {_KW}", estado="publicada", fecha_cierre=cierre))
        s.add(CompraAgil(codigo=_CA, nombre=f"Compra de {_KW}", estado="publicada", fecha_cierre=cierre))
        s.commit()
        perfil_id = perfil.id
    try:
        yield {"perfil_id": perfil_id, "cierre": cierre}
    finally:
        _limpiar()


def _match_solo_perfil(pg_engine: Any, perfil_id: int):
    """match_todos real, pero solo sobre el perfil del test (no toca los de dev)."""
    from app.matching.engine import match_perfil
    from app.models.tables import PerfilBusqueda

    def _fake(session: Session, ahora: datetime | None = None) -> dict[str, Any]:
        perfil = session.get(PerfilBusqueda, perfil_id)
        assert perfil is not None
        r = match_perfil(perfil, session, ahora)
        return {"perfiles_procesados": 1, **r}

    return _fake


class TestRawJsonPostgres:
    def test_detalle_se_guarda_con_fechas_iso_y_la_segunda_corrida_no_lo_pide(
        self, pg_engine, pg_settings, escenario
    ) -> None:
        from app.ingest.orchestrator import run_match

        cierre: datetime = escenario["cierre"]
        v1, v2 = MagicMock(), MagicMock()
        v1.licitacion_detalle.side_effect = lambda c: _lic_detalle(c, cierre, f"Compra de {_KW}")
        v2.detalle_compra_agil.side_effect = lambda c: _ca_detalle(c, cierre, f"Compra de {_KW}")

        with (
            patch("app.matching.engine.match_todos", side_effect=_match_solo_perfil(pg_engine, escenario["perfil_id"])),
            patch("app.ingest.orchestrator._make_clients", return_value=(v1, v2)),
        ):
            r1 = run_match(pg_settings, pg_engine)
            r2 = run_match(pg_settings, pg_engine)

        # Primera corrida: los dos detalles se bajan y se guardan.
        assert r1["sin_detalle_licitaciones"] == [_LIC]
        assert r1["sin_detalle_ca"] == [_CA]
        assert r1["detalles_intentados"] == 2
        assert r1["detalles_guardados"] == 2
        assert r1["detalles_fallidos"] == 0
        assert r1["detalles_pendientes"] == 0

        # Segunda corrida: idempotente, ya no hay nada sin detalle.
        assert r2["sin_detalle_licitaciones"] == []
        assert r2["sin_detalle_ca"] == []
        assert r2["detalles_intentados"] == 0
        assert v1.licitacion_detalle.call_count == 1
        assert v2.detalle_compra_agil.call_count == 1

        with Session(pg_engine) as s:
            lic = s.get(Licitacion, _LIC)
            assert lic is not None and lic.raw_json is not None
            assert lic.raw_json["fecha_cierre"] == cierre.isoformat()
            assert lic.raw_json["items"][0]["codigo_producto"] == "43211500"
            assert lic.descripcion == f"Descripción del detalle {_LIC}"
            assert lic.detalle_obtenido is True
            items = s.execute(
                select(LicitacionItem).where(LicitacionItem.licitacion_codigo == _LIC)
            ).scalars().all()
            assert [i.codigo_producto for i in items] == ["43211500"]

            ca = s.get(CompraAgil, _CA)
            assert ca is not None and ca.raw_json is not None
            assert ca.raw_json["fecha_cierre"] == cierre.isoformat()
            assert ca.raw_json["fecha_ultimo_cambio"] == (cierre - timedelta(days=1)).isoformat()
            assert ca.descripcion == f"Descripción del detalle {_CA}"
            assert ca.id_orden_compra == "1234-56-SE26"
            prods = s.execute(select(CaProducto).where(CaProducto.ca_codigo == _CA)).scalars().all()
            assert [p.codigo_producto for p in prods] == ["44121600"]
