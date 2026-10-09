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
    _candidatos_ca,
    _candidatos_licitaciones,
    _rubros_hit,
    _score_ca,
    _score_licitacion,
    _upsert_match,
    match_perfil,
    match_todos,
    relevancia,
)
from app.matching.perfiles import (
    PerfilInvalido,
    actualizar_perfil,
    crear_perfil,
    eliminar_perfil,
    listar_perfiles,
    obtener_perfil,
)
from app.matching.text import build_exclude_tsquery, build_tsquery, keywords_validas
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


class TestRelevancia:
    """F-match-1: la relevancia no depende de cuántas keywords tenga el perfil ni
    de la urgencia/competencia (esas ya no suman)."""

    def test_hit_en_nombre_base_50(self):
        assert relevancia(["cable"], True, False, False) == 50.0

    def test_hit_solo_en_item_o_descripcion_base_35(self):
        assert relevancia(["cable"], False, False, False) == 35.0

    def test_keyword_adicional_suma_10_con_tope_20(self):
        assert relevancia(["a", "b"], True, False, False) == 60.0
        assert relevancia(["a", "b", "c"], True, False, False) == 70.0
        assert relevancia(["a", "b", "c", "d", "e"], True, False, False) == 70.0

    def test_keywords_repetidas_no_suman(self):
        assert relevancia(["a", "a"], True, False, False) == 50.0

    def test_rubro_suma_20_y_organismo_15(self):
        assert relevancia(["a"], True, True, False) == 70.0
        assert relevancia(["a"], True, False, True) == 65.0
        assert relevancia(["a"], False, True, True) == 70.0  # 35 + 20 + 15

    def test_sin_keyword_con_rubro_o_organismo_base_40(self):
        assert relevancia([], False, True, False) == 40.0
        assert relevancia([], False, False, True) == 40.0

    def test_sin_keyword_con_rubro_y_organismo_55(self):
        assert relevancia([], False, True, True) == 55.0

    def test_sin_nada_es_cero(self):
        assert relevancia([], False, False, False) == 0.0

    def test_tope_100(self):
        assert relevancia(["a", "b", "c"], True, True, True) == 100.0
        assert relevancia(["a", "b", "c", "d"], True, True, True) == 100.0

    def test_perfil_de_20_keywords_con_un_acierto_en_el_nombre_da_50(self):
        # Antes: 1/20 × 60 + 5 = 8. El denominador ya no se usa.
        assert relevancia(["kw1"], True, False, False) == 50.0


class TestRubrosHit:
    def test_sin_categorias_no_hay_hits(self):
        assert _rubros_hit([], ["43211500"]) == []

    def test_prefijo_matchea_codigo_mas_largo(self):
        assert _rubros_hit(["4321"], ["43211500", "99999999"]) == ["4321"]

    def test_multiples_prefijos_solo_devuelve_los_que_matchean(self):
        hits = _rubros_hit(["4321", "1010"], ["43211500"])
        assert hits == ["4321"]

    def test_codigos_vacios_o_none_no_rompen(self):
        assert _rubros_hit(["4321"], ["", None]) == []  # type: ignore[list-item]

    def test_sin_codigos_producto(self):
        assert _rubros_hit(["4321"], []) == []


class TestKeywordsValidas:
    def test_quita_el_guion_inicial_suelto(self):
        # `-algo` sin comillas volvería la tsquery "todo menos algo".
        assert keywords_validas(["-algo", " - otra ", "--x"]) == ["algo", "otra", "x"]

    def test_un_guion_solo_se_descarta(self):
        assert keywords_validas(["-", "  ", "ok"]) == ["ok"]

    def test_el_guion_interno_se_conserva(self):
        assert keywords_validas(["sub-total"]) == ["sub-total"]
        assert build_tsquery(["-aseo", "cable"]) == "aseo OR cable"


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


