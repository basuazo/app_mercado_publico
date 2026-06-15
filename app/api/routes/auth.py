"""Rutas de autenticación: login y logout."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth.password import verify_password
from app.auth.rate_limit import clear_attempts, is_rate_limited, record_failed_attempt
from app.auth.session import COOKIE_NAME, create_session_token
from app.models.tables import Usuario

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_SESSION_MAX_AGE = 7 * 24 * 3600


@router.get("/login", response_class=HTMLResponse)
async def login_get(
    request: Request,
    next: str = "/",
    error: str = "",
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "login.html", {"next": next, "error": error}
    )


@router.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    settings = request.app.state.settings
    ip = request.client.host if request.client else "unknown"

    if is_rate_limited(ip):
        return RedirectResponse(
            url=f"/login?next={next}&error=Demasiados+intentos.+Espera+15+minutos.",
            status_code=303,
        )

    user = session.execute(
        select(Usuario).where(Usuario.email == email, Usuario.activo.is_(True))
    ).scalar_one_or_none()

    if user is None or not verify_password(password, user.password_hash):
        record_failed_attempt(ip)
        return RedirectResponse(
            url=f"/login?next={next}&error=Email+o+contraseña+incorrectos.",
            status_code=303,
        )

    clear_attempts(ip)
    token = create_session_token(settings.secret_key, user.id)

    safe_next = next if next.startswith("/") else "/"

    response = RedirectResponse(url=safe_next, status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout(
    request: Request,
    csrf_token: str = Form(""),
) -> RedirectResponse:
    from sqlalchemy.orm import Session as _Session

    from app.api.deps import _get_user_from_request
    from app.auth.csrf import validate_csrf_token

    engine = request.app.state.engine
    settings = request.app.state.settings
    with _Session(engine) as session:
        user = _get_user_from_request(request, session)
        if user is not None:
            token = request.headers.get("X-CSRF-Token") or csrf_token
            if not validate_csrf_token(settings.secret_key, user.id, token):
                return RedirectResponse(url="/login?error=CSRF+inválido", status_code=303)

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response
