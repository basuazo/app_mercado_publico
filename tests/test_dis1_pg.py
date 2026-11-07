"""F-dis-1 sobre Postgres con la migración aplicada: defaults de servidor de la apariencia,
`usuario_vista` (JSONB, UNIQUE por usuario + pantalla, cascada al borrar el usuario)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.mercado import preferencias as pf
from app.models.tables import Usuario, UsuarioVista
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not (_DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
_PREFIJO = "zzdis1-"


@pytest.fixture()
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    yield e
    _limpiar(e)
    e.dispose()


def _limpiar(e) -> None:
    with Session(e) as s:
        s.execute(
            delete(Usuario).where(Usuario.email.like(f"{_PREFIJO}%"))
        )  # cascada a usuario_vista
        s.commit()


def test_usuario_insertado_sin_apariencia_toma_los_defaults_de_servidor(pg_engine):
    with Session(pg_engine) as s:
        s.execute(
            text(
                "INSERT INTO usuarios (email, password_hash, rol, activo, dias_resumen, creado_en) "
                "VALUES (:e, 'x', 'usuario', true, 3, now())"
            ),  # fmt: skip
            {"e": f"{_PREFIJO}crudo@t.cl"},
        )
        s.commit()
        u = s.execute(select(Usuario).where(Usuario.email == f"{_PREFIJO}crudo@t.cl")).scalar_one()
        assert (u.tema, u.densidad, u.inicio) == ("claro", "comoda", "hoy")


def test_vista_jsonb_unica_por_usuario_y_pantalla_y_cascada(pg_engine):
    with Session(pg_engine) as s:
        a = Usuario(email=f"{_PREFIJO}a@t.cl", password_hash="x", rol="usuario", activo=True)
        b = Usuario(email=f"{_PREFIJO}b@t.cl", password_hash="x", rol="usuario", activo=True)
        s.add_all([a, b])
        s.commit()
        pf.guardar_vista(s, a.id, "hoy", {"periodo": "semana", "filtro": "cierres", "x": 1})
        pf.guardar_vista(s, b.id, "hoy", {"periodo": "mes"})
        s.commit()
        tipo = s.execute(
            text("SELECT jsonb_typeof(datos) FROM usuario_vista WHERE usuario_id = :u"), {"u": a.id}
        ).scalar_one()
        assert tipo == "object"
        assert pf.vista_de(s, a.id, "hoy") == {"periodo": "semana", "filtro": "cierres"}
        assert pf.vista_de(s, b.id, "hoy") == {"periodo": "mes"}
        # Reemplazo: sigue habiendo una fila por usuario y pantalla.
        pf.guardar_vista(s, a.id, "hoy", {"periodo": "hoy"})
        s.commit()
        assert s.execute(
            select(UsuarioVista).where(UsuarioVista.usuario_id == a.id)
        ).scalars().all()[0].datos == {"periodo": "hoy"}
        s.add(UsuarioVista(usuario_id=a.id, pantalla="hoy", datos={}))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        # Borrar el usuario borra sus vistas.
        s.execute(delete(Usuario).where(Usuario.id == a.id))
        s.commit()
        assert (
            s.execute(select(UsuarioVista).where(UsuarioVista.usuario_id == a.id)).first() is None
        )
        assert pf.vista_de(s, b.id, "hoy") == {"periodo": "mes"}
