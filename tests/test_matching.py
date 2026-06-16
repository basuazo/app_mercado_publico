"""Tests F4 — matching engine, scoring y CRUD de perfiles.

Tests de score: funciones puras, sin BD.
Tests de CRUD: SQLite en memoria.
Tests de FTS: requieren Postgres con migración aplicada (@needs_postgres).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.matching.engine import (
    _keywords_en_textos,
    _norm,
    _score_ca,
    _score_licitacion,
    _upsert_match,
    match_perfil,
    match_todos,
    score_competencia,
    score_texto,
    score_urgencia,
)
from app.matching.perfiles import (
    PerfilInvalido,
    crear_perfil,
    eliminar_perfil,
    listar_perfiles,
    obtener_perfil,
)
from app.matching.text import build_exclude_tsquery, build_tsquery
from app.models.tables import OportunidadMatch, Usuario
from tests.fixtures.dataset_matching import AHORA, crear_dataset

# ---------------------------------------------------------------------------
# Helpers de detección de Postgres
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_POSTGRES = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")

needs_postgres = pytest.mark.skipif(
    not _TIENE_POSTGRES,
    reason="Requiere DATABASE_URL apuntando a Postgres (con migración aplicada)",
)

# ---------------------------------------------------------------------------
# Fixture SQLite para tests de CRUD y score
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_engine():
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def session(sqlite_engine):
    with Session(sqlite_engine) as s:
        yield s


# ---------------------------------------------------------------------------
# 1. Tests de score puro (sin BD)
# ---------------------------------------------------------------------------


class TestScoreTexto:
    def test_todos_los_keywords_hit(self):
        kws = ["eléctrico", "cable"]
        assert score_texto(kws, kws, hit_en_nombre=False) == pytest.approx(60.0)

    def test_sin_keywords_devuelve_cero(self):
        assert score_texto([], [], hit_en_nombre=False) == 0.0

    def test_hit_parcial_50_pct(self):
        kws = ["eléctrico", "cable"]
        # 1/2 keywords hit → 0.5 × 60 = 30
        assert score_texto(kws, ["eléctrico"], hit_en_nombre=False) == pytest.approx(30.0)

    def test_bonus_nombre_suma_5(self):
        kws = ["eléctrico"]
        # 60 + 5 = 65 → capped a 60
        assert score_texto(kws, kws, hit_en_nombre=True) == pytest.approx(60.0)

    def test_bonus_nombre_en_hit_parcial(self):
        kws = ["eléctrico", "cable", "iluminación"]
        # 1/3 × 60 + 5 = 25
        assert score_texto(kws, ["cable"], hit_en_nombre=True) == pytest.approx(25.0)

    def test_ningún_hit_con_keywords(self):
        kws = ["eléctrico", "cable"]
        assert score_texto(kws, [], hit_en_nombre=False) == 0.0


class TestScoreUrgencia:
    def test_rango_optimo_2_a_7_dias(self):
        for dias in (2.0, 5.0, 7.0):
            assert score_urgencia(dias) == 25.0, f"falló con {dias} días"

    def test_rango_bueno_8_a_30_dias(self):
        for dias in (8.0, 15.0, 30.0):
            assert score_urgencia(dias) == 10.0, f"falló con {dias} días"

    def test_menos_de_2_dias_urgencia_cero(self):
        assert score_urgencia(0.0) == 0.0
        assert score_urgencia(1.9) == 0.0

    def test_mas_de_30_dias_urgencia_cero(self):
        assert score_urgencia(31.0) == 0.0
        assert score_urgencia(90.0) == 0.0

    def test_borde_exacto_2_dias(self):
        assert score_urgencia(2.0) == 25.0

    def test_borde_exacto_7_dias(self):
        assert score_urgencia(7.0) == 25.0

    def test_borde_exacto_30_dias(self):
        assert score_urgencia(30.0) == 10.0


class TestScoreCompetencia:
    def test_ca_sin_ofertas(self):
        assert score_competencia("compras_agiles", 0) == 15.0

    def test_ca_1_oferta(self):
        assert score_competencia("compras_agiles", 1) == 10.0

    def test_ca_3_ofertas(self):
        assert score_competencia("compras_agiles", 3) == 10.0

    def test_ca_mas_de_3_ofertas(self):
        assert score_competencia("compras_agiles", 4) == 5.0
        assert score_competencia("compras_agiles", 10) == 5.0

    def test_licitacion_neutro(self):
        assert score_competencia("licitaciones", 0) == 8.0
        assert score_competencia("licitaciones", 99) == 8.0


class TestBuildTsquery:
    def test_keyword_simple(self):
        assert build_tsquery(["eléctrico"]) == "eléctrico"

    def test_multiples_keywords_se_unen_con_or(self):
        q = build_tsquery(["eléctrico", "cable"])
        assert q == "eléctrico OR cable"

    def test_frase_entre_comillas(self):
        q = build_tsquery(['"cable eléctrico"'])
        assert q == '"cable eléctrico"'

    def test_lista_vacia(self):
        assert build_tsquery([]) == ""

    def test_keywords_con_espacios_extra_se_limpian(self):
        q = build_tsquery(["  eléctrico  ", "cable"])
        assert q == "eléctrico OR cable"

    def test_build_exclude_tsquery(self):
        q = build_exclude_tsquery(["excluido", "rechazado"])
        assert q == "excluido OR rechazado"


class TestNormalizacion:
    def test_tilde_insensitive(self):
        assert _norm("eléctrico") == "electrico"
        assert _norm("Eléctrico") == "electrico"
        assert _norm("ELÉCTRICO") == "electrico"

    def test_keyword_en_texto_con_tilde(self):
        # keyword sin tilde debe encontrarse en texto con tilde
        hit = _keywords_en_textos(["electrico"], ["Material Eléctrico"])
        assert "electrico" in hit

    def test_keyword_con_tilde_en_texto_sin_tilde(self):
        hit = _keywords_en_textos(["eléctrico"], ["Material electrico"])
        assert "eléctrico" in hit

    def test_frase_entre_comillas(self):
        hit = _keywords_en_textos(['"cable electrico"'], ["Compra de cable electrico"])
        assert '"cable electrico"' in hit

    def test_no_hit_cuando_no_aparece(self):
        hit = _keywords_en_textos(["electrico"], ["Servicio de limpieza"])
        assert hit == []


# ---------------------------------------------------------------------------
# 2. Tests de CRUD (SQLite)
# ---------------------------------------------------------------------------

_PW_HASH = "$2b$12$fakehashfortestsislong.enough.xyz12345"


class TestPerfilesCRUD:
    def _user(self, session: Session) -> Usuario:
        u = Usuario(email="test@test.com", password_hash=_PW_HASH, activo=True)
        session.add(u)
        session.flush()
        return u

    def test_crear_perfil_con_keyword(self, session: Session):
        u = self._user(session)
        p = crear_perfil(session, u.id, "Mi perfil", keywords=["eléctrico"])
        assert p.id is not None
        assert list(p.keywords) == ["eléctrico"]  # type: ignore[arg-type]

    def test_crear_perfil_solo_region(self, session: Session):
        u = self._user(session)
        p = crear_perfil(session, u.id, "Solo región", regiones=[13])
        assert p.id is not None

    def test_crear_perfil_solo_monto(self, session: Session):
        u = self._user(session)
        p = crear_perfil(session, u.id, "Solo monto", monto_min_clp=100_000.0)
        assert p.id is not None

    def test_perfil_sin_nada_invalido(self, session: Session):
        u = self._user(session)
        with pytest.raises(PerfilInvalido):
            crear_perfil(session, u.id, "Vacío")

    def test_ownership_obtener_propio(self, session: Session):
        u = self._user(session)
        p = crear_perfil(session, u.id, "Mío", keywords=["cable"])
        result = obtener_perfil(session, p.id, u.id)
        assert result is not None and result.id == p.id

    def test_ownership_obtener_ajeno_devuelve_none(self, session: Session):
        u1 = self._user(session)
        u2 = Usuario(email="otro@test.com", password_hash=_PW_HASH, activo=True)
        session.add(u2)
        session.flush()
        p = crear_perfil(session, u1.id, "De u1", keywords=["cable"])
        assert obtener_perfil(session, p.id, u2.id) is None

    def test_listar_solo_perfiles_propios(self, session: Session):
        u1 = self._user(session)
        u2 = Usuario(email="otro2@test.com", password_hash=_PW_HASH, activo=True)
        session.add(u2)
        session.flush()
        crear_perfil(session, u1.id, "P1", keywords=["a"])
        crear_perfil(session, u1.id, "P2", keywords=["b"])
        crear_perfil(session, u2.id, "P3 ajeno", keywords=["c"])
        perfiles_u1 = listar_perfiles(session, u1.id)
        assert len(perfiles_u1) == 2
        assert all(p.owner_id == u1.id for p in perfiles_u1)

    def test_listar_excluye_inactivos(self, session: Session):
        u = self._user(session)
        p_activo = crear_perfil(session, u.id, "Activo", keywords=["a"])
        p_inactivo = crear_perfil(session, u.id, "Inactivo", keywords=["b"])
        p_inactivo.activo = False
        session.flush()
        visibles = listar_perfiles(session, u.id)
        assert len(visibles) == 1
        assert visibles[0].id == p_activo.id
        # obtener_perfil sí lo devuelve (no filtra por activo)
        assert obtener_perfil(session, p_inactivo.id, u.id) is not None

    def test_eliminar_propio(self, session: Session):
        u = self._user(session)
        p = crear_perfil(session, u.id, "A eliminar", keywords=["x"])
        assert eliminar_perfil(session, p.id, u.id) is True
        session.flush()
        assert obtener_perfil(session, p.id, u.id) is None

    def test_eliminar_ajeno_devuelve_false(self, session: Session):
        u1 = self._user(session)
        u2 = Usuario(email="otro3@test.com", password_hash=_PW_HASH, activo=True)
        session.add(u2)
        session.flush()
        p = crear_perfil(session, u1.id, "No borrar", keywords=["x"])
        assert eliminar_perfil(session, p.id, u2.id) is False


# ---------------------------------------------------------------------------
# 3. Tests FTS — requieren Postgres con migración aplicada
# ---------------------------------------------------------------------------


@needs_postgres
class TestMatchFTS:
    """Tests de matching con FTS real en Postgres.

    Prerequisito: DATABASE_URL apunta a un Postgres con `alembic upgrade head` ejecutado.
    """

    @pytest.fixture()
    def pg_engine(self):
        import app.models.tables  # noqa: F401
        from app.models.base import Base

        e = create_engine(_DB_URL)
        # Crear tablas que falten (la migración ya debe haber creado tsv + funciones)
        # Solo creamos si no existen para evitar borrar datos de otra suite
        Base.metadata.create_all(e, checkfirst=True)
        yield e
        e.dispose()

    @pytest.fixture()
    def pg_session(self, pg_engine):
        with Session(pg_engine) as s:
            yield s

    @pytest.fixture()
    def ds(self, pg_session):
        """Dataset completo cargado en la sesión Postgres."""
        data = crear_dataset(pg_session)
        pg_session.commit()
        yield data
        # Limpiar después del test (borrar por email de los users creados)
        for u in [data["users"]["a"], data["users"]["b"]]:
            obj = pg_session.get(Usuario, u.id)
            if obj:
                pg_session.delete(obj)
        pg_session.commit()

    def test_match_tilde_insensitive(self, pg_session, ds):
        """Keyword 'electrico' (sin tilde) debe encontrar 'Suministro material electrico'."""
        perfil = ds["perfiles"]["a2"]  # keywords=["eléctrico"], solo licitaciones
        match_perfil(perfil, pg_session, ahora=AHORA)
        codigos = [
            m.codigo_oportunidad
            for m in pg_session.execute(
                select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
            ).scalars()
        ]
        assert "LIC-TILDE" in codigos

    def test_match_keyword_en_producto(self, pg_session, ds):
        """LIC-PRODUCTO tiene 'Cable electrico' solo en item — debe matchear por el EXISTS subquery."""
        perfil = ds["perfiles"]["a2"]  # keywords=["eléctrico"], solo licitaciones
        match_perfil(perfil, pg_session, ahora=AHORA)
        codigos = [
            m.codigo_oportunidad
            for m in pg_session.execute(
                select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
            ).scalars()
        ]
        assert "LIC-PRODUCTO" in codigos

    def test_match_exclusion_descarta(self, pg_session, ds):
        """LIC-EXCLUIDO contiene keyword excluida → no debe quedar en matches de PERFIL-A1."""
        perfil = ds["perfiles"]["a1"]  # excluir=["excluido"]
        match_perfil(perfil, pg_session, ahora=AHORA)
        codigos = [
            m.codigo_oportunidad
            for m in pg_session.execute(
                select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
            ).scalars()
        ]
        assert "LIC-EXCLUIDO" not in codigos

    def test_match_monto_fuera_rango_descartado(self, pg_session, ds):
        """LIC-MONTO-BAJO (50k) < monto_min A1 (100k) → descartado."""
        perfil = ds["perfiles"]["a1"]
        result = match_perfil(perfil, pg_session, ahora=AHORA)
        assert result["descartados"] >= 1
        codigos = [
            m.codigo_oportunidad
            for m in pg_session.execute(
                select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
            ).scalars()
        ]
        assert "LIC-MONTO-BAJO" not in codigos

    def test_match_monto_null_pasa_con_razon(self, pg_session, ds):
        """LIC-MONTO-NULL: monto=None → pasa filtro, razones.monto_no_informado=True."""
        perfil = ds["perfiles"]["a1"]
        match_perfil(perfil, pg_session, ahora=AHORA)
        match = pg_session.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.codigo_oportunidad == "LIC-MONTO-NULL",
            )
        ).scalar_one_or_none()
        assert match is not None
        assert match.razones.get("monto_no_informado") is True

    def test_match_ca_otra_region_descartada(self, pg_session, ds):
        """CA-OTRA-REGION (región 7) → descartada para PERFIL-B1 (región 13)."""
        perfil = ds["perfiles"]["b1"]  # regiones=[13]
        match_perfil(perfil, pg_session, ahora=AHORA)
        codigos = [
            m.codigo_oportunidad
            for m in pg_session.execute(
                select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
            ).scalars()
        ]
        assert "CA-OTRA-REGION" not in codigos

    def test_match_bonus_nombre_campo_hit(self, pg_session, ds):
        """LIC-NOMBRE-BONUS: keyword en nombre → campo_hit='nombre' en razones."""
        perfil = ds["perfiles"]["a2"]
        match_perfil(perfil, pg_session, ahora=AHORA)
        match = pg_session.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.codigo_oportunidad == "LIC-NOMBRE-BONUS",
            )
        ).scalar_one_or_none()
        assert match is not None
        assert match.razones.get("campo_hit") == "nombre"

    def test_match_orden_score_descendente(self, pg_session, ds):
        """PERFIL-B1: CA-0OF debe tener mayor score que CA-CIERRE-1DIA."""
        perfil = ds["perfiles"]["b1"]
        match_perfil(perfil, pg_session, ahora=AHORA)
        matches = list(
            pg_session.execute(
                select(OportunidadMatch)
                .where(OportunidadMatch.perfil_id == perfil.id)
                .order_by(OportunidadMatch.score.desc())
            ).scalars()
        )
        codigos_ordenados = [m.codigo_oportunidad for m in matches]
        assert codigos_ordenados.index("CA-0OF") < codigos_ordenados.index("CA-CIERRE-1DIA")

    def test_match_ca_0_ofertas_score_maximo_competencia(self, pg_session, ds):
        """CA-0OF: 0 ofertas → score_competencia=15, score total más alto."""
        perfil = ds["perfiles"]["b1"]
        match_perfil(perfil, pg_session, ahora=AHORA)
        match = pg_session.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.codigo_oportunidad == "CA-0OF",
            )
        ).scalar_one_or_none()
        assert match is not None
        # 60 (texto+bonus) + 25 (urgencia 4d) + 15 (0 ofertas) = 100
        assert match.score == pytest.approx(100.0)

    def test_match_ca_urgencia_cero_menos_2dias(self, pg_session, ds):
        """CA-CIERRE-1DIA (<2 días): score_urgencia=0."""
        perfil = ds["perfiles"]["b1"]
        match_perfil(perfil, pg_session, ahora=AHORA)
        match = pg_session.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.codigo_oportunidad == "CA-CIERRE-1DIA",
            )
        ).scalar_one_or_none()
        assert match is not None
        assert match.razones["dias_al_cierre"] < 2.0

    def test_ownership_perfil_a_no_visible_para_owner_b(self, pg_session, ds):
        """Los matches de PERFIL-A1 (owner A) no son accesibles para owner B."""
        perfil_a1 = ds["perfiles"]["a1"]
        perfil_b1 = ds["perfiles"]["b1"]
        match_perfil(perfil_a1, pg_session, ahora=AHORA)

        # Owner B no puede obtener el perfil de A
        assert obtener_perfil(pg_session, perfil_a1.id, ds["users"]["b"].id) is None

        # Los matches de A1 no aparecen bajo perfil_b1
        matches_b1 = list(
            pg_session.execute(
                select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil_b1.id)
            ).scalars()
        )
        matches_a1 = list(
            pg_session.execute(
                select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil_a1.id)
            ).scalars()
        )
        # Cada match tiene solo el perfil_id correcto — no hay cross-contamination
        assert all(m.perfil_id == perfil_a1.id for m in matches_a1)
        assert all(m.perfil_id == perfil_b1.id for m in matches_b1)

    def test_match_todos_procesa_todos_perfiles(self, pg_session, ds):
        """match_todos corre para los 3 perfiles activos del dataset."""
        result = match_todos(pg_session, ahora=AHORA)
        assert result["perfiles_procesados"] == 3

    def test_match_upsert_idempotente(self, pg_session, ds):
        """Ejecutar match_perfil dos veces no duplica matches."""
        perfil = ds["perfiles"]["b1"]
        match_perfil(perfil, pg_session, ahora=AHORA)
        matches_1 = pg_session.execute(
            select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
        ).scalars().all()
        n1 = len(matches_1)

        match_perfil(perfil, pg_session, ahora=AHORA)
        matches_2 = pg_session.execute(
            select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
        ).scalars().all()
        assert len(matches_2) == n1

    def test_sin_detalle_lista_codigos_sin_raw_json(self, pg_session, ds):
        """sin_detalle_* contiene los códigos de oportunidades sin raw_json."""
        perfil = ds["perfiles"]["a2"]  # solo licitaciones
        result = match_perfil(perfil, pg_session, ahora=AHORA)
        # Ninguna licitación del dataset tiene raw_json → todos en sin_detalle
        assert len(result["sin_detalle_licitaciones"]) > 0


# ---------------------------------------------------------------------------
# 4. Tests de scoring privado y match_perfil/match_todos (SQLite, candidatos mockeados)
# ---------------------------------------------------------------------------

_PW_HASH2 = "$2b$12$fakehashfortestsislong.enough.xyz12345"
_AHORA_LOCAL = datetime(2026, 6, 16, 10, 0)


def _make_lic(session: Session, codigo: str, nombre: str = "Test", cierre_dias: int = 5) -> Licitacion:  # type: ignore[name-defined]  # noqa: F821
    from app.models.tables import Licitacion

    lic = Licitacion(
        codigo=codigo,
        nombre=nombre,
        descripcion="descripcion de prueba",
        estado="publicada",
        fecha_cierre=_AHORA_LOCAL + timedelta(days=cierre_dias),
        monto_clp=500_000.0,
        raw_json=None,
    )
    session.add(lic)
    session.flush()
    return lic


def _make_ca(session: Session, codigo: str, nombre: str = "CA Test", cierre_dias: int = 5, region: int = 13, ofertas: int = 0) -> CompraAgil:  # type: ignore[name-defined]  # noqa: F821
    from app.models.tables import CompraAgil

    ca = CompraAgil(
        codigo=codigo,
        nombre=nombre,
        descripcion="desc CA",
        estado="publicada",
        fecha_cierre=_AHORA_LOCAL + timedelta(days=cierre_dias),
        monto_disponible_clp=200_000.0,
        region=region,
        total_ofertas=ofertas,
        raw_json=None,
    )
    session.add(ca)
    session.flush()
    return ca


class TestScoreLicitacion:
    def test_score_con_keyword_en_nombre(self, session: Session):
        lic = _make_lic(session, "LIC-SC1", nombre="material eléctrico", cierre_dias=5)
        keywords = ["eléctrico"]
        score, razones = _score_licitacion(lic, keywords, _AHORA_LOCAL)
        assert score > 0
        assert razones["campo_hit"] == "nombre"
        assert razones["dias_al_cierre"] == pytest.approx(5.0, abs=0.1)

    def test_score_con_keyword_en_descripcion(self, session: Session):
        lic = _make_lic(session, "LIC-SC2", nombre="compra servicios", cierre_dias=10)
        # La descripcion contiene "prueba" (ver _make_lic)
        keywords = ["prueba"]
        score, razones = _score_licitacion(lic, keywords, _AHORA_LOCAL)
        assert razones["campo_hit"] == "descripcion"

    def test_score_sin_fecha_cierre(self, session: Session):
        from app.models.tables import Licitacion

        lic = Licitacion(codigo="LIC-NODATE", nombre="sin fecha", descripcion="", estado="publicada", fecha_cierre=None)
        session.add(lic)
        session.flush()
        _, razones = _score_licitacion(lic, ["algo"], _AHORA_LOCAL)
        assert razones["dias_al_cierre"] == 0.0

    def test_campo_hit_desconocido(self, session: Session):
        lic = _make_lic(session, "LIC-SC3", nombre="sin match", cierre_dias=5)
        _, razones = _score_licitacion(lic, ["xyznotfound"], _AHORA_LOCAL)
        assert razones["campo_hit"] == "desconocido"


class TestScoreCa:
    def test_score_ca_con_keyword(self, session: Session):
        ca = _make_ca(session, "CA-SC1", nombre="silla ergonómica", cierre_dias=4, ofertas=0)
        score, razones = _score_ca(ca, ["ergonómica"], _AHORA_LOCAL)
        assert score > 0
        assert razones["campo_hit"] == "nombre"
        assert razones["ofertas"] == 0

    def test_score_ca_campo_hit_descripcion(self, session: Session):
        ca = _make_ca(session, "CA-SC2", nombre="compra varios", cierre_dias=4)
        score, razones = _score_ca(ca, ["CA"], _AHORA_LOCAL)
        assert razones["campo_hit"] in ("nombre", "descripcion", "desconocido")

    def test_score_ca_sin_fecha_cierre(self, session: Session):
        from app.models.tables import CompraAgil

        ca = CompraAgil(codigo="CA-NODATE", nombre="sin fecha", descripcion="", estado="publicada", fecha_cierre=None, total_ofertas=0)
        session.add(ca)
        session.flush()
        _, razones = _score_ca(ca, ["algo"], _AHORA_LOCAL)
        assert razones["dias_al_cierre"] == 0.0


class TestUpsertMatch:
    def test_nuevo_match_retorna_true(self, session: Session):
        from app.models.tables import OportunidadMatch, PerfilBusqueda, Usuario

        u = Usuario(email="u@test.cl", password_hash=_PW_HASH2, activo=True)
        session.add(u)
        session.flush()
        p = PerfilBusqueda(owner_id=u.id, nombre="P", keywords=["k"], activo=True)
        session.add(p)
        session.flush()

        es_nuevo = _upsert_match(session, p.id, "licitaciones", "LIC-NEW", 80.0, {"k": "v"}, _AHORA_LOCAL)
        assert es_nuevo is True

        match = session.execute(select(OportunidadMatch).where(OportunidadMatch.perfil_id == p.id)).scalar_one()
        assert match.score == 80.0

    def test_match_existente_actualiza_score(self, session: Session):
        from app.models.tables import OportunidadMatch, PerfilBusqueda, Usuario

        u = Usuario(email="u2@test.cl", password_hash=_PW_HASH2, activo=True)
        session.add(u)
        session.flush()
        p = PerfilBusqueda(owner_id=u.id, nombre="P2", keywords=["k"], activo=True)
        session.add(p)
        session.flush()

        _upsert_match(session, p.id, "licitaciones", "LIC-UPD", 70.0, {}, _AHORA_LOCAL)
        session.flush()
        es_nuevo = _upsert_match(session, p.id, "licitaciones", "LIC-UPD", 90.0, {"nuevo": True}, _AHORA_LOCAL)
        assert es_nuevo is False

        match = session.execute(select(OportunidadMatch).where(OportunidadMatch.perfil_id == p.id)).scalar_one()
        assert match.score == 90.0


class TestMatchPerfilMockedCandidatos:
    """Testea match_perfil con _candidatos_* mockeados para no requerir Postgres."""

    def _perfil(self, session: Session, keywords=None, regiones=None, fuentes=None, monto_min=None, monto_max=None):
        from app.models.tables import PerfilBusqueda, Usuario

        u = Usuario(email=f"mp{id(session)}@test.cl", password_hash=_PW_HASH2, activo=True)
        session.add(u)
        session.flush()
        p = PerfilBusqueda(
            owner_id=u.id,
            nombre="Test",
            keywords=keywords or ["eléctrico"],
            regiones=regiones,
            fuentes=fuentes or ["licitaciones", "compras_agiles"],
            monto_min_clp=monto_min,
            monto_max_clp=monto_max,
            activo=True,
        )
        session.add(p)
        session.flush()
        return p

    def test_match_perfil_con_licitacion(self, session: Session):
        lic = _make_lic(session, "LIC-MP1", nombre="material eléctrico")
        perfil = self._perfil(session)

        with patch("app.matching.engine._candidatos_licitaciones", return_value=[lic]), \
             patch("app.matching.engine._candidatos_ca", return_value=[]):
            result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert result["nuevos"] == 1
        assert "LIC-MP1" in result["sin_detalle_licitaciones"]

    def test_match_perfil_descarta_por_monto_min(self, session: Session):
        lic = _make_lic(session, "LIC-MP2")
        lic.monto_clp = 50_000.0  # < monto_min
        perfil = self._perfil(session, monto_min=100_000.0)

        with patch("app.matching.engine._candidatos_licitaciones", return_value=[lic]), \
             patch("app.matching.engine._candidatos_ca", return_value=[]):
            result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert result["descartados"] == 1
        assert result["nuevos"] == 0

    def test_match_perfil_descarta_por_monto_max(self, session: Session):
        lic = _make_lic(session, "LIC-MP3")
        lic.monto_clp = 5_000_000.0  # > monto_max
        perfil = self._perfil(session, monto_max=1_000_000.0)

        with patch("app.matching.engine._candidatos_licitaciones", return_value=[lic]), \
             patch("app.matching.engine._candidatos_ca", return_value=[]):
            result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert result["descartados"] == 1

    def test_match_perfil_monto_none_pasa(self, session: Session):
        from app.models.tables import Licitacion

        lic = Licitacion(codigo="LIC-MP4", nombre="eléctrico", descripcion="", estado="publicada",
                         fecha_cierre=_AHORA_LOCAL + timedelta(days=5), monto_clp=None, raw_json=None)
        session.add(lic)
        session.flush()
        perfil = self._perfil(session, monto_min=100_000.0)

        with patch("app.matching.engine._candidatos_licitaciones", return_value=[lic]), \
             patch("app.matching.engine._candidatos_ca", return_value=[]):
            result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert result["nuevos"] == 1

    def test_match_perfil_ca_filtro_region(self, session: Session):
        ca_rm = _make_ca(session, "CA-MP1", region=13)
        ca_otra = _make_ca(session, "CA-MP2", region=7)
        perfil = self._perfil(session, regiones=[13])

        with patch("app.matching.engine._candidatos_licitaciones", return_value=[]), \
             patch("app.matching.engine._candidatos_ca", return_value=[ca_rm, ca_otra]):
            result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert result["descartados"] == 1
        assert result["nuevos"] == 1

    def test_match_perfil_ca_monto_none_pasa(self, session: Session):
        from app.models.tables import CompraAgil

        ca = CompraAgil(codigo="CA-MP3", nombre="ergonómica", descripcion="", estado="publicada",
                        fecha_cierre=_AHORA_LOCAL + timedelta(days=5), monto_disponible_clp=None,
                        region=13, total_ofertas=0, raw_json=None)
        session.add(ca)
        session.flush()
        perfil = self._perfil(session, fuentes=["compras_agiles"], monto_min=100_000.0)

        with patch("app.matching.engine._candidatos_licitaciones", return_value=[]), \
             patch("app.matching.engine._candidatos_ca", return_value=[ca]):
            result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert result["nuevos"] == 1

    def test_match_perfil_upsert_idempotente_sqlite(self, session: Session):
        lic = _make_lic(session, "LIC-MP5", nombre="eléctrico")
        perfil = self._perfil(session)

        with patch("app.matching.engine._candidatos_licitaciones", return_value=[lic]), \
             patch("app.matching.engine._candidatos_ca", return_value=[]):
            r1 = match_perfil(perfil, session, ahora=_AHORA_LOCAL)
            r2 = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert r1["nuevos"] == 1
        assert r2["nuevos"] == 0
        assert r2["actualizados"] == 1

    def test_match_todos_corre_perfiles_activos(self, session: Session):
        from app.models.tables import PerfilBusqueda, Usuario

        u = Usuario(email="mt@test.cl", password_hash=_PW_HASH2, activo=True)
        session.add(u)
        session.flush()
        for i in range(2):
            session.add(PerfilBusqueda(owner_id=u.id, nombre=f"P{i}", keywords=["k"], activo=True))
        session.flush()

        with patch("app.matching.engine._candidatos_licitaciones", return_value=[]), \
             patch("app.matching.engine._candidatos_ca", return_value=[]):
            result = match_todos(session, ahora=_AHORA_LOCAL)

        assert result["perfiles_procesados"] == 2

    def test_match_todos_maneja_error_en_perfil(self, session: Session):
        from app.models.tables import PerfilBusqueda, Usuario

        u = Usuario(email="mterr@test.cl", password_hash=_PW_HASH2, activo=True)
        session.add(u)
        session.flush()
        session.add(PerfilBusqueda(owner_id=u.id, nombre="P", keywords=["k"], activo=True))
        session.flush()

        with patch("app.matching.engine._candidatos_licitaciones", side_effect=RuntimeError("fallo")), \
             patch("app.matching.engine._candidatos_ca", return_value=[]):
            result = match_todos(session, ahora=_AHORA_LOCAL)

        # match_todos captura errores internamente
        assert result["perfiles_procesados"] == 1
