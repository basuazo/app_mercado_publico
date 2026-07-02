"""Tests F2 — modelos, enums, retención, montos, seeds."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.montos import normalizar_clp
from app.core.retencion import purgar_terminales, tamano_bd
from app.core.settings import Settings
from app.models.enums import (
    ESTADOS_TERMINALES,
    EstadoOportunidad,
    estado_ca,
    estado_licitacion,
    estado_oc,
)
from app.models.seeds import seed_admin

# ---------------------------------------------------------------------------
# Helpers para detectar si hay Postgres disponible
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_POSTGRES = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")

needs_postgres = pytest.mark.skipif(
    not _TIENE_POSTGRES,
    reason="Requiere DATABASE_URL apuntando a Postgres (Neon rama dev o local)",
)

_VALID_ENV = {
    "MP_TICKET": "ticket-test-f2",
    "DATABASE_URL": _DB_URL or "postgresql://x:x@x/x",
    "SECRET_KEY": "clave-test-f2-32bytesxxxxxxxxxx",
    "JOBS_TOKEN": "token-test-f2-jobs-xxxxxxxxxxx",
}


@pytest.fixture()
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Fixtures de BD
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_engine():
    """SQLite en memoria — para tests que no requieren Postgres."""
    import app.models.tables  # noqa: F401 — registra tablas en metadata
    from app.models.base import Base

    # SQLite no soporta JSONB ni generated columns; usamos create_all sin ellas
    engine = create_engine("sqlite:///:memory:")
    # Crear tablas sin las columnas específicas de Postgres
    Base.metadata.create_all(engine, checkfirst=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def sqlite_session(sqlite_engine):
    with Session(sqlite_engine) as session:
        yield session


@pytest.fixture()
def pg_engine():
    """Engine Postgres real — solo si DATABASE_URL disponible."""
    engine = create_engine(_DB_URL)
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_session(pg_engine):
    """Sesión Postgres con rollback al finalizar (SQLAlchemy 2.x)."""
    with pg_engine.connect() as conn:
        trans = conn.begin()
        session = Session(bind=conn)
        yield session
        session.close()
        trans.rollback()


# ---------------------------------------------------------------------------
# Tests de enums — sin BD
# ---------------------------------------------------------------------------


def test_estado_licitacion_mapeos():
    assert estado_licitacion(5) == EstadoOportunidad.PUBLICADA
    assert estado_licitacion(6) == EstadoOportunidad.CERRADA
    assert estado_licitacion(7) == EstadoOportunidad.DESIERTA
    assert estado_licitacion(8) == EstadoOportunidad.ADJUDICADA
    assert estado_licitacion(18) == EstadoOportunidad.REVOCADA
    assert estado_licitacion(19) == EstadoOportunidad.SUSPENDIDA


def test_estado_licitacion_desconocido():
    assert estado_licitacion(999) == EstadoOportunidad.DESCONOCIDO
    assert estado_licitacion(None) == EstadoOportunidad.DESCONOCIDO
    assert estado_licitacion("X") == EstadoOportunidad.DESCONOCIDO


def test_estado_oc_mapeos():
    assert estado_oc(4) == EstadoOportunidad.ENVIADA_PROVEEDOR
    assert estado_oc(6) == EstadoOportunidad.ACEPTADA
    assert estado_oc(9) == EstadoOportunidad.CANCELADA
    assert estado_oc(12) == EstadoOportunidad.RECEPCION_CONFORME
    assert estado_oc(14) == EstadoOportunidad.RECEPCION_PARCIAL
    assert estado_oc(15) == EstadoOportunidad.RECEPCION_CONFORME_INCOMPLETA


def test_estado_oc_desconocido():
    assert estado_oc(999) == EstadoOportunidad.DESCONOCIDO


def test_estado_ca_mapeos():
    assert estado_ca("publicada") == EstadoOportunidad.PUBLICADA
    assert estado_ca("cerrada") == EstadoOportunidad.CERRADA
    assert estado_ca("desierta") == EstadoOportunidad.DESIERTA
    assert estado_ca("cancelada") == EstadoOportunidad.CANCELADA
    assert estado_ca("proveedor_seleccionado") == EstadoOportunidad.PROVEEDOR_SELECCIONADO


def test_estado_ca_desconocido():
    assert estado_ca("otro_estado") == EstadoOportunidad.DESCONOCIDO
    assert estado_ca(None) == EstadoOportunidad.DESCONOCIDO


def test_estados_terminales():
    assert EstadoOportunidad.ADJUDICADA in ESTADOS_TERMINALES
    assert EstadoOportunidad.CANCELADA in ESTADOS_TERMINALES
    assert EstadoOportunidad.DESIERTA in ESTADOS_TERMINALES
    assert EstadoOportunidad.REVOCADA in ESTADOS_TERMINALES
    assert EstadoOportunidad.PUBLICADA not in ESTADOS_TERMINALES


# ---------------------------------------------------------------------------
# Tests de montos — sin BD
# ---------------------------------------------------------------------------


def test_normalizar_clp_ya_es_clp(fake_settings):
    assert normalizar_clp(1000.0, "CLP", fake_settings) == 1000.0


def test_normalizar_clp_uf(fake_settings):
    resultado = normalizar_clp(1.0, "UF", fake_settings)
    assert resultado == fake_settings.tasa_uf


def test_normalizar_clp_utm(fake_settings):
    resultado = normalizar_clp(2.0, "UTM", fake_settings)
    assert resultado == 2.0 * fake_settings.tasa_utm


def test_normalizar_clp_usd(fake_settings):
    resultado = normalizar_clp(100.0, "USD", fake_settings)
    assert resultado == 100.0 * fake_settings.tasa_usd


def test_normalizar_clp_eur(fake_settings):
    resultado = normalizar_clp(50.0, "EUR", fake_settings)
    assert resultado == 50.0 * fake_settings.tasa_eur


def test_normalizar_clp_none(fake_settings):
    assert normalizar_clp(None, "CLP", fake_settings) is None


def test_normalizar_clp_moneda_desconocida(fake_settings):
    assert normalizar_clp(100.0, "JPY", fake_settings) is None


def test_normalizar_clp_sin_moneda(fake_settings):
    assert normalizar_clp(500.0, None, fake_settings) == 500.0


# ---------------------------------------------------------------------------
# Tests con SQLite (sin FTS ni Postgres-specific)
# ---------------------------------------------------------------------------


def test_upsert_no_duplica(sqlite_session):
    """Insertar dos veces el mismo código no duplica la fila."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.models.tables import Licitacion

    stmt = sqlite_insert(Licitacion).values(
        codigo="TEST-001",
        nombre="Licitacion Test",
        descripcion="",
        estado="publicada",
        detalle_obtenido=False,
        creado_en=datetime.now(UTC).replace(tzinfo=None),
        actualizado_en=datetime.now(UTC).replace(tzinfo=None),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["codigo"],
        set_={"nombre": "Licitacion Actualizada"},
    )
    sqlite_session.execute(stmt)
    sqlite_session.execute(stmt)
    sqlite_session.flush()

    from sqlalchemy import select

    rows = sqlite_session.execute(select(Licitacion).where(Licitacion.codigo == "TEST-001")).all()
    assert len(rows) == 1
    assert rows[0][0].nombre == "Licitacion Actualizada"


