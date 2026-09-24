"""Tests F-detalles-match: job `detalles-match` y parseo real del detalle de CA.

- Tope por tiempo con reloj inyectable, errores por detalle y cortes del canal:
  lógica pura, con la cola y el guardado parchados (SQLite).
- Sin reintento de 5xx/timeout en el cliente, con enfriamiento: respx y reloj falso.
- Cola, JSON `null`, guardado JSONB, monto preservado e idempotencia: Postgres de
  dev (JSONB y jsonb_typeof reales). Solo con filas propias del test.
- FTS por descripción de producto: Postgres (tsv y unaccent reales).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session

from app.clients.base import (
    BaseClient,
    MPConcurrencyError,
    MPRateLimitError,
    MPServerError,
    QuotaTracker,
    RateLimiter,
)
from app.clients.mp_v1 import MercadoPublicoV1Client
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.clients.types import (
    CompraAgilDetalle,
    CompraAgilItem,
    ItemLicitacion,
    LicitacionDetalle,
)
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.models.tables import (
    CaProducto,
    CompraAgil,
    Licitacion,
    LicitacionItem,
    OportunidadMatch,
    Usuario,
)

_VALID_ENV = {
    "MP_TICKET": "ticket-test-detalles-match",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-detalles-32bytesxxxxx",
    "JOBS_TOKEN": "token-test-detalles-jobs-xxxxxxx",
}

# 13:00 en Chile (día) y 23:30 en Chile (noche), para en_ventana_nocturna.
_DIA = lambda tz: datetime(2026, 9, 24, 13, 0, tzinfo=tz)  # noqa: E731
_NOCHE = lambda tz: datetime(2026, 9, 24, 23, 30, tzinfo=tz)  # noqa: E731


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture()
def sqlite_engine() -> Iterator[Any]:
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


class _Reloj:
    """Reloj monotónico falso: avanza solo cuando alguien lo mueve."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def sleep(self, segundos: float) -> None:
        self.t += max(0.0, segundos)


def _cola(n: int) -> list[tuple[str, str]]:
    return [("compras_agiles", f"CA-{i:03d}") for i in range(n)]


# ---------------------------------------------------------------------------
# 1. Presupuesto de tiempo y errores (lógica pura)
# ---------------------------------------------------------------------------


