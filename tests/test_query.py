"""Tests de las funciones puras de presentación en app/api/query.py."""

from __future__ import annotations

from app.api.query import _url_ficha, mostrar_ficha_oficial
from app.models.enums import EstadoOportunidad

# ---------------------------------------------------------------------------
# _url_ficha — licitaciones (fix: idlicitacion en vez de qs, ver docs/10-enlace-ficha.md)
# ---------------------------------------------------------------------------


def test_url_ficha_licitacion_usa_idlicitacion() -> None:
    """qs=<codigo> no abre (espera un token encriptado); idlicitacion=<codigo>
    hace que Mercado Público resuelva y redirija al qs correcto — verificado
    en el spike (docs/10-enlace-ficha.md) contra la licitación real 1300-31-LE26."""
    url = _url_ficha("licitaciones", "1300-31-LE26")
    assert url == (
        "https://www.mercadopublico.cl/Procurement/Modules/RFB/"
        "DetailsAcquisition.aspx?idlicitacion=1300-31-LE26"
    )
    assert "qs=" not in url


def test_url_ficha_licitacion_escapa_codigo() -> None:
    """El código va url-encoded (defensivo, aunque los códigos vistos hasta
    ahora solo usan dígitos/letras/guion — ver docs/10-enlace-ficha.md §6)."""
    url = _url_ficha("licitaciones", "1300/31 LE26&x=1")
    assert "idlicitacion=1300%2F31%20LE26%26x%3D1" in url


def test_url_ficha_compra_agil_sin_cambios() -> None:
    """Compra Ágil sigue apuntando al buscador genérico — fuera de alcance
    de este fix (ver docs/10-enlace-ficha.md §5)."""
    url = _url_ficha("compras_agiles", "CA-001")
    assert url == "https://buscador.mercadopublico.cl/compra-agil"


# ---------------------------------------------------------------------------
# mostrar_ficha_oficial — sin cambios, solo procesos abiertos
# ---------------------------------------------------------------------------


def test_mostrar_ficha_oficial_solo_publicada() -> None:
    assert mostrar_ficha_oficial(EstadoOportunidad.PUBLICADA.value) is True
    assert mostrar_ficha_oficial(EstadoOportunidad.CERRADA.value) is False
    assert mostrar_ficha_oficial(EstadoOportunidad.ADJUDICADA.value) is False
    assert mostrar_ficha_oficial(None) is False