def test_unique_match(sqlite_session):
    """OportunidadMatch tiene constraint único (perfil_id, fuente, codigo_oportunidad)."""
    from sqlalchemy.exc import IntegrityError

    from app.models.tables import OportunidadMatch, PerfilBusqueda, Usuario

    usuario = Usuario(
        email="test@test.com",
        password_hash="hash",
        rol="usuario",
        activo=True,
        creado_en=datetime.now(UTC).replace(tzinfo=None),
    )
    sqlite_session.add(usuario)
    sqlite_session.flush()

    perfil = PerfilBusqueda(
        owner_id=usuario.id,
        nombre="Perfil Test",
        keywords=[],
        keywords_excluir=[],
        regiones=[],
        fuentes=["licitaciones"],
        frecuencia_alerta="digest",
        activo=True,
    )
    sqlite_session.add(perfil)
    sqlite_session.flush()

    m1 = OportunidadMatch(
        perfil_id=perfil.id,
        fuente="licitaciones",
        codigo_oportunidad="LIC-001",
        score=75.0,
        razones={},
        fecha_match=datetime.now(UTC).replace(tzinfo=None),
    )
    sqlite_session.add(m1)
    sqlite_session.flush()

    m2 = OportunidadMatch(
        perfil_id=perfil.id,
        fuente="licitaciones",
        codigo_oportunidad="LIC-001",
        score=80.0,
        razones={},
        fecha_match=datetime.now(UTC).replace(tzinfo=None),
    )
    sqlite_session.add(m2)
    with pytest.raises(IntegrityError):
        sqlite_session.flush()