class TestPresupuestoDeTiempo:
    def _correr(
        self,
        settings: Settings,
        engine: Any,
        cola: list[tuple[str, str]],
        bajar: Any,
        reloj: _Reloj,
        now_fn: Any = _DIA,
    ) -> dict[str, Any]:
        from app.ingest.orchestrator import run_detalles_match

        with (
            patch("app.ingest.orchestrator._cola_detalles_match", return_value=cola),
            patch("app.ingest.orchestrator._make_clients", return_value=(MagicMock(), MagicMock())),
            patch("app.ingest.orchestrator._bajar_detalle", side_effect=bajar),
        ):
            return run_detalles_match(settings, engine, reloj=reloj, now_fn=now_fn)

    def test_corta_al_pasarse_del_presupuesto_y_deja_pendientes(self, settings, sqlite_engine):
        reloj = _Reloj()
        llamadas: list[str] = []

        def _bajar(_s, _e, _v1, _v2, _f, codigo):
            llamadas.append(codigo)
            reloj.t += 6 * 60  # cada detalle "tarda" 6 min

        r = self._correr(settings, sqlite_engine, _cola(10), _bajar, reloj)

        # Revisa ANTES de cada detalle: 0, 6, 12, 18 min pasan; a los 24 corta.
        assert llamadas == ["CA-000", "CA-001", "CA-002", "CA-003"]
        assert r["cortado_por_tiempo"] is True
        assert r["detalles_intentados"] == 4
        assert r["detalles_guardados"] == 4
        assert r["detalles_fallidos"] == 0
        assert r["detalles_pendientes"] == 6
        assert r["presupuesto_minutos"] == 20
        assert r["minutos_usados"] == 24.0

    def test_de_noche_usa_el_presupuesto_nocturno(self, settings, sqlite_engine):
        reloj = _Reloj()

        def _bajar(*_a):
            reloj.t += 6 * 60

        r = self._correr(settings, sqlite_engine, _cola(30), _bajar, reloj, now_fn=_NOCHE)

        assert r["presupuesto_minutos"] == 120
        assert r["detalles_intentados"] == 20  # 0, 6, …, 114 min
        assert r["detalles_pendientes"] == 10

    def test_sin_cortar_si_alcanza_el_tiempo(self, settings, sqlite_engine):
        r = self._correr(settings, sqlite_engine, _cola(3), lambda *_a: None, _Reloj())

        assert r["detalles_intentados"] == 3
        assert r["detalles_pendientes"] == 0
        assert "cortado_por_tiempo" not in r
        assert r["minutos_usados"] == 0.0

    def test_un_detalle_que_falla_no_corta_la_cola(self, settings, sqlite_engine):
        def _bajar(_s, _e, _v1, _v2, _f, codigo):
            if codigo == "CA-001":
                raise MPServerError("504", status_code=504)

        r = self._correr(settings, sqlite_engine, _cola(4), _bajar, _Reloj())

        assert r["detalles_intentados"] == 4
        assert r["detalles_guardados"] == 3
        assert r["detalles_fallidos"] == 1
        assert "detalles_interrumpidos" not in r

    @pytest.mark.parametrize(
        "exc",
        [
            MPRateLimitError("tope diario", retry_after_seconds=3600),
            MPConcurrencyError("10500 agotado", retry_after_seconds=900),
        ],
        ids=["429-no-10500", "10500-agotado"],
    )
    def test_error_del_canal_corta_el_job(self, settings, sqlite_engine, exc):
        n = 0

        def _bajar(*_a):
            nonlocal n
            n += 1
            if n == 2:
                raise exc

        r = self._correr(settings, sqlite_engine, _cola(10), _bajar, _Reloj())

        assert r["detalles_interrumpidos"] is True
        assert r["detalles_intentados"] == 2
        assert r["detalles_guardados"] == 1
        assert r["detalles_fallidos"] == 1
        assert r["detalles_pendientes"] == 8

    def test_cola_vacia_no_crea_clientes(self, settings, sqlite_engine):
        from app.ingest.orchestrator import run_detalles_match

        with (
            patch("app.ingest.orchestrator._cola_detalles_match", return_value=[]),
            patch("app.ingest.orchestrator._make_clients") as mk,
        ):
            r = run_detalles_match(settings, sqlite_engine, reloj=_Reloj(), now_fn=_DIA)

        mk.assert_not_called()
        assert r["detalles_intentados"] == 0
        assert r["detalles_pendientes"] == 0

    def test_bajar_detalle_pide_sin_reintento(self, settings, sqlite_engine):
        """El orchestrator activa la opción explícita del cliente en los dos detalles."""
        from app.ingest.orchestrator import _bajar_detalle

        v1, v2 = MagicMock(), MagicMock()
        v1.licitacion_detalle.side_effect = MPServerError("504", status_code=504)
        v2.detalle_compra_agil.side_effect = MPServerError("504", status_code=504)

        with pytest.raises(MPServerError):
            _bajar_detalle(settings, sqlite_engine, v1, v2, "licitaciones", "LIC-1")
        with pytest.raises(MPServerError):
            _bajar_detalle(settings, sqlite_engine, v1, v2, "compras_agiles", "CA-1")

        v1.licitacion_detalle.assert_called_once_with("LIC-1", reintentar_transitorios=False)
        v2.detalle_compra_agil.assert_called_once_with("CA-1", reintentar_transitorios=False)


# ---------------------------------------------------------------------------
# 2. Cliente: sin reintento de 504/timeout, con enfriamiento (respx, reloj falso)
# ---------------------------------------------------------------------------

