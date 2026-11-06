"""Precio, mi oferta y modo sombra (F-plat-5).

- `/mesa/ca/{codigo}/competidores`: quiénes le han ofertado al comprador en la familia (12 meses),
  como historial (no «competidores probables»: el top 1 «esperado» ofertó solo en 8–11 % de las CA).
- `/mesa/ca/{codigo}/precio`: la sección «Precio y mi oferta» de la ficha interna (la carga por
  HTMX al abrirse). Evalúa EN VIVO con `query.evaluacion_ca`; la lista de la mesa nunca lo hace.
- `simular` (sin persistir), `oferta` (guardar), `enviar` (`ca_trabajo` → enviada) y `exportar`
  (XLSX para transcribir al portal: no hay API para enviar ofertas [I]).
- `/mesa/resultados`: el juez del peldaño 2.

Ownership (reglas 17 y 26): todo pasa por `html_require_empresa`; la empresa sale de la sesión,
nunca del cliente. Una CA fuera del territorio activo o un proveedor de otra empresa → 404.
CSRF en simular, guardar, enviar y exportar (regla 18). Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import check_csrf, get_db, html_require_empresa
from app.api.query import _url_ficha, evaluacion_ca, mostrar_ficha_oficial
from app.api.routes.pages import _TEMPLATES, _ctx, _es_htmx
from app.auth.csrf import generate_csrf_token
from app.core.tiempo import ahora_utc
from app.mercado import adjudicacion as adj
from app.mercado import equipo as eq
from app.mercado import oferta as of
from app.mercado import sombra
from app.mercado.competencia import competidores_de
from app.mercado.comprador import clave_unidad
from app.mercado.territorio import regiones_activas
from app.models.tables import CaProducto, CompraAgil, Empresa, Prediccion, Usuario

router = APIRouter()

COMPETIDORES_TARJETA = 8
_ETIQUETA_PUNTO = {"agresivo": "Agresivo", "sugerido": "Sugerido", "comodo": "Cómodo"}


def _csrf(request: Request) -> str:
    return generate_csrf_token(request.app.state.settings.secret_key, request.state.csrf_nonce)


def _ca_o_404(request: Request, session: Session, codigo: str) -> CompraAgil:
    """Una CA del territorio activo; si no, 404 (regla 26: indistinguible de inexistente)."""
    ca = session.get(CompraAgil, codigo)
    if ca is None or ca.region not in regiones_activas(session, request.app.state.settings):
        raise HTTPException(status_code=404, detail="Compra Ágil no encontrada")
    return ca


def _tasa(request: Request) -> float:
    return float(request.app.state.settings.costo_financiero_mensual)


# ---------------------------------------------------------------------------
# Competidores (historial) de la tarjeta
# ---------------------------------------------------------------------------


@router.get("/mesa/ca/{codigo}/competidores", response_class=HTMLResponse)
async def mesa_competidores(
    request: Request,
    codigo: str,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    _, empresa_id = sesion
    ca = _ca_o_404(request, session, codigo)
    regiones = regiones_activas(session, request.app.state.settings)
    familia = adj.familia_de_ca(
        session.execute(
            select(CaProducto.codigo_producto, CaProducto.nombre).where(
                CaProducto.ca_codigo == codigo
            )
        ).tuples()
    )
    datos = competidores_de(
        session, clave_unidad(ca.organismo_rut, ca.unidad_compra), familia, regiones,
        nosotros=eq.rut_empresa(session.get(Empresa, empresa_id)),
    )  # fmt: skip
    return _TEMPLATES.TemplateResponse(
        request,
        "_mesa_competidores.html",
        {
            "d": datos,
            "familia": familia,
            "competidores": datos["competidores"][:COMPETIDORES_TARJETA],
            "n_mas": max(0, len(datos["competidores"]) - COMPETIDORES_TARJETA),
        },
    )


# ---------------------------------------------------------------------------
# Sección «Precio y mi oferta» de la ficha
# ---------------------------------------------------------------------------


def curva_svg(curva: list[dict[str, Any]], ancho: int = 320, alto: int = 140) -> dict[str, Any]:
    """Puntos (precio, P) → coordenadas de un SVG inline (sin librería de gráficos)."""
    if not curva:
        return {}
    precios = [float(c["precio"]) for c in curva]
    lo, hi = min(precios), max(precios)
    span = (hi - lo) or max(hi, 1.0)
    x0, x1, y0, y1 = 40, ancho - 15, 15, alto - 30
    puntos = []
    for c in curva:
        x = x0 + (float(c["precio"]) - lo) / span * (x1 - x0) if hi > lo else (x0 + x1) / 2
        y = y1 - float(c["p"]) * (y1 - y0)
        puntos.append({**c, "x": round(x, 1), "y": round(y, 1),
                       "etiqueta": _ETIQUETA_PUNTO.get(c["nombre"], c["nombre"])})  # fmt: skip
    return {
        "ancho": ancho, "alto": alto, "x0": x0, "x1": x1, "y0": y0, "y1": y1,
        "puntos": puntos, "linea": " ".join(f"{p['x']},{p['y']}" for p in puntos),
    }  # fmt: skip


def _curva_dicts(ev: adj.EvaluacionCA | None) -> list[dict[str, Any]]:
    return [{"nombre": p.nombre, "precio": p.precio, "p": p.p} for p in ev.curva] if ev else []


def _contexto(
    request: Request,
    session: Session,
    user: Usuario,
    empresa_id: int,
    ca: CompraAgil,
    *,
    entradas: dict[int, of.Entrada] | None = None,
    plazo: int | None = None,
    notas: str | None = None,
    mensaje: str | None = None,
    error: str | None = None,
    conflicto: str | None = None,
) -> dict[str, Any]:
    tasa = _tasa(request)
    ev = evaluacion_ca(session, ca.codigo, empresa_id, costo_financiero_mensual=tasa)
    guardada = of.oferta_de(session, empresa_id, ca.codigo)
    lineas = of.armar(session, empresa_id, ca, ev, guardada, entradas)
    sim = of.simular(session, empresa_id, ca, lineas, tasa)
    pred = session.execute(
        select(Prediccion).where(
            Prediccion.empresa_id == empresa_id, Prediccion.ca_codigo == ca.codigo
        )
    ).scalar_one_or_none()
    trabajo = eq.trabajo_de(session, empresa_id, ca.codigo)
    empresa = session.get(Empresa, empresa_id)
    return {
        "ca": ca,
        "codigo": ca.codigo,
        "ev": ev,
        "por_n": {e.n: e for e in ev.lineas} if ev else {},
        "lineas": lineas,
        "sim": sim,
        "svg": curva_svg(_curva_dicts(ev)),
        "pred": pred,
        "pred_svg": curva_svg((pred.detalle or {}).get("curva") or []) if pred else {},
        "guardada": guardada,
        "version": guardada.version if guardada else 0,
        "plazo": plazo
        if plazo is not None
        else (guardada.plazo_entrega_dias if guardada else ca.plazo_entrega_dias),
        "notas": notas if notas is not None else (guardada.notas if guardada else "") or "",
        "estado_equipo": eq.ETIQUETA_ESTADO.get(trabajo.estado if trabajo else eq.NUEVA),
        "enviada": trabajo is not None and trabajo.estado == eq.ENVIADA,
        "empresa_sin_rut": eq.rut_empresa(empresa) is None,
        "url_publica": _url_ficha("compras_agiles", ca.codigo),
        "mostrar_publica": mostrar_ficha_oficial(ca.estado),
        "mensaje": mensaje,
        "error": error,
        "conflicto": conflicto,
        "guardada_por": eq.nombre_visible(session.get(Usuario, guardada.usuario_id))
        if guardada and guardada.usuario_id
        else None,
        "guardada_hace": eq.hace(guardada.guardada_en, ahora_utc()) if guardada else "",
        "csrf_token": _csrf(request),
        "etiqueta_punto": _ETIQUETA_PUNTO,
    }


def _render(request: Request, ctx: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "_precio_oferta.html", ctx, status_code=status_code)


@router.get("/mesa/ca/{codigo}/precio", response_class=HTMLResponse)
async def mesa_precio(
    request: Request,
    codigo: str,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = sesion
    ca = _ca_o_404(request, session, codigo)
    return _render(request, _contexto(request, session, user, empresa_id, ca))


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


def _crudas(form: dict[str, str]) -> dict[int, tuple[str, str]]:
    """`precio_<linea_id>` y `prov_<linea_id>` → {linea_id: (precio, proveedor)}."""
    out: dict[int, tuple[str, str]] = {}
    for k, v in form.items():
        if k.startswith("precio_") and k[7:].isdigit():
            lid = int(k[7:])
            out[lid] = (v, form.get(f"prov_{lid}", ""))
    return out


def _entradas(
    session: Session, empresa_id: int, codigo: str, form: dict[str, str]
) -> dict[int, of.Entrada]:
    try:
        return of.validar_entradas(session, empresa_id, codigo, _crudas(form))
    except of.ProveedorAjeno:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado") from None


@router.post("/mesa/ca/{codigo}/simular", response_class=HTMLResponse)
async def mesa_simular(
    request: Request,
    codigo: str,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Margen y P(ganar) recalculados con los precios en pantalla. No persiste nada."""
    form = await _form(request)
    check_csrf(request, form.get("csrf_token", ""))
    _, empresa_id = sesion
    ca = _ca_o_404(request, session, codigo)
    try:
        entradas = _entradas(session, empresa_id, codigo, form)
    except of.ErrorOferta as err:
        return _TEMPLATES.TemplateResponse(
            request, "_oferta_simulacion.html", {"sim": None, "error": str(err)}, status_code=422
        )
    tasa = _tasa(request)
    ev = evaluacion_ca(session, codigo, empresa_id, costo_financiero_mensual=tasa)
    lineas = of.armar(
        session, empresa_id, ca, ev, of.oferta_de(session, empresa_id, codigo), entradas
    )
    sim = of.simular(session, empresa_id, ca, lineas, tasa)
    session.rollback()  # nada que guardar: la simulación no persiste
    return _TEMPLATES.TemplateResponse(
        request, "_oferta_simulacion.html", {"sim": sim, "lineas": lineas, "error": None}
    )


