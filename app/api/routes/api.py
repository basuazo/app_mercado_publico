"""Rutas REST JSON: /api/oportunidades, /api/perfiles, /api/salud, /api/jobs."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any, NamedTuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (
    api_require_admin,
    api_require_user,
    check_csrf,
    get_db,
)
from app.api.query import get_oportunidades_usuario
from app.api.salud_data import get_salud_data
from app.core.tiempo import ahora_utc
from app.matching.perfiles import (
    actualizar_perfil,
    crear_perfil,
    eliminar_perfil,
    listar_perfiles,
    obtener_perfil,
)
from app.models.tables import JobRun, Usuario

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Ping público
# ---------------------------------------------------------------------------


@router.get("/salud/ping")
async def ping() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dead-man's switch de jobs (público)
# ---------------------------------------------------------------------------


class _JobVigilado(NamedTuple):
    """Un job del watchlist: nombre canónico, alias y ventana máxima sin OK."""

    job: str
    alias: frozenset[str]
    umbral_horas: float
    critico: bool


# El mismo job lógico llega con nombres distintos según el disparador (endpoint,
# scheduler, ciclo nocturno); job_runs los graba tal cual y acá se resuelven.
#
# Los umbrales son amplios A PROPÓSITO: esto detecta "la ingesta se detuvo" —el
# incidente real duró semanas— y no "un job se atrasó una hora". 30 h cubre el
# hueco nocturno sin falsos positivos. Ajustables acá.
_JOBS_VIGILADOS: tuple[_JobVigilado, ...] = (
    _JobVigilado("activas", frozenset({"activas", "sync_activas"}), 30, True),
    _JobVigilado("ca", frozenset({"ca", "ca_incremental"}), 30, True),
    _JobVigilado("datos-abiertos", frozenset({"datos-abiertos", "datos_abiertos"}), 36, True),
    _JobVigilado("resumen", frozenset({"resumen"}), 30, False),
)


@router.get("/salud/jobs")
async def salud_jobs(
    response: Response,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Dead-man's switch: 503 si algún job crítico dejó de correr.

    Público y sin secretos, igual que /salud/ping (un monitor externo gratuito
    no puede mandar cookies). El no-2xx es la señal que dispara el aviso.
    A diferencia de /ping, este sí toca la base: una consulta por job, resuelta
    por ix_job_runs_job_iniciado.
    """
    ahora = ahora_utc()
    jobs: list[dict[str, Any]] = []
    hay_critico_stale = False

    for vigilado in _JOBS_VIGILADOS:
        ultimo_ok = session.execute(
            select(JobRun.iniciado_en)
            .where(JobRun.estado == "ok", JobRun.job.in_(sorted(vigilado.alias)))
            .order_by(JobRun.iniciado_en.desc())
            .limit(1)
        ).scalar_one_or_none()

        if ultimo_ok is None:
            # Nunca corrió OK: tan grave como estar atrasado.
            edad_horas: float | None = None
            stale = True
        else:
            edad_horas = round((ahora - ultimo_ok).total_seconds() / 3600, 1)
            stale = edad_horas > vigilado.umbral_horas

        if stale and vigilado.critico:
            hay_critico_stale = True

        jobs.append(
            {
                "job": vigilado.job,
                "ultimo_ok": ultimo_ok.isoformat() if ultimo_ok is not None else None,
                "edad_horas": edad_horas,
                "umbral_horas": vigilado.umbral_horas,
                "critico": vigilado.critico,
                "stale": stale,
            }
        )

    if hay_critico_stale:
        response.status_code = 503

    return {
        "status": "stale" if hay_critico_stale else "ok",
        "generado_en": ahora.isoformat(),
        "jobs": jobs,
    }


# ---------------------------------------------------------------------------
# Salud (admin)
# ---------------------------------------------------------------------------


