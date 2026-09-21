"""Tests de las funciones puras de presentación."""

from __future__ import annotations

from app.api.presentacion import (
    banda_relevancia,
    formato_clp,
    formato_numero,
    nombre_region,
    razones_legibles,
)


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


def test_razones_no_repiten_el_cierre() -> None:
    """El badge de cierre es el único portador del dato (F-ui-fixes 2.3)."""
    assert razones_legibles({"dias_al_cierre": 3.0}) == []
    assert razones_legibles({"dias_al_cierre": 0.4}) == []
    assert razones_legibles({"dias_al_cierre": 30.0}) == []


def test_razones_cierre_no_tapa_las_demas() -> None:
    frases = razones_legibles({"dias_al_cierre": 2.0, "ofertas": 0})
    assert frases == ["Aún sin ofertas competidoras"]


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


# ---------------------------------------------------------------------------
# Formato de montos y cantidades (F-ui-fixes 2.1)
# ---------------------------------------------------------------------------


def test_formato_clp_separador_chileno() -> None:
    assert formato_clp(12500000) == "$12.500.000"


def test_formato_clp_cero_y_negativo() -> None:
    assert formato_clp(0) == "$0"
    assert formato_clp(-1500) == "$-1.500"


def test_formato_clp_none_no_deja_hueco() -> None:
    assert formato_clp(None) == "No informado"


def test_formato_clp_diez_digitos() -> None:
    assert formato_clp(1234567890) == "$1.234.567.890"


def test_formato_clp_redondea_decimales() -> None:
    assert formato_clp(1500.6) == "$1.501"


def test_formato_numero_sin_simbolo() -> None:
    assert formato_numero(0) == "0"
    assert formato_numero(-2500) == "-2.500"
    assert formato_numero(1234567890) == "1.234.567.890"


def test_formato_numero_none() -> None:
    assert formato_numero(None) == "—"


# ---------------------------------------------------------------------------
# Banda de relevancia (F-ui-fixes 2.2)
# ---------------------------------------------------------------------------


def test_banda_en_los_cortes_exactos() -> None:
    assert banda_relevancia(60, 60, 40) == "alta"
    assert banda_relevancia(40, 60, 40) == "media"


def test_banda_justo_debajo_y_justo_encima() -> None:
    assert banda_relevancia(59.9, 60, 40) == "media"
    assert banda_relevancia(60.1, 60, 40) == "alta"
    assert banda_relevancia(39.9, 60, 40) == "baja"
    assert banda_relevancia(40.1, 60, 40) == "media"


def test_banda_extremos() -> None:
    assert banda_relevancia(0, 60, 40) == "baja"
    assert banda_relevancia(100, 60, 40) == "alta"


def test_banda_65_es_alta_como_el_preset() -> None:
    """El caso que motivó el cambio: 65 pasaba el preset "Alta" y salía ámbar."""
    assert banda_relevancia(65, 60, 40) == "alta"