def _respuesta(request: Request, ctx: dict[str, Any], status_code: int) -> Response:
    if not _es_htmx(request) and status_code >= 400:
        raise HTTPException(
            status_code=status_code, detail=ctx.get("error") or ctx.get("conflicto")
        )
    return _render(request, ctx, status_code)


@router.post("/mesa/ca/{codigo}/oferta", response_model=None)
async def mesa_guardar_oferta(
    request: Request,
    codigo: str,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """Crea o actualiza la oferta (versión que vio el usuario); `nueva|priorizada` → cotizando."""
    form = await _form(request)
    check_csrf(request, form.get("csrf_token", ""))
    user, empresa_id = sesion
    ca = _ca_o_404(request, session, codigo)
    entradas: dict[int, of.Entrada] | None = None
    notas = form.get("notas", "")
    try:
        entradas = _entradas(session, empresa_id, codigo, form)
        plazo = of.plazo_valido(form.get("plazo", ""))
        version = int(form.get("version", "0") or 0)
        ev = evaluacion_ca(session, codigo, empresa_id, costo_financiero_mensual=_tasa(request))
        lineas = of.armar(
            session, empresa_id, ca, ev, of.oferta_de(session, empresa_id, codigo), entradas
        )
        of.guardar(session, empresa_id, user, ca, lineas, plazo=plazo, notas=notas, version=version)
        session.commit()
    except (of.ErrorOferta, ValueError) as err:
        session.rollback()
        msg = str(err) if isinstance(err, of.ErrorOferta) else "Versión inválida"
        ctx = _contexto(request, session, user, empresa_id, ca, entradas=entradas, notas=notas,
                        error=msg)  # fmt: skip
        return _respuesta(request, ctx, 422)
    except (of.ConflictoOferta, eq.ConflictoVersion) as c:
        session.rollback()
        session.expire_all()
        quien = c.quien
        ctx = _contexto(
            request, session, user, empresa_id, ca,
            conflicto=f"No se guardó: {quien} cambió esta oferta antes. Esto es lo que hay ahora.",
        )  # fmt: skip
        return _respuesta(request, ctx, 409)
    session.expire_all()
    return _render(
        request, _contexto(request, session, user, empresa_id, ca, mensaje="Oferta guardada.")
    )


@router.post("/mesa/ca/{codigo}/enviar", response_model=None)
async def mesa_enviar(
    request: Request,
    codigo: str,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """`ca_trabajo` → `enviada` con `equipo.cambiar_estado` (exige empresa con RUT)."""
    form = await _form(request)
    check_csrf(request, form.get("csrf_token", ""))
    user, empresa_id = sesion
    ca = _ca_o_404(request, session, codigo)
    try:
        of.marcar_enviada(session, empresa_id, user, codigo)
        session.commit()
    except of.ErrorOferta as err:
        session.rollback()
        return _respuesta(
            request, _contexto(request, session, user, empresa_id, ca, error=str(err)), 422
        )
    except eq.ConflictoVersion as c:
        session.rollback()
        ctx = _contexto(request, session, user, empresa_id, ca,
                        conflicto=f"No se aplicó: {c.quien} cambió esta CA antes.")  # fmt: skip
        return _respuesta(request, ctx, 409)
    session.expire_all()
    return _render(
        request,
        _contexto(
            request,
            session,
            user,
            empresa_id,
            ca,
            mensaje="Marcada como enviada. Recuerda subir la oferta en Mercado Público.",
        ),  # fmt: skip
    )


@router.post("/mesa/ca/{codigo}/exportar", response_model=None)
async def mesa_exportar(
    request: Request,
    codigo: str,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """XLSX de la oferta GUARDADA (líneas, cantidades, precio unitario, total, plazo, fuente)."""
    form = await _form(request)
    check_csrf(request, form.get("csrf_token", ""))
    _, empresa_id = sesion
    ca = _ca_o_404(request, session, codigo)
    guardada = of.oferta_de(session, empresa_id, codigo)
    if guardada is None:
        raise HTTPException(status_code=404, detail="Guarda la oferta antes de exportarla")
    lineas = of.armar(session, empresa_id, ca, None, guardada)
    contenido = of.xlsx(ca, lineas, guardada.plazo_entrega_dias)
    nombre = "".join(ch for ch in codigo if ch.isalnum() or ch in "-_") or "oferta"
    return Response(
        content=contenido,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="oferta-{nombre}.xlsx"'},
    )


# ---------------------------------------------------------------------------
# /mesa/resultados
# ---------------------------------------------------------------------------


@router.get("/mesa/resultados", response_class=HTMLResponse)
async def mesa_resultados(
    request: Request,
    semana: str = "",
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = sesion
    ahora = ahora_utc()
    r = sombra.resultados(session, empresa_id, ahora, semana.strip()[:10])
    inicio = session.execute(
        select(Prediccion.creada_en)
        .where(Prediccion.empresa_id == empresa_id)
        .order_by(Prediccion.creada_en)
        .limit(1)
    ).scalar_one_or_none()
    return _TEMPLATES.TemplateResponse(
        request,
        "mesa_resultados.html",
        _ctx(
            request,
            user,
            r=r,
            inicio=inicio,
            etiqueta_resultado=eq.ETIQUETA_RESULTADO,
            sin_resultado=sombra.ofertas_sin_resultado(session, empresa_id, ahora),
        ),
    )
