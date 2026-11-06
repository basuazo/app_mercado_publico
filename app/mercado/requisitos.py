"""Checklist administrativo por Compra Ágil (F-plat-seguimiento §1.1–1.2).

- `empresa_requisitos`: la plantilla de la empresa (editable).
- `ca_requisitos`: la copia que recibe cada CA al priorizarse o entrar a un lote, más un
  requisito por cada documento de la CA que parezca administrativo (`ca_anexos.nombre_original`).
  La semilla es idempotente: no duplica por `(empresa, CA, texto)`.
- Un requisito obligatorio sin cumplir bloquea «enviada» (`equipo.cambiar_estado`).

Sin dependencias de `equipo` (es `equipo` quien importa este módulo). Ownership (reglas 17 y 26):
toda función recibe `empresa_id` y filtra por él. Fuente de los datos de la CA: Dirección ChileCompra.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.retencion import registrar_purga
from app.core.tiempo import ahora_utc
from app.models.tables import CaAnexo, CaRequisito, CaTrabajo, CompraAgil, EmpresaRequisito

# Semilla inicial de la plantilla. Nada se inventa como obligatorio del portal sin verificar [I]:
# todo es editable y los primeros tres son lo que Mario describió (Revisión CBS, 5-nov).
PLANTILLA_INICIAL: tuple[tuple[str, bool], ...] = (
    ("Cotización formal adjunta", True),
    ("Precios ingresados línea a línea en el portal", True),
    ("Plazo de entrega indicado", True),
    ("Documentos de la empresa vigentes (boleta de garantía / certificados si los pide)", False),
)
TEXTO_COTIZACION = PLANTILLA_INICIAL[0][0]  # el que marca la persona tras adjuntar el DOCX

# Nombres de archivo de la CA que parecen documentos administrativos (no anexos de productos)
# [I: ampliable; una lista en un solo lugar].
PATRON_ADMINISTRATIVO = re.compile(
    r"formulario|declaraci|anexo\W+.*oferta|ficha|identificaci", re.IGNORECASE
)

TEXTO_MAX = 300
RETENCION_DIAS = 365  # requisitos de CA descartadas (F-plat-7 §1.4)
ORIGEN_PLANTILLA = "plantilla"
ORIGEN_DOCUMENTO = "documento_ca"
ORIGEN_MANUAL = "manual"


class ErrorRequisito(ValueError):
    """Entrada inválida (→ 422)."""


class RequisitoAjeno(LookupError):
    """Requisito de otra empresa o inexistente (→ 404, regla 26)."""


def _texto(valor: str) -> str:
    t = " ".join((valor or "").split())
    if not t:
        raise ErrorRequisito("El texto es obligatorio")
    if len(t) > TEXTO_MAX:
        raise ErrorRequisito(f"Máximo {TEXTO_MAX} caracteres")
    return t


# ---------------------------------------------------------------------------
# Plantilla de la empresa
# ---------------------------------------------------------------------------


def plantilla(
    session: Session, empresa_id: int, *, solo_activos: bool = False
) -> list[EmpresaRequisito]:
    q = select(EmpresaRequisito).where(EmpresaRequisito.empresa_id == empresa_id)
    if solo_activos:
        q = q.where(EmpresaRequisito.activo.is_(True))
    return list(session.execute(q.order_by(EmpresaRequisito.orden, EmpresaRequisito.id)).scalars())


def sembrar_plantilla(session: Session, empresa_id: int) -> int:
    """Crea la plantilla inicial si la empresa no tiene ninguna (al crearla). Devuelve cuántas."""
    existe = session.execute(
        select(func.count())
        .select_from(EmpresaRequisito)
        .where(EmpresaRequisito.empresa_id == empresa_id)
    ).scalar_one()
    if existe:
        return 0
    for orden, (texto, obligatorio) in enumerate(PLANTILLA_INICIAL):
        session.add(
            EmpresaRequisito(
                empresa_id=empresa_id, texto=texto, obligatorio=obligatorio, orden=orden, activo=True
            )
        )
    session.flush()
    return len(PLANTILLA_INICIAL)


def _de_plantilla(session: Session, empresa_id: int, req_id: int) -> EmpresaRequisito:
    r = session.execute(
        select(EmpresaRequisito).where(
            EmpresaRequisito.id == req_id, EmpresaRequisito.empresa_id == empresa_id
        )
    ).scalar_one_or_none()
    if r is None:
        raise RequisitoAjeno(req_id)
    return r


def agregar_a_plantilla(
    session: Session, empresa_id: int, texto: str, obligatorio: bool
) -> EmpresaRequisito:
    texto = _texto(texto)
    ya = session.execute(
        select(EmpresaRequisito.id).where(
            EmpresaRequisito.empresa_id == empresa_id, EmpresaRequisito.texto == texto
        )
    ).first()
    if ya is not None:
        raise ErrorRequisito("Ya existe un requisito con ese texto")
    orden = (
        session.execute(
            select(func.max(EmpresaRequisito.orden)).where(EmpresaRequisito.empresa_id == empresa_id)
        ).scalar_one()
        or 0
    ) + 1
    r = EmpresaRequisito(
        empresa_id=empresa_id, texto=texto, obligatorio=obligatorio, orden=orden, activo=True
    )
    session.add(r)
    session.flush()
    return r


def editar_plantilla(
    session: Session,
    empresa_id: int,
    req_id: int,
    *,
    texto: str | None = None,
    obligatorio: bool | None = None,
    activo: bool | None = None,
) -> EmpresaRequisito:
    """Cambia la plantilla; no toca lo ya copiado a las CA (cada CA conserva su checklist)."""
    r = _de_plantilla(session, empresa_id, req_id)
    if texto is not None:
        nuevo = _texto(texto)
        if nuevo != r.texto:
            ya = session.execute(
                select(EmpresaRequisito.id).where(
                    EmpresaRequisito.empresa_id == empresa_id, EmpresaRequisito.texto == nuevo
                )
            ).first()
            if ya is not None:
                raise ErrorRequisito("Ya existe un requisito con ese texto")
            r.texto = nuevo
    if obligatorio is not None:
        r.obligatorio = obligatorio
    if activo is not None:
        r.activo = activo
    session.flush()
    return r


# ---------------------------------------------------------------------------
# Checklist de una CA
# ---------------------------------------------------------------------------


def _documentos_administrativos(session: Session, codigo: str) -> list[str]:
    """Nombres de los documentos de la CA que parecen administrativos (sin repetir): los que lista
    el detalle (`compras_agiles.documentos`, F-plat-anexos-2) y los anexos subidos."""
    nombres: list[str] = []
    listados = session.execute(
        select(CompraAgil.documentos).where(CompraAgil.codigo == codigo)
    ).scalar_one_or_none()
    candidatos = [
        str(d.get("nombre") or "") for d in (listados or []) if isinstance(d, dict)
    ] + [
        n
        for (n,) in session.execute(
            select(CaAnexo.nombre_original).where(CaAnexo.ca_codigo == codigo).order_by(CaAnexo.id)
        )
    ]
    for nombre in candidatos:
        n = " ".join((nombre or "").split())
        if n and PATRON_ADMINISTRATIVO.search(n) and n not in nombres:
            nombres.append(n)
    return nombres


def sembrar_ca(session: Session, empresa_id: int, codigo: str) -> int:
    """Copia la plantilla activa de la empresa y los documentos administrativos de la CA a su
    checklist. Idempotente: no duplica por `(empresa, CA, texto)`. No hace commit. Devuelve
    cuántos requisitos agregó."""
    existentes = set(
        session.execute(
            select(CaRequisito.texto).where(
                CaRequisito.empresa_id == empresa_id, CaRequisito.ca_codigo == codigo
            )
        ).scalars()
    )
    orden = len(existentes)
    candidatos: list[tuple[str, bool, str]] = [
        (r.texto, r.obligatorio, ORIGEN_PLANTILLA)
        for r in plantilla(session, empresa_id, solo_activos=True)
    ]
    candidatos += [
        (f"Documento de la CA: {n}"[:TEXTO_MAX], True, ORIGEN_DOCUMENTO)
        for n in _documentos_administrativos(session, codigo)
    ]
    nuevos = 0
    for texto, obligatorio, origen in candidatos:
        if texto in existentes:
            continue
        try:
            with session.begin_nested():
                session.add(
                    CaRequisito(
                        empresa_id=empresa_id,
                        ca_codigo=codigo,
                        texto=texto,
                        obligatorio=obligatorio,
                        origen=origen,
                        orden=orden,
                    )
                )
        except IntegrityError:  # otra sesión la sembró a la vez: es lo mismo
            continue
        existentes.add(texto)
        orden += 1
        nuevos += 1
    session.flush()
    return nuevos


def listar(session: Session, empresa_id: int, codigo: str) -> list[CaRequisito]:
    return list(
        session.execute(
            select(CaRequisito)
            .where(CaRequisito.empresa_id == empresa_id, CaRequisito.ca_codigo == codigo)
            .order_by(CaRequisito.orden, CaRequisito.id)
        ).scalars()
    )


def pendientes(session: Session, empresa_id: int, codigo: str) -> list[str]:
    """Textos de los requisitos obligatorios sin cumplir."""
    return list(
        session.execute(
            select(CaRequisito.texto)
            .where(
                CaRequisito.empresa_id == empresa_id,
                CaRequisito.ca_codigo == codigo,
                CaRequisito.obligatorio.is_(True),
                CaRequisito.cumplido.is_(False),
            )
            .order_by(CaRequisito.orden, CaRequisito.id)
        ).scalars()
    )


def _de_ca(session: Session, empresa_id: int, req_id: int) -> CaRequisito:
    r = session.execute(
        select(CaRequisito).where(CaRequisito.id == req_id, CaRequisito.empresa_id == empresa_id)
    ).scalar_one_or_none()
    if r is None:
        raise RequisitoAjeno(req_id)
    return r


def marcar(
    session: Session, empresa_id: int, usuario_id: int, req_id: int, cumplido: bool
) -> CaRequisito:
    r = _de_ca(session, empresa_id, req_id)
    if r.cumplido != cumplido:
        r.cumplido = cumplido
        r.cumplido_por_id = usuario_id if cumplido else None
        r.cumplido_en = ahora_utc() if cumplido else None
        session.flush()
    return r


def agregar_manual(
    session: Session, empresa_id: int, codigo: str, texto: str, obligatorio: bool
) -> CaRequisito:
    texto = _texto(texto)
    actuales = listar(session, empresa_id, codigo)
    if any(r.texto == texto for r in actuales):
        raise ErrorRequisito("Ya existe un requisito con ese texto")
    r = CaRequisito(
        empresa_id=empresa_id,
        ca_codigo=codigo,
        texto=texto,
        obligatorio=obligatorio,
        origen=ORIGEN_MANUAL,
        orden=len(actuales),
    )
    session.add(r)
    session.flush()
    return r


def quitar(session: Session, empresa_id: int, req_id: int) -> str:
    """Quita un requisito de la CA (no de la plantilla). Devuelve el código de la CA."""
    r = _de_ca(session, empresa_id, req_id)
    codigo = r.ca_codigo
    session.delete(r)
    session.flush()
    return codigo


def resumen_por_ca(
    session: Session, empresa_id: int, codigos: list[str]
) -> dict[str, tuple[int, int]]:
    """`{codigo: (cumplidos, total)}` de los requisitos obligatorios (para las tarjetas)."""
    if not codigos:
        return {}
    out: dict[str, tuple[int, int]] = {}
    for codigo, cumplido, n in session.execute(
        select(CaRequisito.ca_codigo, CaRequisito.cumplido, func.count())
        .where(
            CaRequisito.empresa_id == empresa_id,
            CaRequisito.ca_codigo.in_(codigos),
            CaRequisito.obligatorio.is_(True),
        )
        .group_by(CaRequisito.ca_codigo, CaRequisito.cumplido)
    ):
        c, t = out.get(codigo, (0, 0))
        out[codigo] = (c + (n if cumplido else 0), t + n)
    return out


# ---------------------------------------------------------------------------
# Retención (F-plat-7 §1.4)
# ---------------------------------------------------------------------------


def purgar(session: Session, ahora: datetime, dias: int = RETENCION_DIAS) -> int:
    """Borra los requisitos de CA `descartada` sin cambios hace más de `dias`. Los de una CA viva
    (o enviada, o con venta) no se tocan."""
    corte = ahora - timedelta(days=dias)
    descartada_vieja = exists().where(
        CaTrabajo.empresa_id == CaRequisito.empresa_id,
        CaTrabajo.ca_codigo == CaRequisito.ca_codigo,
        CaTrabajo.estado == "descartada",
        CaTrabajo.actualizado_en < corte,
    )
    r = session.execute(
        delete(CaRequisito).where(descartada_vieja).execution_options(synchronize_session=False)
    )
    return int(getattr(r, "rowcount", 0) or 0)


registrar_purga("requisitos", purgar)