_V2_DETALLE = "https://api2.mercadopublico.cl/v2/compra-agil/CA-1"
_V1_LIC = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"


@pytest.fixture()
def reloj_cliente(monkeypatch: pytest.MonkeyPatch) -> _Reloj:
    r = _Reloj()
    monkeypatch.setattr("app.clients.base.time.monotonic", r.monotonic)
    monkeypatch.setattr("app.clients.base.time.sleep", r.sleep)
    return r


def _clientes(settings: Settings, reloj: _Reloj) -> tuple[MercadoPublicoV1Client, MercadoPublicoV2Client, RateLimiter]:
    engine = create_engine("sqlite:///:memory:")
    limiter = RateLimiter(rps=1000.0)
    quota = QuotaTracker(engine, budget=100)
    v1 = MercadoPublicoV1Client.__new__(MercadoPublicoV1Client)
    v1._ticket = settings.mp_ticket
    v1._client = BaseClient(ticket=settings.mp_ticket, rate_limiter=limiter, quota=quota)
    v2 = MercadoPublicoV2Client.__new__(MercadoPublicoV2Client)
    v2._ticket = settings.mp_ticket
    v2._client = BaseClient(
        ticket=settings.mp_ticket,
        rate_limiter=limiter,
        quota=quota,
        default_headers={"ticket": settings.mp_ticket},
    )
    return v1, v2, limiter


class TestClienteSinReintento:
    @respx.mock
    def test_504_no_se_reintenta_pero_enfria_60_s(self, settings, reloj_cliente):
        v1, v2, limiter = _clientes(settings, reloj_cliente)
        ruta = respx.get(_V2_DETALLE).mock(return_value=httpx.Response(504, text="gateway"))

        with pytest.raises(MPServerError) as exc:
            v2.detalle_compra_agil("CA-1", reintentar_transitorios=False)

        assert exc.value.status_code == 504
        assert ruta.call_count == 1
        # El enfriamiento quedó puesto: la PRÓXIMA request espera 60 s (regla 3).
        antes = reloj_cliente.t
        limiter.acquire()
        assert reloj_cliente.t - antes >= 60.0

    @respx.mock
    def test_timeout_no_se_reintenta_pero_enfria(self, settings, reloj_cliente):
        v1, _v2, limiter = _clientes(settings, reloj_cliente)
        ruta = respx.get(_V1_LIC).mock(side_effect=httpx.ReadTimeout("lento"))

        with pytest.raises(MPServerError) as exc:
            v1.licitacion_detalle("LIC-1", reintentar_transitorios=False)

        assert exc.value.status_code == 0
        assert ruta.call_count == 1
        antes = reloj_cliente.t
        limiter.acquire()
        assert reloj_cliente.t - antes >= 60.0

    @respx.mock
    def test_por_defecto_el_504_se_sigue_reintentando(self, settings, reloj_cliente):
        """Los demás llamadores (lifecycle, detalles de activas) no cambian."""
        _v1, v2, _l = _clientes(settings, reloj_cliente)
        ruta = respx.get(_V2_DETALLE).mock(return_value=httpx.Response(504, text="gateway"))

        with pytest.raises(MPServerError):
            v2.detalle_compra_agil("CA-1")

        assert ruta.call_count == 2

    @respx.mock
    def test_10500_conserva_su_backoff_sin_reintento_de_transitorios(self, settings, reloj_cliente):
        _v1, v2, _l = _clientes(settings, reloj_cliente)
        ruta = respx.get(_V2_DETALLE).mock(
            return_value=httpx.Response(429, json={"Codigo": 10500, "Mensaje": "simultáneas"})
        )

        with pytest.raises(MPConcurrencyError):
            v2.detalle_compra_agil("CA-1", reintentar_transitorios=False)

        assert ruta.call_count == 4  # 1 + 3 reintentos 30/60/120 s

    @respx.mock
    def test_429_no_10500_corta_sin_reintento(self, settings, reloj_cliente):
        _v1, v2, _l = _clientes(settings, reloj_cliente)
        ruta = respx.get(_V2_DETALLE).mock(return_value=httpx.Response(429, json={"Codigo": 999}))

        with pytest.raises(MPRateLimitError) as exc:
            v2.detalle_compra_agil("CA-1", reintentar_transitorios=False)

        assert not isinstance(exc.value, MPConcurrencyError)
        assert ruta.call_count == 1


