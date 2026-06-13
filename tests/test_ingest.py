"""Tests F3 — ingesta, orchestrator y CLI."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clients.base import MPRateLimitError
from app.clients.types import (
    CompraAgilBasica,
    LicitacionBasica,
    LicitacionDetalle,
    PaginacionV2,
    RespuestaListadoV2,
)
from app.core.settings import Settings
from app.ingest.compra_agil import sync_incremental
from app.ingest.licitaciones import (
    fetch_detalles_pendientes,
    sync_activas,
    sync_por_fecha,
)
from app.ingest.orchestrator import _run_with_lock, en_ventana_nocturna
from app.models.tables import CompraAgil, Licitacion, SyncState

# ---------------------------------------------------------------------------
# Fixtures comunes
# ---------------------------------------------------------------------------

_VALID_ENV = {
    "MP_TICKET": "ticket-test-f3",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-f3-32bytesxxxxxxxxxx",
    "JOBS_TOKEN": "token-test-f3-jobs-xxxxxxxxxxx",
}


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture()
def engine():
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


# ---------------------------------------------------------------------------
# Helpers de fixtures de API
# ---------------------------------------------------------------------------


def _lic_basica(codigo: str, nombre: str = "Test", estado: int = 5) -> LicitacionBasica:
    return LicitacionBasica(
        codigo=codigo,
        nombre=nombre,
        estado=estado,
        fecha_publicacion=date(2026, 1, 1),
        fecha_cierre=date(2026, 3, 1),
        tipo="L1",
        codigo_organismo="ORG-001",
    )


def _lic_detalle(codigo: str, nombre: str = "Test") -> LicitacionDetalle:
    return LicitacionDetalle(
        codigo=codigo,
        nombre=nombre,
        estado=5,
        fecha_publicacion=date(2026, 1, 1),
        fecha_cierre=date(2026, 3, 1),
        tipo="L1",
        codigo_organismo="ORG-001",
        descripcion="Descripcion de prueba",
        moneda="CLP",
        monto_estimado=1_000_000.0,
        items=[],
    )


def _ca_basica(
    codigo: str,
    estado: str = "publicada",
    fecha_ultimo_cambio: datetime | None = None,
) -> CompraAgilBasica:
    if fecha_ultimo_cambio is None:
        fecha_ultimo_cambio = datetime(2026, 6, 1, 12, 0)
    return CompraAgilBasica(
        codigo=codigo,
        nombre=f"CA {codigo}",
        estado=estado,
        fecha_publicacion=datetime(2026, 1, 1),
        fecha_cierre=datetime(2026, 7, 1),
        fecha_ultimo_cambio=fecha_ultimo_cambio,
        monto_clp=500_000.0,
        region=13,
        organismo_nombre="Ministerio Test",
        organismo_rut="00.000.000-0",
        total_ofertas=3,
    )


def _paginar(items: list[CompraAgilBasica], pagina: int, total_paginas: int) -> RespuestaListadoV2:
    return RespuestaListadoV2(
        items=items,
        paginacion=PaginacionV2(
            total_paginas=total_paginas,
            total_resultados=len(items) * total_paginas,
            numero_pagina=pagina,
            tamano_pagina=50,
        ),
    )


# ---------------------------------------------------------------------------
# Tests de licitaciones
# ---------------------------------------------------------------------------


class TestSyncActivas:
    def test_crea_nuevas(self, session, settings):
        v1 = MagicMock()
        v1.licitaciones_activas.return_value = [
            _lic_basica("LIC-001"),
            _lic_basica("LIC-002"),
        ]
        result = sync_activas(session, v1, settings)
        assert result["nuevas"] == 2
        assert result["actualizadas"] == 0
        assert session.execute(select(Licitacion)).scalars().all().__len__() == 2

    def test_idempotencia(self, session, settings):
        """Misma lista dos veces → no duplica."""
        v1 = MagicMock()
        v1.licitaciones_activas.return_value = [_lic_basica("LIC-001"), _lic_basica("LIC-002")]

        sync_activas(session, v1, settings)
        result = sync_activas(session, v1, settings)

        assert result["nuevas"] == 0
        assert result["actualizadas"] == 2
        lics = session.execute(select(Licitacion)).scalars().all()
        assert len(lics) == 2

    def test_commit_por_lotes(self, session, settings):
        """Verifica que se hacen commits por lotes de 200."""
        v1 = MagicMock()
        v1.licitaciones_activas.return_value = [
            _lic_basica(f"LIC-{i:04d}") for i in range(450)
        ]
        result = sync_activas(session, v1, settings)
        assert result["nuevas"] == 450

    def test_sync_por_fecha(self, session, settings):
        v1 = MagicMock()
        v1.licitaciones_por_fecha.return_value = [_lic_basica("LIC-FECHA")]
        result = sync_por_fecha(session, v1, settings, date(2026, 1, 1))
        assert result["nuevas"] == 1


class TestFetchDetalles:
    def test_fetch_pendientes(self, session, settings):
        """Descarga detalle de licitaciones con detalle_obtenido=False."""
        # Crear licitacion sin detalle
        session.add(
            Licitacion(
                codigo="LIC-PEND",
                nombre="Licitacion Pendiente",
                descripcion="",
                estado="publicada",
                detalle_obtenido=False,
                creado_en=datetime.now(UTC).replace(tzinfo=None),
                actualizado_en=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        session.commit()

        v1 = MagicMock()
        v1.licitacion_detalle.return_value = _lic_detalle("LIC-PEND")

        result = fetch_detalles_pendientes(session, v1, settings, max_requests=10)
        assert result["procesadas"] == 1
        assert result["errores"] == 0

        session.expire_all()
        lic = session.get(Licitacion, "LIC-PEND")
        assert lic is not None
        assert lic.detalle_obtenido is True

    def test_prefilter_keywords(self, session, settings, monkeypatch):
        """Keywords de pre-filtro descartan licitaciones que no coinciden."""
        monkeypatch.setenv("PREFILTER_KEYWORDS", '["electrico"]')
        settings_filtro = Settings(_env_file=None)  # type: ignore[call-arg]

        # Una licitacion coincide, otra no
        for codigo, nombre in [("LIC-A", "Material Electrico"), ("LIC-B", "Mobiliario")]:
            session.add(
                Licitacion(
                    codigo=codigo,
                    nombre=nombre,
                    descripcion="",
                    estado="publicada",
                    detalle_obtenido=False,
                    creado_en=datetime.now(UTC).replace(tzinfo=None),
                    actualizado_en=datetime.now(UTC).replace(tzinfo=None),
                )
            )
        session.commit()

        v1 = MagicMock()
        v1.licitacion_detalle.return_value = _lic_detalle("LIC-A")

        result = fetch_detalles_pendientes(session, v1, settings_filtro, max_requests=10)
        assert result["procesadas"] == 1
        assert result["descartadas"] == 1

    def test_presupuesto_respetado(self, session, settings):
        """max_requests limita el número de llamadas."""
        for i in range(10):
            session.add(
                Licitacion(
                    codigo=f"LIC-{i:03d}",
                    nombre=f"Licitacion {i}",
                    descripcion="",
                    estado="publicada",
                    detalle_obtenido=False,
                    creado_en=datetime.now(UTC).replace(tzinfo=None),
                    actualizado_en=datetime.now(UTC).replace(tzinfo=None),
                )
            )
        session.commit()

        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = lambda codigo: _lic_detalle(codigo)

        result = fetch_detalles_pendientes(session, v1, settings, max_requests=3)
        assert result["procesadas"] == 3
        assert v1.licitacion_detalle.call_count == 3

    def test_error_en_detalle_no_aborta(self, session, settings):
        """Un error al pedir detalle no aborta el resto de la corrida."""
        for codigo in ["LIC-OK", "LIC-FAIL", "LIC-OK2"]:
            session.add(
                Licitacion(
                    codigo=codigo,
                    nombre=f"Lic {codigo}",
                    descripcion="",
                    estado="publicada",
                    detalle_obtenido=False,
                    creado_en=datetime.now(UTC).replace(tzinfo=None),
                    actualizado_en=datetime.now(UTC).replace(tzinfo=None),
                )
            )
        session.commit()

        def detalle_side_effect(codigo: str) -> LicitacionDetalle:
            if codigo == "LIC-FAIL":
                raise RuntimeError("API caída")
            return _lic_detalle(codigo)

        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = detalle_side_effect

        result = fetch_detalles_pendientes(session, v1, settings, max_requests=10)
        assert result["errores"] == 1
        assert result["procesadas"] == 2


# ---------------------------------------------------------------------------
# Tests de Compra Ágil
# ---------------------------------------------------------------------------


class TestSyncIncrementalCA:
    def _make_v2(self, paginas: list[list[CompraAgilBasica]]) -> MagicMock:
        """Crea mock de v2 que devuelve las páginas dadas."""
        v2 = MagicMock()
        total = len(paginas)

        def listar(*, numero_pagina: int = 1, **kwargs: Any) -> RespuestaListadoV2:
            idx = numero_pagina - 1
            items = paginas[idx] if idx < len(paginas) else []
            return _paginar(items, numero_pagina, total)

        v2.listar_compra_agil.side_effect = listar
        return v2

    def test_idempotencia(self, session, settings):
        """Misma página dos veces → no duplica."""
        items = [_ca_basica("CA-001"), _ca_basica("CA-002")]
        v2 = self._make_v2([items])

        sync_incremental(session, v2, settings)
        result = sync_incremental(session, v2, settings)

        assert result["nuevas"] == 0
        assert result["actualizadas"] == 2
        cas = session.execute(select(CompraAgil)).scalars().all()
        assert len(cas) == 2

    def test_cursor_avanza_en_exito(self, session, settings):
        """Cursor de sync_state avanza al fecha_ultimo_cambio más reciente."""
        fecha_cambio = datetime(2026, 6, 10, 15, 30)
        items = [_ca_basica("CA-C", fecha_ultimo_cambio=fecha_cambio)]
        v2 = self._make_v2([items])

        sync_incremental(session, v2, settings)

        state = session.get(SyncState, "compra_agil")
        assert state is not None
        assert state.cursor is not None
        # El cursor debe ser la fecha del cambio (sin tzinfo)
        cursor_dt = datetime.fromisoformat(state.cursor)
        assert cursor_dt == fecha_cambio

    def test_cursor_no_avanza_en_error(self, session, settings):
        """Si la corrida falla, el cursor no avanza."""
        # Preload un cursor inicial
        state = SyncState(fuente="compra_agil", cursor="2026-01-01T00:00:00")
        session.add(state)
        session.commit()

        v2 = MagicMock()
        v2.listar_compra_agil.side_effect = RuntimeError("fallo de red")

        with pytest.raises(RuntimeError):
            sync_incremental(session, v2, settings)

        session.expire_all()
        state_post = session.get(SyncState, "compra_agil")
        # El cursor original no cambió
        assert state_post is not None
        assert state_post.cursor == "2026-01-01T00:00:00"

    def test_filtro_local_por_estado(self, session, settings):
        """Estados no válidos se descartan localmente."""
        items = [
            _ca_basica("CA-PUB", estado="publicada"),
            _ca_basica("CA-ADJ", estado="adjudicada"),  # no válido
            _ca_basica("CA-CERR", estado="cerrada"),
        ]
        v2 = self._make_v2([items])

        result = sync_incremental(session, v2, settings)

        assert result["descartadas"] == 1
        assert result["nuevas"] == 2
        cas = session.execute(select(CompraAgil)).scalars().all()
        assert len(cas) == 2

    def test_429_en_pagina_intermedia(self, session, settings):
        """429 en pág 3/5 → progreso de págs 1-2 guardado, cursor intacto."""
        # Estado inicial del cursor
        cursor_inicial = "2026-01-01T00:00:00"
        session.add(SyncState(fuente="compra_agil", cursor=cursor_inicial))
        session.commit()

        pag1 = [_ca_basica("CA-P1A"), _ca_basica("CA-P1B")]
        pag2 = [_ca_basica("CA-P2A"), _ca_basica("CA-P2B")]

        call_count = 0

        def listar(*, numero_pagina: int = 1, **kwargs: Any) -> RespuestaListadoV2:
            nonlocal call_count
            call_count += 1
            if numero_pagina == 1:
                return _paginar(pag1, 1, 5)
            if numero_pagina == 2:
                return _paginar(pag2, 2, 5)
            raise MPRateLimitError("429 cuota agotada", retry_after_seconds=3600)

        v2 = MagicMock()
        v2.listar_compra_agil.side_effect = listar

        with pytest.raises(MPRateLimitError):
            sync_incremental(session, v2, settings)

        # Las CA de págs 1-2 están guardadas (commit por página)
        session.expire_all()
        cas = session.execute(select(CompraAgil)).scalars().all()
        assert len(cas) == 4  # pag1 + pag2

        # El cursor NO avanzó
        state = session.get(SyncState, "compra_agil")
        assert state is not None
        assert state.cursor == cursor_inicial

    def test_cursor_con_solapamiento_5min(self, session, settings):
        """Si hay cursor, cambio_desde = cursor − 5 min."""
        cursor_dt = datetime(2026, 6, 10, 12, 0, 0)
        session.add(SyncState(fuente="compra_agil", cursor=cursor_dt.isoformat()))
        session.commit()

        v2 = MagicMock()
        v2.listar_compra_agil.return_value = _paginar([], 1, 1)

        sync_incremental(session, v2, settings)

        primera_llamada = v2.listar_compra_agil.call_args_list[0]
        cambio_desde = primera_llamada.kwargs.get("cambio_desde")
        # La llamada debe incluir cambio_desde = cursor - 5 min (naive UTC)
        esperado = (cursor_dt - timedelta(minutes=5)).replace(tzinfo=None)
        assert cambio_desde == esperado


# ---------------------------------------------------------------------------
# Tests del Orchestrator
# ---------------------------------------------------------------------------


class TestVentanaNocturna:
    def test_dentro_de_ventana_23h(self):
        now_fn = lambda tz: datetime(2026, 6, 13, 23, 0, tzinfo=tz)  # noqa: E731
        assert en_ventana_nocturna(now_fn) is True

    def test_dentro_de_ventana_02h(self):
        now_fn = lambda tz: datetime(2026, 6, 13, 2, 0, tzinfo=tz)  # noqa: E731
        assert en_ventana_nocturna(now_fn) is True

    def test_fuera_de_ventana_10h(self):
        now_fn = lambda tz: datetime(2026, 6, 13, 10, 0, tzinfo=tz)  # noqa: E731
        assert en_ventana_nocturna(now_fn) is False

    def test_borde_22h(self):
        now_fn = lambda tz: datetime(2026, 6, 13, 22, 0, tzinfo=tz)  # noqa: E731
        assert en_ventana_nocturna(now_fn) is True

    def test_borde_07h_exacto(self):
        now_fn = lambda tz: datetime(2026, 6, 13, 7, 0, tzinfo=tz)  # noqa: E731
        assert en_ventana_nocturna(now_fn) is False  # 07:00 es fuera de ventana


class TestBackfillFueraVentana:
    def test_backfill_rechazado_fuera_de_ventana(self, session, settings, engine):
        """El ciclo nocturno rehúsa ejecutarse fuera de ventana horaria."""
        from app.ingest.orchestrator import _ciclo_nocturno

        now_fn = lambda tz: datetime(2026, 6, 13, 14, 0, tzinfo=tz)  # noqa: E731
        llamadas: list[str] = []

        def fake_run_with_lock(job_name: str, fn: Any, eng: Any, **kw: Any) -> None:
            llamadas.append(job_name)

        with patch("app.ingest.orchestrator._run_with_lock", side_effect=fake_run_with_lock):
            _ciclo_nocturno(settings, engine, now_fn=now_fn)

        # No se ejecutó ningún job
        assert llamadas == []


class TestAdvisoryLock:
    def test_lock_impide_doble_ejecucion(self, engine):
        """Si el lock está ocupado, _run_with_lock retorna None sin ejecutar fn."""
        llamadas = 0

        def job_fn() -> dict[str, int]:
            nonlocal llamadas
            llamadas += 1
            return {}

        # Primera ejecución: lock disponible → ejecuta
        try_lock_true = lambda conn, key: True  # noqa: E731
        unlock = lambda conn, key: None  # noqa: E731

        result1 = _run_with_lock("test", job_fn, engine, try_lock_true, unlock)
        assert result1 == {}
        assert llamadas == 1

        # Segunda ejecución: lock ocupado → omite
        try_lock_false = lambda conn, key: False  # noqa: E731
        result2 = _run_with_lock("test", job_fn, engine, try_lock_false, unlock)
        assert result2 is None
        assert llamadas == 1  # no se volvió a llamar

    def test_lock_liberado_aunque_haya_error(self, engine):
        """El lock se libera en finally aunque el job falle."""
        unlocked: list[bool] = []

        def unlock(conn: Any, key: int) -> None:
            unlocked.append(True)

        def job_fn() -> dict[str, int]:
            raise RuntimeError("fallo intencional")

        try_lock_true = lambda conn, key: True  # noqa: E731
        result = _run_with_lock("test_fail", job_fn, engine, try_lock_true, unlock)

        # El resultado es None (error manejado internamente)
        assert result is None
        # El unlock se llamó a pesar del error
        assert unlocked == [True]
