"""Rutas REST JSON: /api/oportunidades, /api/perfiles, /api/salud, /api/jobs."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import (
    api_require_admin,
    api_require_user,
    check_csrf,
    get_db,
)
from app.api.query import get_oportunidades_usuario
from app.api.salud_data import get_salud_data
from app.matching.perfiles import (
    actualizar_perfil,
    crear_perfil,
    eliminar_perfil,
    listar_perfiles,
    obtener_perfil,
)
from app.models.enums import FrecuenciaAlerta
from app.models.tables import Usuario

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Ping público
# ---------------------------------------------------------------------------


@router.get("/salud/ping")
async def ping() -> dict[str, str]:
    return {"status": "ok"}


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
    items, total = get_oportunidades_usuario(
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
    frecuencia_alerta: str = "inmediata"


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
            "frecuencia_alerta": p.frecuencia_alerta,
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
        frecuencia_alerta=FrecuenciaAlerta(body.frecuencia_alerta),
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
        frecuencia_alerta=FrecuenciaAlerta(body.frecuencia_alerta),
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
        run_alerts,
        run_competencia,
        run_datos_abiertos,
        run_detalles,
        run_digest,
        run_lifecycle,
        run_match,
        run_sync_activas,
        run_sync_ca,
    )

    engine = request.app.state.engine

    _jobs: dict[str, Any] = {
        "ca": lambda: run_sync_ca(settings, engine),
        "activas": lambda: run_sync_activas(settings, engine),
        "detalles": lambda: run_detalles(settings, engine),
        "datos-abiertos": lambda: run_datos_abiertos(settings, engine),
        "lifecycle": lambda: run_lifecycle(settings, engine),
        "match": lambda: run_match(settings, engine),
        "competencia": lambda: run_competencia(settings, engine),
        "alerts": lambda: run_alerts(settings, engine),
        "digest": lambda: run_digest(settings, engine),
    }

    def _full_cycle() -> None:
        run_sync_activas(settings, engine)
        run_detalles(settings, engine)
        run_datos_abiertos(settings, engine)
        run_lifecycle(settings, engine)
        run_match(settings, engine)
        run_competencia(settings, engine)
        run_alerts(settings, engine)

    if job == "all":
        background_tasks.add_task(_full_cycle)
    elif job in _jobs:
        background_tasks.add_task(_jobs[job])
    else:
        raise HTTPException(status_code=400, detail=f"job desconocido: {job!r}")

    return {"queued": True, "job": job}
