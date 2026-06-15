"""Cookie de sesión firmada con itsdangerous (7 días, HttpOnly + Secure)."""

from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "mp_session"
_MAX_AGE = 7 * 24 * 3600  # 7 días


def create_session_token(secret_key: str, user_id: int) -> str:
    s = URLSafeTimedSerializer(secret_key, salt="session")
    return s.dumps(user_id)


def decode_session_token(secret_key: str, token: str) -> int | None:
    s = URLSafeTimedSerializer(secret_key, salt="session")
    try:
        return int(s.loads(token, max_age=_MAX_AGE))
    except (BadSignature, SignatureExpired, ValueError):
        return None
