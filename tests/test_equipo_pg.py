"""F-equipo sobre Postgres con la migración aplicada: índice único parcial, FOR UPDATE entre dos
sesiones, JSONB y bytes por evento (regla 11). Aislado: CA `ZZEQ-*` y una empresa propia."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.core.tiempo import ahora_utc
from app.mercado import equipo as eq
from app.models.tables import CaEvento, CaTrabajo, CompraAgil, Empresa, SyncState, Usuario
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "ZZEQ-"
_EMPRESA = "ZZ Equipo Tests"


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        ids = select(Empresa.id).where(Empresa.nombre == _EMPRESA)
        s.execute(delete(SyncState).where(SyncState.fuente.like("alarmas_equipo:%")))
        s.execute(delete(Usuario).where(Usuario.email.like("zzeq-%")))
        s.execute(delete(CaTrabajo).where(CaTrabajo.empresa_id.in_(ids)))
        s.execute(delete(Empresa).where(Empresa.nombre == _EMPRESA))  # cascada a eventos
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like(f"{_PREFIJO}%")))
        s.commit()


def _base(e) -> tuple[int, int, int]:
    with Session(e) as s:
        emp = Empresa(nombre=_EMPRESA, rut="76.123.456-0", regiones_prioritarias=[16])
        s.add(emp)
        s.flush()
        a = Usuario(email="zzeq-a@t.cl", nombre="Ana", password_hash="x", empresa_id=emp.id)
        b = Usuario(email="zzeq-b@t.cl", nombre="Beto", password_hash="x", empresa_id=emp.id)
        s.add_all([a, b])
        s.add(CompraAgil(codigo=f"{_PREFIJO}1", nombre="Leche Ñuble", estado="publicada",
                         region=16, fecha_cierre=ahora_utc() + timedelta(hours=3)))
        s.commit()
        return emp.id, a.id, b.id


def test_indice_parcial_unico_solo_para_eventos_de_sistema(pg_engine):
    emp, a, _ = _base(pg_engine)
    cod = f"{_PREFIJO}1"
    with Session(pg_engine) as s:
        # De usuario: se repiten sin problema.
        for _ in range(2):
            s.add(CaEvento(empresa_id=emp, ca_codigo=cod, usuario_id=a, tipo="estado",
                           creado_en=ahora_utc()))
        s.add(CaEvento(empresa_id=emp, ca_codigo=cod, usuario_id=None, tipo="cierre_4h",
                       creado_en=ahora_utc()))
        s.commit()
        s.add(CaEvento(empresa_id=emp, ca_codigo=cod, usuario_id=None, tipo="cierre_4h",
                       creado_en=ahora_utc()))
        with pytest.raises(IntegrityError):
            s.commit()


def test_dos_sesiones_misma_version_solo_una_gana(pg_engine):
    emp, a, b = _base(pg_engine)
    cod = f"{_PREFIJO}1"
    with Session(pg_engine) as s1, Session(pg_engine) as s2:
        eq.cambiar_estado(s1, emp, s1.get(Usuario, a), cod, eq.PRIORIZADA, 0)
        s1.commit()
        # Las dos vieron la versión 1; s1 toma el FOR UPDATE y confirma primero.
        eq.cambiar_estado(s1, emp, s1.get(Usuario, a), cod, eq.COTIZANDO, 1)
        s1.commit()
        with pytest.raises(eq.ConflictoVersion) as exc:
            eq.cambiar_estado(s2, emp, s2.get(Usuario, b), cod, eq.LISTA, 1)
        assert exc.value.quien == "Ana"
        s2.rollback()
    with Session(pg_engine) as s:
        t = s.execute(select(CaTrabajo).where(CaTrabajo.empresa_id == emp)).scalar_one()
        assert (t.estado, t.version) == (eq.COTIZANDO, 2)


def test_creacion_concurrente_de_la_fila_es_conflicto(pg_engine):
    emp, a, b = _base(pg_engine)
    cod = f"{_PREFIJO}1"
    with Session(pg_engine) as s1, Session(pg_engine) as s2:
        eq.cambiar_estado(s1, emp, s1.get(Usuario, a), cod, eq.PRIORIZADA, 0)
        s1.commit()
        # s2 cree que no hay fila (versión 0): el UNIQUE o la versión lo frenan.
        with pytest.raises(eq.ConflictoVersion):
            eq.cambiar_estado(s2, emp, s2.get(Usuario, b), cod, eq.COTIZANDO, 0)


def test_alarmas_idempotentes_y_bytes_por_evento(pg_engine):
    emp, a, b = _base(pg_engine)
    cod = f"{_PREFIJO}1"
    ahora = ahora_utc()
    with Session(pg_engine) as s:
        eq.cambiar_estado(s, emp, s.get(Usuario, a), cod, eq.COTIZANDO, 0)
        eq.comentar(s, emp, s.get(Usuario, a), cod, "@Beto el flete a Coihueco sale caro")
        s.commit()
        assert eq.generar_alarmas(s, emp, ahora)["cierre_4h"] == 1
        s.commit()
        assert eq.generar_alarmas(s, emp, ahora)["cierre_4h"] == 0
        s.commit()
        assert eq.generar_si_toca(s, emp, ahora) is True
        s.commit()
        assert eq.generar_si_toca(s, emp, ahora + timedelta(seconds=10)) is False
        # JSONB ida y vuelta y no leídos de Beto: estado, mención (no el comentario dos veces), cierre.
        tipos = sorted(e.tipo for e in eq.listar_no_leidos(s, s.get(Usuario, b), emp))
        assert tipos == ["cierre_4h", "estado", "mencion"]
        n, promedio = s.execute(
            text("SELECT count(*), avg(pg_column_size(e.*)) FROM ca_eventos e WHERE empresa_id = :e"),
            {"e": emp},
        ).one()
        assert n == 4
        # Regla 11: un evento pesa ~100 B (12 meses de 10 personas activas ≈ pocos MB).
        assert promedio < 200
        assert s.execute(select(func.count()).select_from(CaEvento).where(
            CaEvento.empresa_id == emp, CaEvento.usuario_id.is_(None))).scalar_one() == 1
