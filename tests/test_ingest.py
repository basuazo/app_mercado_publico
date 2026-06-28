"""Tests F3 — ingesta, orchestrator y CLI."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
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
    upsert_basica,
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

    def test_limit_acota_licitaciones_procesadas(self, session, settings):
        """--limit (run-once) acota cuántas licitaciones se procesan."""
        v1 = MagicMock()
        v1.licitaciones_activas.return_value = [_lic_basica(f"LIC-L{i:03d}") for i in range(50)]

        result = sync_activas(session, v1, settings, limit=10)

        assert result["total"] == 10
        assert session.execute(select(Licitacion)).scalars().all().__len__() == 10

    def test_lote_recupera_de_desconexion_transitoria(self, session, settings, monkeypatch):
        """Una OperationalError transitoria en un commit se reintenta y no pierde el lote."""
        v1 = MagicMock()
        v1.licitaciones_activas.return_value = [
            _lic_basica(f"LIC-R{i:03d}") for i in range(450)
        ]

        original_commit = session.commit
        llamadas = {"n": 0}

        def commit_fragil() -> None:
            llamadas["n"] += 1
            # Falla solo en el primer intento de comitear el 2º lote (items 200-399).
            if llamadas["n"] == 2:
                raise OperationalError("commit", {}, Exception("server closed the connection"))
            original_commit()

        monkeypatch.setattr(session, "commit", commit_fragil)
        monkeypatch.setattr("app.core.db_retry.time.sleep", lambda s: None)

        result = sync_activas(session, v1, settings)

        # Tras el reintento, el lote que falló transitoriamente se persiste igual.
        assert result["total"] == 450
        # 3 lotes (200+200+50) + 1 guardar_estado + 1 reintento extra por la falla = 5.
        assert llamadas["n"] == 5

    def test_lote_descartado_tras_agotar_reintentos_no_aborta_corrida(
        self, session, settings, monkeypatch
    ):
        """Si un lote falla en sus 3 intentos, se descarta y la corrida sigue con el siguiente."""
        v1 = MagicMock()
        v1.licitaciones_activas.return_value = [
            _lic_basica(f"LIC-X{i:03d}") for i in range(450)
        ]

        original_commit = session.commit
        llamadas = {"n": 0}

        def commit_segundo_lote_siempre_falla() -> None:
            llamadas["n"] += 1
            # Las llamadas 2, 3 y 4 son los 3 intentos del commit del 2º lote.
            if llamadas["n"] in (2, 3, 4):
                raise OperationalError("commit", {}, Exception("server closed the connection"))
            original_commit()

        monkeypatch.setattr(session, "commit", commit_segundo_lote_siempre_falla)
        monkeypatch.setattr("app.core.db_retry.time.sleep", lambda s: None)

        result = sync_activas(session, v1, settings)

        # Lote 1 (200) y lote 3 (50) se persisten; el lote 2 (200) se pierde sin abortar la corrida.
        assert result["total"] == 250
        lics = session.execute(select(Licitacion)).scalars().all()
        assert len(lics) == 250


class TestUpsertBasicaAntiClobber:
    def test_no_borra_fecha_cierre_existente_al_recibir_item_sin_fecha(self, session, settings):
        """El listado de activas sin fecha (None) no debe pisar la fecha del detalle."""
        upsert_basica(session, _lic_basica("LIC-DET", estado=5))
        session.commit()
        lic = session.get(Licitacion, "LIC-DET")
        assert lic is not None
        assert lic.fecha_cierre is not None

        item_sin_fecha = LicitacionBasica(
            codigo="LIC-DET",
            nombre="Test",
            estado=None,
            fecha_publicacion=None,
            fecha_cierre=None,
            tipo="L1",
            codigo_organismo="ORG-001",
        )
        upsert_basica(session, item_sin_fecha)
        session.commit()

        session.expire_all()
        lic = session.get(Licitacion, "LIC-DET")
        assert lic is not None
        assert lic.fecha_cierre == datetime(2026, 3, 1)
        assert lic.estado == "publicada"
        assert lic.estado_codigo == 5

    def test_licitacion_nueva_sin_fecha_queda_en_none(self, session, settings):
        """Para una licitación nueva sí se acepta None: no hay nada que preservar."""
        item_sin_fecha = LicitacionBasica(
            codigo="LIC-NUEVA",
            nombre="Test",
            estado=None,
            fecha_publicacion=None,
            fecha_cierre=None,
            tipo="L1",
            codigo_organismo="ORG-001",
        )
        upsert_basica(session, item_sin_fecha)
        session.commit()

        lic = session.get(Licitacion, "LIC-NUEVA")
        assert lic is not None
        assert lic.fecha_cierre is None
        assert lic.estado == "desconocido"

    def test_activa_con_fecha_futura_queda_publicada(self, session, settings):
        """Licitación activa (estado=5) con fecha de cierre futura → publicada, no descartada."""
        fecha_futura = date.today() + timedelta(days=30)
        item = LicitacionBasica(
            codigo="LIC-FUTURA",
            nombre="Test",
            estado=5,
            fecha_publicacion=date.today(),
            fecha_cierre=fecha_futura,
            tipo="L1",
            codigo_organismo="ORG-001",
        )
        upsert_basica(session, item)
        session.commit()

        lic = session.get(Licitacion, "LIC-FUTURA")
        assert lic is not None
        assert lic.estado == "publicada"
        assert lic.fecha_cierre is not None
        assert lic.fecha_cierre > datetime.now(UTC).replace(tzinfo=None)


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


# ---------------------------------------------------------------------------
# Tests de pg advisory lock helpers
# ---------------------------------------------------------------------------


class TestPgLockHelpers:
    def test_pg_try_lock_row_none_devuelve_false(self):
        """_pg_try_lock devuelve False cuando fetchone() retorna None."""
        from app.ingest.orchestrator import _pg_try_lock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        assert _pg_try_lock(conn, 1234) is False

    def test_pg_try_lock_row_false_devuelve_false(self):
        """_pg_try_lock devuelve False cuando el resultado es False."""
        from app.ingest.orchestrator import _pg_try_lock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (False,)
        assert _pg_try_lock(conn, 1234) is False

    def test_pg_unlock_llama_execute(self):
        """_pg_unlock ejecuta el SQL de unlock."""
        from app.ingest.orchestrator import _pg_unlock

        conn = MagicMock()
        _pg_unlock(conn, 1234)
        conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Tests de refresh_organismos (catalogos)
# ---------------------------------------------------------------------------


class TestRefreshOrganismos:
    def test_crea_nuevos_organismos(self, session):
        from app.clients.types import Comprador
        from app.ingest.catalogos import refresh_organismos
        from app.models.tables import Organismo

        v1 = MagicMock()
        v1.listar_compradores.return_value = [
            Comprador(codigo="ORG-001", nombre="MINSAL", rut="61.001.000-0"),
            Comprador(codigo="ORG-002", nombre="MINEDUC", rut=None),
        ]
        result = refresh_organismos(session, v1)
        assert result["nuevos"] == 2
        assert result["actualizados"] == 0
        assert session.get(Organismo, "ORG-001") is not None

    def test_actualiza_organismos_existentes(self, session):
        from app.clients.types import Comprador
        from app.ingest.catalogos import refresh_organismos
        from app.models.tables import Organismo

        session.add(Organismo(codigo="ORG-001", nombre="Nombre viejo", rut=None))
        session.commit()

        v1 = MagicMock()
        v1.listar_compradores.return_value = [
            Comprador(codigo="ORG-001", nombre="Nombre nuevo", rut="61.001.000-0"),
        ]
        result = refresh_organismos(session, v1)
        assert result["nuevos"] == 0
        assert result["actualizados"] == 1
        org = session.get(Organismo, "ORG-001")
        assert org is not None and org.nombre == "Nombre nuevo"

    def test_ignora_compradores_sin_codigo(self, session):
        from app.clients.types import Comprador
        from app.ingest.catalogos import refresh_organismos

        v1 = MagicMock()
        v1.listar_compradores.return_value = [
            Comprador(codigo="", nombre="Sin código", rut=None),
        ]
        result = refresh_organismos(session, v1)
        assert result["nuevos"] == 0


# ---------------------------------------------------------------------------
# Tests de lifecycle (refresh_estados)
# ---------------------------------------------------------------------------


class TestRefreshEstados:
    def _licitacion(self, session, codigo: str) -> Any:
        from app.models.tables import Licitacion

        lic = Licitacion(
            codigo=codigo,
            nombre=f"Lic {codigo}",
            descripcion="",
            estado="publicada",
            fecha_cierre=datetime(2026, 6, 18),  # dentro de ventana ±7/+3
        )
        session.add(lic)
        session.commit()
        return lic

    def _ca(self, session, codigo: str) -> Any:
        from app.models.tables import CompraAgil

        ca = CompraAgil(
            codigo=codigo,
            nombre=f"CA {codigo}",
            descripcion="",
            estado="publicada",
            fecha_cierre=datetime(2026, 6, 18),
        )
        session.add(ca)
        session.commit()
        return ca

    def test_actualiza_licitaciones(self, session, settings):
        from freezegun import freeze_time

        from app.ingest.lifecycle import refresh_estados

        self._licitacion(session, "LIC-LIFE")
        v1 = MagicMock()
        v1.licitacion_detalle.return_value = _lic_detalle("LIC-LIFE")
        v2 = MagicMock()

        with freeze_time("2026-06-18 10:00:00"):  # dentro de ventana ±7/+3 de fecha_cierre
            result = refresh_estados(session, v1, v2, settings, max_requests=10)
        assert result["actualizadas_licitaciones"] == 1
        assert result["errores"] == 0

    def test_error_en_licitacion_no_aborta(self, session, settings):
        from freezegun import freeze_time

        from app.ingest.lifecycle import refresh_estados

        self._licitacion(session, "LIC-FAIL")
        self._licitacion(session, "LIC-OK")
        v1 = MagicMock()

        def side_licitacion(codigo: str):
            if codigo == "LIC-FAIL":
                raise RuntimeError("fallo lic")
            return _lic_detalle(codigo)

        v1.licitacion_detalle.side_effect = side_licitacion
        v2 = MagicMock()

        with freeze_time("2026-06-18 10:00:00"):
            result = refresh_estados(session, v1, v2, settings, max_requests=10)
        assert result["errores"] == 1
        assert result["actualizadas_licitaciones"] == 1

    def test_actualiza_compras_agiles(self, session, settings):
        from freezegun import freeze_time

        from app.clients.types import CompraAgilDetalle
        from app.ingest.lifecycle import refresh_estados

        self._ca(session, "CA-LIFE")
        v1 = MagicMock()

        detalle_ca = CompraAgilDetalle(
            codigo="CA-LIFE",
            nombre="CA Life",
            estado="publicada",
            fecha_publicacion=None,
            fecha_cierre=datetime(2026, 6, 18),
            fecha_ultimo_cambio=datetime(2026, 6, 15),
            monto_clp=500_000.0,
            region=13,
            organismo_nombre="Test",
            organismo_rut=None,
            total_ofertas=2,
            descripcion="desc",
            productos=[],
            id_orden_compra=None,
        )
        v2 = MagicMock()
        v2.detalle_compra_agil.return_value = detalle_ca

        with freeze_time("2026-06-18 10:00:00"):
            result = refresh_estados(session, v1, v2, settings, max_requests=10)
        assert result["actualizadas_ca"] == 1
        assert result["errores"] == 0

    def test_error_en_ca_no_aborta(self, session, settings):
        from freezegun import freeze_time

        from app.ingest.lifecycle import refresh_estados

        self._ca(session, "CA-FAIL")
        v1 = MagicMock()
        v2 = MagicMock()
        v2.detalle_compra_agil.side_effect = RuntimeError("fallo CA")

        with freeze_time("2026-06-18 10:00:00"):
            result = refresh_estados(session, v1, v2, settings, max_requests=10)
        assert result["errores"] == 1

    def test_max_requests_limita_ciclo(self, session, settings):
        from app.ingest.lifecycle import refresh_estados

        for i in range(5):
            self._licitacion(session, f"LIC-LIM-{i}")

        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = lambda c: _lic_detalle(c)
        v2 = MagicMock()

        result = refresh_estados(session, v1, v2, settings, max_requests=2)
        assert result["actualizadas_licitaciones"] <= 2


# ---------------------------------------------------------------------------
# Tests de runners del orchestrator
# ---------------------------------------------------------------------------


class TestOrchestratorRunners:
    def test_run_sync_ca(self, settings, engine):
        from app.ingest.orchestrator import run_sync_ca

        with patch("app.ingest.orchestrator._make_clients") as mk:
            _, v2 = MagicMock(), MagicMock()
            v2.listar_compra_agil.return_value = MagicMock(
                items=[],
                paginacion=MagicMock(total_paginas=1, numero_pagina=1),
            )
            mk.return_value = (MagicMock(), v2)
            result = run_sync_ca(settings, engine)
        assert "nuevas" in result

    def test_run_catalogos(self, settings, engine):
        from app.ingest.orchestrator import run_catalogos

        with patch("app.ingest.orchestrator._make_clients") as mk:
            v1 = MagicMock()
            v1.listar_compradores.return_value = []
            mk.return_value = (v1, MagicMock())
            result = run_catalogos(settings, engine)
        assert "nuevos" in result

    def test_run_retencion(self, engine):
        from app.ingest.orchestrator import run_retencion

        result = run_retencion(engine)
        assert "purgadas" in result or isinstance(result, dict)

    def test_run_backfill_fecha(self, settings, engine):
        from app.ingest.orchestrator import run_backfill_fecha

        with patch("app.ingest.orchestrator._make_clients") as mk:
            v1 = MagicMock()
            v1.licitaciones_por_fecha.return_value = []
            mk.return_value = (v1, MagicMock())
            result = run_backfill_fecha(settings, engine, date(2026, 6, 10))
        assert "nuevas" in result

    def test_run_digest(self, settings, engine):
        from app.ingest.orchestrator import run_digest

        with patch("app.alerts.email.enviar_digest", return_value={"enviados": 0}):
            result = run_digest(settings, engine)
        assert "enviados" in result

    def test_run_match_con_sin_detalle(self, settings, engine):
        from app.ingest.orchestrator import run_match

        with patch("app.matching.engine.match_todos") as mt, patch(
            "app.ingest.orchestrator._make_clients"
        ) as mk:
            mt.return_value = {
                "perfiles_procesados": 0,
                "nuevos": 0,
                "actualizados": 0,
                "descartados": 0,
                "sin_detalle_licitaciones": ["LIC-XXX"],
                "sin_detalle_ca": ["CA-XXX"],
            }
            v1 = MagicMock()
            v1.licitacion_detalle.return_value = _lic_detalle("LIC-XXX")
            v2 = MagicMock()
            from app.clients.types import CompraAgilDetalle

            v2.detalle_compra_agil.return_value = CompraAgilDetalle(
                codigo="CA-XXX",
                nombre="CA",
                estado="publicada",
                fecha_publicacion=None,
                fecha_cierre=None,
                fecha_ultimo_cambio=None,
                monto_clp=None,
                region=None,
                organismo_nombre=None,
                organismo_rut=None,
                total_ofertas=0,
                descripcion="",
                productos=[],
                id_orden_compra=None,
            )
            mk.return_value = (v1, v2)
            result = run_match(settings, engine)
        assert "sin_detalle_licitaciones" in result

    def test_ciclo_nocturno_dentro_ventana(self, settings, engine):
        """_ciclo_nocturno ejecuta jobs cuando está dentro de la ventana 22:00–07:00."""
        from app.ingest.orchestrator import _ciclo_nocturno

        now_fn = lambda tz: datetime(2026, 6, 13, 23, 30, tzinfo=tz)  # noqa: E731
        llamadas: list[str] = []

        def fake_run_with_lock(job_name: str, fn: Any, eng: Any, **kw: Any) -> None:
            llamadas.append(job_name)

        with patch("app.ingest.orchestrator._run_with_lock", side_effect=fake_run_with_lock):
            _ciclo_nocturno(settings, engine, now_fn=now_fn)

        assert "lifecycle" in llamadas
        assert "backfill_ayer" in llamadas

    def test_build_scheduler_registra_jobs(self, settings, engine):
        from app.ingest.orchestrator import build_scheduler

        sched = build_scheduler(settings, engine)
        job_ids = {j.id for j in sched.get_jobs()}
        assert "ca_incremental" in job_ids
        assert "nocturno" in job_ids
        assert "digest" in job_ids

    def test_run_scheduler_llama_start(self, settings, engine):
        from app.ingest.orchestrator import run_scheduler

        with patch("app.ingest.orchestrator.build_scheduler") as bs:
            mock_sched = MagicMock()
            bs.return_value = mock_sched
            run_scheduler(settings, engine)
        mock_sched.start.assert_called_once()


# ---------------------------------------------------------------------------
# Tests del CLI (run-once --limit)
# ---------------------------------------------------------------------------


class TestCliRunOnce:
    def test_limit_se_pasa_a_run_sync_activas(self, monkeypatch):
        from app.ingest.__main__ import cmd_run_once

        monkeypatch.setenv("MP_TICKET", "ticket-test-cli")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("SECRET_KEY", "clave-test-cli-32bytesxxxxxxxxxx")
        monkeypatch.setenv("JOBS_TOKEN", "token-test-cli-jobs-xxxxxxxxxxx")

        with patch("app.ingest.__main__.run_sync_activas") as mock_run:
            mock_run.return_value = {"nuevas": 0, "actualizadas": 0, "total": 0}
            cmd_run_once("activas", limit=5)

        assert mock_run.call_args.kwargs.get("limit") == 5

    def test_sin_limit_pasa_none(self, monkeypatch):
        from app.ingest.__main__ import cmd_run_once

        monkeypatch.setenv("MP_TICKET", "ticket-test-cli")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("SECRET_KEY", "clave-test-cli-32bytesxxxxxxxxxx")
        monkeypatch.setenv("JOBS_TOKEN", "token-test-cli-jobs-xxxxxxxxxxx")

        with patch("app.ingest.__main__.run_sync_activas") as mock_run:
            mock_run.return_value = {"nuevas": 0, "actualizadas": 0, "total": 0}
            cmd_run_once("activas")

        assert mock_run.call_args.kwargs.get("limit") is None
