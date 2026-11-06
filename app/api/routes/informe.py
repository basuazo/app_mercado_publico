"""`/mesa/informe/{AAAA-MM}`: el informe mensual de la empresa (F-plat-7 §1.2), el mismo del correo.

Ownership (reglas 17 y 26): la empresa sale de la sesión (`html_require_empresa`), nunca de la URL;
un mes mal formado o futuro es 404. Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, html_require_empresa
from app.api.routes.pages import _TEMPLATES, _ctx
from app.core.tiempo import ahora_utc
from app.mercado import informe
from app.models.tables import Usuario

router = APIRouter()


@router.get("/mesa/informe/{mes}", response_class=HTMLResponse)
async def mesa_informe(
    request: Request,
    mes: str,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = sesion
    ahora = ahora_utc()
    if not informe.mes_valido(mes) or informe.rango_mes_utc(mes)[0] > ahora:
        raise HTTPException(status_code=404, detail="Informe no encontrado")
    datos = informe.construir(session, empresa_id, mes, ahora)
    return _TEMPLATES.TemplateResponse(request, "mesa_informe.html", _ctx(request, user, i=datos))
