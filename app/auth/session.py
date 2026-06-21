"""Cookie de sesión firmada con itsdangerous (7 días, HttpOnly + Secure)."""

from __future__ import annotations

import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "mp_session"
_MAX_AGE = 7 * 24 * 3600  # 7 días


def create_session_token(secret_key: str, user_id: int) -> str:
    s = URLSafeTimedSerializer(secret_key, salt="session")
    nonce = secrets.token_hex(32)
    return s.dumps({"uid": user_id, "n": nonce})


def decode_session_token(secret_key: str, token: str) -> tuple[int, str] | None:
    """Decodifica la cookie de sesión. Retorna (user_id, nonce) o None.

    Las sesiones antiguas (payload entero, sin nonce) ya no son válidas y
    fuerzan re-login para que todos los CSRF tokens roten.
    """
    s = URLSafeTimedSerializer(secret_key, salt="session")
    try:
        payload = s.loads(token, max_age=_MAX_AGE)
    except (BadSignature, SignatureExpired, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload["uid"]), str(payload["n"])
    except (KeyError, TypeError, ValueError):
        return None