class TestPerfilesCRUDRubrosOrganismos:
    """F9b: categorias_unspsc y organismos_seguidos como criterio mínimo válido."""

    def _user(self, session: Session) -> Usuario:
        u = Usuario(email=f"rubro{id(session)}@test.com", password_hash=_PW_HASH, activo=True)
        session.add(u)
        session.flush()
        return u

    def test_crear_perfil_solo_rubro(self, session: Session):
        u = self._user(session)
        p = crear_perfil(session, u.id, "Solo rubro", categorias_unspsc=["4321"])
        assert p.id is not None
        assert list(p.categorias_unspsc) == ["4321"]  # type: ignore[arg-type]

    def test_crear_perfil_solo_organismo(self, session: Session):
        u = self._user(session)
        p = crear_perfil(session, u.id, "Solo organismo", organismos_seguidos=["ORG-1"])
        assert p.id is not None
        assert list(p.organismos_seguidos) == ["ORG-1"]  # type: ignore[arg-type]

    def test_crear_perfil_sin_nada_sigue_invalido(self, session: Session):
        u = self._user(session)
        with pytest.raises(PerfilInvalido):
            crear_perfil(session, u.id, "Vacío total")

    def test_actualizar_perfil_persiste_rubros_y_organismos(self, session: Session):
        u = self._user(session)
        p = crear_perfil(session, u.id, "Editable", keywords=["x"])
        actualizar_perfil(
            session,
            p.id,
            u.id,
            categorias_unspsc=["4321"],
            organismos_seguidos=["ORG-2"],
        )
        session.flush()
        assert list(p.categorias_unspsc) == ["4321"]  # type: ignore[arg-type]
        assert list(p.organismos_seguidos) == ["ORG-2"]  # type: ignore[arg-type]


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
        """Dataset completo cargado en la sesión Postgres.

        Idempotente: limpia por código/email conocido ANTES y DESPUÉS de
        insertar, para autorepararse si una corrida anterior no llegó a su
        propio teardown (p.ej. el proceso se interrumpió) en vez de chocar
        con un UniqueViolation en la siguiente corrida. Las licitaciones/CA
        del dataset no cuelgan de Usuario (sin cascade), así que se limpian
        aparte por código.
        """
        from app.models.tables import CompraAgil, Licitacion
        from tests.fixtures.dataset_matching import CA_CODIGOS, LICITACION_CODIGOS, USER_EMAILS

        def _limpiar() -> None:
            for u in pg_session.execute(
                select(Usuario).where(Usuario.email.in_(USER_EMAILS))
            ).scalars():
                pg_session.delete(u)
            for codigo in LICITACION_CODIGOS:
                obj = pg_session.get(Licitacion, codigo)
                if obj:
                    pg_session.delete(obj)
            for codigo in CA_CODIGOS:
                obj_ca = pg_session.get(CompraAgil, codigo)
                if obj_ca:
                    pg_session.delete(obj_ca)
            pg_session.commit()

        _limpiar()
        data = crear_dataset(pg_session)
        pg_session.commit()
        try:
            yield data
        finally:
            _limpiar()

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
        match_perfil(perfil, pg_session, ahora=AHORA)
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

    def test_match_score_no_depende_de_urgencia_ni_ofertas(self, pg_session, ds):
        """PERFIL-B1: CA-0OF (0 ofertas, 4 días) y CA-CIERRE-1DIA (2 ofertas, <1 día)
        tienen la misma relevancia: lo urgente se ve en el orden del feed, no en el score."""
        perfil = ds["perfiles"]["b1"]
        match_perfil(perfil, pg_session, ahora=AHORA)
        puntajes = {
            m.codigo_oportunidad: m.score
            for m in pg_session.execute(
                select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
            ).scalars()
        }
        assert puntajes["CA-0OF"] == puntajes["CA-CIERRE-1DIA"]

    def test_match_ca_0_ofertas_score_maximo_competencia(self, pg_session, ds):
        """CA-0OF: 0 ofertas ya no suma al score (F-match-1): solo la relevancia textual."""
        perfil = ds["perfiles"]["b1"]
        match_perfil(perfil, pg_session, ahora=AHORA)
        match = pg_session.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.codigo_oportunidad == "CA-0OF",
            )
        ).scalar_one_or_none()
        assert match is not None
        # keyword en el nombre: base 50; ni urgencia (4 días) ni 0 ofertas suman
        assert match.score == pytest.approx(50.0)
        assert match.razones["ofertas"] == 0

    def test_match_ca_urgencia_cero_menos_2dias(self, pg_session, ds):
        """CA-CIERRE-1DIA (<2 días): la urgencia no es parte del score ni de las razones."""
        perfil = ds["perfiles"]["b1"]
        match_perfil(perfil, pg_session, ahora=AHORA)
        match = pg_session.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.codigo_oportunidad == "CA-CIERRE-1DIA",
            )
        ).scalar_one_or_none()
        assert match is not None
        assert "dias_al_cierre" not in match.razones
        assert match.score == pytest.approx(50.0)

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
        """match_todos corre para TODOS los perfiles activos de usuarios activos
        — no asume una BD vacía (la branch dev puede traer perfiles de otras
        pruebas/uso manual). Se aísla comparando un delta: los perfiles ajenos
        activos (los que había ANTES de sumar el dataset) más los 4 que crea
        el propio dataset, en vez de un total absoluto."""
        from app.models.tables import PerfilBusqueda

        ids_dataset = {p.id for p in ds["perfiles"].values()}
        otros_activos_antes = list(
            pg_session.execute(
                select(PerfilBusqueda)
                .join(PerfilBusqueda.owner)
                .where(
                    PerfilBusqueda.activo.is_(True),
                    Usuario.activo.is_(True),
                    PerfilBusqueda.id.notin_(ids_dataset),
                )
            ).scalars()
        )

        result = match_todos(pg_session, ahora=AHORA)

        assert result["perfiles_procesados"] == len(otros_activos_antes) + len(ids_dataset)

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

    def test_match_variante_morfologica_genero_sube_score(self, pg_session, ds):
        """F9c: keyword 'eléctrico' (masc. sing.) debe contar como hit contra
        'electricas' (fem. plural) — mismo stem 'electr' en FTS 'spanish'.
        Antes (score por substring) el recall encontraba LIC-MORFO-FEM pero el
        score la contaba con 0 keywords_hit; ahora deben quedar unificados."""
        perfil = ds["perfiles"]["a2"]  # keywords=["eléctrico"], solo licitaciones
        match_perfil(perfil, pg_session, ahora=AHORA)
        match = pg_session.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.codigo_oportunidad == "LIC-MORFO-FEM",
            )
        ).scalar_one_or_none()
        assert match is not None
        assert match.razones["keywords_hit"] == ["eléctrico"]
        assert match.razones["campo_hit"] == "nombre"
        # 1 keyword con acierto en el nombre: base 50 (urgencia y competencia ya no suman)
        assert match.score == pytest.approx(50.0)

    def test_match_variante_morfologica_plural_sube_score(self, pg_session, ds):
        """F9c: keyword 'aseo' (singular) debe contar como hit contra 'aseos'
        (plural) — mismo stem 'ase' en FTS 'spanish'."""
        perfil = ds["perfiles"]["a3"]  # keywords=["aseo"], solo compras_agiles
        match_perfil(perfil, pg_session, ahora=AHORA)
        match = pg_session.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.codigo_oportunidad == "CA-MORFO-ASEO",
            )
        ).scalar_one_or_none()
        assert match is not None
        assert match.razones["keywords_hit"] == ["aseo"]
        assert match.razones["campo_hit"] == "nombre"
        assert match.score > 0.0


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
    """F9c: keywords_hit/campo_hit ya vienen precomputados (FTS set-based, fuera
    de esta función) — _score_licitacion solo combina los subscores."""

    def test_score_con_keyword_en_nombre(self, session: Session):
        lic = _make_lic(session, "LIC-SC1", nombre="material eléctrico", cierre_dias=5)
        score, razones = _score_licitacion(lic, ["eléctrico"], "nombre")
        assert score == 50.0
        assert razones["campo_hit"] == "nombre"
        assert razones["hit_en_nombre"] is True
        assert "dias_al_cierre" not in razones

    def test_score_con_keyword_en_descripcion(self, session: Session):
        lic = _make_lic(session, "LIC-SC2", nombre="compra servicios", cierre_dias=10)
        score, razones = _score_licitacion(lic, ["prueba"], "descripcion")
        assert razones["campo_hit"] == "descripcion"
        assert razones["hit_en_nombre"] is False
        assert score == 35.0

    def test_hit_en_nombre_explicito_manda(self, session: Session):
        """`hit_en_nombre` explícito manda sobre lo que se deduzca de `campo_hit`."""
        lic = _make_lic(session, "LIC-SC2B", nombre="compra servicios")
        score, razones = _score_licitacion(lic, ["x"], "descripcion", hit_en_nombre=True)
        assert score == 50.0
        assert razones["hit_en_nombre"] is True

    def test_urgencia_no_cambia_el_score(self, session: Session):
        cerca = _make_lic(session, "LIC-URG1", nombre="material eléctrico", cierre_dias=3)
        lejos = _make_lic(session, "LIC-URG2", nombre="material eléctrico", cierre_dias=60)
        assert _score_licitacion(cerca, ["eléctrico"], "nombre")[0] == _score_licitacion(
            lejos, ["eléctrico"], "nombre"
        )[0]

    def test_score_sin_fecha_cierre(self, session: Session):
        from app.models.tables import Licitacion

        lic = Licitacion(codigo="LIC-NODATE", nombre="sin fecha", descripcion="", estado="publicada", fecha_cierre=None)
        session.add(lic)
        session.flush()
        score, razones = _score_licitacion(lic, [], "desconocido")
        assert score == 0.0
        assert "dias_al_cierre" not in razones

    def test_campo_hit_desconocido(self, session: Session):
        lic = _make_lic(session, "LIC-SC3", nombre="sin match", cierre_dias=5)
        _, razones = _score_licitacion(lic, [], "desconocido")
        assert razones["campo_hit"] == "desconocido"

    def test_categorias_hit_en_razones_y_score_sin_keywords(self, session: Session):
        """F9b: rubro-only (sin keywords) puntúa (base 40 de `relevancia`)."""
        from app.models.tables import LicitacionItem

        lic = _make_lic(session, "LIC-SCRUBRO", nombre="cosa neutra sin relacion")
        session.add(
            LicitacionItem(
                licitacion_codigo=lic.codigo, codigo_producto="43211500", nombre="laptop"
            )
        )
        session.flush()
        score, razones = _score_licitacion(lic, [], "desconocido", categorias_unspsc=["4321"])
        assert razones["categorias_hit"] == ["4321"]
        assert score == 40.0

    def test_organismo_seguido_en_razones_y_score_sin_keywords(self, session: Session):
        lic = _make_lic(session, "LIC-SCORG", nombre="cosa neutra")
        lic.codigo_organismo = "ORG-Y"
        session.flush()
        score, razones = _score_licitacion(lic, [], "desconocido", organismos_seguidos=["ORG-Y"])
        assert razones["organismo_seguido"] is True
        assert score == 40.0

    def test_sin_rubro_ni_organismo_no_aparecen_en_razones(self, session: Session):
        lic = _make_lic(session, "LIC-SCNONE", nombre="sin match", cierre_dias=5)
        _, razones = _score_licitacion(lic, [], "desconocido")
        assert "categorias_hit" not in razones
        assert "organismo_seguido" not in razones


