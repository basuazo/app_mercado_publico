"""Tests F-vigencia — `es_vigente`, la única definición de "todavía se puede
postular" del proyecto. Puro: sin base ni red.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.vigencia import CA_SIN_CIERRE_VIGENCIA_DIAS, es_vigente
from app.models.enums import EstadoOportunidad

_AHORA = datetime(2026, 9, 27, 12, 0, 0)
_FUTURO = _AHORA + timedelta(days=5)
_PASADO = _AHORA - timedelta(days=5)


@pytest.mark.parametrize(
    ("descripcion", "estado", "fecha_cierre", "fuente", "fecha_publicacion", "esperado"),
    [
        ("publicada con cierre futuro", EstadoOportunidad.PUBLICADA, _FUTURO, "licitaciones", None, True),
        # Caso real 1417913-96-L126 [V]: publicada pero con cierre pasado — el
        # estado viene atrasado, la fecha manda.
        ("publicada con cierre pasado", EstadoOportunidad.PUBLICADA, _PASADO, "licitaciones", None, False),
        ("cerrada con cierre futuro", EstadoOportunidad.CERRADA, _FUTURO, "licitaciones", None, False),
        ("adjudicada con cierre futuro", EstadoOportunidad.ADJUDICADA, _FUTURO, "licitaciones", None, False),
        ("desconocido con cierre futuro", "un-estado-nuevo-de-la-api", _FUTURO, "licitaciones", None, True),
        ("desconocido con cierre pasado", "un-estado-nuevo-de-la-api", _PASADO, "licitaciones", None, False),
        ("borde exacto fecha_cierre == ahora", EstadoOportunidad.PUBLICADA, _AHORA, "licitaciones", None, False),
        (
            "CA con cierre NULL, abierta, publicada hace 6 días",
            EstadoOportunidad.PUBLICADA,
            None,
            "compras_agiles",
            _AHORA - timedelta(days=6),
            True,
        ),
        (
            "CA con cierre NULL, abierta, publicada hace 8 días",
            EstadoOportunidad.PUBLICADA,
            None,
            "compras_agiles",
            _AHORA - timedelta(days=8),
            False,
        ),
        (
            "CA con cierre NULL, cerrada",
            EstadoOportunidad.CERRADA,
            None,
            "compras_agiles",
            _AHORA - timedelta(days=1),
            False,
        ),
        (
            "CA con cierre NULL y sin fecha de publicación",
            EstadoOportunidad.PUBLICADA,
            None,
            "compras_agiles",
            None,
            False,
        ),
        # La excepción de "sin cierre" es SOLO de Compra Ágil.
        ("licitación con cierre NULL", EstadoOportunidad.PUBLICADA, None, "licitaciones", None, False),
    ],
)
def test_es_vigente_tabla_de_casos(
    descripcion: str,
    estado: object,
    fecha_cierre: datetime | None,
    fuente: str,
    fecha_publicacion: datetime | None,
    esperado: bool,
) -> None:
    assert es_vigente(estado, fecha_cierre, fuente, _AHORA, fecha_publicacion) is esperado, descripcion


def test_borde_del_tope_de_dias_de_ca_sin_cierre() -> None:
    """Exactamente en el borde de `CA_SIN_CIERRE_VIGENCIA_DIAS` todavía cuenta
    ("de hace 7 días o menos" — regla `>=`)."""
    justo_en_el_borde = _AHORA - timedelta(days=CA_SIN_CIERRE_VIGENCIA_DIAS)
    assert es_vigente(
        EstadoOportunidad.PUBLICADA, None, "compras_agiles", _AHORA, justo_en_el_borde
    )

    un_segundo_despues_del_borde = justo_en_el_borde - timedelta(seconds=1)
    assert not es_vigente(
        EstadoOportunidad.PUBLICADA, None, "compras_agiles", _AHORA, un_segundo_despues_del_borde
    )
