"""Tests F-ca-explorar contra Postgres real: el vocabulario por rubro (unnest de
tsvector, lift, reemplazo) y los filtros de texto/rubro del explorador (FTS).

Requieren DATABASE_URL de DEV apuntando a un Postgres con las migraciones hasta
`d7f2a4c8b6e1` (la tabla `compras_agiles.tsv` es una columna GENERATED de la
migración inicial). NO requieren la migración de esta fase: `rubro_vocabulario`
se crea como tabla TEMPORAL de la conexión (que tapa a la real si existe), para
que estos tests no dejen una tabla permanente que después choque con
`alembic upgrade head`. Por eso el motor usa UNA sola conexión (StaticPool).

Todo dato de prueba lleva el prefijo `ZZEXP` (o la familia centinela 99xx, que no
existe en UNSPSC) y se borra al terminar. Las CA centinela se aíslan de las reales
con un organismo propio.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token
from app.catalogos.vocabulario_rubro import LEXEMA_RE, PALABRA_RE, construir_vocabulario
from app.core.db import normalizar_url_driver
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.explorador_ca import FiltrosExplorador, buscar, contar
from app.ingest.orchestrator import _LOCK_KEY, _run_with_lock, run_vocabulario_rubros
from app.models.enums import RolUsuario, ValorFeedback
from app.models.tables import (
    CaProducto,
    CompraAgil,
    JobRun,
    Licitacion,
    LicitacionItem,
    MatchFeedback,
    RubroFavorito,
    RubroVocabulario,
    Usuario,
)

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_POSTGRES = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")
_DB_URL_ENGINE = normalizar_url_driver(_DB_URL)
needs_postgres = pytest.mark.skipif(
    not _TIENE_POSTGRES,
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)

_ORG = "ZZEXP ORGANISMO DE PRUEBA"
_EMAIL = "zzexp-usuario@test.cl"
_FUTURO = datetime(2099, 1, 1)

_DDL_VOCABULARIO_TEMP = """
CREATE TEMPORARY TABLE rubro_vocabulario (
    prefijo varchar(8) NOT NULL,
    lexema varchar(60) NOT NULL,
    df_rubro integer NOT NULL,
    lift double precision NOT NULL,
    palabra varchar(60),
    actualizado_en timestamp NOT NULL DEFAULT now(),
    PRIMARY KEY (prefijo, lexema)
)
"""

# Sin FK a `usuarios`: una tabla temporal no puede referenciar una permanente.
_DDL_FAVORITOS_TEMP = """
CREATE TEMPORARY TABLE rubros_favoritos (
    id bigserial PRIMARY KEY,
    owner_id bigint NOT NULL,
    prefijo varchar(8) NOT NULL,
    creado_en timestamp NOT NULL DEFAULT now(),
    CONSTRAINT uq_rubro_favorito UNIQUE (owner_id, prefijo)
)
"""


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(_DB_URL_ENGINE, poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text(_DDL_VOCABULARIO_TEMP))
        conn.execute(text(_DDL_FAVORITOS_TEMP))
    yield engine
    engine.dispose()


def _limpiar(engine) -> None:
    with Session(engine) as s:
        s.execute(delete(RubroVocabulario))
        s.execute(delete(RubroFavorito))
        s.execute(delete(MatchFeedback).where(MatchFeedback.codigo_oportunidad.like("ZZEXP-%")))
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like("ZZEXP-%")))
        s.execute(delete(Licitacion).where(Licitacion.codigo.like("ZZEXP-%")))
        s.execute(delete(Usuario).where(Usuario.email == _EMAIL))
        s.commit()


@pytest.fixture()
def limpio(pg_engine):
    _limpiar(pg_engine)
    yield pg_engine
    _limpiar(pg_engine)


def _lexema(session: Session, palabra: str) -> str:
    """El lexema que Postgres le saca a la palabra (mismo camino que el job)."""
    return str(
        session.execute(
            text("SELECT lexeme FROM unnest(to_tsvector('spanish', inmutable_unaccent(:p))) AS x(lexeme, p, w)"),
            {"p": palabra},
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Vocabulario
# ---------------------------------------------------------------------------


def _sembrar_items(session: Session) -> None:
    lic = Licitacion(codigo="ZZEXP-LIC", nombre="lic de prueba")
    session.add(lic)
    session.flush()

    def item(codigo: str, nombre: str) -> None:
        session.add(LicitacionItem(licitacion_codigo="ZZEXP-LIC", codigo_producto=codigo, nombre=nombre))

    for i in range(10):
        item(f"9912{i:04d}", f"Cable coaxial N{i} servicio")
        item(f"9921{i:04d}", f"Tornillo acero N{i} servicio")
    item("99120099", "Unicornio dorado")
    item("99120098", "Unicornio plateado")  # df = 2 < 3: lexema raro, se descarta
    for i in range(4):
        # Contiene "9912" pero NO como prefijo: no debe caer en la familia 9912.
        item(f"19912{i:03d}", "Intruso servicio")
    # Una CA por cada uno de 20 nombres comunes, dentro de la ventana de 30 días.
    for i in range(20):
        session.add(
            CompraAgil(
                codigo=f"ZZEXP-V{i:02d}",
                nombre=f"Servicio general {i}",
                creado_en=_FUTURO - timedelta(days=1),
                organismo_nombre=_ORG,
            )
        )
    session.commit()


@needs_postgres
class TestVocabulario:
    def test_lift_prefijo_reemplazo_e_idempotencia(self, limpio):
        with Session(limpio) as s:
            _sembrar_items(s)
            s.add(RubroVocabulario(prefijo="9912", lexema="viejo", df_rubro=1, lift=1.0))
            s.add(RubroVocabulario(prefijo="9930", lexema="ajeno", df_rubro=1, lift=1.0))
            s.commit()

            res = construir_vocabulario(
                s, k=10, lift_min=10.0, ahora=_FUTURO, familias=["9912", "9921"]
            )
            filas = s.execute(select(RubroVocabulario)).scalars().all()
            por_fam: dict[str, set[str]] = {}
            for f in filas:
                por_fam.setdefault(f.prefijo, set()).add(f.lexema)

            cable, coaxial, tornillo = _lexema(s, "cable"), _lexema(s, "coaxial"), _lexema(s, "tornillo")
            comun, raro, intruso = _lexema(s, "servicio"), _lexema(s, "unicornio"), _lexema(s, "intruso")

            assert {cable, coaxial} <= por_fam["9912"]
            assert {tornillo} <= por_fam["9921"]
            # El término común en nombres de CA queda fuera por lift; el raro, por frecuencia.
            assert comun not in por_fam["9912"] | por_fam["9921"]
            assert raro not in por_fam["9912"]
            # La familia 9912 no captura ítems que solo CONTIENEN "9912" (19912…).
            assert intruso not in por_fam["9912"]
            # Cada familia con lo suyo.
            assert cable not in por_fam["9921"] and tornillo not in por_fam["9912"]
            # Reemplazo: lo viejo de una familia recalculada se va; lo de otra familia no se toca.
            assert "viejo" not in por_fam["9912"]
            assert por_fam["9930"] == {"ajeno"}
            assert all(LEXEMA_RE.match(lx) for fam in ("9912", "9921") for lx in por_fam[fam])
            assert res["familias"] == 2 and res["items_analizados"] == 10 + 10 + 2
            assert res["ca_ventana"] == 20

            # Idempotente: correr de nuevo deja exactamente lo mismo, sin duplicar.
            antes = sorted((f.prefijo, f.lexema, f.df_rubro) for f in filas)
            construir_vocabulario(s, k=10, lift_min=10.0, ahora=_FUTURO, familias=["9912", "9921"])
            despues = sorted(
                (f.prefijo, f.lexema, f.df_rubro) for f in s.execute(select(RubroVocabulario)).scalars()
            )
            assert despues == antes

    def test_palabra_legible_por_frecuencia_con_tilde_o_null(self, limpio):
        with Session(limpio) as s:
            _sembrar_items(s)

            def item(codigo: str, nombre: str) -> None:
                s.add(LicitacionItem(licitacion_codigo="ZZEXP-LIC", codigo_producto=codigo, nombre=nombre))

            for i in range(3):
                item(f"9931{i:04d}", f"Construcción de obra N{i}")
            item("99310009", "Construccion menor")  # sin tilde: pierde por frecuencia (3 contra 1)
            for i in range(4):
                # La â parte la palabra en el regex ("t", "piz"), así que el lexema
                # "tapiz" no tiene una palabra válida que lo produzca -> NULL.
                item(f"9932{i:04d}", f"Tâpiz N{i}")
            s.commit()

            construir_vocabulario(s, k=10, lift_min=10.0, ahora=_FUTURO, familias=["9931", "9932", "9912"])
            filas = {(f.prefijo, f.lexema): f for f in s.execute(select(RubroVocabulario)).scalars()}

            lx_construccion = _lexema(s, "construccion")
            assert filas[("9931", lx_construccion)].palabra == "construcción"
            assert filas[("9931", _lexema(s, "obra"))].palabra == "obra"
            assert filas[("9932", _lexema(s, "tapiz"))].palabra is None
            # Lo que se guarda siempre valida el formato (o es NULL).
            assert all(f.palabra is None or PALABRA_RE.match(f.palabra) for f in filas.values())
            assert filas[("9912", _lexema(s, "cable"))].palabra == "cable"

            # Reemplazo sin duplicar: otra corrida deja las mismas filas y palabras.
            antes = sorted((k, f.palabra) for k, f in filas.items())
            construir_vocabulario(s, k=10, lift_min=10.0, ahora=_FUTURO, familias=["9931", "9932", "9912"])
            filas2 = {(f.prefijo, f.lexema): f for f in s.execute(select(RubroVocabulario)).scalars()}
            assert sorted((k, f.palabra) for k, f in filas2.items()) == antes

    def test_k_limita_los_lexemas_por_familia(self, limpio):
        with Session(limpio) as s:
            _sembrar_items(s)
            construir_vocabulario(s, k=1, lift_min=10.0, ahora=_FUTURO, familias=["9912"])
            assert len(s.execute(select(RubroVocabulario)).scalars().all()) == 1

    def test_sin_items_no_borra_el_vocabulario_vigente(self, limpio):
        with Session(limpio) as s:
            s.add(RubroVocabulario(prefijo="9912", lexema="vigente", df_rubro=5, lift=12.0))
            s.commit()
            res = construir_vocabulario(s, familias=["9912"], ahora=_FUTURO)
            assert res["lexemas"] == 0
            assert s.execute(select(RubroVocabulario.lexema)).scalars().all() == ["vigente"]

    def test_familias_invalidas_se_ignoran(self, limpio):
        with Session(limpio) as s:
            res = construir_vocabulario(s, familias=["99", "abcd", "'; DROP TABLE x;--"])
            assert res["lexemas"] == 0

    def test_el_job_completo_corre_y_devuelve_conteos(self, limpio):
        settings = Settings(
            mp_ticket="T", database_url="sqlite:///:memory:", secret_key="s" * 32, jobs_token="j" * 16
        )
        res = run_vocabulario_rubros(settings, limpio)
        assert set(res) == {"familias", "lexemas", "items_analizados", "ca_ventana"}
        with Session(limpio) as s:
            n = s.execute(text("SELECT count(*) FROM rubro_vocabulario")).scalar_one()
        assert n == res["lexemas"]

    def test_el_job_respeta_el_lock_unico(self, limpio):
        llamadas: list[int] = []
        inicio = ahora_utc()
        otro = create_engine(_DB_URL_ENGINE)
        try:
            with otro.connect() as ocupante:
                assert ocupante.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar_one()
                try:
                    res = _run_with_lock("vocabulario-rubros", lambda: llamadas.append(1) or {}, otro)
                finally:
                    ocupante.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
                    ocupante.commit()
            assert res is None and llamadas == []
        finally:
            with Session(otro) as s:
                s.execute(delete(JobRun).where(JobRun.job == "vocabulario-rubros", JobRun.iniciado_en >= inicio))
                s.commit()
            otro.dispose()


# ---------------------------------------------------------------------------
# Explorador: rubro confirmado / posible y texto
# ---------------------------------------------------------------------------


def _sembrar_explorador(session: Session) -> int:
    ahora = ahora_utc()
    usuario = Usuario(email=_EMAIL, password_hash=hash_password("clave-de-prueba-larga"), rol=RolUsuario.USUARIO, activo=True)
    session.add(usuario)
    session.flush()

    def ca(codigo: str, nombre: str, **kw) -> None:
        base = {
            "codigo": codigo,
            "nombre": nombre,
            "estado": "publicada",
            "fecha_publicacion": ahora - timedelta(days=1),
            "fecha_cierre": ahora + timedelta(days=2),
            "monto_disponible_clp": 1_000_000.0,
            "organismo_nombre": _ORG,
            "region": 13,
        }
        base.update(kw)
        session.add(CompraAgil(**base))

    ca("ZZEXP-1", "Compra de zzcable coaxial")
    ca("ZZEXP-2", "Adquisicion de zzcable para oficina", region=5)
    ca("ZZEXP-3", "Servicio de aseo")
    ca("ZZEXP-4", "Otra cosa distinta", monto_disponible_clp=50.0)
    ca("ZZEXP-5", "Zzcable vencido", fecha_cierre=ahora - timedelta(hours=1))
    session.flush()
    session.add(CaProducto(ca_codigo="ZZEXP-1", codigo_producto="99120001", nombre="cable"))
    session.add(CaProducto(ca_codigo="ZZEXP-4", codigo_producto="99120005", nombre="otro"))
    session.add(RubroVocabulario(prefijo="9912", lexema=_lexema(session, "zzcable"), df_rubro=10, lift=20.0))
    session.commit()
    return int(usuario.id)


def _codigos(items: list[dict]) -> set[str]:
    return {i["codigo"] for i in items}


def _codigos_html(html: str) -> set[str]:
    return set(re.findall(r"ZZEXP-\d", html))


@needs_postgres
class TestExplorador:
    def _f(self, uid: int, **kw) -> FiltrosExplorador:
        return FiltrosExplorador(usuario_id=uid, organismo=_ORG, **kw)

    def test_por_defecto_solo_confirmadas_y_posibles_opcionales(self, limpio):
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            solo = buscar(s, self._f(uid, prefijos=["9912"]))
            assert _codigos(solo.items) == {"ZZEXP-1", "ZZEXP-4"}  # la 2 solo calza por vocabulario
            assert all(i["confirmado"] and not i["posible"] for i in solo.items)

            r = buscar(s, self._f(uid, prefijos=["9912"], incluir_posibles=True))
            por_codigo = {i["codigo"]: i for i in r.items}
            assert set(por_codigo) == {"ZZEXP-1", "ZZEXP-2", "ZZEXP-4"}  # sin la vencida ni la ajena al rubro
            assert por_codigo["ZZEXP-1"]["confirmado"] and not por_codigo["ZZEXP-1"]["posible"]
            assert por_codigo["ZZEXP-4"]["confirmado"]
            assert por_codigo["ZZEXP-2"]["posible"] and not por_codigo["ZZEXP-2"]["confirmado"]
            assert por_codigo["ZZEXP-2"]["familia_posible"] == "9912"  # sin nombre en el catálogo: el código
            # Segmento y prefijo fino llegan a la misma familia.
            posibles = {"prefijos": ["99"], "incluir_posibles": True}
            assert _codigos(buscar(s, self._f(uid, **posibles)).items) == set(por_codigo)
            posibles = {"prefijos": ["991200"], "incluir_posibles": True}
            assert _codigos(buscar(s, self._f(uid, **posibles)).items) == set(por_codigo)
            # Otro rubro sin vocabulario ni productos: nada.
            assert buscar(s, self._f(uid, prefijos=["9955"], incluir_posibles=True)).items == []

    def test_sin_incluir_posibles_no_se_lee_el_vocabulario(self, limpio, monkeypatch):
        import app.explorador_ca as ex

        def prohibido(*a, **k):
            raise AssertionError("no debe leer el vocabulario ni etiquetar familias")

        monkeypatch.setattr(ex, "vocabulario_de_prefijos", prohibido)
        monkeypatch.setattr(ex, "_familias_posibles", prohibido)
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            f = self._f(uid, prefijos=["9912"])
            assert _codigos(buscar(s, f).items) == {"ZZEXP-1", "ZZEXP-4"}
            assert contar(s, f) == 2

    def test_palabras_rubro_amplian_y_confirmada_gana(self, limpio):
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            # Sin rubros: el rubro son las palabras.
            solo_palabras = buscar(s, self._f(uid, palabras_rubro=["zzcable"]))
            assert _codigos(solo_palabras.items) == {"ZZEXP-1", "ZZEXP-2"}
            assert all(i["por_palabras"] and not i["confirmado"] for i in solo_palabras.items)

            # Con rubro: amplía. La 2 (sin productos) entra por palabras; la 1 sigue "Confirmado".
            f = self._f(uid, prefijos=["9912"], palabras_rubro=["zzcable"])
            por_codigo = {i["codigo"]: i for i in buscar(s, f).items}
            assert set(por_codigo) == {"ZZEXP-1", "ZZEXP-2", "ZZEXP-4"}
            assert por_codigo["ZZEXP-1"]["confirmado"] and not por_codigo["ZZEXP-1"]["por_palabras"]
            assert por_codigo["ZZEXP-2"]["por_palabras"] and not por_codigo["ZZEXP-2"]["posible"]
            assert por_codigo["ZZEXP-4"]["confirmado"]

            # Una palabra que no calza con nada no deja pasar a las demás.
            assert buscar(s, self._f(uid, palabras_rubro=["qqxwz"])).items == []

    def test_texto_sigue_siendo_and_global_con_palabras(self, limpio):
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            f = self._f(uid, palabras_rubro=["zzcable", "aseo"], texto="coaxial")
            assert _codigos(buscar(s, f).items) == {"ZZEXP-1"}
            f = self._f(uid, palabras_rubro=["zzcable", "aseo"])
            assert _codigos(buscar(s, f).items) == {"ZZEXP-1", "ZZEXP-2", "ZZEXP-3"}

    def test_palabras_y_texto_con_tilde_calzan(self, limpio):
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            s.add(
                CompraAgil(
                    codigo="ZZEXP-9",
                    nombre="Compra de artículos de ferretería y reparación de señalética",
                    estado="publicada",
                    fecha_publicacion=ahora_utc() - timedelta(days=1),
                    fecha_cierre=ahora_utc() + timedelta(days=2),
                    monto_disponible_clp=1_000_000.0,
                    organismo_nombre=_ORG,
                    region=13,
                )
            )
            s.commit()
            for kw in ({"palabras_rubro": ["ferretería"]}, {"palabras_rubro": ["señalética"]}, {"texto": "reparación"}):
                assert "ZZEXP-9" in _codigos(buscar(s, self._f(uid, **kw)).items), kw
            assert "ZZEXP-9" not in _codigos(buscar(s, self._f(uid, palabras_rubro=["papelería"])).items)

    def test_conteo_igual_al_total_con_palabras_y_posibles(self, limpio):
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            for f in (
                self._f(uid, prefijos=["9912"], incluir_posibles=True, palabras_rubro=["aseo"]),
                self._f(uid, prefijos=["9912"], palabras_rubro=["aseo"]),
                self._f(uid, palabras_rubro=["zzcable"]),
            ):
                assert contar(s, f) == buscar(s, f).total

    def test_rubro_combinado_con_region_monto_y_cierre(self, limpio):
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            f = self._f(uid, prefijos=["9912"], regiones=[13], monto_min=1000.0, cierre="3d")
            assert _codigos(buscar(s, f).items) == {"ZZEXP-1"}  # la 2 es de otra región, la 4 es barata
            f = self._f(uid, prefijos=["9912"], regiones=[5, 13], incluir_posibles=True)
            assert _codigos(buscar(s, f).items) == {"ZZEXP-1", "ZZEXP-2", "ZZEXP-4"}
            f = self._f(uid, prefijos=["9912"], regiones=[5, 13])
            assert _codigos(buscar(s, f).items) == {"ZZEXP-1", "ZZEXP-4"}

    def test_descartadas_del_usuario_no_aparecen(self, limpio):
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            s.add(
                MatchFeedback(
                    usuario_id=uid,
                    fuente="compras_agiles",
                    codigo_oportunidad="ZZEXP-2",
                    valor=ValorFeedback.DESCARTE.value,
                )
            )
            s.commit()
            propio = self._f(uid, prefijos=["9912"], incluir_posibles=True)
            ajeno = self._f(uid + 1_000_000, prefijos=["9912"], incluir_posibles=True)
            assert "ZZEXP-2" not in _codigos(buscar(s, propio).items)
            assert "ZZEXP-2" in _codigos(buscar(s, ajeno).items)

    def test_texto_libre_y_entradas_hostiles(self, limpio):
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            assert _codigos(buscar(s, self._f(uid, texto="zzcable")).items) == {"ZZEXP-1", "ZZEXP-2"}
            assert _codigos(buscar(s, self._f(uid, texto='"zzcable coaxial"')).items) == {"ZZEXP-1"}
            assert _codigos(buscar(s, self._f(uid, texto="zzcable -coaxial")).items) == {"ZZEXP-2"}
            for hostil in ["'; DROP TABLE compras_agiles;--", '"', "100%", "a' OR '1'='1", "%%", "\\", "(((", "!:*&|"]:
                buscar(s, self._f(uid, texto=hostil))  # no revienta
            assert s.execute(select(CompraAgil.codigo).where(CompraAgil.codigo == "ZZEXP-1")).first() is not None

    def test_paginacion_suma_el_conteo(self, limpio):
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            f = self._f(uid)
            total = contar(s, f)
            assert total == 4  # las 5 sembradas menos la vencida
            vistos: list[str] = []
            for n in range(1, 6):
                pag = buscar(s, f, pagina=n, page_size=3)
                assert pag.total == total and pag.total_paginas == 2
                vistos += [i["codigo"] for i in pag.items]
            assert len(vistos) == len(set(vistos)) == total

    def test_ruta_muestra_etiquetas_y_no_toca_la_red(self, limpio):
        import respx

        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
        settings = Settings(
            mp_ticket="T", database_url="sqlite:///:memory:", secret_key="secret-test-key-larga-32chars!!", jobs_token="j" * 16
        )
        client = TestClient(create_app(settings, limpio))
        cookies = {COOKIE_NAME: create_session_token(settings.secret_key, uid)}
        base = {"categorias_unspsc": "9912", "organismo": _ORG, "sin_favoritos": "1"}
        with respx.mock(assert_all_called=False) as router:
            # Por defecto: solo confirmadas, sin etiqueta "Posible".
            r0 = client.get("/compras-agiles", params=base, cookies=cookies)
            c0 = client.get("/compras-agiles/conteo", params=base, cookies=cookies)
            # URL vieja: se ignora y da lo mismo que el default.
            vieja = client.get("/compras-agiles", params={**base, "solo_confirmados": "1"}, cookies=cookies)
            # Con posibles.
            con = {**base, "incluir_posibles": "1"}
            r = client.get("/compras-agiles", params=con, cookies=cookies)
            c = client.get("/compras-agiles/conteo", params=con, cookies=cookies)
            # Con palabras (y sin rubros).
            pal = {"palabras_rubro": ["zzcable", "a&b", "x"], "organismo": _ORG, "sin_favoritos": "1"}
            rp = client.get("/compras-agiles", params=pal, cookies=cookies)
            cp = client.get("/compras-agiles/conteo", params=pal, cookies=cookies)
            assert not router.calls
        for resp in (r0, c0, vieja, r, c, rp, cp):
            assert resp.status_code == 200
        assert "Posible (por nombre)" not in r0.text and "ZZEXP-2" not in r0.text
        assert ">2<" in c0.text
        assert _codigos_html(vieja.text) == _codigos_html(r0.text) == {"ZZEXP-1", "ZZEXP-4"}
        assert "Confirmado" in r.text and "Posible (por nombre): 9912" in r.text
        assert "Incluye posibles" in r.text
        assert ">3<" in c.text
        # Palabras: etiqueta, chip, y el conteo en vivo coincide con la pantalla.
        assert "Por tus palabras" in rp.text and "Palabra: zzcable" in rp.text
        assert "a&amp;b" not in rp.text and "Palabra: x" not in rp.text
        assert _codigos_html(rp.text) == {"ZZEXP-1", "ZZEXP-2"}
        assert ">2<" in cp.text

    def test_avisos_de_vocabulario(self, limpio):
        with Session(limpio) as s:
            uid = _sembrar_explorador(s)
            s.execute(delete(RubroVocabulario))
            s.commit()
        settings = Settings(
            mp_ticket="T", database_url="sqlite:///:memory:", secret_key="secret-test-key-larga-32chars!!", jobs_token="j" * 16
        )
        client = TestClient(create_app(settings, limpio))
        cookies = {COOKIE_NAME: create_session_token(settings.secret_key, uid)}
        params = {"categorias_unspsc": "9912", "organismo": _ORG, "sin_favoritos": "1"}
        vacia = client.get("/compras-agiles", params=params, cookies=cookies)
        assert "Todavía no hay vocabulario" in vacia.text
        with Session(limpio) as s:
            s.add(RubroVocabulario(prefijo="9930", lexema="otro", df_rubro=5, lift=12.0, palabra="otro"))
            s.commit()
        con_filas = client.get("/compras-agiles", params=params, cookies=cookies)
        assert "Todavía no hay vocabulario" not in con_filas.text
        assert "no tienen palabras típicas aprendidas" in con_filas.text
        # Con vocabulario del rubro, ninguno de los dos avisos.
        with Session(limpio) as s:
            s.add(RubroVocabulario(prefijo="9912", lexema="zzcable", df_rubro=5, lift=12.0, palabra="zzcable"))
            s.commit()
        ok = client.get("/compras-agiles", params=params, cookies=cookies)
        assert "no tienen palabras típicas" not in ok.text and "Todavía no hay vocabulario" not in ok.text
        # Y las sugerencias salen como botones con la palabra.
        assert 'data-palabra="zzcable"' in ok.text
