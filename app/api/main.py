"""Fábrica de la aplicación FastAPI con lifespan (scheduler + seed)."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.deps import LoginRequired
from app.api.routes import api as api_router
from app.api.routes import auth as auth_router
from app.api.routes import pages as pages_router
from app.core.settings import Settings


def _normalizar_url_driver(url: str) -> str:
    """Fuerza el driver psycopg v3 en URLs postgres sin driver explícito.

    SQLAlchemy elige psycopg2 por defecto para "postgresql://"/"postgres://",
    pero el proyecto depende de psycopg[binary] (v3), no psycopg2.
    """
    if url.startswith("postgresql+") or url.startswith("postgres+"):
        return url
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        _, _, resto = url.partition("://")
        return f"postgresql+psycopg://{resto}"
    return url


def make_engine(settings: Settings) -> Engine:
    """Crea el engine con parámetros apropiados para Neon (Postgres en producción)."""
    from typing import Any

    url = _normalizar_url_driver(settings.database_url)
    is_postgres = url.startswith("postgresql") or url.startswith("postgres")

    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if is_postgres:
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 0
        kwargs["pool_recycle"] = 300

    return create_engine(url, **kwargs)


def _wait_for_db(engine: Engine, intentos: int = 5, pausa: float = 2.0) -> None:
    """Reintenta conexión al arrancar (Neon puede estar suspendida tras idle)."""
    for i in range(intentos):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception:
            if i == intentos - 1:
                raise
            time.sleep(pausa * (i + 1))


def create_app(settings: Settings, engine: Engine) -> FastAPI:
    from app.ingest.orchestrator import build_scheduler
    from app.models.seeds import seed_admin

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Esperar que Neon esté disponible
        _wait_for_db(engine)

        # Seed idempotente del admin (no falla si ya existe)
        if settings.admin_email and settings.admin_password:
            with Session(engine) as session:
                seed_admin(session, settings.admin_email, settings.admin_password)
                session.commit()

        # Arrancar scheduler en background (APScheduler BackgroundScheduler)
        sched = build_scheduler(settings, engine)
        # build_scheduler devuelve BlockingScheduler; usar BackgroundScheduler en lugar
        from apscheduler.schedulers.background import BackgroundScheduler as _BG

        bg_sched = _BG(timezone="America/Santiago")
        # Copiar jobs del scheduler configurado al background scheduler
        for job in sched.get_jobs():
            bg_sched.add_job(
                job.func,
                trigger=job.trigger,
                id=job.id,
                name=job.name,
                replace_existing=True,
            )
        bg_sched.start()
        app.state.scheduler = bg_sched

        yield

        # Apagado limpio en SIGTERM (Render reinicia en deploys)
        bg_sched.shutdown(wait=False)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

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

    @app.middleware("http")
    async def security_headers(request: Request, call_next: object) -> Response:
        response: Response = await call_next(request)  # type: ignore[operator]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    return app


# ---------------------------------------------------------------------------
# Factory para uvicorn --factory (startCommand en render.yaml)
# ---------------------------------------------------------------------------

def _make_app() -> FastAPI:
    from app.core.settings import get_settings

    s = get_settings()
    e = make_engine(s)
    return create_app(s, e)


# Instancia de módulo requerida por: uvicorn app.api.main:app
app = _make_app()

