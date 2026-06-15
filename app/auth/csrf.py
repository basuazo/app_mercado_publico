"""CSRF token determinístico por usuario (HMAC-SHA256 sobre secret_key + user_id)."""

from __future__ import annotations

import hashlib
import hmac


def generate_csrf_token(secret_key: str, user_id: int) -> str:
    msg = f"csrf:{user_id}".encode()
    return hmac.new(secret_key.encode(), msg, hashlib.sha256).hexdigest()[:40]


def validate_csrf_token(secret_key: str, user_id: int, token: str) -> bool:
    if not token:
        return False
    expected = generate_csrf_token(secret_key, user_id)
    return hmac.compare_digest(expected, token)
