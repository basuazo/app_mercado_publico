"""Dependencias FastAPI: sesión, autorización, CSRF."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth.csrf import validate_csrf_token
from app.auth.session import COOKIE_NAME, decode_session_token
from app.models.enums import RolUsuario
from app.models.tables import Usuario


class LoginRequired(Exception):
    """Lanzada por rutas HTML cuando el usuario no tiene sesión válida."""

    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


# ---------------------------------------------------------------------------
# DB session
# ---------------------------------------------------------------------------


def get_db(request: Request) -> Generator[Session, None, None]:
    engine = request.app.state.engine
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _get_user_from_request(request: Request, session: Session) -> Usuario | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    settings = request.app.state.settings
    decoded = decode_session_token(settings.secret_key, token)
    if decoded is None:
        return None
    user_id, nonce = decoded
    user = session.get(Usuario, user_id)
    if user is None or not user.activo:
        return None
    request.state.csrf_nonce = nonce
    return user


# ---------------------------------------------------------------------------
# Dependencias para rutas HTML (redirigen en fallo)
# ---------------------------------------------------------------------------


def html_require_user(
    request: Request,
    session: Session = Depends(get_db),
) -> Usuario:
    user = _get_user_from_request(request, session)
    if user is None:
        raise LoginRequired(next_url=str(request.url.path))
    return user


def html_require_admin(
    user: Usuario = Depends(html_require_user),
) -> Usuario:
    if user.rol != RolUsuario.ADMIN:
        raise HTTPException(status_code=403, detail="Acceso restringido a administradores")
    return user


# ---------------------------------------------------------------------------
# Dependencias para rutas API (devuelven 401/403 en fallo)
# ---------------------------------------------------------------------------


def api_require_user(
    request: Request,
    session: Session = Depends(get_db),
) -> Usuario:
    user = _get_user_from_request(request, session)
    if user is None:
        raise HTTPException(status_code=401, detail="Autenticación requerida")
    return user


def api_require_admin(
    user: Usuario = Depends(api_require_user),
) -> Usuario:
    if user.rol != RolUsuario.ADMIN:
        raise HTTPException(status_code=403, detail="Acceso restringido a administradores")
    return user


# ---------------------------------------------------------------------------
# CSRF helper (llamado manualmente en cada mutación)
# ---------------------------------------------------------------------------


def check_csrf(request: Request, form_token: str = "") -> None:
    """Valida CSRF desde header X-CSRF-Token o campo de formulario.

    Prioridad: header X-CSRF-Token > campo form csrf_token.
    Lanza HTTPException 403 si el token no es válido.
    """
    token = request.headers.get("X-CSRF-Token") or form_token
    settings = request.app.state.settings
    nonce = getattr(request.state, "csrf_nonce", None)
    if not nonce or not validate_csrf_token(settings.secret_key, nonce, token):
        raise HTTPException(status_code=403, detail="CSRF token inválido")
