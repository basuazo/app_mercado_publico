"""Tests F9b — catálogo UNSPSC (app.catalogos.unspsc)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.catalogos import unspsc


@pytest.fixture(autouse=True)
def _reset_cache():
    """El catálogo se cachea con functools.cache; limpiar entre tests."""
    unspsc._datos.cache_clear()
    yield
    unspsc._datos.cache_clear()


def test_segmentos_no_vacio():
    segs = unspsc.segmentos()
    assert len(segs) > 0
    assert all(len(codigo) == 2 for codigo, _ in segs)


def test_familias_no_vacio():
    fams = unspsc.familias()
    assert len(fams) > 0
    assert all(len(codigo) == 4 for codigo, _ in fams)


def test_familia_codigo_pertenece_a_su_segmento():
    """familia.codigo[:2] debe ser un código de segmento existente."""
    segs_codigos = {codigo for codigo, _ in unspsc.segmentos()}
    fams = unspsc.familias()
    assert fams  # hay datos
    for fam_codigo, _ in fams[:20]:
        assert fam_codigo[:2] in segs_codigos


def test_nombre_rubro_match_exacto_segmento():
    seg_codigo, seg_nombre = unspsc.segmentos()[0]
    assert unspsc.nombre_rubro(seg_codigo) == seg_nombre


def test_nombre_rubro_match_exacto_familia():
    fam_codigo, fam_nombre = unspsc.familias()[0]
    assert unspsc.nombre_rubro(fam_codigo) == fam_nombre


def test_nombre_rubro_codigo_8_digitos_resuelve_por_familia():
    """Un código de 8 dígitos (commodity) no está en el catálogo: debe resolver
    por su familia ([:4])."""
    fam_codigo, fam_nombre = unspsc.familias()[0]
    commodity = fam_codigo + "0000"  # 8 dígitos, [:4] == fam_codigo
    assert unspsc.nombre_rubro(commodity) == fam_nombre


def test_nombre_rubro_codigo_8_digitos_sin_familia_resuelve_por_segmento():
    """Si el prefijo de familia ([:4]) no existe, cae al segmento ([:2])."""
    seg_codigo, seg_nombre = unspsc.segmentos()[0]
    # 4 dígitos que casi seguro no son una familia real, pero comparten segmento
    commodity = seg_codigo + "990000"
    assert unspsc.nombre_rubro(commodity) == seg_nombre


def test_nombre_rubro_desconocido_devuelve_none():
    assert unspsc.nombre_rubro("00") is None
    assert unspsc.nombre_rubro("0099") is None


def test_nombre_rubro_vacio_devuelve_none():
    assert unspsc.nombre_rubro("") is None
    assert unspsc.nombre_rubro("   ") is None


def test_archivo_faltante_es_defensivo(monkeypatch):
    """Si el CSV no existe, segmentos()/familias() devuelven listas vacías sin romper."""
    monkeypatch.setattr(unspsc, "_CSV_PATH", Path("/ruta/que/no/existe.csv"))
    unspsc._datos.cache_clear()
    assert unspsc.segmentos() == []
    assert unspsc.familias() == []
    assert unspsc.nombre_rubro("43") is None
