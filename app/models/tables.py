"""Definiciones de tablas SQLAlchemy 2.x para mp-oportunidades."""

from __future__ import annotations

from datetime import datetime
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
from app.models.enums import EstadoOportunidad, FrecuenciaAlerta, RolUsuario

# Renders as JSONB on Postgres (GIN indexable), falls back to JSON elsewhere (tests).
JSONB = JSON().with_variant(_PG_JSONB(), "postgresql")

# BigInteger on Postgres, Integer on SQLite (autoincrement requires INTEGER type in SQLite).
BigInt = BigInteger().with_variant(Integer(), "sqlite")


def _now() -> datetime:
    return datetime.utcnow()


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
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    perfiles: Mapped[list[PerfilBusqueda]] = relationship(
        "PerfilBusqueda", back_populates="owner", cascade="all, delete-orphan"
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


class Alerta(Base):
    __tablename__ = "alertas"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("oportunidades_match.id", ondelete="CASCADE"), nullable=False
    )
    tipo: Mapped[str] = mapped_column(String(50), nullable=False)
    enviada_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    canal: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="pendiente")

    match: Mapped[OportunidadMatch] = relationship("OportunidadMatch", back_populates="alertas")


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