class TestScoreCa:
    """F9c: keywords_hit/campo_hit ya vienen precomputados (FTS set-based, fuera
    de esta función) — _score_ca solo combina los subscores."""

    def test_score_ca_con_keyword(self, session: Session):
        ca = _make_ca(session, "CA-SC1", nombre="silla ergonómica", cierre_dias=4, ofertas=0)
        score, razones = _score_ca(ca, ["ergonómica"], "nombre")
        assert score == 50.0
        assert razones["campo_hit"] == "nombre"
        assert razones["ofertas"] == 0
        assert "dias_al_cierre" not in razones

    def test_ofertas_no_cambian_el_score(self, session: Session):
        sin = _make_ca(session, "CA-OF0", nombre="silla", ofertas=0)
        con = _make_ca(session, "CA-OF9", nombre="silla", ofertas=9)
        assert _score_ca(sin, ["silla"], "nombre")[0] == _score_ca(con, ["silla"], "nombre")[0]

    def test_score_ca_campo_hit_descripcion(self, session: Session):
        ca = _make_ca(session, "CA-SC2", nombre="compra varios", cierre_dias=4)
        score, razones = _score_ca(ca, ["CA"], "descripcion")
        assert razones["campo_hit"] == "descripcion"
        assert score == 35.0

    def test_score_ca_sin_fecha_cierre(self, session: Session):
        from app.models.tables import CompraAgil

        ca = CompraAgil(codigo="CA-NODATE", nombre="sin fecha", descripcion="", estado="publicada", fecha_cierre=None, total_ofertas=0)
        session.add(ca)
        session.flush()
        score, razones = _score_ca(ca, [], "desconocido")
        assert score == 0.0
        assert "dias_al_cierre" not in razones

    def test_ca_categorias_hit_en_razones_sin_keywords(self, session: Session):
        from app.models.tables import CaProducto

        ca = _make_ca(session, "CA-SCRUBRO", nombre="cosa neutra")
        session.add(
            CaProducto(ca_codigo=ca.codigo, codigo_producto="43211500", nombre="laptop")
        )
        session.flush()
        score, razones = _score_ca(ca, [], "desconocido", categorias_unspsc=["4321"])
        assert razones["categorias_hit"] == ["4321"]
        assert score == 40.0

    def test_ca_organismo_seguido_en_razones_sin_keywords(self, session: Session):
        ca = _make_ca(session, "CA-SCORG", nombre="cosa neutra")
        ca.organismo_rut = "76999999-9"
        session.flush()
        score, razones = _score_ca(ca, [], "desconocido", organismos_ruts=["76999999-9"])
        assert razones["organismo_seguido"] is True
        assert score == 40.0

    def test_ca_organismo_seguido_compara_rut_con_o_sin_puntos(self, session: Session):
        ca = _make_ca(session, "CA-SCORG2", nombre="cosa neutra")
        ca.organismo_rut = "76.999.999-9"
        session.flush()
        _, razones = _score_ca(ca, [], "desconocido", organismos_ruts=["769999999"])
        assert razones["organismo_seguido"] is True
        ca.organismo_rut = "7.699.999-K"
        _, razones = _score_ca(ca, [], "desconocido", organismos_ruts=["7699999-k"])
        assert razones["organismo_seguido"] is True


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

        primera_fecha = _AHORA_LOCAL - timedelta(days=10)
        segunda_fecha = _AHORA_LOCAL
        _upsert_match(session, p.id, "licitaciones", "LIC-UPD", 70.0, {}, primera_fecha)
        session.flush()
        es_nuevo = _upsert_match(
            session, p.id, "licitaciones", "LIC-UPD", 90.0, {"nuevo": True}, segunda_fecha
        )
        assert es_nuevo is False

        match = session.execute(select(OportunidadMatch).where(OportunidadMatch.perfil_id == p.id)).scalar_one()
        assert match.score == 90.0
        assert match.razones == {"nuevo": True}
        assert match.fecha_match == primera_fecha