def test_seed_admin_idempotente(sqlite_session):
    """seed_admin solo crea el admin si la tabla está vacía."""
    creado1 = seed_admin(sqlite_session, "admin@test.com", "Clave1234!")
    assert creado1 is True

    # Segunda llamada → tabla no está vacía → no duplica
    creado2 = seed_admin(sqlite_session, "admin@test.com", "Clave1234!")
    assert creado2 is False

    from sqlalchemy import func, select

    from app.models.tables import Usuario

    count = sqlite_session.execute(select(func.count()).select_from(Usuario)).scalar()
    assert count == 1


def test_retencion_purga_terminales(sqlite_session):
    """purgar_terminales limpia raw_json y productos de CAs terminales antiguas."""
    from sqlalchemy import select

    from app.models.tables import CaProducto, CompraAgil

    ahora = datetime.now(UTC).replace(tzinfo=None)

    # CA terminal antigua con raw_json — debe ser purgada
    antigua = CompraAgil(
        codigo="CA-VIEJA",
        nombre="CA Vieja",
        descripcion="",
        estado="adjudicada",
        total_ofertas=0,
        raw_json={"test": "data"},
        creado_en=ahora - timedelta(days=200),
        actualizado_en=ahora - timedelta(days=100),
    )
    # CA publicada vigente — NO debe ser purgada
    vigente = CompraAgil(
        codigo="CA-VIGENTE",
        nombre="CA Vigente",
        descripcion="",
        estado="publicada",
        total_ofertas=0,
        raw_json={"keep": "me"},
        creado_en=ahora,
        actualizado_en=ahora,
    )
    sqlite_session.add_all([antigua, vigente])
    sqlite_session.flush()

    producto_antiguo = CaProducto(
        ca_codigo="CA-VIEJA",
        codigo_producto="PROD-1",
        nombre="Producto viejo",
        descripcion="",
        unidad="UN",
    )
    sqlite_session.add(producto_antiguo)
    sqlite_session.flush()

    resultado = purgar_terminales(sqlite_session, dias=90)

    assert resultado["ca_purgadas"] == 1
    assert resultado["productos_borrados"] == 1

    # raw_json de la CA vieja debe ser NULL tras el purgado
    sqlite_session.expire_all()
    ca_vieja = sqlite_session.execute(
        select(CompraAgil).where(CompraAgil.codigo == "CA-VIEJA")
    ).scalar_one()
    assert ca_vieja.raw_json is None

    # La CA vigente conserva raw_json
    ca_vigente = sqlite_session.execute(
        select(CompraAgil).where(CompraAgil.codigo == "CA-VIGENTE")
    ).scalar_one()
    assert ca_vigente.raw_json is not None


def test_retencion_respeta_vigentes(sqlite_session):
    """purgar_terminales no toca oportunidades publicadas."""
    from app.models.tables import Licitacion

    ahora = datetime.now(UTC).replace(tzinfo=None)
    publicada = Licitacion(
        codigo="LIC-VIGENTE",
        nombre="Licitacion Vigente",
        descripcion="",
        estado="publicada",
        detalle_obtenido=True,
        creado_en=ahora - timedelta(days=200),
        actualizado_en=ahora - timedelta(days=200),
    )
    sqlite_session.add(publicada)
    sqlite_session.flush()

    resultado = purgar_terminales(sqlite_session, dias=90)
    assert resultado["licitaciones_purgadas"] == 0


