"""Tests F-indices — upsert de matches en una sentencia (Postgres)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.matching.engine import _upsert_match
from app.models.tables import OportunidadMatch, PerfilBusqueda, Usuario

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_POSTGRES = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")
needs_postgres = pytest.mark.skipif(
    not _TIENE_POSTGRES,
    reason="Requiere DATABASE_URL apuntando a Postgres (con migración aplicada)",
)

_PW_HASH = "$2b$12$fakehashforteststhatislong.enough.xyz"
_T0 = datetime(2026, 10, 1, 12, 0)


@pytest.fixture()
def pg_session():
    e = create_engine(normalizar_url_driver(_DB_URL))
    s = Session(e)
    try:
        yield s
    finally:
        s.rollback()  # todo ocurre sin commit: nada queda en la base
        s.close()
        e.dispose()


@needs_postgres
def test_upsert_una_sentencia_en_postgres(pg_session: Session):
    s = pg_session
    u = Usuario(email="idx_upsert@test.cl", password_hash=_PW_HASH, activo=True)
    s.add(u)
    s.flush()
    p = PerfilBusqueda(owner_id=u.id, nombre="P", keywords=["k"], activo=True)
    s.add(p)
    s.flush()

    def _fila() -> tuple[OportunidadMatch, str]:
        m = s.execute(
            select(OportunidadMatch).where(OportunidadMatch.perfil_id == p.id)
        ).scalar_one()
        s.refresh(m)
        ctid = s.execute(
            text("SELECT ctid::text FROM oportunidades_match WHERE id = :i"), {"i": m.id}
        ).scalar_one()
        return m, ctid

    # 1) inserta y devuelve True
    assert _upsert_match(s, p.id, "licitaciones", "IDX-1", 50.0, {"a": 1}, _T0) is True
    m, ctid1 = _fila()
    assert m.score == 50.0 and m.fecha_match == _T0

    # 2) otro score: actualiza, devuelve False, NO toca fecha_match
    assert (
        _upsert_match(s, p.id, "licitaciones", "IDX-1", 70.0, {"a": 2}, _T0 + timedelta(days=3))
        is False
    )
    m, ctid2 = _fila()
    assert m.score == 70.0 and m.razones == {"a": 2}
    assert m.fecha_match == _T0
    assert ctid2 != ctid1  # la fila se reescribió

    # 3) mismos valores: no reescribe (mismo ctid) y devuelve False
    assert (
        _upsert_match(s, p.id, "licitaciones", "IDX-1", 70.0, {"a": 2}, _T0 + timedelta(days=5))
        is False
    )
    m, ctid3 = _fila()
    assert ctid3 == ctid2
    assert m.fecha_match == _T0