class TestMatchPerfilMockedCandidatos:
    """Testea match_perfil con _candidatos_* mockeados para no requerir Postgres."""

    @pytest.fixture(autouse=True)
    def _sin_limpieza(self):
        # F-guardar: match_perfil termina con limpiar_matches_perfil, que usa los
        # mismos fragmentos FTS de Postgres que los candidatos mockeados acá.
        with patch("app.matching.engine.limpiar_matches_perfil", return_value=0):
            yield

    def _perfil(self, session: Session, keywords=None, regiones=None, fuentes=None, monto_min=None, monto_max=None):
        from app.models.tables import PerfilBusqueda, Usuario

        u = Usuario(email=f"mp{id(session)}@test.cl", password_hash=_PW_HASH2, activo=True)
        session.add(u)
        session.flush()
        p = PerfilBusqueda(
            owner_id=u.id,
            nombre="Test",
            keywords=keywords if keywords is not None else ["eléctrico"],
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
             patch("app.matching.engine._candidatos_ca", return_value=[]), \
             patch("app.matching.engine._hits_licitaciones", return_value={}):
            result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert result["nuevos"] == 1
        assert "LIC-MP1" in result["sin_detalle_licitaciones"]

    # Región y monto se filtran en el SQL de candidatos (F-match-1), no en Python:
    # estos tests usan los candidatos reales (SQL estándar, sin keywords no hay FTS).

    def _codigos_match(self, session: Session, perfil) -> set[str]:
        from app.models.tables import OportunidadMatch

        return {
            m.codigo_oportunidad
            for m in session.execute(
                select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
            ).scalars()
        }

    def test_match_perfil_descarta_por_monto_min(self, session: Session):
        lic = _make_lic(session, "LIC-MP2")
        lic.monto_clp = 50_000.0  # < monto_min
        perfil = self._perfil(session, keywords=[], monto_min=100_000.0)

        result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert result["nuevos"] == 0
        assert "LIC-MP2" not in self._codigos_match(session, perfil)

    def test_match_perfil_descarta_por_monto_max(self, session: Session):
        lic = _make_lic(session, "LIC-MP3")
        lic.monto_clp = 5_000_000.0  # > monto_max
        perfil = self._perfil(session, keywords=[], monto_max=1_000_000.0)

        match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert "LIC-MP3" not in self._codigos_match(session, perfil)

    def test_match_perfil_monto_none_pasa(self, session: Session):
        from app.models.tables import Licitacion, OportunidadMatch

        lic = Licitacion(codigo="LIC-MP4", nombre="eléctrico", descripcion="", estado="publicada",
                         fecha_cierre=_AHORA_LOCAL + timedelta(days=5), monto_clp=None, raw_json=None)
        session.add(lic)
        session.flush()
        perfil = self._perfil(session, keywords=[], monto_min=100_000.0)

        result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert result["nuevos"] == 1
        m = session.execute(
            select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
        ).scalar_one()
        assert m.razones["monto_no_informado"] is True

    def test_match_perfil_ca_filtro_region(self, session: Session):
        _make_ca(session, "CA-MP1", region=13)
        _make_ca(session, "CA-MP2", region=7)
        perfil = self._perfil(session, keywords=[], regiones=[13])

        match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert self._codigos_match(session, perfil) == {"CA-MP1"}

    def test_match_perfil_licitacion_filtra_region_y_la_no_informada_pasa(self, session: Session):
        from app.models.tables import OportunidadMatch

        en = _make_lic(session, "LIC-REG-EN")
        en.region = 13
        otra = _make_lic(session, "LIC-REG-OTRA")
        otra.region = 7
        _make_lic(session, "LIC-REG-NULL")  # region NULL: pasa con la razón
        perfil = self._perfil(session, keywords=[], regiones=[13], fuentes=["licitaciones"])

        match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert self._codigos_match(session, perfil) == {"LIC-REG-EN", "LIC-REG-NULL"}
        sin_region = session.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.codigo_oportunidad == "LIC-REG-NULL",
            )
        ).scalar_one()
        assert sin_region.razones["region_no_informada"] is True

    def test_match_perfil_ca_monto_none_pasa(self, session: Session):
        from app.models.tables import CompraAgil

        ca = CompraAgil(codigo="CA-MP3", nombre="ergonómica", descripcion="", estado="publicada",
                        fecha_cierre=_AHORA_LOCAL + timedelta(days=5), monto_disponible_clp=None,
                        region=13, total_ofertas=0, raw_json=None)
        session.add(ca)
        session.flush()
        perfil = self._perfil(session, keywords=[], fuentes=["compras_agiles"], monto_min=100_000.0)

        result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)

        assert result["nuevos"] == 1

    def test_match_perfil_upsert_idempotente_sqlite(self, session: Session):
        lic = _make_lic(session, "LIC-MP5", nombre="eléctrico")
        perfil = self._perfil(session)

        with patch("app.matching.engine._candidatos_licitaciones", return_value=[lic]), \
             patch("app.matching.engine._candidatos_ca", return_value=[]), \
             patch("app.matching.engine._hits_licitaciones", return_value={}):
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


