"""Tests F-estados-vencidos — estados de licitaciones que ya salieron del listado de activas."""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clients.base import MPRateLimitError
from app.clients.datos_abiertos import stream_estados, url_lic_da
from app.clients.types import CompraAgilBasica, LicitacionDetalle, PaginacionV2, RespuestaListadoV2
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.ingest.datos_abiertos import (
    CacheZipsDA,
    sync_estados_datos_abiertos,
    sync_items_datos_abiertos,
)
from app.ingest.lifecycle import refresh_estados_vencidos
from app.models.enums import EstadoOportunidad, estado_licitacion_da
from app.models.tables import (
    CompraAgil,
    Licitacion,
    OportunidadMatch,
    PerfilBusqueda,
    SyncState,
    Usuario,
)

_VALID_ENV = {
    "MP_TICKET": "ticket-test-vencidos",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-vencidos-32bytesxxxxxx",
    "JOBS_TOKEN": "token-test-vencidos-jobs-xxxxxxxx",
}

# Mes "en curso" fijo: con DATOS_ABIERTOS_MESES_ATRAS=0 se lee solo 2026-9.
_HOY = "2026-09-24 12:00:00"
_NOCHE = lambda tz: datetime(2026, 9, 24, 23, 30, tzinfo=tz)  # noqa: E731
_DIA = lambda tz: datetime(2026, 9, 24, 13, 0, tzinfo=tz)  # noqa: E731


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    s.datos_abiertos_meses_atras = 0
    return s


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


_HEADER = '"CodigoExterno";"CodigoEstado";"Estado";"Codigoitem"\r\n'


def _fila(codigo: str, codigo_estado: str, item: str = "1") -> str:
    return f'"{codigo}";"{codigo_estado}";"x";"{item}"\r\n'