@router.get("/salud")
async def salud(
    request: Request,
    user: Usuario = Depends(api_require_admin),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = request.app.state.settings
    data = get_salud_data(session, settings)
    datos_str = str(data)
    for secreto in ("mp_ticket", "secret_key", "jobs_token"):
        if secreto in datos_str:
            raise RuntimeError(f"get_salud_data filtró el campo '{secreto}'")
    return data


# ---------------------------------------------------------------------------
# Oportunidades
# ---------------------------------------------------------------------------


@router.get("/oportunidades")
async def listar_oportunidades(
    request: Request,
    fuente: str = "",
    texto: str = "",
    perfil_id: int | None = None,
    pagina: int = 1,
    user: Usuario = Depends(api_require_user),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    limit = 50
    offset = (pagina - 1) * limit
    items, total, _ = get_oportunidades_usuario(
        session,
        user.id,
        fuente=fuente or None,
        texto=texto or None,
        perfil_id=perfil_id,
        limit=limit,
        offset=offset,
    )
    return {
        "total": total,
        "pagina": pagina,
        "items": [
            {
                "fuente": item["match"].fuente,
                "codigo": item["match"].codigo_oportunidad,
                "nombre": item["nombre"],
                "score": item["match"].score,
                "estado": item["estado"],
                "fecha_cierre": item["fecha_cierre"].isoformat() if item["fecha_cierre"] else None,
                "dias_al_cierre": item["dias_al_cierre"],
                "monto": item["monto"],
                "organismo": item["organismo"],
                "url_ficha": item["url_ficha"],
            }
            for item in items
        ],
    }


# ---------------------------------------------------------------------------
# Perfiles CRUD
# ---------------------------------------------------------------------------


class PerfilIn(BaseModel):
    nombre: str
    keywords: list[str] = []
    keywords_excluir: list[str] = []
    fuentes: list[str] = ["licitaciones", "compras_agiles"]


@router.get("/perfiles")
async def api_listar_perfiles(
    request: Request,
    user: Usuario = Depends(api_require_user),
    session: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    perfiles = listar_perfiles(session, user.id)
    return [
        {
            "id": p.id,
            "nombre": p.nombre,
            "keywords": p.keywords,
            "keywords_excluir": p.keywords_excluir,
            "fuentes": p.fuentes,
            "activo": p.activo,
        }
        for p in perfiles
    ]


@router.post("/perfiles", status_code=201)
async def api_crear_perfil(
    request: Request,
    body: PerfilIn,
    user: Usuario = Depends(api_require_user),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    check_csrf(request)
    nuevo = crear_perfil(
        session,
        owner_id=user.id,
        nombre=body.nombre,
        keywords=body.keywords,
        keywords_excluir=body.keywords_excluir,
        fuentes=body.fuentes,
    )
    session.commit()
    return {"id": nuevo.id, "nombre": nuevo.nombre}


@router.put("/perfiles/{perfil_id}")
async def api_actualizar_perfil(
    request: Request,
    perfil_id: int,
    body: PerfilIn,
    user: Usuario = Depends(api_require_user),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    check_csrf(request)
    if obtener_perfil(session, perfil_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    actualizar_perfil(
        session,
        perfil_id=perfil_id,
        owner_id=user.id,
        nombre=body.nombre,
        keywords=body.keywords,
        keywords_excluir=body.keywords_excluir,
        fuentes=body.fuentes,
    )
    session.commit()
    return {"id": perfil_id, "nombre": body.nombre}


@router.delete("/perfiles/{perfil_id}", status_code=204)
async def api_eliminar_perfil(
    request: Request,
    perfil_id: int,
    user: Usuario = Depends(api_require_user),
    session: Session = Depends(get_db),
) -> None:
    check_csrf(request)
    if obtener_perfil(session, perfil_id, user.id) is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    eliminar_perfil(session, perfil_id, user.id)
    session.commit()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@router.post("/jobs/run")
async def jobs_run(
    request: Request,
    background_tasks: BackgroundTasks,
    job: str = "all",
    session: Session = Depends(get_db),
) -> dict[str, object]:
    settings = request.app.state.settings
    token = request.headers.get("X-Jobs-Token", "")
    if not secrets.compare_digest(token, settings.jobs_token):
        raise HTTPException(status_code=401, detail="Token inválido")

    from app.ingest.orchestrator import (
        _ciclo_nocturno,
        _run_with_lock,
        run_alerts,
        run_catalogos,
        run_competencia,
        run_datos_abiertos,
        run_detalles,
        run_lifecycle,
        run_match,
        run_resumen,
        run_retencion,
        run_sync_activas,
        run_sync_ca,
    )

    engine = request.app.state.engine

    # try_lock_fn/unlock_fn se pueden inyectar desde app.state (tests): el default
    # es pg_try_advisory_lock, que solo existe en Postgres.
    lock_kwargs: dict[str, Any] = {}
    for _nombre_fn in ("try_lock_fn", "unlock_fn"):
        _override = getattr(request.app.state, _nombre_fn, None)
        if _override is not None:
            lock_kwargs[_nombre_fn] = _override

    def _locked(nombre: str, fn: Callable[[], Any]) -> Callable[[], Any]:
        """Envuelve un runner en el pg_advisory_lock (regla 13 de CLAUDE.md).

        Todo camino de disparo —scheduler interno, este endpoint y el CLI— toma
        el mismo lock, para que un cron externo no se solape con el ciclo interno
        y gaste la cuota de API dos veces sobre el mismo trabajo. Si el lock está
        ocupado `_run_with_lock` devuelve None y el ciclo se omite: eso es el
        comportamiento correcto, no un error que haya que reintentar.
        """
        return lambda: _run_with_lock(nombre, fn, engine, **lock_kwargs)

    _jobs: dict[str, Any] = {
        "ca": _locked("ca", lambda: run_sync_ca(settings, engine)),
        "activas": _locked("activas", lambda: run_sync_activas(settings, engine)),
        "detalles": _locked("detalles", lambda: run_detalles(settings, engine)),
        "datos-abiertos": _locked("datos-abiertos", lambda: run_datos_abiertos(settings, engine)),
        "lifecycle": _locked("lifecycle", lambda: run_lifecycle(settings, engine)),
        "match": _locked("match", lambda: run_match(settings, engine)),
        "competencia": _locked("competencia", lambda: run_competencia(settings, engine)),
        "alerts": _locked("alerts", lambda: run_alerts(settings, engine)),
        "resumen": _locked("resumen", lambda: run_resumen(settings, engine)),
        "retencion": _locked("retencion", lambda: run_retencion(engine)),
        "catalogos": _locked("catalogos", lambda: run_catalogos(settings, engine)),
        # NO se envuelve en _locked: _ciclo_nocturno ya toma el lock en cada paso
        # interno. Tomarlo por fuera lo dejaría ocupado y cada paso encontraría el
        # lock tomado → el ciclo entero se volvería un no-op silencioso.
        # Su guard de ventana 22:00–07:00 (America/Santiago) queda intacto: los
        # crons externos corren en UTC y no se les cree la hora (regla 5).
        "nocturno": lambda: _ciclo_nocturno(settings, engine),
    }

    def _secuencia(*nombres: str) -> Callable[[], None]:
        """Encadena entradas YA envueltas en `_locked`, sin volver a envolverlas.

        Cada paso toma y suelta el advisory lock por su cuenta —igual que
        `_full_cycle` y que el scheduler interno—. Como se reusan las entradas
        existentes, job_runs sigue registrando cada paso con su nombre propio
        ("ca", "match", …) y el watchlist de /api/salud/jobs no necesita saber
        que existen estos jobs compuestos.
        """

        def _correr() -> None:
            for nombre in nombres:
                _jobs[nombre]()

        return _correr

    # Jobs compuestos: reproducen los grupos que el scheduler interno disparaba
    # junto, para que el cron externo pida la secuencia con una sola llamada
    # (F-invertir-modelo). No reemplazan a `all`: son la cadencia frecuente.
    _jobs["ciclo-ca"] = _secuencia("ca", "match", "alerts")
    _jobs["ciclo-activas"] = _secuencia("activas", "detalles", "match", "alerts")

    _CICLO_COMPLETO = (
        "activas",
        "detalles",
        "datos-abiertos",
        "lifecycle",
        "match",
        "competencia",
        "alerts",
        "resumen",
    )

    def _full_cycle() -> None:
        for nombre in _CICLO_COMPLETO:
            _jobs[nombre]()

    if job == "all":
        background_tasks.add_task(_full_cycle)
    elif job in _jobs:
        background_tasks.add_task(_jobs[job])
    else:
        raise HTTPException(status_code=400, detail=f"job desconocido: {job!r}")

    return {"queued": True, "job": job}
