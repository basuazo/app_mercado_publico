"""Hashing y verificación de contraseñas con bcrypt/passlib."""

from __future__ import annotations

from passlib.context import CryptContext

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def verify_password(plain: str, hashed: str) -> bool:
    return bool(_pwd_ctx.verify(plain, hashed))


def hash_password(plain: str) -> str:
    return str(_pwd_ctx.hash(plain))
