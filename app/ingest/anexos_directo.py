"""Descarga directa del adjunto público de las CA vigentes sin detalle (F-plat-anexos-2 §2.7).

Última alternativa de la regla 9, apagada por defecto (`ANEXOS_DESCARGA_DIRECTA=false`) y,
además, inerte mientras `app.clients.mp_documentos.URL_ADJUNTO` no tenga la URL capturada en el
navegador (R8). Corre dentro de `mercado-ca`, después del detalle de las vigentes y con el mismo
lock: un solo proceso toca mercadopublico.cl. NO usa el ticket ni la cuota de la API.

Estado en Postgres (regla 10), en `sync_state`:
- `anexos_pausa`: `cursor` = hasta cuándo (UTC naive, ISO) no se descarga nada. La pone un 403 o
  un 429 (60 min), igual que la pausa de la API.
- `anexos_descargas:AAAA-MM-DD` (día de Chile): `requests_usadas_hoy` = descargas del día; tope
  `MERCADO_ANEXOS_MAX_DIA`.
- `anexos_cortes`: `notas` = JSON con las fechas de los cortes por 403/429 (las últimas 20); /salud
  cuenta los de los últimos 7 días (más de 2 → apagar y volver al flujo manual, decisiones.md).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.clients.mp_documentos import (
    DescargaBloqueada,
    DescargaTransitoria,
    DocumentosClient,
)
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.models.tables import CompraAgil, SyncState

_log = get_logger(__name__)

FUENTE_PAUSA = "anexos_pausa"
FUENTE_CORTES = "anexos_cortes"
PREFIJO_DESCARGAS = "anexos_descargas:"
PAUSA_MIN = 60
_MAX_CORTES_GUARDADOS = 20


def dia_chile(ahora: datetime) -> str:
    """AAAA-MM-DD del día en Chile (`ahora` es UTC naive)."""
    from datetime import UTC

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


def _sumar_descarga(engine: Engine, ahora: datetime) -> None:
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


def _documentos_de(ca: CompraAgil, del_run: list[Any] | None) -> list[tuple[str, str]]:
    """(id, nombre) de los adjuntos: los del detalle pedido en esta corrida o los guardados."""
    if del_run is not None:
        return [(d.id, d.nombre) for d in del_run]
    return [
        (str(d.get("id")), str(d.get("nombre") or ""))
        for d in (ca.documentos or [])
        if isinstance(d, dict) and d.get("id")
    ]


def bajar_anexos_directo(
    settings: Settings,
    engine: Engine,
    regiones: list[int],
    resultado: dict[str, Any],
    documentos: dict[str, list[Any]] | None = None,
    *,
    cliente: DocumentosClient | None = None,
    extractor: Any | None = None,
    ahora: datetime | None = None,
) -> None:
    """Baja los adjuntos de :func:`app.ingest.mercado.cola_anexos` (CA vigentes de `regiones`,
    `sin_detalle` con placeholder, `n_documentos > 0`, sin anexos) y los pasa por la extracción
    con `fuente = descarga_directa`, `subido_por = NULL`. Tope por corrida `MERCADO_ANEXOS_MAX`
    y por día `MERCADO_ANEXOS_MAX_DIA`. Un 403/429 corta y persiste la pausa."""
    from app.clients.openrouter import OpenRouterClient
    from app.ingest.mercado import cola_anexos
    from app.mercado import anexos

    if not settings.anexos_descarga_directa:
        return
    cliente = cliente or DocumentosClient()
    if not cliente.configurado:
        _log.warning("mercado-ca: ANEXOS_DESCARGA_DIRECTA encendida sin URL del adjunto (R8)")
        resultado["anexos_directa"] = "sin_url"
        return
    extractor = extractor if extractor is not None else OpenRouterClient(settings, engine)
    if not extractor.habilitado:
        resultado["anexos_directa"] = "sin_llm"  # el anexo quedaría sin leer y fuera de la cola
        return
    ahora = ahora or ahora_utc()
    documentos = documentos or {}
    with Session(engine) as s:
        hasta = pausa_hasta(s)
        if hasta is not None and hasta > ahora:
            resultado["anexos_directa"] = "pausa"
            return
        disponibles = settings.mercado_anexos_max_dia - descargas_del_dia(s, ahora)
        cola = cola_anexos(s, regiones, ahora, settings.mercado_anexos_max)
        docs = {}
        for codigo in cola:
            ca = s.get(CompraAgil, codigo)
            if ca is not None:
                docs[codigo] = _documentos_de(ca, documentos.get(codigo))
    tope = max(0, min(settings.mercado_anexos_max, disponibles))
    bajados = procesados = 0
    motivos: dict[str, int] = {}
    cortado = False
    for codigo in cola:
        for doc_id, nombre in docs.get(codigo, []):
            if bajados >= tope:
                break
            try:
                d = cliente.descargar_adjunto(codigo, doc_id)
            except DescargaBloqueada as exc:
                _log.warning("mercado-ca: descarga directa bloqueada (HTTP %s): pausa", exc.status)
                _persistir_corte(engine, ahora_utc(), exc.status)
                resultado["anexos_directa_cortado_por"] = f"http_{exc.status}"
                _sumar_descarga(engine, ahora)
                bajados += 1
                cortado = True
                break
            except DescargaTransitoria as exc:
                motivos[str(exc)] = motivos.get(str(exc), 0) + 1
                _sumar_descarga(engine, ahora)
                bajados += 1
                continue
            _sumar_descarga(engine, ahora)
            bajados += 1
            if d.data is None:
                motivos[d.motivo or "?"] = motivos.get(d.motivo or "?", 0) + 1
                continue
            with Session(engine) as session:
                ca = session.get(CompraAgil, codigo)
                if ca is None:
                    break
                try:
                    anexos.procesar_anexo_ca(
                        session, ca, d.data, nombre or f"{codigo}-{doc_id}", usuario_id=None,
                        extractor=extractor, max_paginas_imagen=settings.llm_max_paginas_imagen,
                        fuente="descarga_directa",
                    )  # fmt: skip
                except anexos.AnexoInvalido as exc:
                    session.rollback()
                    ca = session.get(CompraAgil, codigo)
                    if ca is None:
                        break
                    sha = hashlib.sha256(d.data).hexdigest()
                    if anexos.anexo_existente(session, codigo, sha) is None:
                        session.add(
                            anexos.anexo_rechazado(
                                ca, d.data, nombre, str(exc), fuente="descarga_directa"
                            )
                        )
                session.commit()
                procesados += 1
        if cortado or bajados >= tope:
            break
    resultado.update(
        {
            "anexos_directa_cola": len(cola),
            "anexos_directa_bajados": bajados,
            "anexos_directa_procesados": procesados,
        }
    )
    if motivos:
        resultado["anexos_directa_sin_archivo"] = motivos