# ---------------------------------------------------------------------------
# 3. upsert_ca_detalle: el detalle no pisa con None (SQLite)
# ---------------------------------------------------------------------------


def _ca_det(codigo: str, cierre: datetime | None, **kw: Any) -> CompraAgilDetalle:
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": "Compra ágil de prueba",
        "estado": "publicada",
        "fecha_publicacion": None,
        "fecha_cierre": cierre,
        "fecha_ultimo_cambio": None,
        "monto_clp": None,
        "region": None,
        "organismo_nombre": None,
        "organismo_rut": None,
        "total_ofertas": 0,
        "descripcion": f"Descripción del detalle {codigo}",
        "productos": [CompraAgilItem("44121600", "Resmas", 10.0, "Caja", "Papel carta 75 g")],
        "id_orden_compra": "1234-56-SE26",
        "estado_convocatoria": 1,
    }
    base.update(kw)
    return CompraAgilDetalle(**base)


class TestUpsertCaDetalle:
    def test_monto_y_otros_campos_existentes_no_se_pisan_con_none(self, sqlite_engine):
        from app.ingest.compra_agil import upsert_ca_detalle

        cierre = datetime(2026, 10, 1, 16, 0)
        with Session(sqlite_engine) as s:
            s.add(
                CompraAgil(
                    codigo="CA-P",
                    nombre="Del listado",
                    estado="publicada",
                    fecha_publicacion=datetime(2026, 9, 20, 12, 0),
                    fecha_cierre=cierre,
                    fecha_ultimo_cambio=datetime(2026, 9, 21, 12, 0),
                    monto_disponible_clp=500_000.0,
                    region=13,
                    organismo_nombre="MINSAL",
                    organismo_rut="61.001.000-0",
                    total_ofertas=3,
                    id_orden_compra="OC-EXISTENTE",
                )
            )
            s.commit()

            upsert_ca_detalle(
                s, _ca_det("CA-P", None, nombre="", estado="", id_orden_compra=None, estado_convocatoria=None)
            )
            s.commit()
            ca = s.get(CompraAgil, "CA-P")
            assert ca is not None
            assert ca.monto_disponible_clp == 500_000.0
            assert ca.nombre == "Del listado"
            assert ca.estado == "publicada"
            assert ca.fecha_publicacion == datetime(2026, 9, 20, 12, 0)
            assert ca.fecha_cierre == cierre
            assert ca.fecha_ultimo_cambio == datetime(2026, 9, 21, 12, 0)
            assert ca.region == 13
            assert ca.organismo_nombre == "MINSAL"
            assert ca.organismo_rut == "61.001.000-0"
            assert ca.total_ofertas == 3
            assert ca.id_orden_compra == "OC-EXISTENTE"
            # Lo que sí trae el detalle se guarda.
            assert ca.descripcion == "Descripción del detalle CA-P"

    def test_lo_que_trae_el_detalle_actualiza(self, sqlite_engine):
        from app.ingest.compra_agil import upsert_ca_detalle

        with Session(sqlite_engine) as s:
            s.add(CompraAgil(codigo="CA-U", nombre="x", estado="publicada", monto_disponible_clp=1.0))
            s.commit()
            upsert_ca_detalle(s, _ca_det("CA-U", None, monto_clp=900_000.0, estado="cerrada", total_ofertas=5))
            s.commit()
            ca = s.get(CompraAgil, "CA-U")
            assert ca is not None
            assert ca.monto_disponible_clp == 900_000.0
            assert ca.estado == "cerrada"
            assert ca.total_ofertas == 5
            assert ca.id_orden_compra == "1234-56-SE26"
            assert ca.estado_convocatoria == 1

    def test_descripcion_de_producto_se_guarda_truncada_a_1000(self, sqlite_engine):
        from app.ingest.compra_agil import upsert_ca_detalle

        largo = "x" * 1500
        with Session(sqlite_engine) as s:
            upsert_ca_detalle(
                s, _ca_det("CA-T", None, productos=[CompraAgilItem("1", "Resmas", 1.0, "Caja", largo)])
            )
            s.commit()
            prods = s.execute(select(CaProducto).where(CaProducto.ca_codigo == "CA-T")).scalars().all()
            assert len(prods) == 1
            assert prods[0].descripcion == "x" * 1000


