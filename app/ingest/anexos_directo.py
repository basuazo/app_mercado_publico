"""Job `mercado-anexos` (F-plat-anexos-auto): baja el adjunto público de las CA vigentes del
territorio y lo pasa por la lectura automática. Regla 9, última alternativa.

Corre en su propio workflow de Actions (no en Render: regla 12) con el advisory lock de siempre.
NO usa el ticket ni la cuota de la API. Interruptor `ANEXOS_DESCARGA_DIRECTA`; sin
`MP_ADJUNTOS_USER_KEY` no pide nada.

La cola (qué documento falta) vive en `app.mercado.adjuntos`; el cliente HTTP en
`app.clients.mp_documentos`. Cada archivo se procesa en memoria con
`anexos_carga.procesar_archivo(fuente="descarga_directa", empresa_id=None)`: el anexo es dato
público del territorio, no de una empresa. Dedupe por sha256 y relectura en la misma fila de
`ca_anexos` (F-plat-lectura) vienen de ahí. Nada se guarda en disco.

Estado en Postgres (regla 10), en `sync_state`:
- `anexos_pausa`: `cursor` = hasta cuándo (UTC naive, ISO) no se pide nada. La pone un 403 o un
  429 (60 min), igual que la pausa de la API.
- `anexos_descargas:AAAA-MM-DD` (día de Chile): `requests_usadas_hoy` = requests al servicio de
  adjuntos del día (token + descargas); tope `ANEXOS_DESCARGA_MAX`.
- `anexos_cortes`: `notas` = JSON con las fechas de los cortes por 403/429 (las últimas 20); /salud
  cuenta los de los últimos 7 días (más de 2 → apagar y escalar a ChileCompra, decisiones.md).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients.mp_documentos import (
    DescargaBloqueada,
    DescargaTransitoria,
    DocumentosClient,
    TokenNoDisponible,
)
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.mercado import adjuntos
from app.models.tables import SyncState

_log = get_logger(__name__)

FUENTE_PAUSA = "anexos_pausa"
FUENTE_CORTES = "anexos_cortes"
PREFIJO_DESCARGAS = "anexos_descargas:"
PAUSA_MIN = 60
_MAX_CORTES_GUARDADOS = 20


class TopeAlcanzado(Exception):
    """Se llegó al tope de requests del día o de la corrida: no se pide nada más."""


def dia_chile(ahora: datetime) -> str:
    """AAAA-MM-DD del día en Chile (`ahora` es UTC naive)."""
    return ahora.replace(tzinfo=UTC).astimezone(TZ_CHILE).strftime("%Y-%m-%d")


def pausa_hasta(session: Session) -> datetime | None:
    fila = session.get(SyncState, FUENTE_PAUSA)
    if fila is None or not fila.cursor:
        return None
    try:
        return datetime.fromisoformat(fila.cursor)
    except ValueError:
        return None


def descargas_del_dia(session: Session, ahora: datetime) -> int:
    fila = session.get(SyncState, PREFIJO_DESCARGAS + dia_chile(ahora))
    return fila.requests_usadas_hoy if fila is not None else 0


def cortes(session: Session) -> list[datetime]:
    fila = session.get(SyncState, FUENTE_CORTES)
    if fila is None or not fila.notas:
        return []
    try:
        return [datetime.fromisoformat(x) for x in json.loads(fila.notas)]
    except (ValueError, TypeError):
        return []


def cortes_recientes(session: Session, ahora: datetime, dias: int = 7) -> int:
    desde = ahora - timedelta(days=dias)
    return sum(1 for c in cortes(session) if c >= desde)


def _fila(session: Session, fuente: str) -> SyncState:
    fila = session.get(SyncState, fuente)
    if fila is None:
        fila = SyncState(fuente=fuente, requests_usadas_hoy=0)
        session.add(fila)
    return fila


def _sumar_request(engine: Engine, ahora: datetime) -> None:
    """Cuenta UNA request al servicio de adjuntos ANTES de hacerla (regla 10: en Postgres)."""
    with Session(engine) as s:
        dia = dia_chile(ahora)
        fila = _fila(s, PREFIJO_DESCARGAS + dia)
        fila.requests_usadas_hoy = (fila.requests_usadas_hoy or 0) + 1
        fila.fecha_contador = dia
        fila.ultima_ejecucion = ahora
        s.commit()


def _persistir_corte(engine: Engine, ahora: datetime, status: int) -> None:
    """Pausa de 60 min y el corte anotado (regla 9: como la pausa del 429 de la API)."""
    with Session(engine) as s:
        pausa = _fila(s, FUENTE_PAUSA)
        pausa.cursor = (ahora + timedelta(minutes=PAUSA_MIN)).isoformat()
        pausa.notas = f"HTTP {status}"
        pausa.ultima_ejecucion = ahora
        fila = _fila(s, FUENTE_CORTES)
        previos = [c.isoformat() for c in cortes(s)]
        fila.notas = json.dumps([*previos, ahora.isoformat()][-_MAX_CORTES_GUARDADOS:])
        fila.ultima_ejecucion = ahora
        s.commit()


def _anotar(engine: Engine, p: adjuntos.AdjuntoPendiente, motivo: str | None,
            anexo_id: int | None = None) -> None:  # fmt: skip
    doc_id = p.documento_id or adjuntos.LISTADO
    with Session(engine) as s:
        if motivo is None:
            adjuntos.marcar_bajado(s, p.codigo, doc_id, anexo_id, ahora_utc())
        else:
            adjuntos.marcar_fallido(s, p.codigo, doc_id, motivo, ahora_utc())
        try:
            s.commit()
        except IntegrityError:  # la CA se purgó durante la corrida: no hay qué anotar
            s.rollback()
            _log.warning("mercado-anexos: %s ya no existe; no se anota", p.codigo)


def _registrar_listado(
    engine: Engine, p: adjuntos.AdjuntoPendiente, archivos: list[tuple[str, str]]
) -> list[adjuntos.AdjuntoPendiente]:
    with Session(engine) as s:
        nuevos = adjuntos.registrar_listado(s, p.codigo, archivos, ahora_utc())
        try:
            s.commit()
        except IntegrityError:  # la CA se purgó durante la corrida
            s.rollback()
            return []
    # Heredan la prioridad de la CA (alimentos) para el informe; se bajan enseguida.
    return [adjuntos.AdjuntoPendiente(n.codigo, n.documento_id, n.nombre, n.fecha_cierre,
                                      n.pedido, p.alimentos) for n in nuevos]  # fmt: skip


def run_mercado_anexos(
    settings: Settings,
    engine: Engine,
    *,
    cliente: DocumentosClient | None = None,
    extractor: Any | None = None,
    ahora: datetime | None = None,
    reloj: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Una corrida: hasta `ANEXOS_DESCARGA_MAX_CORRIDA` requests (sin pasar el tope del día)
    durante `ANEXOS_DESCARGA_MINUTOS`, en el orden de :func:`adjuntos.cola`. Una CA por listar se
    lista (1 request), sus archivos quedan anotados como `listado` y se bajan enseguida, antes de
    pasar a la CA siguiente: así una CA que cierra pronto queda completa en la misma corrida y lo
    que no alcanzó queda en cola para la próxima. Un 403/429 corta y persiste la pausa; un fallo
    del token corta sin pausa. Idempotente: lo bajado queda en `ca_adjunto_descargas` y un
    archivo repetido no vuelve a llamar al modelo."""
    from app.clients.openrouter import OpenRouterClient
    from app.mercado import anexos_carga as carga
    from app.mercado.territorio import regiones_activas_engine

    if not settings.anexos_descarga_directa:
        return {"omitido": "apagado"}
    cliente = cliente or DocumentosClient.desde_settings(settings)
    if not cliente.configurado:
        _log.warning("mercado-anexos: encendido sin MP_ADJUNTOS_USER_KEY o sin URL del token")
        return {"omitido": "sin_configurar"}
    extractor = extractor if extractor is not None else OpenRouterClient(settings, engine)
    if not extractor.habilitado:
        return {"omitido": "sin_llm"}  # el archivo quedaría sin leer: no se gasta un request
    ahora = ahora or ahora_utc()
    regiones = regiones_activas_engine(engine, settings)
    with Session(engine) as s:
        hasta = pausa_hasta(s)
        if hasta is not None and hasta > ahora:
            return {"omitido": "pausa", "pausa_hasta": hasta.isoformat()}
        disponibles = settings.anexos_descarga_max - descargas_del_dia(s, ahora)
        pendientes = adjuntos.cola(s, regiones, ahora)
    tope = max(0, min(settings.anexos_descarga_max_corrida, disponibles))
    resultado: dict[str, Any] = {
        "regiones": regiones,
        "ca_por_listar": sum(1 for p in pendientes if p.es_listado),
        "archivos_en_cola": sum(1 for p in pendientes if not p.es_listado),
        "tope_corrida": tope,
    }
    if tope == 0:
        resultado["omitido"] = "tope_diario"
        return resultado
    hechas = 0

    def _contar() -> None:
        nonlocal hechas
        if hechas >= tope:
            raise TopeAlcanzado
        _sumar_request(engine, ahora_utc())
        hechas += 1

    cliente.al_pedir = _contar
    conteo = {"listadas": 0, "bajados": 0, "ya_existian": 0, "invalidos": 0, "extraidos": 0,
              "sin_lectura": 0}  # fmt: skip
    motivos: dict[str, int] = {}
    inicio = reloj()
    gasto_antes = _gasto(extractor)
    trabajo = list(pendientes)
    while trabajo:
        p = trabajo.pop(0)
        if reloj() - inicio > settings.anexos_descarga_minutos * 60:
            resultado["cortado_por_tiempo"] = True
            break
        try:
            if p.documento_id is None:
                lst = cliente.listar(p.codigo)
            else:
                d = cliente.descargar_adjunto(p.codigo, p.documento_id)
        except TopeAlcanzado:
            resultado["tope_alcanzado"] = True
            break
        except DescargaBloqueada as exc:
            _log.warning("mercado-anexos: el servicio respondió HTTP %s: pausa de %s min",
                         exc.status, PAUSA_MIN)  # fmt: skip
            _persistir_corte(engine, ahora_utc(), exc.status)
            resultado["cortado_por"] = f"http_{exc.status}"
            break
        except TokenNoDisponible as exc:
            _log.warning("mercado-anexos: sin token anónimo (%s): se corta", exc)
            resultado["cortado_por"] = "token"
            break
        except DescargaTransitoria as exc:
            motivos[str(exc)] = motivos.get(str(exc), 0) + 1
            _anotar(engine, p, str(exc))
            continue
        if p.documento_id is None:
            if lst.archivos is None:
                motivo = f"listar_{lst.motivo}"
                motivos[motivo] = motivos.get(motivo, 0) + 1
                _anotar(engine, p, lst.motivo or "?")
                continue
            conteo["listadas"] += 1
            trabajo[:0] = _registrar_listado(engine, p, lst.archivos)
            continue
        if d.data is None:
            motivo = d.motivo or "?"
            motivos[motivo] = motivos.get(motivo, 0) + 1
            _anotar(engine, p, motivo)
            continue
        conteo["bajados"] += 1
        nombre = p.nombre or f"{p.codigo}-{p.documento_id[:8]}"
        with Session(engine) as s:
            fila = carga.procesar_archivo(
                s, regiones, nombre, d.data, extractor=extractor, usuario_id=None,
                empresa_id=None, fuente="descarga_directa",
                max_paginas_imagen=settings.llm_max_paginas_imagen, codigo=p.codigo,
            )  # fmt: skip
        if fila.estado == carga.INVALIDO:
            conteo["invalidos"] += 1
            _anotar(engine, p, "invalido")
            continue
        if fila.estado == carga.NO_ASOCIADO:  # la CA salió del territorio o se purgó
            continue
        _anotar(engine, p, None, fila.anexo_id)
        if fila.duplicado:
            conteo["ya_existian"] += 1
        elif fila.estado == "hecha":
            conteo["extraidos"] += 1
        else:
            conteo["sin_lectura"] += 1
        if fila.estado == "sin_presupuesto":
            # El presupuesto del mes se agotó: los siguientes quedarían sin leer igual.
            resultado["cortado_por_llm"] = "sin_presupuesto"
            break
    resultado.update(conteo)
    resultado["requests"] = hechas
    resultado["gasto_llm_usd"] = round(max(0.0, _gasto(extractor) - gasto_antes), 4)
    if motivos:
        resultado["sin_archivo"] = motivos
    return resultado


def _gasto(extractor: Any) -> float:
    try:
        return float(extractor.gasto_del_mes())
    except Exception:  # un extractor de prueba sin presupuesto
        return 0.0
