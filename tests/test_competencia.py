"""Tests F-competencia — cliente (stream_ofertas), captura idempotente y queries."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.query import detalle_competencia, resumen_competencia
from app.clients.datos_abiertos import stream_ofertas, url_lic_da
from app.core.settings import Settings
from app.ingest import datos_abiertos as datos_abiertos_mod
from app.ingest.datos_abiertos import capturar_competencia
from app.models.enums import EstadoOportunidad, RolUsuario
from app.models.tables import Licitacion, OfertaCompetencia, OportunidadSeguida, Usuario

_VALID_ENV = {
    "MP_TICKET": "ticket-test-competencia",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-competencia-32bytesxx",
    "JOBS_TOKEN": "token-test-competencia-jobs-xxx",
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


_HEADER_OFERTAS = (
    '"CodigoExterno";"Codigoitem";"RutProveedor";"NombreProveedor";'
    '"MontoUnitarioOferta";"MontoLineaAdjudica";"CantidadAdjudicada";"Oferta seleccionada"\r\n'
)


def _fila_oferta(
    codigo_externo: str,
    codigo_item: str,
    rut: str,
    nombre: str,
    monto_unitario: str,
    monto_linea: str,
    cantidad: str,
    seleccionada: bool,
) -> str:
    sel = "Seleccionada" if seleccionada else "No Seleccionada"
    return (
        f'"{codigo_externo}";"{codigo_item}";"{rut}";"{nombre}";'
        f'"{monto_unitario}";"{monto_linea}";"{cantidad}";"{sel}"\r\n'
    )


# ---------------------------------------------------------------------------
# Cliente: stream_ofertas
# ---------------------------------------------------------------------------


class TestStreamOfertas:
    def test_filtra_por_codigo_externo(self, tmp_path):
        csv_text = (
            _HEADER_OFERTAS
            + _fila_oferta("LIC-A", "ITEM-1", "1-9", "Prov A", "100", "0", "0", False)
            + _fila_oferta("LIC-B", "ITEM-1", "2-7", "Prov B", "200", "200", "1", True)
        )
        zip_path = tmp_path / "lic.zip"
        zip_path.write_bytes(_build_zip_bytes("lic.csv", csv_text))

        ofertas = list(stream_ofertas(str(zip_path), "LIC-B"))
        assert len(ofertas) == 1
        assert ofertas[0].rut_proveedor == "2-7"
        assert ofertas[0].seleccionada is True

    def test_ganador_y_no_ganador(self, tmp_path):
        """Caso real de docs/05-competencia.md §2: 2 oferentes, 1 gana."""
        csv_text = (
            _HEADER_OFERTAS
            + _fila_oferta("LIC-A", "ITEM-1", "1-9", "TRANSPORTE OBA", "5700000", "0", "0", False)
            + _fila_oferta("LIC-A", "ITEM-1", "2-7", "Servicio Tres", "5500000", "5500000", "1", True)
        )
        zip_path = tmp_path / "lic.zip"
        zip_path.write_bytes(_build_zip_bytes("lic.csv", csv_text))

        ofertas = {o.rut_proveedor: o for o in stream_ofertas(str(zip_path), "LIC-A")}
        assert ofertas["1-9"].seleccionada is False
        assert ofertas["1-9"].monto_linea_adjudicada == 0.0
        assert ofertas["2-7"].seleccionada is True
        assert ofertas["2-7"].monto_linea_adjudicada == 5500000.0

    def test_notacion_cientifica(self, tmp_path):
        csv_text = _HEADER_OFERTAS + _fila_oferta(
            "LIC-A", "ITEM-1", "1-9", "Prov", "5e+07", "5e+07", "1", True
        )
        zip_path = tmp_path / "lic.zip"
        zip_path.write_bytes(_build_zip_bytes("lic.csv", csv_text))

        oferta = next(stream_ofertas(str(zip_path), "LIC-A"))
        assert oferta.monto_unitario == 5e7
        assert oferta.monto_linea_adjudicada == 5e7

    def test_coma_decimal_y_notacion_cientifica(self, tmp_path):
        csv_text = _HEADER_OFERTAS + _fila_oferta(
            "LIC-A", "ITEM-1", "1-9", "Prov", "9,9e+07", "9,9e+07", "1", True
        )
        zip_path = tmp_path / "lic.zip"
        zip_path.write_bytes(_build_zip_bytes("lic.csv", csv_text))

        oferta = next(stream_ofertas(str(zip_path), "LIC-A"))
        assert oferta.monto_unitario == 99000000.0
        assert oferta.monto_linea_adjudicada == 99000000.0

    def test_monto_invalido_no_rompe(self, tmp_path):
        csv_text = _HEADER_OFERTAS + _fila_oferta(
            "LIC-A", "ITEM-1", "1-9", "Prov", "n/a", "n/a", "1", False
        )
        zip_path = tmp_path / "lic.zip"
        zip_path.write_bytes(_build_zip_bytes("lic.csv", csv_text))

        oferta = next(stream_ofertas(str(zip_path), "LIC-A"))
        assert oferta.monto_unitario is None
        assert oferta.monto_linea_adjudicada is None

    def test_multiples_proveedores_mismo_item(self, tmp_path):
        csv_text = (
            _HEADER_OFERTAS
            + _fila_oferta("LIC-A", "ITEM-1", "1-1", "Prov 1", "10", "0", "0", False)
            + _fila_oferta("LIC-A", "ITEM-1", "2-2", "Prov 2", "20", "0", "0", False)
            + _fila_oferta("LIC-A", "ITEM-1", "3-3", "Prov 3", "5", "5", "1", True)
        )
        zip_path = tmp_path / "lic.zip"
        zip_path.write_bytes(_build_zip_bytes("lic.csv", csv_text))

        ofertas = list(stream_ofertas(str(zip_path), "LIC-A"))
        assert len(ofertas) == 3
        assert sum(1 for o in ofertas if o.seleccionada) == 1


# ---------------------------------------------------------------------------
# Ingesta: capturar_competencia
# ---------------------------------------------------------------------------


def _setup_seguida_adjudicada(
    session: Session, codigo: str = "LIC-A", fecha_publicacion: datetime | None = None
) -> Usuario:
    lic = Licitacion(
        codigo=codigo,
        nombre=f"Lic {codigo}",
        estado=EstadoOportunidad.ADJUDICADA.value,
        fecha_publicacion=fecha_publicacion,
    )
    session.add(lic)
    owner = Usuario(
        email=f"{codigo.lower()}@test.cl", password_hash="x", rol=RolUsuario.USUARIO, activo=True
    )
    session.add(owner)
    session.flush()
    session.add(
        OportunidadSeguida(
            owner_id=owner.id,
            fuente="licitaciones",
            codigo_oportunidad=codigo,
            estado_visto=EstadoOportunidad.ADJUDICADA.value,
            archivada=False,
        )
    )
    session.commit()
    return owner


class TestCapturarCompetencia:
    def test_captura_basica_dedup_por_item_y_proveedor(self, session, settings):
        _setup_seguida_adjudicada(session, "LIC-A", fecha_publicacion=datetime(2026, 5, 10))

        csv_text = (
            _HEADER_OFERTAS
            + _fila_oferta("LIC-A", "ITEM-1", "1-9", "Prov A", "5700000", "0", "0", False)
            + _fila_oferta("LIC-A", "ITEM-1", "2-7", "Prov B", "5500000", "5500000", "1", True)
            + _fila_oferta("LIC-A", "ITEM-1", "2-7", "Prov B", "5500000", "5500000", "1", True)
        )
        url = url_lic_da(2026, 5, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.get(url).mock(return_value=httpx.Response(200, content=_build_zip_bytes("c.csv", csv_text)))
            result = capturar_competencia(session, settings)

        assert result == {
            "licitaciones_tocadas": 1,
            "ofertas_insertadas": 2,
            "sin_encontrar": 0,
            "descargados": 1,
        }
        ofertas = session.execute(select(OfertaCompetencia)).scalars().all()
        assert len(ofertas) == 2

    def test_segunda_corrida_no_reprocesa(self, session, settings):
        _setup_seguida_adjudicada(session, "LIC-A", fecha_publicacion=datetime(2026, 5, 10))
        csv_text = _HEADER_OFERTAS + _fila_oferta(
            "LIC-A", "ITEM-1", "1-9", "Prov A", "100", "100", "1", True
        )
        url = url_lic_da(2026, 5, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.get(url).mock(return_value=httpx.Response(200, content=_build_zip_bytes("c.csv", csv_text)))
            r1 = capturar_competencia(session, settings)
            # Sin mock adicional: si reintentara descargar, respx fallaría.
            r2 = capturar_competencia(session, settings)

        assert r1["ofertas_insertadas"] == 1
        assert r2 == {
            "licitaciones_tocadas": 0,
            "ofertas_insertadas": 0,
            "sin_encontrar": 0,
            "descargados": 0,
        }

    def test_selectiva_solo_seguidas_no_archivadas_adjudicadas(self, session, settings):
        _setup_seguida_adjudicada(session, "LIC-A", fecha_publicacion=datetime(2026, 5, 1))

        owner_b = Usuario(email="b@test.cl", password_hash="x", rol=RolUsuario.USUARIO, activo=True)
        session.add(owner_b)
        session.flush()
        session.add(
            Licitacion(
                codigo="LIC-B",
                nombre="Lic B",
                estado=EstadoOportunidad.ADJUDICADA.value,
                fecha_publicacion=datetime(2026, 5, 1),
            )
        )
        session.add(
            OportunidadSeguida(
                owner_id=owner_b.id,
                fuente="licitaciones",
                codigo_oportunidad="LIC-B",
                estado_visto=EstadoOportunidad.ADJUDICADA.value,
                archivada=True,  # archivada -> no es objetivo
            )
        )

        owner_c = Usuario(email="c@test.cl", password_hash="x", rol=RolUsuario.USUARIO, activo=True)
        session.add(owner_c)
        session.flush()
        session.add(Licitacion(codigo="LIC-C", nombre="Lic C", estado=EstadoOportunidad.PUBLICADA.value))
        session.add(
            OportunidadSeguida(
                owner_id=owner_c.id,
                fuente="licitaciones",
                codigo_oportunidad="LIC-C",
                estado_visto=EstadoOportunidad.PUBLICADA.value,
                archivada=False,  # seguida pero no adjudicada -> no es objetivo
            )
        )
        session.commit()

        csv_text = _HEADER_OFERTAS + _fila_oferta(
            "LIC-A", "ITEM-1", "1-9", "Prov A", "100", "100", "1", True
        )
        url = url_lic_da(2026, 5, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.get(url).mock(return_value=httpx.Response(200, content=_build_zip_bytes("c.csv", csv_text)))
            result = capturar_competencia(session, settings)

        assert result["licitaciones_tocadas"] == 1
        codigos = {o.licitacion_codigo for o in session.execute(select(OfertaCompetencia)).scalars()}
        assert codigos == {"LIC-A"}

    def test_fallback_escanea_meses_cuando_fecha_publicacion_es_null(self, session, settings, monkeypatch):
        """Caso real (docs/05-competencia.md §0): fecha_publicacion NULL en adjudicadas."""
        monkeypatch.setattr(datos_abiertos_mod, "_mes_actual_chile", lambda: (2026, 6))
        _setup_seguida_adjudicada(session, "LIC-A", fecha_publicacion=None)

        csv_vacio = _HEADER_OFERTAS
        csv_con_lic = _HEADER_OFERTAS + _fila_oferta(
            "LIC-A", "ITEM-1", "1-9", "Prov A", "100", "100", "1", True
        )
        url_jun = url_lic_da(2026, 6, settings.datos_abiertos_base_url)
        url_may = url_lic_da(2026, 5, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.get(url_jun).mock(return_value=httpx.Response(200, content=_build_zip_bytes("c.csv", csv_vacio)))
            respx.get(url_may).mock(
                return_value=httpx.Response(200, content=_build_zip_bytes("c.csv", csv_con_lic))
            )
            result = capturar_competencia(session, settings)

        assert result["licitaciones_tocadas"] == 1
        assert result["ofertas_insertadas"] == 1
        assert result["descargados"] == 2  # intentó junio (vacío) y mayo (encontrada)

    def test_no_encontrada_en_ningun_mes_no_rompe(self, session, settings, monkeypatch):
        monkeypatch.setattr(datos_abiertos_mod, "_mes_actual_chile", lambda: (2026, 6))
        _setup_seguida_adjudicada(session, "LIC-Z", fecha_publicacion=None)

        csv_vacio = _HEADER_OFERTAS
        with respx.mock:
            for anio, mes in [(2026, 6), (2026, 5), (2026, 4), (2026, 3)]:
                url = url_lic_da(anio, mes, settings.datos_abiertos_base_url)
                respx.get(url).mock(return_value=httpx.Response(200, content=_build_zip_bytes("c.csv", csv_vacio)))
            result = capturar_competencia(session, settings)

        assert result == {
            "licitaciones_tocadas": 0,
            "ofertas_insertadas": 0,
            "sin_encontrar": 1,
            "descargados": 4,
        }
        assert session.execute(select(OfertaCompetencia)).scalars().all() == []

    def test_descarga_fallida_continua_con_siguiente_mes(self, session, settings, monkeypatch):
        """Regla 6: un error de red/descarga en un mes no rompe la captura completa."""
        monkeypatch.setattr(datos_abiertos_mod, "_mes_actual_chile", lambda: (2026, 6))
        _setup_seguida_adjudicada(session, "LIC-A", fecha_publicacion=None)

        csv_con_lic = _HEADER_OFERTAS + _fila_oferta(
            "LIC-A", "ITEM-1", "1-9", "Prov A", "100", "100", "1", True
        )
        url_jun = url_lic_da(2026, 6, settings.datos_abiertos_base_url)
        url_may = url_lic_da(2026, 5, settings.datos_abiertos_base_url)
        with respx.mock:
            respx.get(url_jun).mock(return_value=httpx.Response(500))
            respx.get(url_may).mock(
                return_value=httpx.Response(200, content=_build_zip_bytes("c.csv", csv_con_lic))
            )
            result = capturar_competencia(session, settings)

        assert result["licitaciones_tocadas"] == 1
        assert result["sin_encontrar"] == 0

    def test_sin_objetivo_no_hace_nada(self, session, settings):
        with respx.mock:
            # Ninguna ruta mockeada: si intentara cualquier request, fallaría.
            result = capturar_competencia(session, settings)
        assert result == {
            "licitaciones_tocadas": 0,
            "ofertas_insertadas": 0,
            "sin_encontrar": 0,
            "descargados": 0,
        }

    def test_deshabilitado_no_hace_nada(self, session, settings, monkeypatch):
        monkeypatch.setenv("DATOS_ABIERTOS_HABILITADO", "false")
        settings_off = Settings(_env_file=None)  # type: ignore[call-arg]
        _setup_seguida_adjudicada(session, "LIC-A", fecha_publicacion=datetime(2026, 5, 1))

        with respx.mock:
            result = capturar_competencia(session, settings_off)

        assert result == {
            "licitaciones_tocadas": 0,
            "ofertas_insertadas": 0,
            "sin_encontrar": 0,
            "descargados": 0,
        }


# ---------------------------------------------------------------------------
# Queries: resumen_competencia / detalle_competencia
# ---------------------------------------------------------------------------


class TestResumenCompetencia:
    def test_totales_y_ganador_por_proveedor(self, session):
        session.add_all(
            [
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-1",
                    rut_proveedor="1-9",
                    nombre_proveedor="Prov A",
                    monto_unitario=5700000,
                    monto_linea_adjudicada=0,
                    cantidad=0,
                    seleccionada=False,
                ),
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-1",
                    rut_proveedor="2-7",
                    nombre_proveedor="Prov B",
                    monto_unitario=5500000,
                    monto_linea_adjudicada=5500000,
                    cantidad=1,
                    seleccionada=True,
                ),
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-2",
                    rut_proveedor="2-7",
                    nombre_proveedor="Prov B",
                    monto_unitario=1000000,
                    monto_linea_adjudicada=1000000,
                    cantidad=1,
                    seleccionada=True,
                ),
            ]
        )
        session.commit()

        resumen = resumen_competencia(session, "LIC-A")
        # incluye también al no-ganador (Prov A): panorama competitivo completo
        assert len(resumen) == 2
        assert resumen[0]["rut_proveedor"] == "2-7"
        assert resumen[0]["items_ofertados"] == 2
        assert resumen[0]["items_ganados"] == 2
        assert resumen[0]["total_adjudicado"] == 6500000.0
        assert resumen[1]["rut_proveedor"] == "1-9"
        assert resumen[1]["items_ofertados"] == 1
        assert resumen[1]["items_ganados"] == 0
        assert resumen[1]["total_adjudicado"] == 0.0

    def test_incluye_no_ganadores(self, session):
        """Proveedor que ofertó pero no ganó ningún ítem: items_ofertados>0,
        items_ganados=0, total_adjudicado=0 — no desaparece del resumen."""
        session.add_all(
            [
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-1",
                    rut_proveedor="PERDEDOR",
                    nombre_proveedor="Perdedor",
                    monto_linea_adjudicada=0,
                    seleccionada=False,
                ),
            ]
        )
        session.commit()

        resumen = resumen_competencia(session, "LIC-A")
        assert len(resumen) == 1
        assert resumen[0]["rut_proveedor"] == "PERDEDOR"
        assert resumen[0]["items_ofertados"] == 1
        assert resumen[0]["items_ganados"] == 0
        assert resumen[0]["total_adjudicado"] == 0.0

    def test_orden_descendente_por_total(self, session):
        session.add_all(
            [
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-1",
                    rut_proveedor="A",
                    nombre_proveedor="A",
                    monto_linea_adjudicada=100,
                    seleccionada=True,
                ),
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-2",
                    rut_proveedor="B",
                    nombre_proveedor="B",
                    monto_linea_adjudicada=900,
                    seleccionada=True,
                ),
            ]
        )
        session.commit()

        resumen = resumen_competencia(session, "LIC-A")
        assert [r["rut_proveedor"] for r in resumen] == ["B", "A"]

    def test_ganadores_antes_que_no_ganadores_aunque_no_ganador_oferte_mas(self, session):
        """Ganadores siempre primero, sin importar items_ofertados del no-ganador."""
        session.add_all(
            [
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-1",
                    rut_proveedor="GANADOR",
                    nombre_proveedor="Ganador",
                    monto_linea_adjudicada=100,
                    seleccionada=True,
                ),
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-2",
                    rut_proveedor="PERDEDOR",
                    nombre_proveedor="Perdedor",
                    monto_linea_adjudicada=0,
                    seleccionada=False,
                ),
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-3",
                    rut_proveedor="PERDEDOR",
                    nombre_proveedor="Perdedor",
                    monto_linea_adjudicada=0,
                    seleccionada=False,
                ),
            ]
        )
        session.commit()

        resumen = resumen_competencia(session, "LIC-A")
        assert [r["rut_proveedor"] for r in resumen] == ["GANADOR", "PERDEDOR"]

    def test_no_ganadores_ordenados_por_items_ofertados_desc(self, session):
        session.add_all(
            [
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-1",
                    rut_proveedor="MENOS",
                    nombre_proveedor="Menos",
                    seleccionada=False,
                ),
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-2",
                    rut_proveedor="MAS",
                    nombre_proveedor="Mas",
                    seleccionada=False,
                ),
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-3",
                    rut_proveedor="MAS",
                    nombre_proveedor="Mas",
                    seleccionada=False,
                ),
            ]
        )
        session.commit()

        resumen = resumen_competencia(session, "LIC-A")
        assert [r["rut_proveedor"] for r in resumen] == ["MAS", "MENOS"]

    def test_sin_ofertas_retorna_vacio(self, session):
        assert resumen_competencia(session, "LIC-NOPE") == []


class TestDetalleCompetencia:
    def test_incluye_seleccionadas_y_no_seleccionadas(self, session):
        session.add_all(
            [
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-1",
                    rut_proveedor="1-9",
                    nombre_proveedor="Prov A",
                    monto_unitario=100,
                    seleccionada=False,
                ),
                OfertaCompetencia(
                    licitacion_codigo="LIC-A",
                    codigo_item="ITEM-1",
                    rut_proveedor="2-7",
                    nombre_proveedor="Prov B",
                    monto_unitario=90,
                    monto_linea_adjudicada=90,
                    seleccionada=True,
                ),
            ]
        )
        session.commit()

        detalle = detalle_competencia(session, "LIC-A")
        assert len(detalle) == 2
        assert any(d["seleccionada"] for d in detalle)
        assert any(not d["seleccionada"] for d in detalle)