# ---------------------------------------------------------------------------
# 4. Postgres: cola, JSON null, guardado JSONB e idempotencia
# ---------------------------------------------------------------------------

_EMAIL = "detalles_match_test@test.com"
_PREFIJO = "DM-TEST-"
_CODIGOS = {
    "lic_cola": f"{_PREFIJO}LIC-COLA",
    "ca_json_null": f"{_PREFIJO}CA-JSONNULL",
    "ca_sin_cierre": f"{_PREFIJO}CA-SINCIERRE",
    "lic_con_detalle": f"{_PREFIJO}LIC-CONDET",
    "ca_sin_match": f"{_PREFIJO}CA-SINMATCH",
    "lic_cerrada": f"{_PREFIJO}LIC-CERRADA",
    "lic_vencida": f"{_PREFIJO}LIC-VENCIDA",
}


@pytest.fixture()
def pg_engine(db_url: str) -> Iterator[Any]:
    if not db_url.startswith("postgres"):
        pytest.skip("Requiere DATABASE_URL de Postgres (JSONB real)")
    from app.core.db import normalizar_url_driver

    e = create_engine(normalizar_url_driver(db_url), pool_pre_ping=True)
    yield e
    e.dispose()


@pytest.fixture()
def pg_settings(db_url: str) -> Settings:
    return Settings()  # type: ignore[call-arg]


def _limpiar_pg(engine: Any) -> None:
    with Session(engine) as s:
        s.execute(delete(OportunidadMatch).where(OportunidadMatch.codigo_oportunidad.like(f"{_PREFIJO}%")))
        s.execute(delete(Licitacion).where(Licitacion.codigo.like(f"{_PREFIJO}%")))
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like(f"{_PREFIJO}%")))
        u = s.execute(select(Usuario).where(Usuario.email == _EMAIL)).scalar_one_or_none()
        if u:
            s.delete(u)
        s.commit()