def _zip(*filas: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("lic_2026-9.csv", (_HEADER + "".join(filas)).encode("latin-1"))
    return buf.getvalue()


def _mock_zip(settings: Settings, contenido: bytes, anio: int = 2026, mes: int = 9) -> respx.Route:
    url = url_lic_da(anio, mes, settings.datos_abiertos_base_url)
    return respx.get(url).mock(return_value=httpx.Response(200, content=contenido))


def _lic(session: Session, codigo: str, estado: str, *, dias_desde_cierre: int = 30) -> Licitacion:
    lic = Licitacion(
        codigo=codigo,
        nombre=f"Lic {codigo}",
        estado=estado,
        estado_codigo=5,
        fecha_cierre=ahora_utc() - timedelta(days=dias_desde_cierre),
    )
    session.add(lic)
    session.commit()
    return lic


def _con_match(session: Session, *codigos: str) -> None:
    u = session.execute(select(Usuario)).scalar_one_or_none()
    if u is None:
        u = Usuario(email="vencidos@test.cl", password_hash="x", activo=True)
        session.add(u)
        session.flush()
        session.add(PerfilBusqueda(owner_id=u.id, nombre="p", keywords=[]))
        session.flush()
    perfil = session.execute(select(PerfilBusqueda)).scalar_one()
    for c in codigos:
        session.add(
            OportunidadMatch(perfil_id=perfil.id, fuente="licitaciones", codigo_oportunidad=c, score=50.0, razones={})
        )
    session.commit()


def _detalle(codigo: str, estado: int = 8) -> LicitacionDetalle:
    return LicitacionDetalle(
        codigo=codigo,
        nombre=f"Lic {codigo}",
        estado=estado,
        fecha_publicacion=None,
        fecha_cierre=None,
        tipo="L1",
        codigo_organismo="ORG-1",
        descripcion="",
        moneda="CLP",
        monto_estimado=None,
        items=[],
    )


def _estado(session: Session, codigo: str) -> tuple[str, int | None]:
    session.expire_all()
    lic = session.get(Licitacion, codigo)
    assert lic is not None
    return lic.estado, lic.estado_codigo


# ---------------------------------------------------------------------------
# Mapeo de códigos de datos abiertos
# ---------------------------------------------------------------------------


def test_codigos_da_no_son_los_de_v1():
    """[V] 24-sep: en lic-da revocada es 15 y suspendida 16 (en v1: 18 y 19)."""
    assert estado_licitacion_da(15) is EstadoOportunidad.REVOCADA
    assert estado_licitacion_da(16) is EstadoOportunidad.SUSPENDIDA
    assert estado_licitacion_da(9) is EstadoOportunidad.ADJUDICADA
    for cerrada in (6, 11, 12, 13, 14):
        assert estado_licitacion_da(cerrada) is EstadoOportunidad.CERRADA


def test_stream_estados_una_fila_por_fila_del_csv(tmp_path):
    ruta = tmp_path / "z.zip"
    ruta.write_bytes(_zip(_fila("LIC-A", "8", "1"), _fila("LIC-A", "8", "2"), _fila("", "6")))
    filas = list(stream_estados(str(ruta)))
    assert [(f.codigo_externo, f.codigo_estado) for f in filas] == [("LIC-A", "8"), ("LIC-A", "8")]


# ---------------------------------------------------------------------------
# Paso 1: datos abiertos
# ---------------------------------------------------------------------------


class TestEstadosDatosAbiertos:
    @freeze_time(_HOY)
    @respx.mock
    def test_adjudicada_actualiza_una_publicada(self, session, settings):
        _lic(session, "LIC-A", "publicada")
        _mock_zip(settings, _zip(_fila("LIC-A", "8")))

        contadores, resueltas = sync_estados_datos_abiertos(session, settings)

        assert _estado(session, "LIC-A") == ("adjudicada", 8)
        assert contadores["actualizadas"] == 1
        assert resueltas == {"LIC-A"}

    @freeze_time(_HOY)
    @respx.mock
    def test_revocada_guarda_el_codigo_de_v1(self, session, settings):
        _lic(session, "LIC-R", "cerrada")
        _mock_zip(settings, _zip(_fila("LIC-R", "15")))

        sync_estados_datos_abiertos(session, settings)

        assert _estado(session, "LIC-R") == ("revocada", 18)

    @freeze_time(_HOY)
    @respx.mock
    def test_terminal_no_retrocede(self, session, settings):
        _lic(session, "LIC-T", "desierta")
        _mock_zip(settings, _zip(_fila("LIC-T", "6")))

        contadores, resueltas = sync_estados_datos_abiertos(session, settings)

        assert _estado(session, "LIC-T")[0] == "desierta"
        assert contadores["actualizadas"] == 0
        assert resueltas == set()  # las terminales ni siquiera son objetivo

    @freeze_time(_HOY)
    @respx.mock
    def test_no_retrocede_de_cerrada_a_publicada(self, session, settings):
        _lic(session, "LIC-C", "cerrada")
        _mock_zip(settings, _zip(_fila("LIC-C", "5")))

        contadores, _ = sync_estados_datos_abiertos(session, settings)

        assert _estado(session, "LIC-C")[0] == "cerrada"
        assert contadores["actualizadas"] == 0

    @freeze_time(_HOY)
    @respx.mock
    def test_licitacion_ajena_se_ignora(self, session, settings):
        _lic(session, "LIC-A", "publicada")
        _mock_zip(settings, _zip(_fila("LIC-AJENA", "8")))

        contadores, resueltas = sync_estados_datos_abiertos(session, settings)

        assert session.get(Licitacion, "LIC-AJENA") is None
        assert _estado(session, "LIC-A")[0] == "publicada"
        assert contadores["actualizadas"] == 0
        assert resueltas == set()

    @freeze_time(_HOY)
    @respx.mock
    def test_estado_desconocido_no_pisa_uno_conocido(self, session, settings, caplog):
        _lic(session, "LIC-X", "cerrada")
        _mock_zip(settings, _zip(_fila("LIC-X", "99")))

        with caplog.at_level(logging.WARNING):
            contadores, resueltas = sync_estados_datos_abiertos(session, settings)

        assert _estado(session, "LIC-X") == ("cerrada", 5)
        assert contadores["desconocidos"] == 1
        assert contadores["actualizadas"] == 0
        assert resueltas == set()  # queda para el fallback por API
        assert any("sin mapeo: 99" in r.getMessage() for r in caplog.records)

    @freeze_time(_HOY)
    @respx.mock
    def test_conocido_reemplaza_a_desconocido(self, session, settings):
        _lic(session, "LIC-D", "desconocido")
        _mock_zip(settings, _zip(_fila("LIC-D", "6")))

        sync_estados_datos_abiertos(session, settings)

        assert _estado(session, "LIC-D") == ("cerrada", 6)

    @freeze_time(_HOY)
    @respx.mock
    def test_codigo_repetido_en_varias_filas_una_sola_actualizacion(self, session, settings):
        _lic(session, "LIC-A", "publicada")
        _mock_zip(
            settings,
            _zip(_fila("LIC-A", "6", "1"), _fila("LIC-A", "8", "2"), _fila("LIC-A", "8", "3")),
        )

        contadores, _ = sync_estados_datos_abiertos(session, settings)

        # Primera fila por código: el CSV repite la licitación por ítem × oferta.
        assert _estado(session, "LIC-A")[0] == "cerrada"
        assert contadores["actualizadas"] == 1

    @freeze_time(_HOY)
    @respx.mock
    def test_idempotente(self, session, settings):
        _lic(session, "LIC-A", "publicada")
        _lic(session, "LIC-X", "publicada")
        _mock_zip(settings, _zip(_fila("LIC-A", "8"), _fila("LIC-X", "99")))

        primera, _ = sync_estados_datos_abiertos(session, settings)
        segunda, _ = sync_estados_datos_abiertos(session, settings)

        assert primera["actualizadas"] == 1  # LIC-X (desconocido) no se aplica
        assert segunda["actualizadas"] == 0
        assert _estado(session, "LIC-A")[0] == "adjudicada"

    @freeze_time(_HOY)
    @respx.mock
    def test_mes_que_falla_no_corta_los_demas(self, session, settings):
        settings.datos_abiertos_meses_atras = 1
        _lic(session, "LIC-A", "publicada")
        respx.get(url_lic_da(2026, 9, settings.datos_abiertos_base_url)).mock(return_value=httpx.Response(404))
        _mock_zip(settings, _zip(_fila("LIC-A", "8")), mes=8)

        contadores, _ = sync_estados_datos_abiertos(session, settings)

        assert contadores["meses_fallidos"] == 1
        assert contadores["meses_escaneados"] == 1
        assert _estado(session, "LIC-A")[0] == "adjudicada"

    @freeze_time(_HOY)
    @respx.mock
    def test_zip_se_baja_una_vez_si_items_ya_lo_bajo(self, session, settings):
        _lic(session, "LIC-A", "publicada")  # sin ítems: objetivo de la ingesta de ítems
        url = url_lic_da(2026, 9, settings.datos_abiertos_base_url)
        respx.head(url).mock(
            return_value=httpx.Response(200, headers={"last-modified": "Thu, 24 Sep 2026 10:00:00 GMT"})
        )
        ruta = _mock_zip(settings, _zip(_fila("LIC-A", "8")))

        with CacheZipsDA(settings) as zips:
            sync_items_datos_abiertos(session, settings, zips=zips)
            contadores, _ = sync_estados_datos_abiertos(session, settings, zips=zips)

        assert ruta.call_count == 1
        assert contadores["descargados"] == 0
        assert _estado(session, "LIC-A")[0] == "adjudicada"


# ---------------------------------------------------------------------------
# Paso 2: fallback por API + registro
# ---------------------------------------------------------------------------


class TestRefreshEstadosVencidos:
    @freeze_time(_HOY)
    @respx.mock
    def test_solo_rezagadas_con_match_no_cubiertas_por_da(self, session, settings):
        _lic(session, "LIC-DA", "publicada")
        _lic(session, "LIC-API", "publicada")
        _lic(session, "LIC-SIN-MATCH", "publicada")
        _lic(session, "LIC-RECIENTE", "publicada", dias_desde_cierre=3)
        _lic(session, "LIC-TERMINAL", "adjudicada")
        _con_match(session, "LIC-DA", "LIC-API", "LIC-RECIENTE", "LIC-TERMINAL")
        _mock_zip(settings, _zip(_fila("LIC-DA", "8")))
        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = lambda c: _detalle(c)

        r = refresh_estados_vencidos(session, v1, settings, now_fn=_NOCHE)

        assert [c.args[0] for c in v1.licitacion_detalle.call_args_list] == ["LIC-API"]
        assert _estado(session, "LIC-API")[0] == "adjudicada"
        assert _estado(session, "LIC-DA")[0] == "adjudicada"
        assert r["actualizadas_da"] == 1
        assert r["consultadas_api"] == 1
        assert r["cambiadas_api"] == 1
        assert r["rezagadas"] == 0

    @freeze_time(_HOY)
    @respx.mock
    def test_vista_en_da_como_cerrada_sigue_elegible_para_la_api(self, session, settings):
        """Un mes de lic-da que ya no se republica puede dejarla `cerrada` para siempre."""
        _lic(session, "LIC-C", "publicada")
        _con_match(session, "LIC-C")
        _mock_zip(settings, _zip(_fila("LIC-C", "6")))
        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = lambda c: _detalle(c)

        r = refresh_estados_vencidos(session, v1, settings, now_fn=_NOCHE)

        assert r["actualizadas_da"] == 1
        assert [c.args[0] for c in v1.licitacion_detalle.call_args_list] == ["LIC-C"]
        assert _estado(session, "LIC-C")[0] == "adjudicada"

    @respx.mock
    def test_el_tope_rota_entre_noches(self, session, settings):
        """Una que sigue `cerrada` tras consultarla va al fondo: la otra entra la noche siguiente."""
        settings.estados_vencidos_max_requests = 1
        with freeze_time("2026-09-20 12:00:00"):
            _lic(session, "LIC-10", "cerrada", dias_desde_cierre=10)
            _lic(session, "LIC-20", "cerrada", dias_desde_cierre=20)
            _con_match(session, "LIC-10", "LIC-20")
        _mock_zip(settings, _zip())
        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = lambda c: _detalle(c, estado=6)

        with freeze_time("2026-09-23 12:00:00"):
            r1 = refresh_estados_vencidos(session, v1, settings, now_fn=_NOCHE)
        with freeze_time("2026-09-24 12:00:00"):
            r2 = refresh_estados_vencidos(session, v1, settings, now_fn=_NOCHE)

        assert [c.args[0] for c in v1.licitacion_detalle.call_args_list] == ["LIC-10", "LIC-20"]
        assert r1["consultadas_api"] == r2["consultadas_api"] == 1
        assert r1["cambiadas_api"] == r2["cambiadas_api"] == 0

    @freeze_time(_HOY)
    @respx.mock
    def test_respeta_el_tope_y_entre_iguales_prioriza_las_mas_recientes(self, session, settings):
        settings.estados_vencidos_max_requests = 2
        for dias in (10, 20, 30, 40, 50):
            _lic(session, f"LIC-{dias}", "publicada", dias_desde_cierre=dias)
        _con_match(session, *(f"LIC-{d}" for d in (10, 20, 30, 40, 50)))
        _mock_zip(settings, _zip())
        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = lambda c: _detalle(c)

        r = refresh_estados_vencidos(session, v1, settings, now_fn=_NOCHE)

        assert [c.args[0] for c in v1.licitacion_detalle.call_args_list] == ["LIC-10", "LIC-20"]
        assert r["consultadas_api"] == 2
        assert r["rezagadas"] == 3

    @freeze_time(_HOY)
    @respx.mock
    def test_fuera_de_ventana_no_llama_a_la_api(self, session, settings):
        _lic(session, "LIC-A", "publicada")
        _lic(session, "LIC-B", "publicada")
        _con_match(session, "LIC-A", "LIC-B")
        _mock_zip(settings, _zip(_fila("LIC-A", "8")))
        v1 = MagicMock()

        r = refresh_estados_vencidos(session, v1, settings, now_fn=_DIA)

        v1.licitacion_detalle.assert_not_called()
        assert r["api_fuera_de_ventana"] == 1
        assert r["actualizadas_da"] == 1  # datos abiertos no gasta cuota: corre igual
        assert r["rezagadas"] == 1

    @freeze_time(_HOY)
    @respx.mock
    def test_429_corta_guarda_lo_avanzado_y_relanza(self, session, settings):
        for dias in (10, 20, 30):
            _lic(session, f"LIC-{dias}", "publicada", dias_desde_cierre=dias)
        _con_match(session, "LIC-10", "LIC-20", "LIC-30")
        _mock_zip(settings, _zip())
        v1 = MagicMock()

        def _detalle_o_429(codigo: str) -> LicitacionDetalle:
            if codigo == "LIC-20":
                raise MPRateLimitError("429", retry_after_seconds=60)
            return _detalle(codigo)

        v1.licitacion_detalle.side_effect = _detalle_o_429

        with pytest.raises(MPRateLimitError):
            refresh_estados_vencidos(session, v1, settings, now_fn=_NOCHE)

        assert v1.licitacion_detalle.call_count == 2  # no sigue contra una API que rechaza
        assert _estado(session, "LIC-10")[0] == "adjudicada"
        state = session.get(SyncState, "estados_vencidos")
        assert state is not None
        assert state.notas is not None and "consultadas_api=1" in state.notas and "cortado" in state.notas
        assert state.ultimo_ok is None

    @freeze_time(_HOY)
    @respx.mock
    def test_error_de_una_licitacion_no_corta(self, session, settings):
        for dias in (10, 20):
            _lic(session, f"LIC-{dias}", "publicada", dias_desde_cierre=dias)
        _con_match(session, "LIC-10", "LIC-20")
        _mock_zip(settings, _zip())
        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = [RuntimeError("roto"), _detalle("LIC-20")]

        r = refresh_estados_vencidos(session, v1, settings, now_fn=_NOCHE)

        assert r["errores_api"] == 1
        assert r["consultadas_api"] == 1
        assert r["rezagadas"] == 1

    @freeze_time(_HOY)
    @respx.mock
    def test_registra_en_sync_state(self, session, settings):
        _lic(session, "LIC-A", "publicada")
        _mock_zip(settings, _zip(_fila("LIC-A", "8")))

        refresh_estados_vencidos(session, MagicMock(), settings, now_fn=_NOCHE)

        state = session.get(SyncState, "estados_vencidos")
        assert state is not None and state.ultimo_ok is not None
        assert state.notas == "datos_abiertos=1 consultadas_api=0 cambiadas_api=0 rezagadas=0 errores_api=0"

    @freeze_time(_HOY)
    @respx.mock
    def test_idempotente(self, session, settings):
        _lic(session, "LIC-DA", "publicada")
        _lic(session, "LIC-API", "publicada")
        _con_match(session, "LIC-DA", "LIC-API")
        _mock_zip(settings, _zip(_fila("LIC-DA", "8")))
        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = lambda c: _detalle(c)

        refresh_estados_vencidos(session, v1, settings, now_fn=_NOCHE)
        segunda = refresh_estados_vencidos(session, v1, settings, now_fn=_NOCHE)

        # Ya terminales: la segunda corrida no encuentra nada que hacer.
        assert segunda["actualizadas_da"] == 0
        assert segunda["consultadas_api"] == 0
        assert segunda["rezagadas"] == 0
        assert v1.licitacion_detalle.call_count == 1


# ---------------------------------------------------------------------------
# Compra Ágil: el cambio de estado entra por el ciclo horario `ca`
# ---------------------------------------------------------------------------


def test_ca_cambio_de_estado_entra_por_el_listado_incremental(session, settings):
    """Paso 0 (24-sep): 0 CA con match quedaron pegadas; la vía es `ca`, no esta fase.

    Ojo: el listado filtra por publicada/cerrada/proveedor_seleccionado, así que
    una CA que pasa a desierta o cancelada NO llega por acá (queda `cerrada`).
    """
    from app.ingest.compra_agil import sync_incremental

    session.add(CompraAgil(codigo="CA-1", nombre="CA", estado="cerrada", fecha_cierre=ahora_utc()))
    session.add(SyncState(fuente="compra_agil", cursor=(ahora_utc() - timedelta(hours=1)).isoformat()))
    session.commit()

    item = CompraAgilBasica(
        codigo="CA-1",
        nombre="CA",
        estado="proveedor_seleccionado",
        fecha_publicacion=None,
        fecha_cierre=ahora_utc(),
        fecha_ultimo_cambio=ahora_utc() - timedelta(minutes=30),
        monto_clp=None,
        region=13,
        organismo_nombre=None,
        organismo_rut=None,
        total_ofertas=1,
    )
    v2 = MagicMock()
    v2.listar_compra_agil.return_value = RespuestaListadoV2(
        items=[item],
        paginacion=PaginacionV2(total_paginas=1, total_resultados=1, numero_pagina=1, tamano_pagina=20),
    )

    sync_incremental(session, v2, settings)

    session.expire_all()
    ca = session.get(CompraAgil, "CA-1")
    assert ca is not None and ca.estado == "proveedor_seleccionado"


# ---------------------------------------------------------------------------
# Ciclo nocturno
# ---------------------------------------------------------------------------


def test_ciclo_nocturno_corre_estados_vencidos_tras_datos_abiertos(settings, engine):
    from app.ingest.orchestrator import _ciclo_nocturno

    llamadas: list[str] = []

    def fake_run_with_lock(job_name: str, fn: Any, eng: Any, **kw: Any) -> None:
        llamadas.append(job_name)

    with patch("app.ingest.orchestrator._run_with_lock", side_effect=fake_run_with_lock):
        _ciclo_nocturno(settings, engine, now_fn=_NOCHE)

    assert llamadas.index("datos_abiertos") < llamadas.index("estados-vencidos")
    assert llamadas.index("estados-vencidos") < llamadas.index("competencia")
