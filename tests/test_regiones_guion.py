"""region_codigo tolera guiones y rayas (F-regiones-guion)."""

from __future__ import annotations

import pytest

from app.catalogos.regiones import region_codigo


@pytest.mark.parametrize(
    ("texto", "codigo"),
    [
        ("Región del Bío-Bío", 8),
        ("Región del Bío–Bío", 8),
        ("Región del Bío—Bío", 8),
        ("Región del Biobío", 8),
        ("Región de Ñuble", 16),
        ("Región de Ñuble ", 16),
    ],
)
def test_region_codigo_guiones(texto: str, codigo: int) -> None:
    assert region_codigo(texto) == codigo


def test_region_codigo_sin_dato() -> None:
    assert region_codigo(None) is None
    assert region_codigo("NA") is None