@pytest.fixture()
def escenario_pg(pg_engine) -> Iterator[dict[str, Any]]:
    """Filas propias (prefijo DM-TEST-) que cubren cada condición de la cola."""
    from app.matching.perfiles import crear_perfil

    _limpiar_pg(pg_engine)
    ahora = ahora_utc().replace(microsecond=0)
    c = _CODIGOS
    with Session(pg_engine) as s:
        u = Usuario(email=_EMAIL, password_hash="$2b$12$fakehashfordetallesmatch.xyz", activo=True)
        s.add(u)
        s.flush()
        perfil = crear_perfil(s, u.id, "Detalles match (test)", keywords=["zorglubdetalles"])
        s.flush()

        def lic(codigo: str, cierre: datetime | None, estado: str = "publicada") -> None:
            s.add(Licitacion(codigo=codigo, nombre=codigo, estado=estado, fecha_cierre=cierre))

        lic(c["lic_cola"], ahora + timedelta(days=2))
        lic(c["lic_con_detalle"], ahora + timedelta(days=1))
        lic(c["lic_cerrada"], ahora + timedelta(days=1), estado="cerrada")
        lic(c["lic_vencida"], ahora - timedelta(days=1))
        s.add(
            CompraAgil(
                codigo=c["ca_json_null"],
                nombre="ca",
                estado="publicada",
                fecha_cierre=ahora + timedelta(days=1),
                monto_disponible_clp=500_000.0,
            )
        )
        s.add(CompraAgil(codigo=c["ca_sin_cierre"], nombre="ca", estado="publicada", fecha_cierre=None))
        s.add(CompraAgil(codigo=c["ca_sin_match"], nombre="ca", estado="publicada", fecha_cierre=ahora + timedelta(hours=1)))
        s.flush()
        for clave, fuente in (
            ("lic_cola", "licitaciones"),
            ("ca_json_null", "compras_agiles"),
            ("ca_sin_cierre", "compras_agiles"),
            ("lic_con_detalle", "licitaciones"),
            ("lic_cerrada", "licitaciones"),
            ("lic_vencida", "licitaciones"),
        ):
            s.add(OportunidadMatch(perfil_id=perfil.id, fuente=fuente, codigo_oportunidad=c[clave], score=50.0, razones={}))
        s.commit()
        # JSON `null` guardado (no NULL de SQL) y un raw_json real.
        s.execute(
            text("UPDATE compras_agiles SET raw_json = 'null'::jsonb WHERE codigo = :c"),
            {"c": c["ca_json_null"]},
        )
        s.execute(
            text("UPDATE licitaciones SET raw_json = '{\"ya\": true}'::jsonb WHERE codigo = :c"),
            {"c": c["lic_con_detalle"]},
        )
        s.commit()
    try:
        yield {"ahora": ahora}
    finally:
        _limpiar_pg(pg_engine)