# ---------------------------------------------------------------------------
# 5. F9b: recall aditivo por rubro UNSPSC / organismo seguido (SQL real, SQLite)
#
# Sin keywords no se invoca FTS (websearch_to_tsquery, Postgres-only), así que
# _candidatos_* corren SQL estándar (EXISTS/LIKE/IN) y funcionan también en
# SQLite — no requieren @needs_postgres.
# ---------------------------------------------------------------------------


class TestCandidatosRecallAditivo:
    def test_candidatos_licitaciones_por_rubro_sin_keyword(self, session: Session):
        from app.models.tables import LicitacionItem

        lic = _make_lic(session, "LIC-RECALL1", nombre="cosa sin relacion")
        session.add(
            LicitacionItem(licitacion_codigo=lic.codigo, codigo_producto="43211500", nombre="laptop")
        )
        session.flush()

        candidatos = _candidatos_licitaciones(
            session, _AHORA_LOCAL, None, None, categorias_unspsc=["4321"]
        )
        assert "LIC-RECALL1" in [c.codigo for c in candidatos]

    def test_candidatos_licitaciones_sin_rubro_match_no_aparece(self, session: Session):
        from app.models.tables import LicitacionItem

        lic = _make_lic(session, "LIC-RECALL2", nombre="cosa sin relacion")
        session.add(
            LicitacionItem(licitacion_codigo=lic.codigo, codigo_producto="99999999", nombre="otro")
        )
        session.flush()

        candidatos = _candidatos_licitaciones(
            session, _AHORA_LOCAL, None, None, categorias_unspsc=["4321"]
        )
        assert "LIC-RECALL2" not in [c.codigo for c in candidatos]

    def test_candidatos_licitaciones_por_organismo_seguido(self, session: Session):
        lic = _make_lic(session, "LIC-RECALL3")
        lic.codigo_organismo = "ORG-X"
        session.flush()

        candidatos = _candidatos_licitaciones(
            session, _AHORA_LOCAL, None, None, organismos_seguidos=["ORG-X"]
        )
        assert "LIC-RECALL3" in [c.codigo for c in candidatos]

    def test_candidatos_licitaciones_organismo_no_seguido_no_aparece(self, session: Session):
        lic = _make_lic(session, "LIC-RECALL4")
        lic.codigo_organismo = "ORG-OTRO"
        session.flush()

        candidatos = _candidatos_licitaciones(
            session, _AHORA_LOCAL, None, None, organismos_seguidos=["ORG-X"]
        )
        assert "LIC-RECALL4" not in [c.codigo for c in candidatos]

    def test_candidatos_ca_por_rubro_sin_keyword(self, session: Session):
        from app.models.tables import CaProducto

        ca = _make_ca(session, "CA-RECALL1")
        session.add(
            CaProducto(ca_codigo=ca.codigo, codigo_producto="43211500", nombre="laptop")
        )
        session.flush()

        candidatos = _candidatos_ca(session, _AHORA_LOCAL, None, None, categorias_unspsc=["4321"])
        assert "CA-RECALL1" in [c.codigo for c in candidatos]

    def test_candidatos_ca_por_organismo_seguido(self, session: Session):
        ca = _make_ca(session, "CA-RECALL2")
        ca.organismo_rut = "76123456-7"
        session.flush()

        candidatos = _candidatos_ca(
            session, _AHORA_LOCAL, None, None, organismos_seguidos=["761234567"]
        )
        assert "CA-RECALL2" in [c.codigo for c in candidatos]

    def test_candidatos_ca_publicada_sin_fecha_cierre_es_candidata(self, session: Session):
        ca = _make_ca(session, "CA-RECALL-NULL-CIERRE")
        ca.fecha_cierre = None
        ca.fecha_publicacion = _AHORA_LOCAL - timedelta(days=1)  # sin cierre: vigente si es reciente
        session.flush()

        candidatos = _candidatos_ca(session, _AHORA_LOCAL, None, None)
        assert "CA-RECALL-NULL-CIERRE" in [c.codigo for c in candidatos]

    def test_candidatos_ca_cerrada_sin_fecha_cierre_no_es_candidata(self, session: Session):
        ca = _make_ca(session, "CA-RECALL-CERRADA")
        ca.estado = "cerrada"
        ca.fecha_cierre = None
        session.flush()

        candidatos = _candidatos_ca(session, _AHORA_LOCAL, None, None)
        assert "CA-RECALL-CERRADA" not in [c.codigo for c in candidatos]

    def test_candidatos_ca_publicada_con_fecha_futura_sigue_siendo_candidata(
        self, session: Session
    ):
        _make_ca(session, "CA-RECALL-FUTURA", cierre_dias=5)

        candidatos = _candidatos_ca(session, _AHORA_LOCAL, None, None)
        assert "CA-RECALL-FUTURA" in [c.codigo for c in candidatos]

    def test_candidatos_sin_criterios_devuelve_todo_como_antes(self, session: Session):
        """Sin keywords/rubros/organismos: comportamiento preexistente (sin filtro
        de inclusión, filtran región/monto localmente en match_perfil)."""
        _make_lic(session, "LIC-RECALL5")
        candidatos = _candidatos_licitaciones(session, _AHORA_LOCAL, None, None)
        assert "LIC-RECALL5" in [c.codigo for c in candidatos]


