"""Normalización de montos a CLP.

Limitación conocida: las tasas de cambio son valores estáticos configurados
en settings (TASA_UF, TASA_UTM, TASA_USD, TASA_EUR). No se consulta un
provider externo en tiempo real. Para tasas actualizadas, reemplazar esta
función por una que consuma el Banco Central u otro servicio, manteniendo
la misma firma normalizar_clp(monto, moneda, settings).
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.core.settings import Settings

_log = get_logger(__name__)


def normalizar_clp(
    monto: float | None,
    moneda: str | None,
    settings: Settings,
) -> float | None:
    """Convierte un monto en la moneda indicada a CLP.

    Retorna None si monto es None o la moneda es desconocida (no lanza).
    """
    if monto is None:
        return None
    if not moneda:
        return monto  # Asumir CLP si no hay moneda

    m = moneda.strip().upper()
    if m in ("CLP", ""):
        return monto

    tasa: float | None = None
    if m in ("CLF", "UF"):
        tasa = settings.tasa_uf
    elif m == "UTM":
        tasa = settings.tasa_utm
    elif m == "USD":
        tasa = settings.tasa_usd
    elif m == "EUR":
        tasa = settings.tasa_eur
    else:
        _log.warning(
            "normalizar_clp: moneda desconocida %r, retornando monto sin convertir", moneda
        )
        return None

    return monto * tasa
