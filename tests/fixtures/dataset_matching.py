"""Dataset sintético para tests de matching (F4).

15 oportunidades (9 licitaciones + 6 compras ágiles) y 3 perfiles de 2 dueños distintos.
Cubre: tilde vs sin tilde, keyword en producto, exclusión, monto fuera de rango,
CA otra región, bonus nombre, 0 ofertas vs >3 ofertas, urgencia límite.

Uso:
    from tests.fixtures.dataset_matching import crear_dataset, AHORA

    def test_algo(session):
        ds = crear_dataset(session)
        perfil_a1 = ds["perfiles"]["a1"]
        ...
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.matching.perfiles import crear_perfil
from app.models.tables import (
    CompraAgil,
    Licitacion,
    LicitacionItem,
    Usuario,
)

# Referencia temporal fija para todos los tests
AHORA = datetime(2026, 6, 13, 12, 0, 0)

_PW_HASH = "$2b$12$fakehashforteststhatislong.enough.xyz"  # bcrypt placeholder

# Identificadores fijos del dataset — única fuente de verdad para que los
# fixtures de Postgres puedan limpiar antes/después sin duplicar la lista.
USER_EMAILS: tuple[str, ...] = ("owner_a@test.com", "owner_b@test.com")
LICITACION_CODIGOS: tuple[str, ...] = (
    "LIC-TILDE",
    "LIC-ILUMINA",
    "LIC-PRODUCTO",
    "LIC-EXCLUIDO",
    "LIC-MONTO-BAJO",
    "LIC-MONTO-NULL",
    "LIC-NOMBRE-BONUS",
    "LIC-NO-MATCH",
    "LIC-URGENCIA-ZERO",
)
CA_CODIGOS: tuple[str, ...] = (
    "CA-0OF",
    "CA-SIN-MONTO",
    "CA-2OF",
    "CA-OTRA-REGION",
    "CA-ILUMINA-5OF",
    "CA-CIERRE-1DIA",
)


def crear_dataset(session: Session) -> dict:
    """Inserta el dataset completo en `session`. Hace flush pero NO commit.

    Retorna dict con:
        users:      {"a": Usuario, "b": Usuario}
        perfiles:   {"a1": PerfilBusqueda, "a2": PerfilBusqueda, "b1": PerfilBusqueda}
        licitaciones: {codigo: Licitacion, ...}
        cas:          {codigo: CompraAgil, ...}
    """
    # ------------------------------------------------------------------
    # Usuarios (2 dueños distintos)
    # ------------------------------------------------------------------
    user_a = Usuario(
        email="owner_a@test.com",
        password_hash=_PW_HASH,
        activo=True,
    )
    user_b = Usuario(
        email="owner_b@test.com",
        password_hash=_PW_HASH,
        activo=True,
    )
    session.add_all([user_a, user_b])
    session.flush()

    # ------------------------------------------------------------------
    # Perfiles (3 perfiles, 2 dueños)
    # ------------------------------------------------------------------
    # A1: palabras clave "eléctrico" + "iluminación", excluye "excluido",
    #     fuentes ambas, región 13, monto mínimo 100k
    perfil_a1 = crear_perfil(
        session,
        user_a.id,
        "Eléctrico e Iluminación (A1)",
        keywords=["eléctrico", "iluminación"],
        keywords_excluir=["excluido"],
        fuentes=["licitaciones", "compras_agiles"],
        regiones=[13],
        monto_min_clp=100_000.0,
    )
    # A2: solo "eléctrico", sin exclusión, solo licitaciones, sin filtros adicionales
    perfil_a2 = crear_perfil(
        session,
        user_a.id,
        "Solo Eléctrico licitaciones (A2)",
        keywords=["eléctrico"],
        fuentes=["licitaciones"],
    )
    # B1 (dueño B): "eléctrico", solo CA, región 13
    perfil_b1 = crear_perfil(
        session,
        user_b.id,
        "Eléctrico CA región 13 (B1)",
        keywords=["eléctrico"],
        fuentes=["compras_agiles"],
        regiones=[13],
    )
    session.flush()

    # ------------------------------------------------------------------
    # Licitaciones
    # ------------------------------------------------------------------
    lics: dict[str, Licitacion] = {}

    def _lic(codigo: str, nombre: str, desc: str = "", monto: float | None = None, dias: float = 5.0) -> Licitacion:
        obj = Licitacion(
            codigo=codigo,
            nombre=nombre,
            descripcion=desc,
            estado="publicada",
            monto_clp=monto,
            fecha_cierre=AHORA + timedelta(days=dias),
        )
        session.add(obj)
        lics[codigo] = obj
        return obj

    # 1. Nombre "electrico" (sin tilde) → keyword "eléctrico" (con tilde) → match FTS
    _lic("LIC-TILDE", "Suministro material electrico", monto=500_000.0, dias=5.0)

    # 2. Nombre "iluminacion" (sin tilde) → keyword "iluminación" → match FTS
    _lic("LIC-ILUMINA", "Servicio de iluminacion LED", monto=200_000.0, dias=20.0)

    # 3. Nombre neutro + item "Cable electrico" → keyword solo en producto
    _lic("LIC-PRODUCTO", "Compra materiales varios", desc="Adquisicion diversa", monto=300_000.0, dias=10.0)

    # 4. Keyword "electrico" + keyword excluida "excluido" → descartada por PERFIL-A1
    _lic("LIC-EXCLUIDO", "Material electrico excluido", monto=300_000.0, dias=7.0)

    # 5. Monto bajo (50k < monto_min=100k de A1) → descartada por A1, no por A2
    _lic("LIC-MONTO-BAJO", "Instalacion electrica basica", monto=50_000.0, dias=5.0)

    # 6. Monto None → pasa filtro, razón monto_no_informado=True
    _lic("LIC-MONTO-NULL", "Equipos electricos varios", monto=None, dias=8.0)

    # 7. Hit en nombre → activa bonus_nombre en score_texto
    _lic("LIC-NOMBRE-BONUS", "Sistema electrico de automatizacion", monto=500_000.0, dias=5.0)

    # 8. Sin keyword match → ningún perfil lo captura
    _lic("LIC-NO-MATCH", "Servicio de limpieza general", monto=100_000.0, dias=5.0)

    # 9. Cierre en 35 días → urgencia 0 (>30)
    _lic("LIC-URGENCIA-ZERO", "Material electrico reposicion", monto=200_000.0, dias=35.0)

    session.flush()

    # Item para LIC-PRODUCTO: keyword solo en producto, no en nombre/descripcion
    session.add(
        LicitacionItem(
            licitacion_codigo="LIC-PRODUCTO",
            codigo_producto="P-CABLE",
            nombre="Cable electrico",
            cantidad=100.0,
            unidad="m",
        )
    )

    # ------------------------------------------------------------------
    # Compras Ágiles
    # ------------------------------------------------------------------
    cas: dict[str, CompraAgil] = {}

    def _ca(
        codigo: str,
        nombre: str,
        region: int,
        ofertas: int,
        monto: float | None = 300_000.0,
        dias: float = 5.0,
        desc: str = "",
    ) -> CompraAgil:
        c = CompraAgil(
            codigo=codigo,
            nombre=nombre,
            descripcion=desc,
            estado="publicada",
            region=region,
            total_ofertas=ofertas,
            monto_disponible_clp=monto,
            fecha_cierre=AHORA + timedelta(days=dias),
        )
        session.add(c)
        cas[codigo] = c
        return c

    # CA-1: región 13, 0 ofertas, cierre 4 días → score_competencia máximo + urgencia 25
    _ca("CA-0OF", "Instalacion electrica rapida", region=13, ofertas=0, monto=300_000.0, dias=4.0)

    # CA-2: región 13, 1 oferta, monto=None, cierre 3 días → monto_no_informado, urgencia 25
    _ca("CA-SIN-MONTO", "Material electrico varios", region=13, ofertas=1, monto=None, dias=3.0)

    # CA-3: región 13, 2 ofertas, cierre 15 días → urgencia 10
    _ca("CA-2OF", "Suministro material electrico", region=13, ofertas=2, monto=500_000.0, dias=15.0)

    # CA-4: región 7 (OTRA región) → filtrada localmente para perfiles con regiones=[13]
    _ca("CA-OTRA-REGION", "Materiales electricos region sur", region=7, ofertas=2, monto=200_000.0, dias=6.0)

    # CA-5: región 13, 5 ofertas, "iluminacion" → match solo para PERFIL-A1 (keyword iluminación)
    _ca("CA-ILUMINA-5OF", "Sistema de iluminacion exterior", region=13, ofertas=5, monto=300_000.0, dias=15.0)

    # CA-6: región 13, cierre en 20 horas (<2 días) → urgencia 0
    _ca(
        "CA-CIERRE-1DIA",
        "Cableado electrico urgente",
        region=13,
        ofertas=2,
        monto=200_000.0,
        dias=20.0 / 24.0,
    )

    session.flush()

    return {
        "users": {"a": user_a, "b": user_b},
        "perfiles": {"a1": perfil_a1, "a2": perfil_a2, "b1": perfil_b1},
        "licitaciones": lics,
        "cas": cas,
    }