class TestMatchPerfilRecallAditivo:
    """match_perfil end-to-end (sin mocks) para perfiles rubro-only / organismo-only."""

    def test_match_perfil_rubro_only_sin_keywords(self, session: Session):
        from app.models.tables import LicitacionItem, PerfilBusqueda, Usuario

        u = Usuario(email="recall1@test.cl", password_hash=_PW_HASH2, activo=True)
        session.add(u)
        session.flush()
        perfil = PerfilBusqueda(
            owner_id=u.id,
            nombre="Rubro only",
            keywords=[],
            categorias_unspsc=["4321"],
            fuentes=["licitaciones"],
            activo=True,
        )
        session.add(perfil)
        session.flush()

        lic = _make_lic(session, "LIC-E2E-RUBRO", nombre="cosa neutra sin keyword")
        session.add(
            LicitacionItem(licitacion_codigo=lic.codigo, codigo_producto="43211500", nombre="laptop")
        )
        session.flush()

        result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)
        assert result["nuevos"] == 1
        match = session.execute(
            select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
        ).scalar_one()
        assert match.razones.get("categorias_hit") == ["4321"]
        assert match.score > 0.0

    def test_match_perfil_organismo_only_sin_keywords(self, session: Session):
        from app.models.tables import PerfilBusqueda, Usuario

        u = Usuario(email="recall2@test.cl", password_hash=_PW_HASH2, activo=True)
        session.add(u)
        session.flush()
        perfil = PerfilBusqueda(
            owner_id=u.id,
            nombre="Organismo only",
            keywords=[],
            organismos_seguidos=["ORG-SEGUIDO"],
            fuentes=["licitaciones"],
            activo=True,
        )
        session.add(perfil)
        session.flush()

        lic = _make_lic(session, "LIC-E2E-ORG", nombre="cosa neutra")
        lic.codigo_organismo = "ORG-SEGUIDO"
        session.flush()

        result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)
        assert result["nuevos"] == 1
        match = session.execute(
            select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil.id)
        ).scalar_one()
        assert match.razones.get("organismo_seguido") is True
        assert match.score > 0.0

    def test_match_perfil_ca_publicada_sin_fecha_cierre_genera_match(self, session: Session):
        from app.models.tables import PerfilBusqueda, Usuario

        u = Usuario(email="recall-ca-null@test.cl", password_hash=_PW_HASH2, activo=True)
        session.add(u)
        session.flush()
        perfil = PerfilBusqueda(
            owner_id=u.id,
            nombre="CA publicadas RM",
            keywords=[],
            regiones=[13],
            fuentes=["compras_agiles"],
            activo=True,
        )
        session.add(perfil)

        ca = _make_ca(session, "CA-E2E-NULL-CIERRE", region=13)
        ca.fecha_cierre = None
        ca.fecha_publicacion = _AHORA_LOCAL - timedelta(days=1)  # sin cierre: vigente si es reciente
        session.flush()

        result = match_perfil(perfil, session, ahora=_AHORA_LOCAL)
        assert result["nuevos"] == 1
        match = session.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.fuente == "compras_agiles",
                OportunidadMatch.codigo_oportunidad == "CA-E2E-NULL-CIERRE",
            )
        ).scalar_one()
        # Perfil solo de región: sin keyword, rubro ni organismo no hay señal de
        # relevancia (antes sumaba urgencia/competencia). Queda 0 y el piso del feed la oculta.
        assert match.score == 0.0


