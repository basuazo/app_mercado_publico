"""Logging estructurado con enmascaramiento de secretos."""

import logging
import os
import re


class _SecretFilter(logging.Filter):
    """Reemplaza valores de MP_TICKET, SECRET_KEY y JOBS_TOKEN en los mensajes."""

    _PLACEHOLDER = "***"
    _ENV_VARS = ("MP_TICKET", "SECRET_KEY", "JOBS_TOKEN")

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []
        self._reload()

    def _reload(self) -> None:
        self._secrets = [v for var in self._ENV_VARS if (v := os.getenv(var, "").strip())]

    def _mask(self, text: str) -> str:
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, self._PLACEHOLDER)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._mask(str(record.msg))
        record.args = tuple(
            self._mask(str(a)) if isinstance(a, str) else a for a in (record.args or ())
        )
        return True


def setup_logging(level: int = logging.INFO) -> None:
    """Configura el logger raíz con formato estructurado y filtro de secretos."""
    handler = logging.StreamHandler()
    handler.addFilter(_SecretFilter())
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# Patrón para uso en tests: detecta si una cadena parece un ticket/token
_SECRET_RE = re.compile(r"[A-Za-z0-9\-_]{16,}")


def looks_like_secret(value: str) -> bool:
    return bool(_SECRET_RE.fullmatch(value))
