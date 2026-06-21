"""CSRF token ligado al nonce de sesión (HMAC-SHA256 sobre secret_key + nonce).

El nonce se genera por login (ver app.auth.session.create_session_token), por
lo que el token CSRF rota en cada sesión nueva en vez de ser determinístico
por usuario.
"""

from __future__ import annotations

import hashlib
import hmac


def generate_csrf_token(secret_key: str, nonce: str) -> str:
    msg = f"csrf:{nonce}".encode()
    return hmac.new(secret_key.encode(), msg, hashlib.sha256).hexdigest()[:40]


def validate_csrf_token(secret_key: str, nonce: str, token: str) -> bool:
    if not token:
        return False
    expected = generate_csrf_token(secret_key, nonce)
    return hmac.compare_digest(expected, token)