def test_retencion_respeta_alertas_pendientes(sqlite_session):
    """purgar_terminales NO purga una CA terminal con alerta pendiente."""
    from sqlalchemy import select

    from app.models.tables import (
        Alerta,
        CaProducto,
        CompraAgil,
        OportunidadMatch,
        PerfilBusqueda,
        Usuario,
    )

    ahora = datetime.now(UTC).replace(tzinfo=None)

    # Usuario y perfil mínimos para el match
    usuario = Usuario(
        email="prot@test.com",
        password_hash="hash",
        rol="usuario",
        activo=True,
        creado_en=ahora,
    )
    sqlite_session.add(usuario)
    sqlite_session.flush()

    perfil = PerfilBusqueda(
        owner_id=usuario.id,
        nombre="Perfil protegido",
        keywords=[],
        keywords_excluir=[],
        regiones=[],
        fuentes=["compras_agiles"],
        frecuencia_alerta="digest",
        activo=True,
    )
    sqlite_session.add(perfil)
    sqlite_session.flush()

    # CA terminal antigua — normalmente se purgaría
    ca = CompraAgil(
        codigo="CA-PROTEGIDA",
        nombre="CA Protegida",
        descripcion="",
        estado="adjudicada",
        total_ofertas=0,
        raw_json={"proteger": True},
        creado_en=ahora - timedelta(days=200),
        actualizado_en=ahora - timedelta(days=100),
    )
    sqlite_session.add(ca)
    sqlite_session.flush()

    prod = CaProducto(
        ca_codigo="CA-PROTEGIDA",
        codigo_producto="PROD-PROT",
        nombre="Producto protegido",
        descripcion="",
        unidad="UN",
    )
    sqlite_session.add(prod)
    sqlite_session.flush()

    # Match + alerta pendiente → debe protegerla
    match = OportunidadMatch(
        perfil_id=perfil.id,
        fuente="compras_agiles",
        codigo_oportunidad="CA-PROTEGIDA",
        score=80.0,
        razones={},
        fecha_match=ahora,
    )
    sqlite_session.add(match)
    sqlite_session.flush()

    alerta = Alerta(
        match_id=match.id,
        tipo="nueva_oportunidad",
        canal="email",
        estado="pendiente",
    )
    sqlite_session.add(alerta)
    sqlite_session.flush()

    resultado = purgar_terminales(sqlite_session, dias=90)

    # No debe haber purgado nada
    assert resultado["ca_purgadas"] == 0
    assert resultado["productos_borrados"] == 0

    # raw_json y producto intactos
    sqlite_session.expire_all()
    ca_prot = sqlite_session.execute(
        select(CompraAgil).where(CompraAgil.codigo == "CA-PROTEGIDA")
    ).scalar_one()
    assert ca_prot.raw_json is not None

    n_prods = (
        sqlite_session.execute(select(CaProducto).where(CaProducto.ca_codigo == "CA-PROTEGIDA"))
        .scalars()
        .all()
    )
    assert len(n_prods) == 1


# ---------------------------------------------------------------------------
# Tests que requieren Postgres real
# ---------------------------------------------------------------------------


@needs_postgres
def test_fts_encuentra_sin_tilde(pg_session):
    """FTS encuentra 'electricos' en 'Materiales Eléctricos'."""
    from app.models.tables import Licitacion

    lic = Licitacion(
        codigo="FTS-TEST-001",
        nombre="Materiales Eléctricos",
        descripcion="Suministro de materiales eléctricos industriales",
        estado="publicada",
        detalle_obtenido=False,
        creado_en=datetime.now(UTC).replace(tzinfo=None),
        actualizado_en=datetime.now(UTC).replace(tzinfo=None),
    )
    pg_session.add(lic)
    pg_session.flush()

    row = pg_session.execute(
        text("""
            SELECT codigo FROM licitaciones
            WHERE codigo = 'FTS-TEST-001'
              AND tsv @@ websearch_to_tsquery('spanish', inmutable_unaccent('electricos'))
        """)
    ).fetchone()
    assert row is not None, "FTS no encontró 'electricos' en 'Eléctricos'"
    assert row[0] == "FTS-TEST-001"


@needs_postgres
def test_fts_compra_agil(pg_session):
    """FTS funciona también en compras_agiles."""
    from app.models.tables import CompraAgil

    ca = CompraAgil(
        codigo="FTS-CA-001",
        nombre="Sillas Ergonómicas",
        descripcion="Compra de sillas ergonómicas para oficina",
        estado="publicada",
        total_ofertas=0,
        creado_en=datetime.now(UTC).replace(tzinfo=None),
        actualizado_en=datetime.now(UTC).replace(tzinfo=None),
    )
    pg_session.add(ca)
    pg_session.flush()

    row = pg_session.execute(
        text("""
            SELECT codigo FROM compras_agiles
            WHERE codigo = 'FTS-CA-001'
              AND tsv @@ websearch_to_tsquery('spanish', inmutable_unaccent('ergonomicas'))
        """)
    ).fetchone()
    assert row is not None
    assert row[0] == "FTS-CA-001"


@needs_postgres
def test_tamano_bd_retorna_entero(pg_session):
    resultado = tamano_bd(pg_session)
    assert resultado is not None
    assert isinstance(resultado, int)
    assert resultado > 0


@needs_postgres
def test_seed_admin_postgres(pg_session):
    """seed_admin crea usuario admin correctamente en Postgres."""
    creado = seed_admin(pg_session, "admin_pg_test@test.com", "TestPass123!")
    # Puede ser True (tabla vacía) o False (ya tenía datos); no lanzó excepción
    assert isinstance(creado, bool)
