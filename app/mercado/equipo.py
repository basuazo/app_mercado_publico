"""Trabajo en equipo sobre las Compras Ágiles (F-equipo).

- `ca_trabajo`: estado compartido de una CA para una empresa (sin fila = `nueva`). Todo cambio
  manda la `version` que vio el usuario; si no coincide, no se aplica (`ConflictoVersion`, con
  quién y cuándo la cambió). Cada cambio escribe su evento en la MISMA transacción.
- `ca_eventos`: registro que solo se agrega. Los de sistema (`usuario_id` NULL: `cierre_24h`,
  `cierre_4h`, `resultado`) son idempotentes por el índice único parcial.
- `ca_comentarios`: hilo por CA; `@nombre` de un usuario activo de la empresa genera `mencion`.
- No leídos: SIN filas por usuario por evento (nada de fan-out). Una consulta sobre los eventos
  de la empresa posteriores a `usuarios.actividad_leida_hasta`, sin los propios, filtrados por
  las preferencias (`preferencias_alarma`; sin fila = activa). La mención siempre llega.

`generar_alarmas` y `consulta_no_leidos` quedan reutilizables para un resumen por correo futuro.
Sin estado en memoria (regla 10): la marca «una vez por minuto» vive en `sync_state`.
Ownership (reglas 17 y 26): toda función recibe `empresa_id` y filtra por él.
Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.core.logging import get_logger
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.mercado.comprador import rut_competidor
from app.mercado.eleccion import elegir_por_rut
from app.mercado.proveedores import normalizar_rut
from app.models.enums import EstadoOportunidad
from app.models.tables import (
    CaComentario,
    CaCotizacion,
    CaEvento,
    CaTrabajo,
    CompraAgil,
    Empresa,
    PreferenciaAlarma,
    SyncState,
    Usuario,
)

_log = get_logger(__name__)

# --- Estados del flujo (decisión de Boris, 5-nov) -------------------------------------------

NUEVA = "nueva"
PRIORIZADA = "priorizada"
COTIZANDO = "cotizando"
LISTA = "lista"
ENVIADA = "enviada"
DESCARTADA = "descartada"
ESTADOS = (NUEVA, PRIORIZADA, COTIZANDO, LISTA, ENVIADA, DESCARTADA)
ETIQUETA_ESTADO = {
    NUEVA: "Nueva",
    PRIORIZADA: "Priorizada",
    COTIZANDO: "Cotizando",
    LISTA: "Lista para enviar",
    ENVIADA: "Enviada",
    DESCARTADA: "Descartada",
}
# En curso para las alarmas de cierre y el bloque «Hoy».
ACTIVOS = (PRIORIZADA, COTIZANDO, LISTA)
MOTIVO_MAX = 300

# --- Eventos ---------------------------------------------------------------------------------

EV_ESTADO = "estado"
EV_ASIGNADA = "asignada"
EV_COMENTARIO = "comentario"
EV_MENCION = "mencion"
EV_CIERRE_24H = "cierre_24h"
EV_CIERRE_4H = "cierre_4h"
EV_RESULTADO = "resultado"
TIPOS_USUARIO = (EV_ESTADO, EV_ASIGNADA, EV_COMENTARIO, EV_MENCION)
TIPOS_SISTEMA = (EV_CIERRE_24H, EV_CIERRE_4H, EV_RESULTADO)
TIPOS_EVENTO = TIPOS_USUARIO + TIPOS_SISTEMA
ETIQUETA_TIPO = {
    EV_ESTADO: "Cambios de estado",
    EV_ASIGNADA: "Asignaciones",
    EV_COMENTARIO: "Comentarios",
    EV_MENCION: "Menciones",
    EV_CIERRE_24H: "Cierra en 24 h",
    EV_CIERRE_4H: "Cierra en 4 h",
    EV_RESULTADO: "Resultados",
}

# --- Preferencias (las cuatro que eligió Boris; sin fila = activa) ---------------------------

PREF_EQUIPO = "actividad_equipo"
PREF_ASIGNADA = "asignada_a_mi"
PREF_CIERRE = "cierre_cercano"
PREF_RESULTADO = "resultado"
PREFERENCIAS = (PREF_EQUIPO, PREF_ASIGNADA, PREF_CIERRE, PREF_RESULTADO)
ETIQUETA_PREFERENCIA = {
    PREF_EQUIPO: "Actividad del equipo (priorizar, avanzar, comentar, asignar a otros)",
    PREF_ASIGNADA: "Cuando me asignan una CA",
    PREF_CIERRE: "Cierre cercano (24 h y 4 h) de CA en curso",
    PREF_RESULTADO: "Resultado de las CA enviadas",
}

# --- Resultado de una CA enviada (§1.3) -----------------------------------------------------

GANADA = "ganada"
PERDIDA = "perdida"
NO_REGISTRADA = "no_registrada"
SIN_DATO = "sin_dato"
DESIERTA = "desierta"
ETIQUETA_RESULTADO = {
    GANADA: "Ganada",
    PERDIDA: "Perdida",
    NO_REGISTRADA: "Nuestra oferta no aparece",
    SIN_DATO: "Sin dato de selección",
    DESIERTA: "Desierta o cancelada",
}
_ESTADOS_DESIERTA = (EstadoOportunidad.DESIERTA.value, EstadoOportunidad.CANCELADA.value)

TEXTO_MAX = 2000
SONDEO_MIN_S = 60  # generar alarmas desde el sondeo: a lo más una vez por minuto por empresa
FUENTE_SONDEO = "alarmas_equipo:{}"
RETENCION_EVENTOS_DIAS = 365  # 12 meses (regla 11); `ca_trabajo` no se purga
DIAS_SIN_MARCA = 7  # sin `actividad_leida_hasta`, solo es «no leído» lo de los últimos 7 días


# ---------------------------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------------------------


class ErrorEquipo(Exception):
    """Entrada inválida (→ 422)."""


class NoEncontrado(Exception):
    """Recurso de otra empresa o inexistente (→ 404)."""


class SinPermiso(Exception):
    """Mismo equipo, pero no es su comentario (→ 403)."""


@dataclass
class ConflictoVersion(Exception):
    """La CA cambió desde que el usuario la vio: el cambio no se aplicó."""

    trabajo: CaTrabajo | None
    quien: str
    cuando: datetime | None

    def __str__(self) -> str:
        return f"Conflicto de versión ({self.quien})"


# ---------------------------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------------------------


def nombre_visible(u: Usuario | None) -> str:
    """`usuarios.nombre` o, si falta, la parte del correo antes de la @."""
    if u is None:
        return "Alguien"
    if u.nombre and u.nombre.strip():
        return u.nombre.strip()
    return (u.email or "").split("@", 1)[0] or "Alguien"


def iniciales(nombre: str) -> str:
    partes = [p for p in re.split(r"[\s._-]+", nombre) if p]
    if not partes:
        return "?"
    if len(partes) == 1:
        return partes[0][:2].upper()
    return (partes[0][0] + partes[1][0]).upper()


def miembros(session: Session, empresa_id: int) -> list[Usuario]:
    """Usuarios activos de la empresa (a quienes se puede asignar y mencionar)."""
    return list(
        session.execute(
            select(Usuario)
            .where(Usuario.empresa_id == empresa_id, Usuario.activo.is_(True))
            .order_by(Usuario.nombre, Usuario.email)
        ).scalars()
    )


def _nombres(session: Session, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {
        u.id: nombre_visible(u)
        for u in session.execute(select(Usuario).where(Usuario.id.in_(ids))).scalars()
    }


# ---------------------------------------------------------------------------------------------
# Estado compartido
# ---------------------------------------------------------------------------------------------


def trabajo_de(session: Session, empresa_id: int, codigo: str) -> CaTrabajo | None:
    return session.execute(
        select(CaTrabajo).where(CaTrabajo.empresa_id == empresa_id, CaTrabajo.ca_codigo == codigo)
    ).scalar_one_or_none()


def _evento(
    session: Session,
    empresa_id: int,
    codigo: str,
    usuario_id: int | None,
    tipo: str,
    datos: dict[str, Any] | None = None,
    *,
    destinatario_id: int | None = None,
    creado_en: datetime | None = None,
) -> CaEvento:
    ev = CaEvento(
        empresa_id=empresa_id,
        ca_codigo=codigo,
        usuario_id=usuario_id,
        tipo=tipo,
        destinatario_id=destinatario_id,
        datos=datos or None,
        creado_en=creado_en or ahora_utc(),
    )
    session.add(ev)
    return ev


def _conflicto(session: Session, empresa_id: int, codigo: str) -> ConflictoVersion:
    """Quién hizo el último cambio de la CA (para el mensaje «Gonzalo la pasó a … hace 2 min»)."""
    t = trabajo_de(session, empresa_id, codigo)
    quien = "Alguien del equipo"
    if t is not None and t.actualizado_por_id is not None:
        quien = _nombres(session, {t.actualizado_por_id}).get(t.actualizado_por_id, quien)
    return ConflictoVersion(trabajo=t, quien=quien, cuando=t.actualizado_en if t else None)


def _bloquear(session: Session, empresa_id: int, codigo: str, version: int) -> CaTrabajo | None:
    """Fila de trabajo bloqueada (FOR UPDATE en Postgres) si su versión es la que vio el usuario.

    `version` 0 = el usuario la vio sin fila (`nueva`). Lanza `ConflictoVersion` si no calza.
    """
    t = session.execute(
        select(CaTrabajo)
        .where(CaTrabajo.empresa_id == empresa_id, CaTrabajo.ca_codigo == codigo)
        .with_for_update()
    ).scalar_one_or_none()
    actual = t.version if t is not None else 0
    if actual != version:
        raise _conflicto(session, empresa_id, codigo)
    return t


def _guardar_nueva(session: Session, t: CaTrabajo, empresa_id: int, codigo: str) -> None:
    """Inserta la fila: si otro la creó entre medio (UNIQUE empresa × CA), es un conflicto.

    Es lo primero que escribe la operación, así que ante el choque se revierte la sesión entera
    (sin savepoint: con pysqlite un SAVEPOINT fuera de transacción confirma al liberarse)."""
    session.add(t)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise _conflicto(session, empresa_id, codigo) from None


def cambiar_estado(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    codigo: str,
    nuevo: str,
    version: int,
    motivo: str = "",
) -> CaTrabajo:
    """Cambia el estado (transiciones libres; `descartada` exige motivo). No hace commit."""
    if nuevo not in ESTADOS:
        raise ErrorEquipo("Estado desconocido")
    motivo = (motivo or "").strip()[:MOTIVO_MAX]
    if nuevo == DESCARTADA and not motivo:
        raise ErrorEquipo("Para descartar hay que indicar un motivo")
    ahora = ahora_utc()
    t = _bloquear(session, empresa_id, codigo, version)
    anterior = t.estado if t is not None else NUEVA
    if anterior == nuevo and nuevo != DESCARTADA:
        return t if t is not None else _sin_fila(empresa_id, codigo)
    if t is None:
        t = CaTrabajo(
            empresa_id=empresa_id,
            ca_codigo=codigo,
            estado=nuevo,
            version=1,
            creado_en=ahora,
            actualizado_en=ahora,
            actualizado_por_id=usuario.id,
        )
        _guardar_nueva(session, t, empresa_id, codigo)
    else:
        t.estado = nuevo
        t.version += 1
        t.actualizado_en = ahora
        t.actualizado_por_id = usuario.id
    t.motivo_descarte = motivo if nuevo == DESCARTADA else None
    if nuevo != ENVIADA:
        t.resultado = None
        t.resultado_en = None
    datos: dict[str, Any] = {"de": anterior, "a": nuevo}
    if motivo and nuevo == DESCARTADA:
        datos["motivo"] = motivo
    _evento(session, empresa_id, codigo, usuario.id, EV_ESTADO, datos, creado_en=ahora)
    session.flush()
    return t


def _sin_fila(empresa_id: int, codigo: str) -> CaTrabajo:
    return CaTrabajo(empresa_id=empresa_id, ca_codigo=codigo, estado=NUEVA, version=0)


def asignar(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    codigo: str,
    responsable_id: int | None,
    version: int,
) -> CaTrabajo:
    """Asigna (o quita, con None) el responsable. Solo usuarios ACTIVOS de la misma empresa.

    Asignar una CA `nueva` la crea en `priorizada`: tener responsable es empezar a trabajarla.
    No hace commit.
    """
    if responsable_id is not None:
        r = session.get(Usuario, responsable_id)
        if r is None or not r.activo or r.empresa_id != empresa_id:
            raise ErrorEquipo("Solo se puede asignar a alguien activo de tu empresa")
    ahora = ahora_utc()
    t = _bloquear(session, empresa_id, codigo, version)
    if t is not None and t.responsable_id == responsable_id:
        return t
    if t is None:
        if responsable_id is None:
            return _sin_fila(empresa_id, codigo)
        t = CaTrabajo(
            empresa_id=empresa_id,
            ca_codigo=codigo,
            estado=PRIORIZADA,
            responsable_id=responsable_id,
            version=1,
            creado_en=ahora,
            actualizado_en=ahora,
            actualizado_por_id=usuario.id,
        )
        _guardar_nueva(session, t, empresa_id, codigo)
        _evento(
            session, empresa_id, codigo, usuario.id, EV_ESTADO, {"de": NUEVA, "a": PRIORIZADA},
            creado_en=ahora,
        )
    else:
        t.responsable_id = responsable_id
        t.version += 1
        t.actualizado_en = ahora
        t.actualizado_por_id = usuario.id
    _evento(
        session,
        empresa_id,
        codigo,
        usuario.id,
        EV_ASIGNADA,
        {"responsable_id": responsable_id},
        destinatario_id=responsable_id,
        creado_en=ahora,
    )
    session.flush()
    return t


# ---------------------------------------------------------------------------------------------
# Comentarios y menciones
# ---------------------------------------------------------------------------------------------


def _validar_texto(texto: str) -> str:
    t = (texto or "").replace("\r\n", "\n").strip()
    if not t:
        raise ErrorEquipo("El comentario está vacío")
    if len(t) > TEXTO_MAX:
        raise ErrorEquipo(f"El comentario supera los {TEXTO_MAX} caracteres")
    return t


def resolver_menciones(texto: str, candidatos: list[Usuario]) -> list[Usuario]:
    """Usuarios mencionados con `@nombre` (nombre visible o parte del correo antes de la @).

    Se resuelve en el servidor contra `candidatos` (activos de la misma empresa); sin distinguir
    mayúsculas y por palabra completa, los nombres más largos primero. Un `@` que no calza es
    texto.
    """
    encontrados: dict[int, Usuario] = {}
    alias: list[tuple[str, Usuario]] = []
    for u in candidatos:
        for a in {nombre_visible(u), (u.email or "").split("@", 1)[0]}:
            if a.strip():
                alias.append((a.strip(), u))
    for a, u in sorted(alias, key=lambda x: -len(x[0])):
        if u.id in encontrados:
            continue
        if re.search(rf"(?<![\w@])@{re.escape(a)}(?![\w])", texto, flags=re.IGNORECASE):
            encontrados[u.id] = u
    return list(encontrados.values())


def comentar(
    session: Session, empresa_id: int, usuario: Usuario, codigo: str, texto: str
) -> CaComentario:
    """Comentario nuevo + evento `comentario` + un `mencion` por persona. No hace commit.

    No crea `ca_trabajo` ni cambia el estado. El evento y las menciones llevan el MISMO
    `creado_en` (así la consulta de no leídos no cuenta dos veces un comentario que me menciona).
    """
    t = _validar_texto(texto)
    ahora = ahora_utc()
    c = CaComentario(
        empresa_id=empresa_id, ca_codigo=codigo, usuario_id=usuario.id, texto=t, creado_en=ahora
    )
    session.add(c)
    session.flush()
    _evento(
        session, empresa_id, codigo, usuario.id, EV_COMENTARIO, {"comentario_id": c.id},
        creado_en=ahora,
    )
    otros = [m for m in miembros(session, empresa_id) if m.id != usuario.id]
    for m in resolver_menciones(t, otros):
        _evento(
            session,
            empresa_id,
            codigo,
            usuario.id,
            EV_MENCION,
            {"comentario_id": c.id},
            destinatario_id=m.id,
            creado_en=ahora,
        )
    session.flush()
    return c


def _comentario_propio(
    session: Session, empresa_id: int, usuario: Usuario, comentario_id: int
) -> CaComentario:
    c = session.get(CaComentario, comentario_id)
    if c is None or c.empresa_id != empresa_id:
        raise NoEncontrado("Comentario no encontrado")
    if c.usuario_id != usuario.id:
        raise SinPermiso("Solo el autor puede cambiar su comentario")
    if c.eliminado:
        raise ErrorEquipo("El comentario ya fue eliminado")
    return c


def editar_comentario(
    session: Session, empresa_id: int, usuario: Usuario, comentario_id: int, texto: str
) -> CaComentario:
    """Solo el autor. Editar no genera alarma ni menciones nuevas. No hace commit."""
    c = _comentario_propio(session, empresa_id, usuario, comentario_id)
    c.texto = _validar_texto(texto)
    c.editado_en = ahora_utc()
    session.flush()
    return c


def eliminar_comentario(
    session: Session, empresa_id: int, usuario: Usuario, comentario_id: int
) -> CaComentario:
    """Eliminación lógica, solo el autor: el hilo muestra «comentario eliminado». No hace commit."""
    c = _comentario_propio(session, empresa_id, usuario, comentario_id)
    c.eliminado = True
    c.editado_en = ahora_utc()
    session.flush()
    return c


# ---------------------------------------------------------------------------------------------
# Preferencias y no leídos
# ---------------------------------------------------------------------------------------------


def preferencias(session: Session, usuario_id: int) -> dict[str, bool]:
    prefs = dict.fromkeys(PREFERENCIAS, True)
    for tipo, activa in session.execute(
        select(PreferenciaAlarma.tipo, PreferenciaAlarma.activa).where(
            PreferenciaAlarma.usuario_id == usuario_id
        )
    ):
        if tipo in prefs:
            prefs[tipo] = bool(activa)
    return prefs


def guardar_preferencias(session: Session, usuario_id: int, activas: set[str]) -> None:
    """Una fila por preferencia conocida. No hace commit."""
    existentes = {
        p.tipo: p
        for p in session.execute(
            select(PreferenciaAlarma).where(PreferenciaAlarma.usuario_id == usuario_id)
        ).scalars()
    }
    for tipo in PREFERENCIAS:
        fila = existentes.get(tipo)
        if fila is None:
            session.add(PreferenciaAlarma(usuario_id=usuario_id, tipo=tipo, activa=tipo in activas))
        else:
            fila.activa = tipo in activas
    session.flush()


def consulta_no_leidos(session: Session, usuario: Usuario, empresa_id: int) -> Select[Any]:
    """`SELECT ca_eventos` no leídos para `usuario` (sin ORDER BY ni LIMIT). Reutilizable.

    Posteriores a `actividad_leida_hasta`, de la empresa, sin los que hizo él mismo, filtrados
    por sus preferencias; la mención a él siempre llega.
    """
    prefs = preferencias(session, usuario.id)
    yo = usuario.id
    ev = CaEvento
    ramas: list[Any] = [and_(ev.tipo == EV_MENCION, ev.destinatario_id == yo)]
    if prefs[PREF_EQUIPO]:
        # Un comentario que ME menciona ya llega como `mencion`: no se cuenta dos veces.
        m = aliased(CaEvento)
        me_menciona = exists().where(
            m.empresa_id == ev.empresa_id,
            m.ca_codigo == ev.ca_codigo,
            m.usuario_id == ev.usuario_id,
            m.creado_en == ev.creado_en,
            m.tipo == EV_MENCION,
            m.destinatario_id == yo,
        )
        ramas += [
            ev.tipo == EV_ESTADO,
            and_(ev.tipo == EV_COMENTARIO, ~me_menciona),
            and_(
                ev.tipo == EV_ASIGNADA,
                or_(ev.destinatario_id.is_(None), ev.destinatario_id != yo),
            ),
        ]
    if prefs[PREF_ASIGNADA]:
        ramas.append(and_(ev.tipo == EV_ASIGNADA, ev.destinatario_id == yo))
    if prefs[PREF_CIERRE]:
        en_curso = exists().where(
            CaTrabajo.empresa_id == ev.empresa_id,
            CaTrabajo.ca_codigo == ev.ca_codigo,
            CaTrabajo.estado.in_(ACTIVOS),
        )
        ramas.append(and_(ev.tipo.in_((EV_CIERRE_24H, EV_CIERRE_4H)), en_curso))
    if prefs[PREF_RESULTADO]:
        ramas.append(ev.tipo == EV_RESULTADO)
    cond: list[Any] = [
        ev.empresa_id == empresa_id,
        or_(ev.usuario_id.is_(None), ev.usuario_id != yo),
        or_(*ramas),
        ev.creado_en > leido_hasta(usuario),
    ]
    return select(CaEvento).where(*cond)


def leido_hasta(usuario: Usuario, ahora: datetime | None = None) -> datetime:
    """Desde cuándo algo es «no leído». Quien nunca marcó como leído (NULL) ve solo los últimos
    `DIAS_SIN_MARCA` días: así una cuenta nueva no estrena la campana con meses de historia."""
    if usuario.actividad_leida_hasta is not None:
        return usuario.actividad_leida_hasta
    return (ahora or ahora_utc()) - timedelta(days=DIAS_SIN_MARCA)


def contar_no_leidos(session: Session, usuario: Usuario, empresa_id: int) -> int:
    sub = consulta_no_leidos(session, usuario, empresa_id).subquery()
    return int(session.execute(select(func.count()).select_from(sub)).scalar_one())


def listar_no_leidos(
    session: Session, usuario: Usuario, empresa_id: int, limite: int = 20
) -> list[CaEvento]:
    return list(
        session.execute(
            consulta_no_leidos(session, usuario, empresa_id)
            .order_by(CaEvento.creado_en.desc(), CaEvento.id.desc())
            .limit(limite)
        ).scalars()
    )


def marcar_leido(session: Session, usuario: Usuario, hasta: datetime | None = None) -> None:
    usuario.actividad_leida_hasta = hasta or ahora_utc()
    session.flush()


# ---------------------------------------------------------------------------------------------
# Resultado de una CA enviada (§1.3)
# ---------------------------------------------------------------------------------------------


def rut_empresa(empresa: Empresa | None) -> str | None:
    """RUT de la empresa en la forma de `rut_competidor` (None si no tiene o es inválido)."""
    if empresa is None or not empresa.rut:
        return None
    try:
        return normalizar_rut(empresa.rut)
    except ValueError:
        return rut_competidor(empresa.rut)[0] or None


def resultado_de(session: Session, rut: str, ca: CompraAgil, ahora: datetime) -> str | None:
    """Resultado de una CA para el RUT de la empresa, o None si todavía no se puede decir.

    Regla de elección única (F-merc-1c, `eleccion.elegir_por_rut`): una oferta por RUT.
    """
    if ca.estado in _ESTADOS_DESIERTA:
        return DESIERTA
    if ca.cotizaciones_obtenidas_en is None or ca.fecha_cierre is None or ca.fecha_cierre > ahora:
        return None
    ofertas = [
        (rut_competidor(r)[0], sel, valor)
        for r, sel, valor in session.execute(
            select(
                CaCotizacion.rut_proveedor, CaCotizacion.seleccionado, CaCotizacion.valor_neto
            ).where(CaCotizacion.ca_codigo == ca.codigo)
        )
    ]
    elegidas = elegir_por_rut(
        [o for o in ofertas if o[0]], lambda o: o[0], lambda o: o[1], lambda o: o[2]
    )
    nuestra = elegidas.get(rut)
    if nuestra is None:
        return NO_REGISTRADA
    if nuestra[1] is True:
        return GANADA
    if all(o[1] is None for o in elegidas.values()):
        return SIN_DATO
    return PERDIDA


# ---------------------------------------------------------------------------------------------
# Alarmas de sistema (§2)
# ---------------------------------------------------------------------------------------------


def _evento_sistema(
    session: Session, empresa_id: int, codigo: str, tipo: str, datos: dict[str, Any], ahora: datetime
) -> bool:
    """Inserta un evento de sistema en un savepoint; si ya existía (índice parcial), nada."""
    try:
        with session.begin_nested():
            _evento(session, empresa_id, codigo, None, tipo, datos, creado_en=ahora)
    except IntegrityError:
        return False
    return True


def generar_alarmas(session: Session, empresa_id: int, ahora: datetime) -> dict[str, int]:
    """Eventos `cierre_24h` / `cierre_4h` y `resultado` de la empresa. Idempotente. No hace commit.

    - Cierre: CA en `priorizada|cotizando|lista` con `fecha_cierre` (UTC naive) en (ahora, +24 h]
      → `cierre_24h`, o en (ahora, +4 h] → `cierre_4h`. Si al verla ya faltan ≤ 4 h, solo el de
      4 h (no dos avisos a la vez).
    - Resultado: `enviada` sin resultado cuya CA ya se puede resolver (§1.3). Empresa sin RUT →
      no se calcula (la pantalla lo avisa).
    Solo columnas livianas, nunca `raw_json` (regla 12).
    """
    cuenta = {EV_CIERRE_24H: 0, EV_CIERRE_4H: 0, EV_RESULTADO: 0}
    ya = {
        (c, t)
        for c, t in session.execute(
            select(CaEvento.ca_codigo, CaEvento.tipo).where(
                CaEvento.empresa_id == empresa_id,
                CaEvento.usuario_id.is_(None),
                CaEvento.tipo.in_(TIPOS_SISTEMA),
                CaEvento.ca_codigo.in_(
                    select(CaTrabajo.ca_codigo).where(
                        CaTrabajo.empresa_id == empresa_id,
                        CaTrabajo.estado.in_(ACTIVOS + (ENVIADA,)),
                    )
                ),
            )
        )
    }
    for codigo, cierre, estado in session.execute(
        select(CaTrabajo.ca_codigo, CompraAgil.fecha_cierre, CaTrabajo.estado)
        .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
        .where(
            CaTrabajo.empresa_id == empresa_id,
            CaTrabajo.estado.in_(ACTIVOS),
            CompraAgil.fecha_cierre > ahora,
            CompraAgil.fecha_cierre <= ahora + timedelta(hours=24),
        )
    ):
        tipo = EV_CIERRE_4H if cierre <= ahora + timedelta(hours=4) else EV_CIERRE_24H
        if (codigo, tipo) in ya:
            continue
        if tipo == EV_CIERRE_24H and (codigo, EV_CIERRE_4H) in ya:
            continue
        if _evento_sistema(session, empresa_id, codigo, tipo, {"estado": estado}, ahora):
            cuenta[tipo] += 1

    rut = rut_empresa(session.get(Empresa, empresa_id))
    if rut is not None:
        pendientes = session.execute(
            select(CaTrabajo, CompraAgil)
            .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
            .where(
                CaTrabajo.empresa_id == empresa_id,
                CaTrabajo.estado == ENVIADA,
                CaTrabajo.resultado.is_(None),
                or_(
                    CompraAgil.estado.in_(_ESTADOS_DESIERTA),
                    and_(
                        CompraAgil.cotizaciones_obtenidas_en.is_not(None),
                        CompraAgil.fecha_cierre <= ahora,
                    ),
                ),
            )
        ).all()
        for t, ca in pendientes:
            res = resultado_de(session, rut, ca, ahora)
            if res is None:
                continue
            t.resultado = res
            t.resultado_en = ahora
            if (t.ca_codigo, EV_RESULTADO) not in ya and _evento_sistema(
                session, empresa_id, t.ca_codigo, EV_RESULTADO, {"resultado": res}, ahora
            ):
                cuenta[EV_RESULTADO] += 1
    session.flush()
    return cuenta


def generar_alarmas_todas(session: Session, ahora: datetime) -> dict[str, int]:
    """Para todas las empresas activas (al final de `mercado-ca` y de `mercado-backfill`)."""
    total = {EV_CIERRE_24H: 0, EV_CIERRE_4H: 0, EV_RESULTADO: 0}
    for empresa_id in session.execute(select(Empresa.id).where(Empresa.activa.is_(True))).scalars():
        for k, v in generar_alarmas(session, empresa_id, ahora).items():
            total[k] += v
    return total


def generar_si_toca(session: Session, empresa_id: int, ahora: datetime) -> bool:
    """Desde el sondeo de la campana: a lo más una vez por minuto por empresa.

    La marca vive en `sync_state` (regla 10: nada en memoria). El UPDATE condicional es atómico:
    de dos sondeos simultáneos solo uno la toma. No hace commit.
    """
    fuente = FUENTE_SONDEO.format(empresa_id)
    if session.get(SyncState, fuente) is None:
        try:
            with session.begin_nested():
                session.add(SyncState(fuente=fuente, ultima_ejecucion=None))
        except IntegrityError:
            pass
    tomada = session.execute(
        update(SyncState)
        .where(
            SyncState.fuente == fuente,
            or_(
                SyncState.ultima_ejecucion.is_(None),
                SyncState.ultima_ejecucion <= ahora - timedelta(seconds=SONDEO_MIN_S),
            ),
        )
        .values(ultima_ejecucion=ahora)
        .execution_options(synchronize_session=False)
    )
    if not tomada.rowcount:  # type: ignore[attr-defined]
        return False
    generar_alarmas(session, empresa_id, ahora)
    return True


def purgar_eventos(session: Session, ahora: datetime, dias: int = RETENCION_EVENTOS_DIAS) -> int:
    """Borra los eventos con más de 12 meses (regla 11). `ca_trabajo` y comentarios se quedan."""
    r = session.execute(delete(CaEvento).where(CaEvento.creado_en < ahora - timedelta(days=dias)))
    return int(r.rowcount or 0)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------------
# Datos de pantalla
# ---------------------------------------------------------------------------------------------


FILTROS_EQUIPO = ("sin_trabajar", "priorizadas", "mias", "en_curso", "enviadas")
ETIQUETA_FILTRO = {
    "": "Todas",
    "sin_trabajar": "Sin trabajar",
    "priorizadas": "Priorizadas",
    "mias": "Mías",
    "en_curso": "En curso",
    "enviadas": "Enviadas",
}


def estados_de(session: Session, empresa_id: int) -> dict[str, tuple[str, int | None]]:
    """codigo → (estado, responsable_id) de todas las CA trabajadas por la empresa."""
    return {
        c: (e, r)
        for c, e, r in session.execute(
            select(CaTrabajo.ca_codigo, CaTrabajo.estado, CaTrabajo.responsable_id).where(
                CaTrabajo.empresa_id == empresa_id
            )
        )
    }


def filtrar_equipo(
    session: Session,
    empresa_id: int,
    usuario_id: int,
    codigos: list[str],
    filtro: str,
    ver_descartadas: bool,
) -> list[str]:
    """Filtro «Equipo» de la mesa, conservando el orden. Las descartadas se ocultan por defecto."""
    estados = estados_de(session, empresa_id)
    out: list[str] = []
    for c in codigos:
        estado, resp = estados.get(c, (NUEVA, None))
        if estado == DESCARTADA and not ver_descartadas:
            continue
        if filtro == "sin_trabajar" and estado != NUEVA:
            continue
        if filtro == "priorizadas" and estado != PRIORIZADA:
            continue
        if filtro == "mias" and resp != usuario_id:
            continue
        if filtro == "en_curso" and estado not in (COTIZANDO, LISTA):
            continue
        if filtro == "enviadas" and estado != ENVIADA:
            continue
        out.append(c)
    return out


@dataclass
class EquipoCA:
    """Lo que la tarjeta y la ficha muestran del trabajo del equipo sobre una CA."""

    codigo: str
    estado: str = NUEVA
    version: int = 0
    responsable_id: int | None = None
    responsable: str | None = None
    responsable_iniciales: str | None = None
    motivo_descarte: str | None = None
    resultado: str | None = None
    actualizado_por: str | None = None
    actualizado_en: datetime | None = None
    n_comentarios: int = 0
    comentarios_no_leidos: bool = False
    conflicto: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def etiqueta(self) -> str:
        return ETIQUETA_ESTADO.get(self.estado, self.estado)

    @property
    def etiqueta_resultado(self) -> str | None:
        return ETIQUETA_RESULTADO.get(self.resultado) if self.resultado else None


def equipo_de(
    session: Session, empresa_id: int, usuario: Usuario, codigos: list[str]
) -> dict[str, EquipoCA]:
    """`EquipoCA` de cada código (estado, responsable, n.º de comentarios, punto de no leídos)."""
    if not codigos:
        return {}
    out = {c: EquipoCA(codigo=c) for c in codigos}
    trabajos = list(
        session.execute(
            select(CaTrabajo).where(
                CaTrabajo.empresa_id == empresa_id, CaTrabajo.ca_codigo.in_(codigos)
            )
        ).scalars()
    )
    nombres = _nombres(
        session,
        {t.responsable_id for t in trabajos if t.responsable_id}
        | {t.actualizado_por_id for t in trabajos if t.actualizado_por_id},
    )
    for t in trabajos:
        e = out[t.ca_codigo]
        e.estado, e.version, e.responsable_id = t.estado, t.version, t.responsable_id
        e.motivo_descarte, e.resultado = t.motivo_descarte, t.resultado
        e.actualizado_en = t.actualizado_en
        e.actualizado_por = nombres.get(t.actualizado_por_id) if t.actualizado_por_id else None
        if t.responsable_id:
            e.responsable = nombres.get(t.responsable_id)
            e.responsable_iniciales = iniciales(e.responsable or "?")
    vivos = [CaComentario.empresa_id == empresa_id, CaComentario.ca_codigo.in_(codigos)]
    for c, n in session.execute(
        select(CaComentario.ca_codigo, func.count())
        .where(*vivos, CaComentario.eliminado.is_(False))
        .group_by(CaComentario.ca_codigo)
    ):
        out[c].n_comentarios = int(n)
    nuevos = [
        *vivos,
        CaComentario.eliminado.is_(False),
        CaComentario.usuario_id != usuario.id,
        CaComentario.creado_en > leido_hasta(usuario),
    ]
    for (c,) in session.execute(select(CaComentario.ca_codigo).where(*nuevos).distinct()):
        out[c].comentarios_no_leidos = True
    return out


def hace(momento: datetime | None, ahora: datetime) -> str:
    if momento is None:
        return ""
    s = int((ahora - momento).total_seconds())
    if s < 60:
        return "recién"
    if s < 3600:
        return f"hace {s // 60} min"
    if s < 86400:
        return f"hace {s // 3600} h"
    return "el " + hora_chile(momento, "%d/%m %H:%M")


def hora_chile(momento: datetime, formato: str = "%H:%M") -> str:
    return momento.replace(tzinfo=UTC).astimezone(TZ_CHILE).strftime(formato)


def cuando_cierra(cierre: datetime | None, ahora: datetime) -> str:
    """«cierra hoy 15:00» / «cierra mañana 15:00» / «cierra el 12/11 15:00» (hora de Chile)."""
    if cierre is None:
        return "sin fecha de cierre"
    if cierre <= ahora:
        return "ya cerró"
    hoy = ahora.replace(tzinfo=UTC).astimezone(TZ_CHILE).date()
    local = cierre.replace(tzinfo=UTC).astimezone(TZ_CHILE)
    if local.date() == hoy:
        return f"cierra hoy {local:%H:%M}"
    if local.date() == hoy + timedelta(days=1):
        return f"cierra mañana {local:%H:%M}"
    return f"cierra el {local:%d/%m %H:%M}"


_VERBO_ESTADO = {
    NUEVA: "devolvió a Nueva",
    PRIORIZADA: "priorizó",
    COTIZANDO: "pasó a Cotizando",
    LISTA: "dejó lista para enviar",
    ENVIADA: "marcó como enviada",
    DESCARTADA: "descartó",
}
_FRASE_RESULTADO = {
    GANADA: "Ganamos",
    PERDIDA: "Perdimos",
    NO_REGISTRADA: "No aparece nuestra oferta en",
    SIN_DATO: "Sin dato de selección en",
    DESIERTA: "Quedó desierta o cancelada",
}
FRAGMENTO_MAX = 80


def _fragmento(texto: str) -> str:
    t = " ".join(texto.split())
    return t if len(t) <= FRAGMENTO_MAX else t[: FRAGMENTO_MAX - 1] + "…"


def describir_eventos(
    session: Session, eventos: list[CaEvento], yo: int, ahora: datetime
) -> list[dict[str, Any]]:
    """Texto humano de cada evento: `antes` + nombre de la CA (énfasis) + `despues`.

    La plantilla escapa todo; nada se marca `|safe` (los comentarios son texto del equipo).
    """
    if not eventos:
        return []
    codigos = {e.ca_codigo for e in eventos}
    cas = {
        c: (n, f)
        for c, n, f in session.execute(
            select(CompraAgil.codigo, CompraAgil.nombre, CompraAgil.fecha_cierre).where(
                CompraAgil.codigo.in_(codigos)
            )
        )
    }
    estados = {
        c: e
        for c, e in session.execute(
            select(CaTrabajo.ca_codigo, CaTrabajo.estado).where(
                CaTrabajo.empresa_id == eventos[0].empresa_id, CaTrabajo.ca_codigo.in_(codigos)
            )
        )
    }
    ids_com = {
        int(e.datos["comentario_id"])
        for e in eventos
        if e.datos and e.datos.get("comentario_id") is not None
    }
    comentarios = (
        {
            c.id: c
            for c in session.execute(
                select(CaComentario).where(CaComentario.id.in_(ids_com))
            ).scalars()
        }
        if ids_com
        else {}
    )
    nombres = _nombres(
        session,
        {e.usuario_id for e in eventos if e.usuario_id}
        | {e.destinatario_id for e in eventos if e.destinatario_id},
    )
    out: list[dict[str, Any]] = []
    for e in eventos:
        nombre_ca, cierre = cas.get(e.ca_codigo, (e.ca_codigo, None))
        actor = nombres.get(e.usuario_id or 0, "Alguien")
        d = e.datos or {}
        antes, despues = "", ""
        if e.tipo == EV_ESTADO:
            a, de = d.get("a", ""), d.get("de", "")
            verbo = _VERBO_ESTADO.get(a, "cambió")
            if de == DESCARTADA and a != DESCARTADA:
                verbo = f"reabrió ({ETIQUETA_ESTADO.get(a, a)})"
            antes = f"{actor} {verbo}"
            despues = f"· {cuando_cierra(cierre, ahora)}"
            if a == DESCARTADA and d.get("motivo"):
                despues = f"· motivo: {d['motivo']}"
        elif e.tipo == EV_ASIGNADA:
            dest = e.destinatario_id
            if dest is None:
                antes = f"{actor} quitó el responsable de"
            elif dest == e.usuario_id:
                antes = f"{actor} tomó"
            elif dest == yo:
                antes = f"{actor} te asignó"
            else:
                antes = f"{actor} asignó a {nombres.get(dest, 'alguien')}"
            despues = f"· {cuando_cierra(cierre, ahora)}"
        elif e.tipo in (EV_COMENTARIO, EV_MENCION):
            c = comentarios.get(int(d.get("comentario_id") or 0))
            if e.tipo == EV_MENCION:
                quien = "te" if e.destinatario_id == yo else f"a {nombres.get(e.destinatario_id or 0, 'alguien')}"
                antes = f"{actor} {quien} mencionó en"
            else:
                antes = f"{actor} comentó en"
            if c is None or c.eliminado:
                despues = ": (comentario eliminado)"
            else:
                despues = f": «{_fragmento(c.texto)}»"
        elif e.tipo in (EV_CIERRE_24H, EV_CIERRE_4H):
            horas = 4 if e.tipo == EV_CIERRE_4H else 24
            estado = ETIQUETA_ESTADO.get(estados.get(e.ca_codigo, d.get("estado", "")), "")
            antes = f"Cierra en {horas} h y sigue en {estado}:"
            despues = f"· {cuando_cierra(cierre, ahora)}"
        elif e.tipo == EV_RESULTADO:
            antes = _FRASE_RESULTADO.get(d.get("resultado", ""), "Resultado de")
        out.append(
            {
                "id": e.id,
                "tipo": e.tipo,
                "antes": antes,
                "ca": nombre_ca,
                "codigo": e.ca_codigo,
                "despues": despues,
                "url": f"/oportunidad/compras_agiles/{e.ca_codigo}",
                "cuando": hace(e.creado_en, ahora),
                "creado_en": e.creado_en,
            }
        )
    return out


def hilo(
    session: Session, empresa_id: int, codigo: str, ahora: datetime
) -> list[dict[str, Any]]:
    """Comentarios de la CA intercalados con los cambios de estado/asignación/resultado como
    líneas de sistema, del más antiguo al más nuevo."""
    comentarios = list(
        session.execute(
            select(CaComentario)
            .where(CaComentario.empresa_id == empresa_id, CaComentario.ca_codigo == codigo)
            .order_by(CaComentario.creado_en, CaComentario.id)
        ).scalars()
    )
    eventos = list(
        session.execute(
            select(CaEvento)
            .where(
                CaEvento.empresa_id == empresa_id,
                CaEvento.ca_codigo == codigo,
                CaEvento.tipo.in_((EV_ESTADO, EV_ASIGNADA, EV_RESULTADO)),
            )
            .order_by(CaEvento.creado_en, CaEvento.id)
        ).scalars()
    )
    nombres = _nombres(
        session,
        {c.usuario_id for c in comentarios}
        | {e.usuario_id for e in eventos if e.usuario_id}
        | {e.destinatario_id for e in eventos if e.destinatario_id},
    )
    items: list[dict[str, Any]] = []
    for c in comentarios:
        items.append(
            {
                "clase": "comentario",
                "id": c.id,
                "autor_id": c.usuario_id,
                "autor": nombres.get(c.usuario_id, "Alguien"),
                "texto": c.texto,
                "eliminado": c.eliminado,
                "editado": c.editado_en is not None and not c.eliminado,
                "hora": hora_chile(c.creado_en, "%d/%m %H:%M"),
                "orden": (c.creado_en, 1, c.id),
            }
        )
    for e in eventos:
        d = e.datos or {}
        actor = nombres.get(e.usuario_id or 0, "Sistema")
        if e.tipo == EV_ESTADO:
            a = d.get("a", "")
            if a == DESCARTADA:
                txt = f"{actor} descartó" + (f" (motivo: {d['motivo']})" if d.get("motivo") else "")
            elif d.get("de") == DESCARTADA:
                txt = f"{actor} reabrió a {ETIQUETA_ESTADO.get(a, a)}"
            else:
                txt = f"{actor} pasó a {ETIQUETA_ESTADO.get(a, a)}"
        elif e.tipo == EV_ASIGNADA:
            dest = e.destinatario_id
            if dest is None:
                txt = f"{actor} quitó el responsable"
            elif dest == e.usuario_id:
                txt = f"{actor} se hizo responsable"
            else:
                txt = f"{actor} asignó a {nombres.get(dest, 'alguien')}"
        else:
            txt = f"Resultado: {ETIQUETA_RESULTADO.get(d.get('resultado', ''), '—')}"
        items.append(
            {
                "clase": "sistema",
                "texto": txt,
                "hora": hora_chile(e.creado_en, "%d/%m %H:%M"),
                "orden": (e.creado_en, 0, e.id),
            }
        )
    items.sort(key=lambda i: i["orden"])
    return items


def mis_ca(
    session: Session, empresa_id: int, usuario_id: int, ahora: datetime, limite: int = 10
) -> list[dict[str, Any]]:
    """CA en curso con responsable = yo, por cierre (bloque «Equipo» de «Hoy»)."""
    return _ca_en_curso(
        session,
        [CaTrabajo.empresa_id == empresa_id, CaTrabajo.responsable_id == usuario_id,
         CaTrabajo.estado.in_(ACTIVOS)],
        ahora,
        limite,
    )


def cierran_sin_enviar(
    session: Session, empresa_id: int, ahora: datetime, limite: int = 10
) -> list[dict[str, Any]]:
    """CA en curso (aún sin enviar) que cierran en menos de 24 h."""
    return _ca_en_curso(
        session,
        [
            CaTrabajo.empresa_id == empresa_id,
            CaTrabajo.estado.in_(ACTIVOS),
            CompraAgil.fecha_cierre > ahora,
            CompraAgil.fecha_cierre <= ahora + timedelta(hours=24),
        ],
        ahora,
        limite,
    )


def _ca_en_curso(
    session: Session, cond: list[Any], ahora: datetime, limite: int
) -> list[dict[str, Any]]:
    filas = session.execute(
        select(CompraAgil.codigo, CompraAgil.nombre, CompraAgil.fecha_cierre, CaTrabajo.estado)
        .join(CaTrabajo, CaTrabajo.ca_codigo == CompraAgil.codigo)
        .where(*cond)
        .order_by(CompraAgil.fecha_cierre.is_(None), CompraAgil.fecha_cierre)
        .limit(limite)
    ).all()
    return [
        {
            "codigo": c,
            "nombre": n,
            "estado": ETIQUETA_ESTADO.get(e, e),
            "cierre": cuando_cierra(f, ahora),
        }
        for c, n, f, e in filas
    ]


def ultimas_actividades(
    session: Session, empresa_id: int, usuario_id: int, ahora: datetime, limite: int = 5
) -> list[dict[str, Any]]:
    eventos = list(
        session.execute(
            select(CaEvento)
            .where(CaEvento.empresa_id == empresa_id, CaEvento.tipo != EV_MENCION)
            .order_by(CaEvento.creado_en.desc(), CaEvento.id.desc())
            .limit(limite)
        ).scalars()
    )
    return describir_eventos(session, eventos, usuario_id, ahora)
