"""Definiciones de tablas SQLAlchemy 2.x para mp-oportunidades."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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

from app.core.tiempo import ahora_utc
from app.models.base import Base
from app.models.enums import (
    EstadoAlerta,
    EstadoOportunidad,
    EstadoPlanificacionPAC,
    RolUsuario,
)

# Renders as JSONB on Postgres (GIN indexable), falls back to JSON elsewhere (tests).
JSONB = JSON().with_variant(_PG_JSONB(), "postgresql")

# BigInteger on Postgres, Integer on SQLite (autoincrement requires INTEGER type in SQLite).
BigInt = BigInteger().with_variant(Integer(), "sqlite")


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
    dias_resumen: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    ultimo_resumen_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tutorial_visto: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    novedades_visto_hasta: Mapped[date | None] = mapped_column(Date, nullable=True)
    # F-plat-3: un usuario pertenece a una sola empresa (nullable: los anteriores quedan sin).
    empresa_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("empresas.id", ondelete="SET NULL"), nullable=True
    )
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)

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
        DateTime, nullable=False, default=ahora_utc, onupdate=ahora_utc
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
    # Mismas columnas que compras_agiles (F-datos-1); llegan con el detalle v1.
    organismo_nombre: Mapped[str | None] = mapped_column(String(500), nullable=True)
    region: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    detalle_obtenido: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Fallos seguidos de `detalles-match` y el último (F-detalles-fallos): con
    # DETALLES_FALLOS_MAX o más, la oportunidad espera antes de volver a la cola.
    detalle_fallos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    detalle_ultimo_fallo: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=ahora_utc, onupdate=ahora_utc
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
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)


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
    # Ídem Licitacion (F-detalles-fallos).
    detalle_fallos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    detalle_ultimo_fallo: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # F-merc-1: campos del detalle que la ingesta de mercado guarda (rutas de la guía
    # v3.0; `unidad_compra` verificada en la sonda del 31-oct).
    unidad_compra: Mapped[str | None] = mapped_column(String(500), nullable=True)
    direccion_entrega: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    plazo_entrega_dias: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tipo_presupuesto: Mapped[str | None] = mapped_column(String(50), nullable=True)
    fecha_cierre_segundo_llamado: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    motivo_desierta: Mapped[str | None] = mapped_column(Text, nullable=True)
    motivo_cancelacion: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Última vez que se guardaron sus cotizaciones (aunque vinieran vacías).
    cotizaciones_obtenidas_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # F-plat-1: bandera "CA sin detalle" (completo | incompleto | sin_detalle). La calcula
    # `app.mercado.catalogo.recalcular_detalle_lineas` a partir de `ca_lineas`.
    detalle_lineas: Mapped[str] = mapped_column(
        String(12), nullable=False, default="sin_detalle", server_default="sin_detalle"
    )
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=ahora_utc, onupdate=ahora_utc
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


class CaCotizacion(Base):
    """Una oferta de `proveedores_cotizando[]` de una CA (F-merc-1).

    `llamado` = `estado_convocatoria` del detalle (1/2, primer o segundo llamado según
    la guía [I]); el arreglo refleja solo el llamado vigente. `seleccionado` es
    nullable a propósito: None = la API no lo dijo, no "perdió". Sin raw_json (regla 11).

    Un RUT puede mandar varias ofertas distintas a la misma CA [V, sonda 4-nov-2026]: la
    fila es la OFERTA, no el proveedor. UNIQUE por (CA, llamado, `id_cotizacion`); sin
    `id_cotizacion`, la escritura guarda la clave sustituta "rut:<rut>" (F-merc-1c).
    """

    __tablename__ = "ca_cotizaciones"
    __table_args__ = (
        UniqueConstraint("ca_codigo", "llamado", "id_cotizacion", name="uq_ca_cotizacion_oferta"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    ca_codigo: Mapped[str] = mapped_column(
        ForeignKey("compras_agiles.codigo", ondelete="CASCADE"), nullable=False
    )
    llamado: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rut_proveedor: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    razon_social: Mapped[str | None] = mapped_column(String(500), nullable=True)
    es_emt: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    codigo_empresa: Mapped[str | None] = mapped_column(String(50), nullable=True)
    codigo_sucursal: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Para cruzar con oc-da (F-merc-2) sin inferir ganadores en el cliente.
    id_cotizacion: Mapped[str | None] = mapped_column(String(50), nullable=True)
    id_oc: Mapped[str | None] = mapped_column(String(50), nullable=True)
    estado_glosa: Mapped[str | None] = mapped_column(String(100), nullable=True)
    activo: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    seleccionado: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    valor_neto: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_impuesto: Mapped[float | None] = mapped_column(Float, nullable=True)
    monto_despacho: Mapped[float | None] = mapped_column(Float, nullable=True)
    monto_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    fecha_creacion: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fecha_vigencia: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    descripcion: Mapped[str | None] = mapped_column(Text, nullable=True)

    productos: Mapped[list[CaCotizacionProducto]] = relationship(
        "CaCotizacionProducto",
        back_populates="cotizacion",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CaCotizacionProducto(Base):
    """Un producto de una oferta (`productos_cotizados[]`): precio unitario neto (F-merc-1)."""

    __tablename__ = "ca_cotizacion_productos"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    cotizacion_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("ca_cotizaciones.id", ondelete="CASCADE"), nullable=False
    )
    codigo_producto: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nombre_producto: Mapped[str | None] = mapped_column(String(500), nullable=True)
    descripcion: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    cantidad: Mapped[float | None] = mapped_column(Float, nullable=True)
    precio_unitario: Mapped[float | None] = mapped_column(Float, nullable=True)
    monto_total_producto: Mapped[float | None] = mapped_column(Float, nullable=True)

    cotizacion: Mapped[CaCotizacion] = relationship("CaCotizacion", back_populates="productos")


class OcLineaCa(Base):
    """Línea de una orden de compra de Compra Ágil (oc-da) de las regiones del piloto (F-merc-2).

    Solo `EsCompraAgil = Si`, en `GRUPOS` y con comprador en las regiones activas (F-plat-3). PK
    `(codigo_oc, id_item)`: `IDItem` de oc-da [V]. Una OC puede venir en más de un ZIP mensual:
    queda la fila del mes más reciente. Datos
    públicos y comunes (no pertenecen a una empresa). No se purga (excepción de retención).
    """

    __tablename__ = "oc_lineas_ca"

    codigo_oc: Mapped[str] = mapped_column(String(50), primary_key=True)
    id_item: Mapped[str] = mapped_column(String(30), primary_key=True)
    mes: Mapped[str] = mapped_column(String(7), nullable=False)  # AAAA-MM del archivo
    fecha_creacion: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    codigo_estado: Mapped[str | None] = mapped_column(String(10), nullable=True)
    moneda: Mapped[str | None] = mapped_column(String(10), nullable=True)
    rut_unidad: Mapped[str | None] = mapped_column(String(20), nullable=True)
    unidad_compra: Mapped[str | None] = mapped_column(Text, nullable=True)
    organismo: Mapped[str | None] = mapped_column(Text, nullable=True)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    comuna_compra: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region_compra: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rut_proveedor: Mapped[str | None] = mapped_column(String(20), nullable=True)
    proveedor: Mapped[str | None] = mapped_column(Text, nullable=True)
    comuna_proveedor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region_proveedor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proveedor_local: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    codigo_producto: Mapped[str | None] = mapped_column(String(20), nullable=True)
    producto_generico: Mapped[str | None] = mapped_column(Text, nullable=True)
    grupo: Mapped[str] = mapped_column(String(100), nullable=False)
    especificacion: Mapped[str | None] = mapped_column(Text, nullable=True)
    cantidad: Mapped[float | None] = mapped_column(Float, nullable=True)
    unidad_medida: Mapped[str | None] = mapped_column(String(50), nullable=True)
    precio_neto: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_linea_neto: Mapped[float | None] = mapped_column(Float, nullable=True)
    precio_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    base: Mapped[str | None] = mapped_column(String(3), nullable=True)  # kg | L | u

    __table_args__ = (
        Index("ix_oc_lineas_ca_mes", "mes"),
        Index("ix_oc_lineas_ca_region_mes", "region_compra", "mes"),
    )


class PrecioReferencia(Base):
    """Percentiles nacionales de precio base por producto y mes, de oc-da (F-merc-2).

    `clave` = "UNSPSC|base" (kg, L o u). Solo líneas de Compra Ágil en `GRUPOS` y en CLP.
    """

    __tablename__ = "precio_referencia"

    clave: Mapped[str] = mapped_column(String(30), primary_key=True)
    mes: Mapped[str] = mapped_column(String(7), primary_key=True)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    p25: Mapped[float] = mapped_column(Float, nullable=False)
    mediana: Mapped[float] = mapped_column(Float, nullable=False)
    p75: Mapped[float] = mapped_column(Float, nullable=False)


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
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=ahora_utc, onupdate=ahora_utc
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
    # Primera vez que el perfil matcheó esta oportunidad. Inmutable ante re-match:
    # el resumen consolidado la usa para no re-reportar oportunidades viejas.
    fecha_match: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)

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
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=ahora_utc, onupdate=ahora_utc
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
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    # Marca de lote del job `plan-anual` (F-plan-busqueda): NULL = fila cacheada
    # on-demand por institución (get_plan de app/ingest/plan_compra.py); con
    # valor = fila del año completo, reemplazada atómicamente en cada corrida
    # (ver sync_plan_anual_completo).
    lote_id: Mapped[int | None] = mapped_column(BigInt, nullable=True)


class PlanCompraSync(Base):
    """Caché/TTL por (codigo_entidad, agno): evita re-descargar el ZIP en cada
    consulta (el PAC se regenera ~mensualmente, ver §5-bis g)."""

    __tablename__ = "plan_compra_sync"

    codigo_entidad: Mapped[int] = mapped_column(Integer, primary_key=True)
    agno: Mapped[int] = mapped_column(Integer, primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
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
# Explorador de Compras Ágiles (F-ca-explorar)
# ---------------------------------------------------------------------------


class RubroFavorito(Base):
    """Rubro (prefijo UNSPSC de 2 a 8 dígitos) que un usuario marcó como favorito.
    Preselecciona el filtro de rubro del explorador; nada más lo lee todavía."""

    __tablename__ = "rubros_favoritos"
    __table_args__ = (UniqueConstraint("owner_id", "prefijo", name="uq_rubro_favorito"),)

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False
    )
    prefijo: Mapped[str] = mapped_column(String(8), nullable=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)


class RubroVocabulario(Base):
    """Vocabulario típico de un rubro: lexemas de `to_tsvector('spanish', ...)`
    aprendidos de `licitacion_items`. DERIVADO: el job `vocabulario-rubros` lo
    regenera entero (app/catalogos/vocabulario_rubro.py); `prefijo` es siempre
    una familia UNSPSC de 4 dígitos."""

    __tablename__ = "rubro_vocabulario"

    prefijo: Mapped[str] = mapped_column(String(8), primary_key=True)
    lexema: Mapped[str] = mapped_column(String(60), primary_key=True)
    df_rubro: Mapped[int] = mapped_column(Integer, nullable=False)
    lift: Mapped[float] = mapped_column(Float, nullable=False)
    # Palabra legible (minúsculas, con tildes) más frecuente de ese lexema en la
    # familia; NULL si no hay una válida (F-ca-vocab). El panel muestra el lexema.
    palabra: Mapped[str | None] = mapped_column(String(60), nullable=True)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)


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
# Historial de corridas de jobs (observabilidad)
# ---------------------------------------------------------------------------


class JobRun(Base):
    """Una fila por corrida de job: memoria para el dead-man's switch.

    `job` se graba TAL CUAL llega del disparador: el mismo job lógico tiene
    nombres distintos según el camino (endpoint, scheduler, CLI). Los alias se
    resuelven al leer, en el watchlist de /api/salud/jobs.
    """

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(50), nullable=False)
    iniciado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    terminado_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    estado: Mapped[str] = mapped_column(String(20), nullable=False)  # ok | error | omitido
    resultado_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Ãndices adicionales
# ---------------------------------------------------------------------------

Index("ix_licitaciones_estado", Licitacion.estado)
Index("ix_licitaciones_fecha_cierre", Licitacion.fecha_cierre)
Index("ix_licitaciones_region", Licitacion.region)
Index("ix_compras_agiles_estado", CompraAgil.estado)
Index("ix_compras_agiles_region", CompraAgil.region)
Index("ix_oportunidades_match_perfil", OportunidadMatch.perfil_id)
Index("ix_plan_compra_lineas_entidad_agno", PlanCompraLinea.codigo_entidad, PlanCompraLinea.agno)
Index("ix_instituciones_pac_razon_social", InstitucionPAC.razon_social)
Index("ix_match_feedback_usuario", MatchFeedback.usuario_id)
# Sirve al switch (último "ok" por job) y a la purga de retención.
Index("ix_job_runs_job_iniciado", JobRun.job, JobRun.iniciado_en.desc())
# F-plan-busqueda: orden (agno, codigo_entidad) para la búsqueda por palabra
# acotada por año/organismo, y monto para el orden "mayor monto primero". El
# índice GIN de expresión sobre descripcion_producto vive en la migración
# (d7f2a4c8b6e1), no en el ORM: es funcional, sin columna generada.
Index("ix_plan_compra_lineas_agno_entidad", PlanCompraLinea.agno, PlanCompraLinea.codigo_entidad)
Index("ix_plan_compra_lineas_monto", PlanCompraLinea.monto_estimado_clp)

# F-indices: FK sin índice (borrado en cascada, EXISTS del recall, selectinload) y
# fechas de vigencia de CA. Los GIN de expresión de FTS sobre licitacion_items.nombre
# y ca_productos (nombre || ' ' || descripcion) viven en la migración (b8c2d5e9a1f7),
# no en el ORM (mismo criterio que ix_plan_compra_lineas_desc_tsv).
Index("ix_licitacion_items_licitacion_codigo", LicitacionItem.licitacion_codigo)
Index("ix_ca_productos_ca_codigo", CaProducto.ca_codigo)
Index("ix_alertas_match_id", Alerta.match_id)
Index("ix_alertas_seguimiento_id", Alerta.seguimiento_id)
Index("ix_oportunidades_match_fuente_codigo", OportunidadMatch.fuente, OportunidadMatch.codigo_oportunidad)
Index("ix_compras_agiles_fecha_cierre", CompraAgil.fecha_cierre)
Index("ix_compras_agiles_fecha_publicacion", CompraAgil.fecha_publicacion)
Index("ix_compras_agiles_detalle_lineas", CompraAgil.detalle_lineas)

# F-merc-1: quién cotiza dónde (por proveedor) y FK de los productos (borrado en cascada).
# El UNIQUE (ca_codigo, llamado, rut_proveedor) ya sirve a la búsqueda por CA.
Index("ix_ca_cotizaciones_rut_proveedor", CaCotizacion.rut_proveedor)
Index("ix_ca_cotizacion_productos_cotizacion_id", CaCotizacionProducto.cotizacion_id)


# ---------------------------------------------------------------------------
# Pivote plataforma de Compra Ágil (F-plat-3): empresa, proveedores y costos.
# Todo dato comercial pertenece a una empresa (regla 26). Ninguna se purga (regla 11).
# ---------------------------------------------------------------------------


class Empresa(Base):
    __tablename__ = "empresas"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    rut: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True)
    comuna_base: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Unión de las activas = territorio que se ingesta (app.mercado.territorio).
    regiones_prioritarias: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=lambda: [16]
    )
    # Ordenan la mesa y la logística; NO filtran la ingesta (regla 7).
    comunas_prioritarias: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=ahora_utc, onupdate=ahora_utc
    )


class Proveedor(Base):
    __tablename__ = "proveedores"
    __table_args__ = (Index("ix_proveedores_empresa_nombre", "empresa_id", "nombre"),)

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    empresa_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(300), nullable=False)
    rut: Mapped[str | None] = mapped_column(String(20), nullable=True)
    comuna: Mapped[str | None] = mapped_column(String(100), nullable=True)
    direccion: Mapped[str | None] = mapped_column(String(300), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    contacto_nombre: Mapped[str | None] = mapped_column(String(200), nullable=True)
    telefono: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Familias de app.catalogos.mercado sugeridas, pero texto libre ("lácteos, abarrotes").
    rubros: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    plazo_pago_dias: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimo_compra_clp: Mapped[int | None] = mapped_column(BigInt, nullable=True)
    despacha: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=ahora_utc, onupdate=ahora_utc
    )

    productos: Mapped[list[ProveedorProducto]] = relationship(
        "ProveedorProducto", back_populates="proveedor", cascade="all, delete-orphan"
    )


class ProveedorProducto(Base):
    __tablename__ = "proveedor_productos"
    __table_args__ = (
        UniqueConstraint(
            "proveedor_id", "producto_texto", "formato_texto", name="uq_proveedor_producto_formato"
        ),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    proveedor_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("proveedores.id", ondelete="CASCADE"), nullable=False
    )
    # F-plat-1: FK al catálogo canónico (SET NULL: borrar un producto no borra el costo).
    producto_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("producto_canonico.id", ondelete="SET NULL"), nullable=True
    )
    formato_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("producto_formato.id", ondelete="SET NULL"), nullable=True
    )
    producto_texto: Mapped[str] = mapped_column(String(300), nullable=False)
    codigo_unspsc: Mapped[str | None] = mapped_column(String(20), nullable=True)
    formato_texto: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    factor_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    unidad_base: Mapped[str | None] = mapped_column(String(10), nullable=True)  # kg | L | u
    # Neto (sin IVA) por FORMATO, no por kg.
    precio_neto_actual: Mapped[float] = mapped_column(Float, nullable=False)
    vigente_desde: Mapped[date] = mapped_column(Date, nullable=False)
    fuente: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    disponible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=ahora_utc, onupdate=ahora_utc
    )

    proveedor: Mapped[Proveedor] = relationship("Proveedor", back_populates="productos")


class CostoHistorial(Base):
    """Append-only: nunca se edita ni se borra una fila (el precio actual vive en
    `proveedor_productos`; cada cambio inserta una fila acá, en la misma transacción)."""

    __tablename__ = "costo_historial"
    __table_args__ = (
        Index("ix_costo_historial_pp_vigente", "proveedor_producto_id", "vigente_desde"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    proveedor_producto_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("proveedor_productos.id", ondelete="CASCADE"), nullable=False
    )
    precio_neto: Mapped[float] = mapped_column(Float, nullable=False)
    vigente_desde: Mapped[date] = mapped_column(Date, nullable=False)
    fuente: Mapped[str] = mapped_column(String(20), nullable=False)
    # Nullable: importaciones por script.
    usuario_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    registrado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)


class TarifaLogistica(Base):
    __tablename__ = "tarifas_logistica"
    __table_args__ = (
        UniqueConstraint("empresa_id", "ambito", "nombre", name="uq_tarifa_empresa_ambito_nombre"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    empresa_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False
    )
    ambito: Mapped[str] = mapped_column(String(20), nullable=False)  # comuna | provincia
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    costo_fijo_clp: Mapped[int] = mapped_column(BigInt, nullable=False, default=0)
    costo_por_kg_clp: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    dias: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Catálogo canónico y líneas 1:1 por Compra Ágil (F-plat-1). Datos comunes a todas las
# empresas (como `oc_lineas_ca`): el catálogo es compartido y `revisado_por` deja la huella.
# Ninguna se purga (retención: no aplica).
# ---------------------------------------------------------------------------


class ProductoCanonico(Base):
    __tablename__ = "producto_canonico"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    familia: Mapped[str | None] = mapped_column(String(100), nullable=True)
    unspsc_sugerido: Mapped[str | None] = mapped_column(String(20), nullable=True)
    unidad_base: Mapped[str] = mapped_column(String(3), nullable=False)  # kg | L | u
    activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    creado_por: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=ahora_utc, onupdate=ahora_utc
    )

    formatos: Mapped[list[ProductoFormato]] = relationship(
        "ProductoFormato", back_populates="producto", cascade="all, delete-orphan"
    )


class ProductoFormato(Base):
    __tablename__ = "producto_formato"
    __table_args__ = (
        UniqueConstraint("producto_id", "nombre", name="uq_producto_formato"),
        CheckConstraint("factor_base > 0", name="factor"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    producto_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("producto_canonico.id", ondelete="CASCADE"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    factor_base: Mapped[float] = mapped_column(Float, nullable=False)  # > 0
    ean: Mapped[str | None] = mapped_column(String(20), nullable=True)

    producto: Mapped[ProductoCanonico] = relationship("ProductoCanonico", back_populates="formatos")


class ProductoAlias(Base):
    __tablename__ = "producto_alias"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    texto_normalizado: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    producto_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("producto_canonico.id", ondelete="CASCADE"), nullable=False
    )
    formato_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("producto_formato.id", ondelete="SET NULL"), nullable=True
    )
    fuente: Mapped[str] = mapped_column(String(10), nullable=False, default="humano")
    confianza: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    revisado_por: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    revisado_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    veces_visto: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CaLinea(Base):
    """Línea de lo SOLICITADO en una Compra Ágil (`ca_productos`), normalizada a un producto.

    Sin FK a `ca_productos`: la ingesta los borra y reinserta en cada detalle; la clave es
    `(ca_codigo, n)` con `n` = posición del producto dentro de la CA.
    """

    __tablename__ = "ca_lineas"
    __table_args__ = (UniqueConstraint("ca_codigo", "n", name="uq_ca_linea"),)

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    ca_codigo: Mapped[str] = mapped_column(
        String(50), ForeignKey("compras_agiles.codigo", ondelete="CASCADE"), nullable=False
    )
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    producto_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("producto_canonico.id", ondelete="SET NULL"), nullable=True
    )
    formato_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("producto_formato.id", ondelete="SET NULL"), nullable=True
    )
    cantidad: Mapped[float | None] = mapped_column(Float, nullable=True)
    cantidad_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    texto_origen: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Clave de alias de `texto_origen` (`canonico.normalizar_texto`): permite re-aplicar un alias
    # confirmado a todas las líneas iguales con un UPDATE, sin escanear los textos.
    texto_norm: Mapped[str] = mapped_column(
        String(300), nullable=False, default="", server_default=""
    )
    codigo_unspsc: Mapped[str | None] = mapped_column(String(20), nullable=True)
    origen: Mapped[str] = mapped_column(String(10), nullable=False, default="api")
    estado: Mapped[str] = mapped_column(String(10), nullable=False, default="pendiente")
    revisado_por: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    revisado_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ahora_utc)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=ahora_utc, onupdate=ahora_utc
    )


Index("ix_producto_alias_producto_id", ProductoAlias.producto_id)
Index("ix_ca_lineas_producto_id", CaLinea.producto_id)
Index("ix_ca_lineas_estado", CaLinea.estado)
Index("ix_ca_lineas_texto_norm", CaLinea.texto_norm)
