"""Tests F-plan-busqueda — ruta HTML /plan-anual (pestaña "palabra") y los
endpoints HTMX de conteo en vivo y export CSV.

Casos que no requieren FTS real (q vacío, sin perfiles con keywords, filtros
de organismo/monto/mes solos) corren en SQLite. Los que sí escriben una
tsquery (texto libre o perfil con keywords) están en tests/test_plan_busqueda.py
(@needs_postgres) porque SQLite no tiene websearch_to_tsquery/array_agg.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token
from app.core.db import normalizar_url_driver
from app.core.settings import Settings
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import PerfilBusqueda, PlanCompraLinea, SyncState, Usuario

_PW = "contraseña-segura-test"
_AGNO = 2026

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_POSTGRES = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")
# La app siempre normaliza el driver a psycopg v3 (app/core/db.py); un
# create_engine con la URL cruda busca psycopg2, que no está en el stack.
_DB_URL_ENGINE = normalizar_url_driver(_DB_URL)
needs_postgres = pytest.mark.skipif(
    not _TIENE_POSTGRES,
    reason="Requiere DATABASE_URL apuntando a Postgres (con migración aplicada)",
)


@pytest.fixture()
def engine():
    import app.models.tables  # noqa: F401

    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    yield e


@pytest.fixture()
def settings():
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url="sqlite:///:memory:",
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
    )


@pytest.fixture()
def client(engine, settings):
    application = create_app(settings, engine)
    return TestClient(application, raise_server_exceptions=True)


@pytest.fixture()
def usuario(engine):
    with Session(engine) as s:
        u = Usuario(email="user@test.cl", password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _cookie(settings: Settings, user_id: int) -> dict[str, str]:
    token = create_session_token(settings.secret_key, user_id)
    return {COOKIE_NAME: token}


def _marcar_anio_completo_cargado(engine, agno: int = _AGNO) -> None:
    ahora = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as s:
        s.add(SyncState(fuente=f"plan_compra_anual_{agno}", ultima_ejecucion=ahora, ultimo_ok=ahora, cursor="Mon, 28 Sep 2026"))
        s.commit()


def _agregar_linea(engine, *, codigo_entidad: int, agno: int = _AGNO, mes: int, monto: float = 100000.0) -> None:
    with Session(engine) as s:
        s.add(
            PlanCompraLinea(
                codigo_entidad=codigo_entidad,
                agno=agno,
                institucion_nombre=f"ORG {codigo_entidad}",
                codigo_producto=str(codigo_entidad),
                descripcion_producto="algo",
                monto_estimado_clp=monto,
                mes_estimado=mes,
                lote_id=1,
            )
        )
        s.commit()


def test_plan_anual_sin_sesion_redirige(client):
    r = client.get("/plan-anual", follow_redirects=False)
    assert r.status_code == 302


def test_tab_organismo_sigue_funcionando_sin_cambios(client, usuario, settings, engine):
    r = client.get("/plan-anual?tab=organismo", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Plan Anual de Compra" in r.text


def test_palabra_anio_no_cargado_muestra_mensaje_de_carga(client, usuario, settings, engine):
    r = client.get(f"/plan-anual?tab=palabra&agno={_AGNO}", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "se está cargando" in r.text


def test_palabra_sin_perfiles_sin_texto_invita_a_buscar_o_crear_perfil(client, usuario, settings, engine):
    _marcar_anio_completo_cargado(engine)
    r = client.get(f"/plan-anual?tab=palabra&agno={_AGNO}", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "crea un perfil" in r.text


def test_palabra_filtro_organismo_sin_texto_no_llama_fts(client, usuario, settings, engine):
    """Sin texto y sin perfiles, pero CON un organismo elegido: sin_busqueda es
    False (hay un filtro activo) y la ruta no debe intentar FTS (q_include=None),
    así que corre bien contra SQLite."""
    _marcar_anio_completo_cargado(engine)
    _agregar_linea(engine, codigo_entidad=111, mes=3)
    _agregar_linea(engine, codigo_entidad=222, mes=6)

    r = client.get(
        f"/plan-anual?tab=palabra&agno={_AGNO}&organismos=111&vista=lineas&todo_el_anio=true",
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 200
    assert "ORG 111" in r.text
    assert "ORG 222" not in r.text


def test_palabra_desde_este_mes_es_el_default(client, usuario, settings, engine, monkeypatch):
    _marcar_anio_completo_cargado(engine)
    _agregar_linea(engine, codigo_entidad=111, mes=1)  # mes pasado, relativo al mes mockeado (6)
    _agregar_linea(engine, codigo_entidad=222, mes=12)  # mes futuro

    import app.api.routes.pages as pages_mod

    monkeypatch.setattr(pages_mod, "_mes_actual_chile", lambda: 6)

    r = client.get(
        f"/plan-anual?tab=palabra&agno={_AGNO}&organismos=111&organismos=222&vista=lineas",
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 200
    assert "ORG 111" not in r.text  # mes 1: queda fuera con "desde este mes"
    assert "ORG 222" in r.text

    r2 = client.get(
        f"/plan-anual?tab=palabra&agno={_AGNO}&organismos=111&organismos=222&vista=lineas&todo_el_anio=true",
        cookies=_cookie(settings, usuario),
    )
    assert r2.status_code == 200
    assert "ORG 111" in r2.text
    assert "ORG 222" in r2.text


def test_conteo_endpoint_devuelve_solo_numeros_sin_filas(client, usuario, settings, engine):
    _marcar_anio_completo_cargado(engine)
    _agregar_linea(engine, codigo_entidad=111, mes=3, monto=250000.0)

    r = client.get(
        f"/plan-anual/conteo?agno={_AGNO}&organismos=111&todo_el_anio=true",
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 200
    assert "ORG 111" not in r.text  # sin filas, solo el conteo
    assert "línea" in r.text


def test_conteo_endpoint_sin_coincidencias(client, usuario, settings, engine):
    _marcar_anio_completo_cargado(engine)
    r = client.get(
        f"/plan-anual/conteo?agno={_AGNO}&organismos=99999&todo_el_anio=true",
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 200
    assert "Sin coincidencias" in r.text


def test_export_csv_respeta_filtros_y_lleva_la_fuente(client, usuario, settings, engine):
    _marcar_anio_completo_cargado(engine)
    _agregar_linea(engine, codigo_entidad=111, mes=3)
    _agregar_linea(engine, codigo_entidad=222, mes=3)

    r = client.get(
        f"/plan-anual/export.csv?agno={_AGNO}&organismos=111&todo_el_anio=true",
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "Fuente: Dirección ChileCompra" in r.text
    assert "ORG 111" in r.text
    assert "ORG 222" not in r.text


def test_para_mis_perfiles_usa_solo_los_del_usuario_dueno(client, settings, engine):
    """Regla 17: nunca las keywords de otro usuario."""
    _marcar_anio_completo_cargado(engine)
    with Session(engine) as s:
        u1 = Usuario(email="uno@test.cl", password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        u2 = Usuario(email="dos@test.cl", password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        s.add_all([u1, u2])
        s.commit()
        s.refresh(u1)
        s.refresh(u2)
        s.add(
            PerfilBusqueda(
                owner_id=u2.id,
                nombre="perfil de otro usuario",
                keywords=["nunca-deberia-aparecer"],
                keywords_excluir=[],
            )
        )
        s.commit()
        u1_id = u1.id

    r = client.get(f"/plan-anual?tab=palabra&agno={_AGNO}", cookies=_cookie(settings, u1_id))
    assert r.status_code == 200
    # u1 no tiene perfiles propios -> sin_busqueda (invitación), nunca corre la
    # búsqueda con las keywords de u2.
    assert "crea un perfil" in r.text


# ---------------------------------------------------------------------------
# "Para mis perfiles" de punta a punta — requiere FTS real (Postgres)
# ---------------------------------------------------------------------------

_AGNO_PG = 88887  # centinela propio, distinto del usado en test_plan_busqueda.py


@needs_postgres
class TestParaMisPerfilesEndToEnd:
    @pytest.fixture()
    def pg_engine(self):
        import app.models.tables  # noqa: F401

        e = create_engine(_DB_URL_ENGINE)
        Base.metadata.create_all(e, checkfirst=True)
        yield e
        e.dispose()

    @pytest.fixture()
    def pg_settings(self):
        return Settings(
            mp_ticket="TICKET_TEST",
            database_url=_DB_URL,
            secret_key="secret-test-key-larga-32chars!!",
            jobs_token="jobs-token-secreto",
        )

    @pytest.fixture()
    def pg_client(self, pg_engine, pg_settings):
        application = create_app(pg_settings, pg_engine)
        return TestClient(application, raise_server_exceptions=True)

    @pytest.fixture()
    def datos(self, pg_engine):
        emails = ["mios@test-fts.cl", "otro@test-fts.cl"]

        def _limpiar() -> None:
            with Session(pg_engine) as s:
                s.execute(delete(PlanCompraLinea).where(PlanCompraLinea.agno == _AGNO_PG))
                for email in emails:
                    u = s.query(Usuario).filter_by(email=email).one_or_none()
                    if u:
                        s.delete(u)
                s.execute(
                    delete(SyncState).where(SyncState.fuente == f"plan_compra_anual_{_AGNO_PG}")
                )
                s.commit()

        _limpiar()
        with Session(pg_engine) as s:
            ahora = datetime.now(UTC).replace(tzinfo=None)
            s.add(SyncState(fuente=f"plan_compra_anual_{_AGNO_PG}", ultima_ejecucion=ahora, ultimo_ok=ahora, cursor="test"))
            mio = Usuario(email=emails[0], password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
            otro = Usuario(email=emails[1], password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
            s.add_all([mio, otro])
            s.commit()
            s.refresh(mio)
            s.refresh(otro)
            s.add(
                PerfilBusqueda(
                    owner_id=mio.id,
                    nombre="el mio",
                    keywords=["resma"],
                    keywords_excluir=[],
                )
            )
            s.add(
                PerfilBusqueda(
                    owner_id=otro.id,
                    nombre="de otro",
                    keywords=["sillas"],
                    keywords_excluir=[],
                )
            )
            s.add(
                PlanCompraLinea(
                    codigo_entidad=1,
                    agno=_AGNO_PG,
                    institucion_nombre="ORG RESMAS",
                    codigo_producto="1",
                    descripcion_producto="Compra de resmas de papel",
                    lote_id=1,
                )
            )
            s.add(
                PlanCompraLinea(
                    codigo_entidad=2,
                    agno=_AGNO_PG,
                    institucion_nombre="ORG SILLAS",
                    codigo_producto="2",
                    descripcion_producto="Compra de sillas de oficina",
                    lote_id=1,
                )
            )
            s.commit()
            mio_id = mio.id
        try:
            yield mio_id
        finally:
            _limpiar()

    def test_usa_solo_mis_keywords_no_las_de_otro_usuario(self, pg_client, pg_settings, datos):
        mio_id = datos
        r = pg_client.get(
            f"/plan-anual?tab=palabra&agno={_AGNO_PG}&todo_el_anio=true",
            cookies=_cookie(pg_settings, mio_id),
        )
        assert r.status_code == 200
        assert "ORG RESMAS" in r.text
        assert "ORG SILLAS" not in r.text
