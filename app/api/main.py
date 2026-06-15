"""Fábrica de la aplicación FastAPI."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.engine import Engine

from app.api.deps import LoginRequired
from app.api.routes import api as api_router
from app.api.routes import auth as auth_router
from app.api.routes import pages as pages_router
from app.core.settings import Settings


def create_app(settings: Settings, engine: Engine) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    # Guardar estado compartido en app.state
    app.state.settings = settings
    app.state.engine = engine

    # Registrar routers
    app.include_router(auth_router.router)
    app.include_router(pages_router.router)
    app.include_router(api_router.router)

    # Manejar LoginRequired → redirect a /login
    @app.exception_handler(LoginRequired)
    async def login_required_handler(
        request: Request, exc: LoginRequired
    ) -> RedirectResponse:
        next_url = exc.next_url or "/"
        return RedirectResponse(url=f"/login?next={next_url}", status_code=302)

    return app
