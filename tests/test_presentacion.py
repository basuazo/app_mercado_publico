"""Tests de las funciones puras de presentación."""

from __future__ import annotations

from app.api.presentacion import nombre_region, razones_legibles


def test_nombre_region_conocida() -> None:
    assert nombre_region(13) == "Metropolitana de Santiago"
    assert nombre_region(16) == "Ñuble"


def test_nombre_region_none() -> None:
    assert nombre_region(None) is None


def test_nombre_region_desconocida_no_rompe() -> None:
    assert nombre_region(99) == "Región 99"


def test_razones_vacias() -> None:
    assert razones_legibles(None) == []
    assert razones_legibles({}) == []


def test_razones_keywords_y_campo() -> None:
    frases = razones_legibles(
        {"keywords_hit": ["software", "datos"], "campo_hit": "nombre"}
    )
    assert any("título" in f and "software" in f for f in frases)


def test_razones_cierre_pronto() -> None:
    frases = razones_legibles({"dias_al_cierre": 3.0})
    assert any("Cierra pronto" in f for f in frases)


def test_razones_cierre_hoy() -> None:
    frases = razones_legibles({"dias_al_cierre": 0.4})
    assert any("hoy" in f for f in frases)


def test_razones_sin_competencia() -> None:
    frases = razones_legibles({"ofertas": 0})
    assert any("sin ofertas" in f.lower() for f in frases)


def test_razones_monto_no_informado() -> None:
    frases = razones_legibles({"monto_no_informado": True})
    assert any("Monto no informado" in f for f in frases)


def test_razones_valores_invalidos_no_rompen() -> None:
    # dias/ofertas con tipos raros no deben lanzar excepción
    frases = razones_legibles({"dias_al_cierre": "x", "ofertas": None})
    assert isinstance(frases, list)