def _solo_propios(cola: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [x for x in cola if x[1].startswith(_PREFIJO)]


class TestDetallesMatchPostgres:
    def test_cola_filtra_y_ordena(self, pg_engine, escenario_pg):
        from app.ingest.orchestrator import _cola_detalles_match

        with Session(pg_engine) as s:
            cola = _solo_propios(_cola_detalles_match(s, escenario_pg["ahora"]))

        c = _CODIGOS
        assert cola == [
            ("compras_agiles", c["ca_json_null"]),  # JSON null = sin detalle; cierra en 1 día
            ("licitaciones", c["lic_cola"]),  # cierra en 2 días
            ("compras_agiles", c["ca_sin_cierre"]),  # sin fecha, al final
        ]

    def test_guarda_sin_pisar_el_monto_y_la_segunda_corrida_no_repide(
        self, pg_engine, pg_settings, escenario_pg
    ) -> None:
        import app.ingest.orchestrator as orq

        ahora: datetime = escenario_pg["ahora"]
        c = _CODIGOS
        cola_real = orq._cola_detalles_match
        v1, v2 = MagicMock(), MagicMock()
        v1.licitacion_detalle.side_effect = lambda cod, **kw: LicitacionDetalle(
            codigo=cod,
            nombre=cod,
            estado=5,
            fecha_publicacion=ahora - timedelta(days=5),
            fecha_cierre=ahora + timedelta(days=2),
            tipo="L1",
            codigo_organismo="ORG-DM",
            descripcion=f"Descripción {cod}",
            moneda="CLP",
            monto_estimado=1_000_000.0,
            items=[ItemLicitacion("43211500", "Notebook", 3.0, "Unidad")],
        )
        v2.detalle_compra_agil.side_effect = lambda cod, **kw: _ca_det(
            cod, ahora + timedelta(days=1) if cod == c["ca_json_null"] else None
        )

        with (
            patch.object(orq, "_cola_detalles_match", side_effect=lambda s, a: _solo_propios(cola_real(s, a))),
            patch.object(orq, "_make_clients", return_value=(v1, v2)),
        ):
            r1 = orq.run_detalles_match(pg_settings, pg_engine, now_fn=_DIA)
            r2 = orq.run_detalles_match(pg_settings, pg_engine, now_fn=_DIA)

        assert r1["detalles_intentados"] == 3
        assert r1["detalles_guardados"] == 3
        assert r1["detalles_pendientes"] == 0
        assert r2["detalles_intentados"] == 0
        assert v1.licitacion_detalle.call_count == 1
        assert v2.detalle_compra_agil.call_count == 2

        with Session(pg_engine) as s:
            ca = s.get(CompraAgil, c["ca_json_null"])
            assert ca is not None and ca.raw_json is not None
            assert ca.raw_json["fecha_cierre"] == (ahora + timedelta(days=1)).isoformat()
            assert ca.raw_json["productos"][0]["descripcion"] == "Papel carta 75 g"
            assert ca.monto_disponible_clp == 500_000.0  # el detalle trajo None
            assert ca.id_orden_compra == "1234-56-SE26"
            assert ca.estado_convocatoria == 1
            prods = s.execute(select(CaProducto).where(CaProducto.ca_codigo == ca.codigo)).scalars().all()
            assert [p.descripcion for p in prods] == ["Papel carta 75 g"]

            lic = s.get(Licitacion, c["lic_cola"])
            assert lic is not None and lic.raw_json is not None
            assert lic.descripcion == f"Descripción {c['lic_cola']}"
            items = s.execute(
                select(LicitacionItem).where(LicitacionItem.licitacion_codigo == lic.codigo)
            ).scalars().all()
            assert [i.codigo_producto for i in items] == ["43211500"]


# ---------------------------------------------------------------------------
# 5. Postgres: FTS por descripción de producto de CA (3.d)
# ---------------------------------------------------------------------------

_CA_FTS = f"{_PREFIJO}CA-FTS"
_CA_FTS_EXCL = f"{_PREFIJO}CA-FTS-EXCL"


@pytest.fixture()
def ca_fts(pg_engine) -> Iterator[None]:
    _limpiar_pg(pg_engine)
    cierre = ahora_utc() + timedelta(days=3)
    with Session(pg_engine) as s:
        for codigo, desc in (
            (_CA_FTS, "Toner original zorglubtoner para impresora láser"),
            (_CA_FTS_EXCL, "Toner zorglubtoner, marca zorglubexcluir"),
        ):
            s.add(CompraAgil(codigo=codigo, nombre="Compra de insumos", estado="publicada", fecha_cierre=cierre))
            s.flush()
            s.add(CaProducto(ca_codigo=codigo, codigo_producto="44103103", nombre="Insumo", descripcion=desc, unidad="UN"))
        s.commit()
    try:
        yield
    finally:
        _limpiar_pg(pg_engine)


class TestFtsDescripcionProducto:
    def test_ca_que_calza_solo_por_descripcion_de_producto_es_candidata(self, pg_engine, ca_fts):
        from app.matching.engine import _candidatos_ca, _hits_ca
        from app.matching.text import build_tsquery

        q = build_tsquery(["zorglubtoner"])
        with Session(pg_engine) as s:
            cands = [c.codigo for c in _candidatos_ca(s, ahora_utc(), q, None)]
            hits = _hits_ca(s, [_CA_FTS], ["zorglubtoner"])

        assert _CA_FTS in cands
        # Mismo criterio en el score (invariante F9c): el hit se ve como producto.
        assert hits[_CA_FTS] == (["zorglubtoner"], "producto")

    def test_la_exclusion_tambien_mira_la_descripcion_de_producto(self, pg_engine, ca_fts):
        from app.matching.engine import _candidatos_ca
        from app.matching.text import build_exclude_tsquery, build_tsquery

        q = build_tsquery(["zorglubtoner"])
        qx = build_exclude_tsquery(["zorglubexcluir"])
        with Session(pg_engine) as s:
            cands = {c.codigo for c in _candidatos_ca(s, ahora_utc(), q, qx)}

        assert _CA_FTS in cands
        assert _CA_FTS_EXCL not in cands
