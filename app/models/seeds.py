"""Seeds: datos de referencia y usuario admin inicial."""

from __future__ import annotations

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.enums import RolUsuario
from app.models.tables import Usuario

_log = get_logger(__name__)
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------------------------------------------------------------------------
# Datos de referencia (usados en migraciones / scripts de seed)
# ---------------------------------------------------------------------------

REGIONES: list[tuple[int, str]] = [
    (1, "Tarapacá"),
    (2, "Antofagasta"),
    (3, "Atacama"),
    (4, "Coquimbo"),
    (5, "Valparaíso"),
    (6, "Libertador General Bernardo O'Higgins"),
    (7, "Maule"),
    (8, "Biobío"),
    (9, "La Araucanía"),
    (10, "Los Lagos"),
    (11, "Aysén del General Carlos Ibáñez del Campo"),
    (12, "Magallanes y de la Antártica Chilena"),
    (13, "Metropolitana de Santiago"),
    (14, "Los Ríos"),
    (15, "Arica y Parinacota"),
    (16, "Ñuble"),
]

TIPOS_LICITACION: list[str] = [
    "L1",
    "LE",
    "LP",
    "LS",
    "A1",
    "B1",
    "J1",
    "F1",
    "E1",
    "CO",
    "B2",
    "A2",
    "D1",
    "E2",
    "C2",
    "C1",
    "F2",
    "F3",
    "G2",
    "G1",
    "R1",
    "CA",
    "SE",
]

# id → descripción (CM=9, AG=13, CC=14 según plan)
TIPOS_OC: dict[int, str] = {
    1: "OC automática",
    2: "Licitación pública",
    3: "Licitación privada",
    4: "Trato directo",
    5: "Convenio suministro",
    6: "Compra menor",
    7: "Compra urgente",
    8: "Gran compra",
    9: "Convenio Marco (CM)",
    10: "Fondo global",
    11: "Trato directo R1",
    12: "Microcompra",
    13: "Compra Ágil (AG)",
    14: "Compra Coordinada (CC)",
}


# ---------------------------------------------------------------------------
# Seed de usuario admin
# ---------------------------------------------------------------------------


def seed_admin(session: Session, email: str, password: str) -> bool:
    """Crea el usuario admin si la tabla está vacía. Idempotente.

    Returns True si creó el usuario, False si ya existía.
    """
    existing = session.execute(select(Usuario).limit(1)).scalar_one_or_none()
    if existing is not None:
        _log.info("seed_admin: tabla usuarios no vacía, omitiendo seed")
        return False
    admin = Usuario(
        email=email,
        password_hash=_pwd.hash(password),
        rol=RolUsuario.ADMIN,
        activo=True,
    )
    session.add(admin)
    session.flush()
    _log.info("seed_admin: usuario admin creado (%s)", email)
    return True
