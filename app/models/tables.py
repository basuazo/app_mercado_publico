"""Definiciones de tablas SQLAlchemy 2.x para mp-oportunidades."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import (
    EstadoAlerta,
    EstadoOportunidad,
    EstadoPlanificacionPAC,
    FrecuenciaAlerta,
    RolUsuario,
)

# Renders as JSONB on Postgres (GIN indexable), falls back to JSON elsewhere (tests).
JSONB = JSON().with_variant(_PG_JSONB(), "postgresql")

# BigInteger on Postgres, Integer on SQLite (autoincrement requires INTEGER type in SQLite).
BigInt = BigInteger().with_variant(Integer(), "sqlite")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Usuarios
# ---------------------------------------------------------------------------


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    rol: Mapped[RolUsuario] = mapped_column(String(20), nullable=False, default=RolUsuario.USUARIO)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    rut_proveedor: Mapped[str | None] = mapped_column(String(20), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    perfiles: Mapped[list[PerfilBusqueda]] = relationship(
        "PerfilBusqueda", back_populates="owner", cascade="all, delete-orphan"
    )
    seguidas: Mapped[list[OportunidadSeguida]] = relationship(
        "OportunidadSeguida", back_populates="owner", cascade="all, delete-orphan"
    )
    match_feedback: Mapped[list[MatchFeedback]] = relationship(
        "MatchFeedback", back_populates="usuario", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Catálogos
# ---------------------------------------------------------------------------


class Organismo(Base):
    __tablename__ = "organismos"

    codigo: Mapped[str] = mapped_column(String(50), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(500), nullable=False)
    rut: Mapped[str | None] = mapped_column(String(20), nullable=True)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )


# ---------------------------------------------------------------------------
# Licitaciones
# ---------------------------------------------------------------------------


class Licitacion(Base):
    __tablename__ = "licitaciones"

    codigo: Mapped[str] = mapped_column(String(50), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    descripcion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    estado_codigo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estado: Mapped[str] = mapped_column(
        String(50), nullable=False, default=EstadoOportunidad.DESCONOCIDO.value
    )
    tipo: Mapped[str | None] = mapped_column(String(10), nullable=True)
    fecha_publicacion: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fecha_cierre: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    moneda: Mapped[str | None] = mapped_column(String(10), nullable=True)
    monto_estimado: Mapped[float | None] = mapped_column(Float, nullable=True)
    monto_clp: Mapped[float | None] = mapped_column(Float, nullable=True)
    codigo_organismo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    detalle_obtenido: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    items: Mapped[list[LicitacionItem]] = relationship(
        "LicitacionItem", back_populates="licitacion", cascade="all, delete-orphan"
    )


class LicitacionItem(Base):
    __tablename__ = "licitacion_items"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    licitacion_codigo: Mapped[str] = mapped_column(
        ForeignKey("licitaciones.codigo", ondelete="CASCADE"), nullable=False
    )
    codigo_producto: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    nombre: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    cantidad: Mapped[float | None] = mapped_column(Float, nullable=True)
    unidad: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    licitacion: Mapped[Licitacion] = relationship("Licitacion", back_populates="items")


class OfertaCompetencia(Base):
    """Oferta de un proveedor (por ítem) en una licitación adjudicada, capturada
    desde datos abiertos (lic-da) — ver docs/05-competencia.md. Solo lectura,
    poblada por app.ingest.datos_abiertos.capturar_competencia (F-competencia)."""

    __tablename__ = "ofertas_competencia"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    licitacion_codigo: Mapped[str] = mapped_column(
        ForeignKey("licitaciones.codigo", ondelete="CASCADE"), nullable=False, index=True
    )
    codigo_item: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    rut_proveedor: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    nombre_proveedor: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    monto_unitario: Mapped[float | None] = mapped_column(Float, nullable=True)
    monto_linea_adjudicada: Mapped[float | None] = mapped_column(Float, nullable=True)
    cantidad: Mapped[float | None] = mapped_column(Float, nullable=True)
    seleccionada: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


# ---------------------------------------------------------------------------
# Compras Ãgiles
# ---------------------------------------------------------------------------


class CompraAgil(Base):
    __tablename__ = "compras_agiles"

    codigo: Mapped[str] = mapped_column(String(50), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    descripcion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    estado: Mapped[str] = mapped_column(
        String(50), nullable=False, default=EstadoOportunidad.DESCONOCIDO.value
    )
    estado_convocatoria: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fecha_publicacion: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fecha_cierre: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fecha_ultimo_cambio: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    moneda: Mapped[str | None] = mapped_column(String(10), nullable=True)
    monto_disponible_clp: Mapped[float | None] = mapped_column(Float, nullable=True)
    organismo_nombre: Mapped[str | None] = mapped_column(String(500), nullable=True)
    organismo_rut: Mapped[str | None] = mapped_column(String(20), nullable=True)
    region: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_ofertas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    id_orden_compra: Mapped[str | None] = mapped_column(String(50), nullable=True)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    productos: Mapped[list[CaProducto]] = relationship(
        "CaProducto", back_populates="compra_agil", cascade="all, delete-orphan"
    )


class CaProducto(Base):
    __tablename__ = "ca_productos"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    ca_codigo: Mapped[str] = mapped_column(
        ForeignKey("compras_agiles.codigo", ondelete="CASCADE"), nullable=False
    )
    codigo_producto: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    nombre: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    descripcion: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    cantidad: Mapped[float | None] = mapped_column(Float, nullable=True)
    unidad: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    compra_agil: Mapped[CompraAgil] = relationship("CompraAgil", back_populates="productos")


# ---------------------------------------------------------------------------
# Ã“rdenes de Compra
# ---------------------------------------------------------------------------


class OrdenCompra(Base):
    __tablename__ = "ordenes_compra"

    codigo: Mapped[str] = mapped_column(String(50), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    tipo_oc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estado: Mapped[str] = mapped_column(
        String(50), nullable=False, default=EstadoOportunidad.DESCONOCIDO.value
    )


# ---------------------------------------------------------------------------
# Perfiles de bÃºsqueda
# ---------------------------------------------------------------------------


class PerfilBusqueda(Base):
    __tablename__ = "perfiles_busqueda"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    keywords: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=list)
    keywords_excluir: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=list)
    regiones: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=list)
    monto_min_clp: Mapped[float | None] = mapped_column(Float, nullable=True)
    monto_max_clp: Mapped[float | None] = mapped_column(Float, nullable=True)
    categorias_unspsc: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=list)
    organismos_seguidos: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=list)
    fuentes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: ["licitaciones", "compras_agiles"]
    )
    frecuencia_alerta: Mapped[FrecuenciaAlerta] = mapped_column(
        String(20), nullable=False, default=FrecuenciaAlerta.DIGEST
    )
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    owner: Mapped[Usuario] = relationship("Usuario", back_populates="perfiles")
    matches: Mapped[list[OportunidadMatch]] = relationship(
        "OportunidadMatch", back_populates="perfil", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Oportunidades seguidas (F-seguir)
# ---------------------------------------------------------------------------


class OportunidadSeguida(Base):
    __tablename__ = "oportunidades_seguidas"
    __table_args__ = (
        UniqueConstraint("owner_id", "fuente", "codigo_oportunidad", name="uq_seguida"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False
    )
    fuente: Mapped[str] = mapped_column(String(30), nullable=False)
    codigo_oportunidad: Mapped[str] = mapped_column(String(50), nullable=False)
    estado_visto: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    archivada: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    owner: Mapped[Usuario] = relationship("Usuario", back_populates="seguidas")
    alertas: Mapped[list[Alerta]] = relationship(
        "Alerta", back_populates="seguimiento", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Matches y alertas
# ---------------------------------------------------------------------------


class OportunidadMatch(Base):
    __tablename__ = "oportunidades_match"
    __table_args__ = (
        UniqueConstraint("perfil_id", "fuente", "codigo_oportunidad", name="uq_match"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    perfil_id: Mapped[int] = mapped_column(
        ForeignKey("perfiles_busqueda.id", ondelete="CASCADE"), nullable=False
    )
    fuente: Mapped[str] = mapped_column(String(30), nullable=False)
    codigo_oportunidad: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    razones: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    fecha_match: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    perfil: Mapped[PerfilBusqueda] = relationship("PerfilBusqueda", back_populates="matches")
    alertas: Mapped[list[Alerta]] = relationship(
        "Alerta", back_populates="match", cascade="all, delete-orphan"
    )


class MatchFeedback(Base):
    """Feedback explícito del usuario sobre una oportunidad (F10 parte 2).

    Señal para F11 (reponderación del matching): timestamp + valor + qué
    oportunidad bastan; el resto (score, razones, perfil) se joinea al match
    en F11, no se duplica aquí. Un feedback por usuario por oportunidad
    (uq_match_feedback); alternar actualiza el valor existente o lo borra,
    nunca duplica (ver app/matching/feedback.py).
    """

    __tablename__ = "match_feedback"
    __table_args__ = (
        UniqueConstraint("usuario_id", "fuente", "codigo_oportunidad", name="uq_match_feedback"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(
        ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False
    )
    fuente: Mapped[str] = mapped_column(String(30), nullable=False)
    codigo_oportunidad: Mapped[str] = mapped_column(String(50), nullable=False)
    valor: Mapped[str] = mapped_column(String(20), nullable=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    usuario: Mapped[Usuario] = relationship("Usuario", back_populates="match_feedback")


class Alerta(Base):
    """Alerta de un match de perfil O de un seguimiento explícito — exactamente
    uno de match_id/seguimiento_id está poblado (enforced en el código que crea
    cada Alerta, ver app/alerts/detector.py)."""

    __tablename__ = "alertas"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    match_id: Mapped[int | None] = mapped_column(
        ForeignKey("oportunidades_match.id", ondelete="CASCADE"), nullable=True
    )
    seguimiento_id: Mapped[int | None] = mapped_column(
        ForeignKey("oportunidades_seguidas.id", ondelete="CASCADE"), nullable=True
    )
    tipo: Mapped[str] = mapped_column(String(50), nullable=False)
    enviada_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    canal: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
    estado: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EstadoAlerta.PENDIENTE.value
    )
    intentos_envio: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    match: Mapped[OportunidadMatch | None] = relationship("OportunidadMatch", back_populates="alertas")
    seguimiento: Mapped[OportunidadSeguida | None] = relationship(
        "OportunidadSeguida", back_populates="alertas"
    )


# ---------------------------------------------------------------------------
# Plan Anual de Compra (F-plan) â€” datos abiertos, consulta on-demand
# ---------------------------------------------------------------------------


class PlanCompraLinea(Base):
    """Una línea del PAC de una institución/año, cacheada desde datos abiertos
    (ver docs/07-plan-anual.md). codigo_entidad es directamente codigo_organismo."""

    __tablename__ = "plan_compra_lineas"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    codigo_entidad: Mapped[int] = mapped_column(Integer, nullable=False)
    agno: Mapped[int] = mapped_column(Integer, nullable=False)
    institucion_nombre: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    codigo_producto: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    descripcion_producto: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cantidad_estimada: Mapped[float | None] = mapped_column(Float, nullable=True)
    monto_unitario_clp: Mapped[float | None] = mapped_column(Float, nullable=True)
    monto_estimado_clp: Mapped[float | None] = mapped_column(Float, nullable=True)
    mes_estimado: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trimestre_estimado: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estado_planificacion: Mapped[str] = mapped_column(
        String(30), nullable=False, default=EstadoPlanificacionPAC.DESCONOCIDO.value
    )
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class PlanCompraSync(Base):
    """Caché/TTL por (codigo_entidad, agno): evita re-descargar el ZIP en cada
    consulta (el PAC se regenera ~mensualmente, ver §5-bis g)."""

    __tablename__ = "plan_compra_sync"

    codigo_entidad: Mapped[int] = mapped_column(Integer, primary_key=True)
    agno: Mapped[int] = mapped_column(Integer, primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    fuente_last_modified: Mapped[str | None] = mapped_column(String(100), nullable=True)
    n_filas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="sin_plan")


class InstitucionPAC(Base):
    """Catálogo cacheado de instituciones del PAC (alimenta el autocomplete)."""

    __tablename__ = "instituciones_pac"

    codigo_entidad: Mapped[int] = mapped_column(Integer, primary_key=True)
    razon_social: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    rut: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(200), nullable=True)
    id_sector: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ---------------------------------------------------------------------------
# Estado de sincronizaciÃ³n
# ---------------------------------------------------------------------------


class SyncState(Base):
    __tablename__ = "sync_state"

    fuente: Mapped[str] = mapped_column(String(50), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ultima_ejecucion: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ultimo_ok: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    requests_usadas_hoy: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fecha_contador: Mapped[str | None] = mapped_column(String(10), nullable=True)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Ãndices adicionales
# ---------------------------------------------------------------------------

Index("ix_licitaciones_estado", Licitacion.estado)
Index("ix_licitaciones_fecha_cierre", Licitacion.fecha_cierre)
Index("ix_compras_agiles_estado", CompraAgil.estado)
Index("ix_compras_agiles_region", CompraAgil.region)
Index("ix_oportunidades_match_perfil", OportunidadMatch.perfil_id)
Index("ix_plan_compra_lineas_entidad_agno", PlanCompraLinea.codigo_entidad, PlanCompraLinea.agno)
Index("ix_instituciones_pac_razon_social", InstitucionPAC.razon_social)
Index("ix_match_feedback_usuario", MatchFeedback.usuario_id)
