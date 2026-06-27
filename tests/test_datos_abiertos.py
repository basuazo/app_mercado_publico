"""Tests F-rubros — cliente e ingesta de datos abiertos de ChileCompra."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clients.datos_abiertos import head_last_modified, stream_items, url_lic_da
from app.core.settings import Settings
from app.ingest.datos_abiertos import sync_items_datos_abiertos
from app.models.enums import EstadoOportunidad
from app.models.tables import Licitacion, LicitacionItem, SyncState

_VALID_ENV = {
    "MP_TICKET": "ticket-test-rubros",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-rubros-32bytesxxxxxxxx",
    "JOBS_TOKEN": "token-test-rubros-jobs-xxxxxxxxx",
}


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture()
def engine():
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


def _build_zip_bytes(nombre_csv: str, csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(nombre_csv, csv_text.encode("latin-1"))
    return buf.getvalue()


_HEADER = '"CodigoExterno";"Codigoitem";"CodigoProductoONU";"Nombre producto genrico";"UnidadMedida";"Cantidad"\r\n'


def _fila(codigo_externo: str, codigo_item: str, codigo_producto: str, nombre: str, unidad: str, cantidad: str) -> str:
    return f'"{codigo_externo}";"{codigo_item}";"{codigo_producto}";"{nombre}";"{unidad}";"{cantidad}"\r\n'


# ---------------------------------------------------------------------------
# Cliente: url_lic_da
# ---------------------------------------------------------------------------


def test_url_lic_da_mes_sin_cero():
    assert url_lic_da(2026, 5) == "https://transparenciachc.blob.core.windows.net/lic-da/2026-5.zip"


def test_url_lic_da_base_url_personalizada():
    assert url_lic_da(2026, 5, "https://fake.test/") == "https://fake.test/lic-da/2026-5.zip"


# ---------------------------------------------------------------------------
# Cliente: head_last_modified
# ---------------------------------------------------------------------------


@respx.mock
def test_head_last_modified_ok():
    url = "https://fake.test/lic-da/2026-5.zip"
    respx.head(url).mock(
        return_value=httpx.Response(200, headers={"last-modified": "Wed, 01 Apr 2026 00:00:00 GMT"})
    )
    dt = head_last_modified(url)
    assert dt is not None
    assert dt.isoformat() == "2026-04-01T00:00:00"


@respx.mock
def test_head_last_modified_404():
    url = "https://fake.test/lic-da/2099-1.zip"
    respx.head(url).mock(return_value=httpx.Response(404))
    assert head_last_modified(url) is None


@respx.mock
def test_head_last_modified_sin_header():
    url = "https://fake.test/lic-da/2026-5.zip"
    respx.head(url).mock(return_value=httpx.Response(200))
    assert head_last_modified(url) is None


# ---------------------------------------------------------------------------
# Cliente: stream_items
# ---------------------------------------------------------------------------


class TestStreamItems:
    def test_decodifica_latin1_y_campo_multilinea(self, tmp_path):
        """Confirma encoding Latin-1 (no UTF-8) y campos con salto de línea embebido."""
        csv_text = (
            _HEADER
            + '"LIC-A";"ITEM-1";"72102304";"MANTENIMIENTO O\nREPARACI\xd3N DE GASFITER\xcdA";"Unidad";"1"\r\n'
        )
        zip_path = tmp_path / "lic_2026-6.zip"
        zip_path.write_bytes(_build_zip_bytes("lic_2026-6.csv", csv_text))

        items = list(stream_items(str(zip_path)))
        assert len(items) == 1
        assert items[0].codigo_externo == "LIC-A"
        assert items[0].nombre == "MANTENIMIENTO O\nREPARACIÓN DE GASFITERÍA"

    def test_cantidad_formatos_y_invalida(self, tmp_path):
        csv_text = (
            _HEADER
            + _fila("LIC-A", "ITEM-1", "72102304", "Prod A", "Unidad", "1")
            + _fila("LIC-B", "ITEM-2", "102101001", "Consultoria", "Unidad", "2,5")
            + _fila("LIC-C", "ITEM-3", "99999999", "Prod C", "Unidad", "abc")
        )
        zip_path = tmp_path / "lic.zip"
        zip_path.write_bytes(_build_zip_bytes("lic.csv", csv_text))

        items = {i.codigo_externo: i for i in stream_items(str(zip_path))}
        assert items["LIC-A"].cantidad == 1.0
        assert items["LIC-B"].cantidad == 2.5
        assert items["LIC-C"].cantidad is None

    def test_codigo_producto_9_digitos_se_preserva_tal_cual(self, tmp_path):
        """No es UNSPSC estándar (8 díg) pero stream_items no lo descarta ni lo trunca."""
        csv_text = _HEADER + _fila("LIC-B", "ITEM-2", "102101001", "Consultoria", "Unidad", "1")
        zip_path = tmp_path / "lic.zip"
        zip_path.write_bytes(_build_zip_bytes("lic.csv", csv_text))

        items = list(stream_items(str(zip_path)))
        assert items[0].codigo_producto == "102101001"

    def test_no_deduplica_filas_por_oferta(self, tmp_path):
        """stream_items entrega cada fila tal cual; el dedup por ítem es responsabilidad del caller."""
        csv_text = _HEADER + (_fila("LIC-A", "ITEM-1", "72102304", "Prod A", "Unidad", "1") * 2)
        zip_path = tmp_path / "lic.zip"
        zip_path.write_bytes(_build_zip_bytes("lic.csv", csv_text))

        items = list(stream_items(str(zip_path)))
        assert len(items) == 2

    def test_fila_sin_codigo_externo_se_descarta(self, tmp_path):
        csv_text = _HEADER + _fila("", "ITEM-1", "72102304", "Prod A", "Unidad", "1")
        zip_path = tmp_path / "lic.zip"
        zip_path.write_bytes(_build_zip_bytes("lic.csv", csv_text))

        assert list(stream_items(str(zip_path))) == []


# ---------------------------------------------------------------------------
# Ingesta: sync_items_datos_abiertos
# ---------------------------------------------------------------------------


def _licitacion(session, codigo: str, estado: str) -> Licitacion:
    lic = Licitacion(codigo=codigo, nombre=f"Lic {codigo}", estado=estado)
    session.add(lic)
    return lic


class TestSyncItemsDatosAbiertos:
    def test_solo_puebla_licitaciones_activas_sin_items(self, session, settings):
        """LIC-A (activa, sin items) se puebla; LIC-B (activa, ya con items) y LIC-C
        (cerrada) quedan intactas aunque el CSV traiga filas para las tres."""
        _licitacion(session, "LIC-A", EstadoOportunidad.PUBLICADA.value)
        _licitacion(session, "LIC-B", EstadoOportunidad.PUBLICADA.value)
        _licitacion(session, "LIC-C", EstadoOportunidad.CERRADA.value)
        session.add(
            LicitacionItem(
                licitacion_codigo="LIC-B", codigo_producto="11111111", nombre="ya existente", unidad="UN"
            )
        )
        session.commit()

        csv_text = (
            _HEADER
            + _fila("LIC-A", "ITEM-1", "72102304", "Prod A", "Unidad", "1")
            + _fila("LIC-B", "ITEM-2", "22222222", "No debería entrar", "Unidad", "1")
            + _fila("LIC-C", "ITEM-3", "33333333", "No debería entrar", "Unidad", "1")
            + _fila("LIC-X", "ITEM-4", "44444444", "Licitacion inexistente en BD", "Unidad", "1")
        )
        url = url_lic_da(2026, 6, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.head(url).mock(
                return_value=httpx.Response(200, headers={"last-modified": "Mon, 01 Jun 2026 00:00:00 GMT"})
            )
            respx.get(url).mock(return_value=httpx.Response(200, content=_build_zip_bytes("lic.csv", csv_text)))
            result = sync_items_datos_abiertos(session, settings, anio=2026, mes=6)

        assert result == {
            "licitaciones_tocadas": 1,
            "items_insertados": 1,
            "no_unspsc": 0,
            "descargado": 1,
        }

        items_a = session.execute(
            select(LicitacionItem).where(LicitacionItem.licitacion_codigo == "LIC-A")
        ).scalars().all()
        assert len(items_a) == 1
        assert items_a[0].codigo_producto == "72102304"

        items_b = session.execute(
            select(LicitacionItem).where(LicitacionItem.licitacion_codigo == "LIC-B")
        ).scalars().all()
        assert len(items_b) == 1
        assert items_b[0].codigo_producto == "11111111"  # intacto, no pisado por el CSV

        items_c = session.execute(
            select(LicitacionItem).where(LicitacionItem.licitacion_codigo == "LIC-C")
        ).scalars().all()
        assert items_c == []

    def test_dedup_por_codigo_externo_e_item(self, session, settings):
        """Dos filas (misma licitación, mismo ítem, dos ofertas) -> 1 solo LicitacionItem."""
        _licitacion(session, "LIC-A", EstadoOportunidad.PUBLICADA.value)
        session.commit()

        csv_text = _HEADER + (_fila("LIC-A", "ITEM-1", "72102304", "Prod A", "Unidad", "1") * 2)
        url = url_lic_da(2026, 6, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.head(url).mock(
                return_value=httpx.Response(200, headers={"last-modified": "Mon, 01 Jun 2026 00:00:00 GMT"})
            )
            respx.get(url).mock(return_value=httpx.Response(200, content=_build_zip_bytes("lic.csv", csv_text)))
            result = sync_items_datos_abiertos(session, settings, anio=2026, mes=6)

        assert result["items_insertados"] == 1
        items = session.execute(select(LicitacionItem)).scalars().all()
        assert len(items) == 1

    def test_codigo_no_unspsc_se_inserta_y_se_cuenta(self, session, settings):
        """Código de 9 díg (CONSULTORIA) se inserta igual, pero se cuenta como no_unspsc."""
        _licitacion(session, "LIC-A", EstadoOportunidad.PUBLICADA.value)
        session.commit()

        csv_text = _HEADER + _fila("LIC-A", "ITEM-1", "102101001", "Consultoria", "Unidad", "1")
        url = url_lic_da(2026, 6, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.head(url).mock(
                return_value=httpx.Response(200, headers={"last-modified": "Mon, 01 Jun 2026 00:00:00 GMT"})
            )
            respx.get(url).mock(return_value=httpx.Response(200, content=_build_zip_bytes("lic.csv", csv_text)))
            result = sync_items_datos_abiertos(session, settings, anio=2026, mes=6)

        assert result["items_insertados"] == 1
        assert result["no_unspsc"] == 1
        item = session.execute(select(LicitacionItem)).scalars().one()
        assert item.codigo_producto == "102101001"

    def test_segunda_corrida_no_duplica(self, session, settings):
        """Tras la 1ra corrida LIC-A ya tiene ítems, así que sale del set objetivo:
        una 2da corrida (con el blob 'cambiado') no inserta nada más para ella."""
        _licitacion(session, "LIC-A", EstadoOportunidad.PUBLICADA.value)
        session.commit()

        csv_text = _HEADER + _fila("LIC-A", "ITEM-1", "72102304", "Prod A", "Unidad", "1")
        url = url_lic_da(2026, 6, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.head(url).mock(
                side_effect=[
                    httpx.Response(200, headers={"last-modified": "Mon, 01 Jun 2026 00:00:00 GMT"}),
                    httpx.Response(200, headers={"last-modified": "Tue, 02 Jun 2026 00:00:00 GMT"}),
                ]
            )
            respx.get(url).mock(return_value=httpx.Response(200, content=_build_zip_bytes("lic.csv", csv_text)))

            r1 = sync_items_datos_abiertos(session, settings, anio=2026, mes=6)
            r2 = sync_items_datos_abiertos(session, settings, anio=2026, mes=6)

        assert r1["items_insertados"] == 1
        assert r2["licitaciones_tocadas"] == 0
        assert r2["items_insertados"] == 0

        items = session.execute(select(LicitacionItem)).scalars().all()
        assert len(items) == 1

    def test_cursor_sin_cambios_no_descarga(self, session, settings):
        """Si Last-Modified == cursor guardado, ni siquiera intenta el GET del ZIP."""
        _licitacion(session, "LIC-A", EstadoOportunidad.PUBLICADA.value)
        session.add(SyncState(fuente="datos_abiertos_lic", cursor="2026-04-01T00:00:00"))
        session.commit()

        url = url_lic_da(2026, 4, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.head(url).mock(
                return_value=httpx.Response(200, headers={"last-modified": "Wed, 01 Apr 2026 00:00:00 GMT"})
            )
            # No se registra ruta GET: si el código intentara descargar, respx lanzaría
            # un error de "ruta no mockeada" y el test fallaría.
            result = sync_items_datos_abiertos(session, settings, anio=2026, mes=4)

        assert result == {
            "licitaciones_tocadas": 0,
            "items_insertados": 0,
            "no_unspsc": 0,
            "descargado": 0,
        }
        items = session.execute(select(LicitacionItem)).scalars().all()
        assert items == []

    def test_sin_licitaciones_objetivo_no_descarga(self, session, settings):
        """Si no hay licitaciones activas sin ítems, no tiene sentido bajar el ZIP."""
        _licitacion(session, "LIC-C", EstadoOportunidad.CERRADA.value)
        session.commit()

        url = url_lic_da(2026, 6, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.head(url).mock(
                return_value=httpx.Response(200, headers={"last-modified": "Mon, 01 Jun 2026 00:00:00 GMT"})
            )
            result = sync_items_datos_abiertos(session, settings, anio=2026, mes=6)

        assert result["descargado"] == 0
        state = session.get(SyncState, "datos_abiertos_lic")
        assert state is not None
        assert state.cursor == "2026-06-01T00:00:00"

    def test_deshabilitado_no_hace_nada(self, session, settings, monkeypatch):
        monkeypatch.setenv("DATOS_ABIERTOS_HABILITADO", "false")
        settings_off = Settings(_env_file=None)  # type: ignore[call-arg]

        with respx.mock:
            # Ninguna ruta mockeada: si intentara cualquier request, fallaría.
            result = sync_items_datos_abiertos(session, settings_off, anio=2026, mes=6)

        assert result == {
            "licitaciones_tocadas": 0,
            "items_insertados": 0,
            "no_unspsc": 0,
            "descargado": 0,
        }