# ---------------------------------------------------------------------------
# match_todos: un perfil que falla no arrastra a los siguientes
# ---------------------------------------------------------------------------


def test_match_todos_hace_rollback_si_un_perfil_falla(session: Session):
    """El primer perfil deja la sesión con un flush fallido (IntegrityError):
    sin rollback, el segundo recibiría PendingRollbackError al usarla."""
    from app.models.tables import PerfilBusqueda

    u = Usuario(email="mt-rollback@test.cl", password_hash=_PW_HASH2, activo=True)
    session.add(u)
    session.flush()
    p_falla = PerfilBusqueda(owner_id=u.id, nombre="Falla", keywords=["x"], activo=True)
    p_ok = PerfilBusqueda(owner_id=u.id, nombre="Ok", keywords=["y"], activo=True)
    session.add_all([p_falla, p_ok])
    session.commit()
    id_falla, id_ok = p_falla.id, p_ok.id

    llamados: list[int] = []

    def _match_perfil(perfil, s, ahora=None):  # noqa: ARG001
        llamados.append(perfil.id)
        if perfil.id == id_falla:
            # Email duplicado: el flush revienta y deja la sesión abortada.
            s.add(Usuario(email="mt-rollback@test.cl", password_hash=_PW_HASH2, activo=True))
            s.flush()
        # El segundo perfil usa la sesión: falla si nadie hizo rollback.
        s.execute(select(OportunidadMatch)).all()
        return {
            "nuevos": 1,
            "actualizados": 0,
            "descartados": 0,
            "borrados": 0,
            "sin_detalle_licitaciones": [],
            "sin_detalle_ca": [],
        }

    with patch("app.matching.engine.match_perfil", side_effect=_match_perfil):
        r = match_todos(session, ahora=_AHORA_LOCAL)

    assert llamados == [id_falla, id_ok]
    assert r["perfiles_procesados"] == 2
    assert r["nuevos"] == 1  # solo el segundo perfil sumó
