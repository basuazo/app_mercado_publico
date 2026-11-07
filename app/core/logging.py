"""Logging estructurado con enmascaramiento de secretos."""

import json
import logging
import os
import re

# Secretos que nacen en memoria durante la corrida (el token anónimo del servicio de adjuntos,
# F-plat-anexos-auto): nunca van a un log, pero si algo los arrastra, quedan enmascarados.
_DINAMICOS: list[str] = []
_MAX_DINAMICOS = 20


def enmascarar_tambien(valor: str) -> None:
    """Agrega `valor` a lo que todos los filtros de secretos enmascaran (solo en memoria)."""
    valor = (valor or "").strip()
    if len(valor) < 8 or valor in _DINAMICOS:
        return
    _DINAMICOS.append(valor)
    del _DINAMICOS[:-_MAX_DINAMICOS]


class _SecretFilter(logging.Filter):
    """Reemplaza valores de secretos de entorno en los mensajes de log."""

    _PLACEHOLDER = "***"
    _ENV_VARS = (
        "MP_TICKET",
        "SECRET_KEY",
        "JOBS_TOKEN",
        "SMTP_PASSWORD",
        "BREVO_API_KEY",
        "DATABASE_URL",
        "DATABASE_URL_PROD",
        "ADMIN_PASSWORD",
        "OPENROUTER_API_KEY",
        "MP_ADJUNTOS_USER_KEY",
        "MP_ADJUNTOS_HEADERS_EXTRA",
    )
    # JSON de headers: además del texto entero se enmascara cada valor por separado.
    _ENV_JSON = ("MP_ADJUNTOS_HEADERS_EXTRA",)

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []
        self._reload()

    def _reload(self) -> None:
        secretos = [v for var in self._ENV_VARS if (v := os.getenv(var, "").strip())]
        for var in self._ENV_JSON:
            try:
                datos = json.loads(os.getenv(var, "") or "{}")
            except ValueError:
                continue
            if isinstance(datos, dict):
                secretos += [s for v in datos.values() if (s := str(v).strip())]
        # Los largos primero: un secreto que contiene a otro no queda a medio enmascarar.
        self._secrets = sorted(set(secretos), key=len, reverse=True)

    def _mask(self, text: str) -> str:
        for secret in (*self._secrets, *_DINAMICOS):
            if secret:
                text = text.replace(secret, self._PLACEHOLDER)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._mask(str(record.msg))
        args = record.args
        if isinstance(args, dict):
            # `_log.info("x: %s", {"a": 1})`: logging guarda el dict como mapping.
            # Convertirlo a tupla dejaba solo sus claves y reventaba el formateo.
            record.args = {
                k: self._mask(v) if isinstance(v, str) else v for k, v in args.items()
            }
        else:
            record.args = tuple(
                self._mask(str(a)) if isinstance(a, str) else a for a in (args or ())
            )
        return True


def setup_logging(level: int = logging.INFO) -> None:
    """Configura el logger raíz con formato estructurado y filtro de secretos."""
    secret_filter = _SecretFilter()
    handler = logging.StreamHandler()
    handler.addFilter(secret_filter)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    # Añadir el filtro también al logger raíz para capturar registros de librerías terceras
    # (ej. httpx que loguea URLs completas con query params, incluyendo el ticket)
    for f in list(root.filters):
        if isinstance(f, _SecretFilter):
            root.removeFilter(f)
    root.addFilter(secret_filter)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# Patrón para uso en tests: detecta si una cadena parece un ticket/token
_SECRET_RE = re.compile(r"[A-Za-z0-9\-_]{16,}")


def looks_like_secret(value: str) -> bool:
    return bool(_SECRET_RE.fullmatch(value))
