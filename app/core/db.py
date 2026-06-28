"""Helpers compartidos de conexión a la base de datos."""

from __future__ import annotations


def normalizar_url_driver(url: str) -> str:
    """Fuerza el driver psycopg v3 en URLs postgres sin driver explícito.

    SQLAlchemy (y Alembic, que reusa esta misma función) elige psycopg2 por
    defecto para "postgresql://"/"postgres://", pero el proyecto depende de
    psycopg[binary] (v3), no psycopg2. Sin esto, `alembic upgrade head` en el
    startCommand de Render fallaría con ModuleNotFoundError si DATABASE_URL
    no trae el driver explícito.
    """
    if url.startswith("postgresql+") or url.startswith("postgres+"):
        return url
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        _, _, resto = url.partition("://")
        return f"postgresql+psycopg://{resto}"
    return url
