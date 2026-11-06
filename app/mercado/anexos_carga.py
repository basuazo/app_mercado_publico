"""Carga masiva de anexos por código y API para automatización externa (F-plat-anexos-2 §2.2–§2.3).

Todo archivo entra por el mismo camino que la subida individual (:func:`anexos.procesar_anexo_ca`:
validar → `ca_anexos` → extraer → líneas `dudosa`), con commit por archivo (regla 12: nunca el
lote completo en memoria; un ZIP se lee miembro a miembro). El binario nunca se guarda.

Ownership: la web asocia solo CA de las regiones activas (como `/catalogo`); el endpoint externo
solo CA de las regiones de la empresa dueña del token. Una CA fuera de alcance es «no asociado»,
igual que una inexistente: no se revela si existe.

Token externo: `secrets.token_urlsafe(32)`; se guarda solo su sha256 (`empresas.token_anexos_hash`)
y se compara en tiempo constante (`hmac.compare_digest`, regla 19 como modelo). El en claro se
muestra una sola vez al generarlo y nunca se loguea (regla 1).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import IO, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mercado import anexos
from app.models.tables import CompraAgil, Empresa, SyncState

TIEMPO_ZIP_S = 20.0  # por request de un ZIP: al menos un miembro, y se encadena el resto
LIMITE_API_POR_MIN = 60
LIMITE_PENDIENTES = 200
_PREFIJO_LIMITE = "anexos_api:"

# Estados de la vista previa
NUEVA = "nueva"
DUPLICADO = "duplicado"
NO_ASOCIADO = "no asociado"
YA_RESUELTA = "CA ya resuelta"
INVALIDO = "no se acepta"


@dataclass
class FilaCarga:
    """Una fila de la vista previa o del resultado de la carga masiva."""

    nombre: str
    tamano: int
    codigo: str | None = None
    ca_nombre: str | None = None
    comprador: str | None = None
    estado: str = NUEVA
    detalle: str = ""
    anexo_id: int | None = None
    n_lineas: int = 0
    duplicado: bool = False
    indice: int | None = None  # posición del miembro en el ZIP


def ca_en_alcance(session: Session, codigo: str | None, regiones: list[int]) -> CompraAgil | None:
    """La CA si existe y está en `regiones`; si no, None (sin distinguir por qué)."""
    if not codigo:
        return None
    ca = session.get(CompraAgil, codigo)
    if ca is None or ca.region not in regiones:
        return None
    return ca


def fila_vista(
    session: Session,
    regiones: list[int],
    nombre: str,
    tamano: int,
    sha256: str | None = None,
    motivo: str | None = None,
) -> FilaCarga:
    """Estado de un archivo antes de procesarlo: nueva | duplicado | no asociado | CA ya
    resuelta | no se acepta. `sha256` (si se conoce) detecta el duplicado sin leer el archivo."""
    base = anexos.nombre_base(nombre)
    fila = FilaCarga(nombre=base, tamano=tamano, codigo=anexos.codigo_de_nombre(base))
    if motivo:
        fila.estado, fila.detalle = INVALIDO, motivo
        return fila
    if tamano > anexos.MAX_BYTES:
        fila.estado = INVALIDO
        fila.detalle = f"Supera {anexos.MAX_BYTES // (1024 * 1024)} MB"
        return fila
    ca = ca_en_alcance(session, fila.codigo, regiones)
    if ca is None:
        fila.estado = NO_ASOCIADO
        fila.detalle = (
            "El nombre no empieza con un código de Compra Ágil"
            if fila.codigo is None
            else "No hay una Compra Ágil con ese código en tu territorio"
        )
        return fila
    fila.ca_nombre, fila.comprador = ca.nombre, ca.organismo_nombre
    if sha256 and anexos.anexo_existente(session, ca.codigo, sha256.lower()) is not None:
        fila.estado, fila.duplicado = DUPLICADO, True
        fila.detalle = "Ese archivo ya está cargado en esta Compra Ágil"
    elif ca.anexo_resuelto_en is not None:
        fila.estado = YA_RESUELTA
        fila.detalle = "El anexo de esta Compra Ágil ya se resolvió"
    return fila


def procesar_archivo(
    session: Session,
    regiones: list[int],
    nombre: str,
    data: bytes,
    *,
    extractor: anexos.Extractor | None,
    usuario_id: int | None,
    empresa_id: int | None,
    fuente: str,
    max_paginas_imagen: int,
    codigo: str | None = None,
) -> FilaCarga:
    """Procesa UN archivo y confirma (commit). `codigo` explícito (endpoint externo) o, si no,
    el del nombre. Nunca lanza por el archivo: lo informa en la fila (regla 6)."""
    base = anexos.nombre_base(nombre) or "anexo"
    fila = FilaCarga(nombre=base, tamano=len(data))
    fila.codigo = codigo or anexos.codigo_de_nombre(base)
    ca = ca_en_alcance(session, fila.codigo, regiones)
    if ca is None:
        fila.estado = NO_ASOCIADO
        fila.detalle = "No hay una Compra Ágil con ese código en tu territorio"
        return fila
    fila.ca_nombre, fila.comprador = ca.nombre, ca.organismo_nombre
    try:
        res = anexos.procesar_anexo_ca(
            session, ca, data, base, usuario_id=usuario_id, extractor=extractor,
            max_paginas_imagen=max_paginas_imagen, fuente=fuente, empresa_id=empresa_id,
        )  # fmt: skip
    except anexos.AnexoInvalido as exc:
        session.rollback()
        fila.estado, fila.detalle = INVALIDO, str(exc)
        return fila
    session.commit()
    fila.anexo_id = res.anexo.id
    fila.detalle = res.aviso
    fila.n_lineas = len(res.lineas)
    if res.duplicado:
        fila.estado, fila.duplicado = DUPLICADO, True
    else:
        fila.estado = res.anexo.extraccion_estado
    return fila


def vista_zip(session: Session, regiones: list[int], archivo: IO[bytes]) -> list[FilaCarga]:
    """Vista previa de un ZIP: lee SOLO el directorio (:class:`anexos.AnexoInvalido` si las
    guardas lo rechazan). El duplicado no se conoce sin leer el miembro: se ve al procesar."""
    with _abrir_zip(archivo) as z:
        miembros = anexos.miembros_zip(z)
    filas = []
    for m in miembros:
        f = fila_vista(session, regiones, m.nombre, m.tamano, motivo=m.motivo)
        f.indice = m.indice
        filas.append(f)
    return filas


def procesar_zip(
    session: Session,
    regiones: list[int],
    archivo: IO[bytes],
    desde: int,
    *,
    extractor: anexos.Extractor | None,
    usuario_id: int | None,
    empresa_id: int | None,
    max_paginas_imagen: int,
    tiempo_s: float = TIEMPO_ZIP_S,
    reloj: Callable[[], float] = time.monotonic,
) -> tuple[list[FilaCarga], int | None]:
    """Procesa los miembros del ZIP desde la posición `desde` (en la lista de
    :func:`anexos.miembros_zip`) hasta agotar `tiempo_s` (al menos uno). Devuelve las filas y la
    posición siguiente (None si terminó): la vista encadena otro request con ella. Sin estado en
    el servidor (regla 10): el navegador vuelve a mandar el ZIP; lo ya cargado sale `duplicado`
    sin llamar al modelo."""
    inicio = reloj()
    filas: list[FilaCarga] = []
    with _abrir_zip(archivo) as z:
        miembros = anexos.miembros_zip(z)
        i = max(0, desde)
        while i < len(miembros):
            if filas and reloj() - inicio >= tiempo_s:
                return filas, i
            m = miembros[i]
            i += 1
            if m.motivo:
                f = FilaCarga(nombre=m.nombre, tamano=m.tamano, estado=INVALIDO, detalle=m.motivo)
                f.codigo = anexos.codigo_de_nombre(m.nombre)
            elif ca_en_alcance(session, anexos.codigo_de_nombre(m.nombre), regiones) is None:
                f = fila_vista(session, regiones, m.nombre, m.tamano)
            else:
                try:
                    data = anexos.leer_miembro(z, m)
                except anexos.AnexoInvalido as exc:
                    f = FilaCarga(m.nombre, m.tamano, estado=INVALIDO, detalle=str(exc))
                else:
                    f = procesar_archivo(
                        session, regiones, m.nombre, data, extractor=extractor,
                        usuario_id=usuario_id, empresa_id=empresa_id, fuente="masiva",
                        max_paginas_imagen=max_paginas_imagen,
                    )  # fmt: skip
                    del data
            f.indice = m.indice
            filas.append(f)
    return filas, None


def _abrir_zip(archivo: IO[bytes]) -> zipfile.ZipFile:
    try:
        archivo.seek(0, 2)
        tamano = archivo.tell()
        archivo.seek(0)
    except (OSError, ValueError) as exc:
        raise anexos.AnexoInvalido("No pude leer el ZIP") from exc
    if tamano > anexos.MAX_ZIP_BYTES:
        raise anexos.AnexoInvalido(f"El ZIP supera {anexos.MAX_ZIP_BYTES // (1024 * 1024)} MB")
    try:
        return zipfile.ZipFile(archivo)
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise anexos.AnexoInvalido("El archivo no es un ZIP válido") from exc


# ---------------------------------------------------------------------------
# Token de la API externa (§2.3)
# ---------------------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generar_token(empresa: Empresa) -> str:
    """Token nuevo (reemplaza al anterior). Devuelve el en claro: se muestra UNA vez."""
    token = secrets.token_urlsafe(32)
    empresa.token_anexos_hash = hash_token(token)
    return token


def revocar_token(empresa: Empresa) -> None:
    empresa.token_anexos_hash = None


def empresa_por_token(session: Session, token: str | None) -> Empresa | None:
    """La empresa ACTIVA dueña del token, comparando en tiempo constante; None si no hay."""
    if not token:
        return None
    buscado = hash_token(token)
    encontrada: Empresa | None = None
    for e in session.execute(
        select(Empresa).where(Empresa.token_anexos_hash.isnot(None))
    ).scalars():
        # Se comparan todas (sin cortar en la primera) para no filtrar por tiempo cuál calzó.
        if hmac.compare_digest(buscado, e.token_anexos_hash or "") and e.activa:
            encontrada = e
    return encontrada


def consumir_limite(session: Session, empresa_id: int, ahora: datetime) -> int | None:
    """Rate limit propio: `LIMITE_API_POR_MIN` requests por minuto y token (contador en
    `sync_state`, regla 10). None si pasa; si no, los segundos hasta el próximo minuto
    (`Retry-After`). Confirma el contador (commit)."""
    minuto = ahora.strftime("%Y-%m-%dT%H:%M")
    fuente = f"{_PREFIJO_LIMITE}{empresa_id}"
    fila = session.get(SyncState, fuente)
    if fila is None:
        fila = SyncState(fuente=fuente, requests_usadas_hoy=0)
        session.add(fila)
    if fila.cursor != minuto:
        fila.cursor, fila.requests_usadas_hoy = minuto, 0
    if fila.requests_usadas_hoy >= LIMITE_API_POR_MIN:
        session.commit()
        siguiente = ahora.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return max(1, int((siguiente - ahora).total_seconds()))
    fila.requests_usadas_hoy += 1
    fila.ultima_ejecucion = ahora
    session.commit()
    return None


def pendientes(session: Session, empresa: Empresa, ahora: datetime) -> list[dict[str, Any]]:
    """CA vigentes de las regiones de la empresa que necesitan su anexo (criterio único de
    :func:`app.mercado.anexos_pendientes.necesita_anexo`); por cierre, máx. 200. Solo datos del
    proceso (código, nombre, cierre, nombres de los adjuntos): nada personal."""
    from app.mercado.anexos_pendientes import pendientes as lista

    regiones = [int(r) for r in (empresa.regiones_prioritarias or [])]
    return [
        {
            "codigo": p.codigo,
            "nombre": p.nombre,
            # La base guarda UTC naive (`parse_fecha_iso`): se publica con su `Z`.
            "fecha_cierre": p.fecha_cierre.isoformat() + "Z" if p.fecha_cierre else None,
            "documentos": [{"nombre": d} for d in p.documentos],
        }
        for p in lista(session, regiones, ahora, LIMITE_PENDIENTES)
    ]
