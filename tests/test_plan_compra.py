"""Tests F-plan — cliente y servicio del Plan Anual de Compra (PAC, datos abiertos)."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clients.plan_compra import (
    InstitucionPAC as InstitucionPACDA,
)
from app.clients.plan_compra import (
    descargar_pac,
    listar_instituciones,
    parse_pac_csv,
    url_pac,
)
from app.core.settings import Settings
from app.ingest.plan_compra import get_plan, sync_instituciones_pac
from app.models.enums import EstadoPlanificacionPAC
from app.models.tables import InstitucionPAC, PlanCompraLinea, PlanCompraSync, SyncState

_VALID_ENV = {
    "MP_TICKET": "ticket-test-plan",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-plan-32bytesxxxxxxxxx",
    "JOBS_TOKEN": "token-test-plan-jobs-xxxxxxxxxx",
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


def _build_pac_zip(csv_text: str, nombre_csv: str = "pacorganismos_2026_224060.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(nombre_csv, csv_text.encode("utf-8-sig"))
    return buf.getvalue()


_HEADER = (
    "institucion_nombre;rut_institucion;codigo_producto;descripcion_producto;"
    "cantidad_estimada;monto_unitario_clp;monto_estimado_clp;mes_estimado;"
    "trimestre_estimado;estado_planificacion\n"
)


def _fila(
    inst: str, rut: str, prod: str, desc: str, cant: str, munit: str, mest: str, mes: str, trim: str, estado: str
) -> str:
    return f"{inst};{rut};{prod};{desc};{cant};{munit};{mest};{mes};{trim};{estado}\n"


# ---------------------------------------------------------------------------
# Cliente: url_pac / descargar_pac
# ---------------------------------------------------------------------------


def test_url_pac_patron():
    assert url_pac(2026, 224060, "https://fake.test") == "https://fake.test/2026/pacorganismos_2026_224060.zip"


def test_url_pac_quita_slash_final():
    assert url_pac(2026, 224060, "https://fake.test/") == "https://fake.test/2026/pacorganismos_2026_224060.zip"


@respx.mock
def test_descargar_pac_200_devuelve_bytes():
    url = url_pac(2026, 224060, "https://fake.test")
    respx.get(url).mock(return_value=httpx.Response(200, content=b"contenido-zip"))
    assert descargar_pac(224060, 2026, base_url="https://fake.test") == b"contenido-zip"


@respx.mock
def test_descargar_pac_403_devuelve_none():
    """Institución/año sin plan publicado (ver docs/07-plan-anual.md §5-bis f)."""
    url = url_pac(2024, 7055, "https://fake.test")
    respx.get(url).mock(return_value=httpx.Response(403))
    assert descargar_pac(7055, 2024, base_url="https://fake.test") is None


@respx.mock
def test_descargar_pac_500_propaga_excepcion():
    url = url_pac(2026, 224060, "https://fake.test")
    respx.get(url).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        descargar_pac(224060, 2026, base_url="https://fake.test")


# ---------------------------------------------------------------------------
# Cliente: listar_instituciones
# ---------------------------------------------------------------------------


@respx.mock
def test_listar_instituciones_ok():
    respx.get("https://fake.test/instituciones").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": "OK",
                "trace": None,
                "payload": [
                    {"id": 1, "codigoEntidad": 224060, "rut": "61.935.400-1", "razonSocial": "MINISTERIO  PUBLICO"},
                    {"id": 2, "codigoEntidad": 7383, "rut": "X", "razonSocial": "SERVICIO SALUD OCCIDENTE"},
                ],
                "errores": None,
            },
        )
    )
    out = listar_instituciones(kpi_url="https://fake.test/instituciones")
    assert out == [
        InstitucionPACDA(codigo_entidad=224060, razon_social="MINISTERIO  PUBLICO", rut="61.935.400-1"),
        InstitucionPACDA(codigo_entidad=7383, razon_social="SERVICIO SALUD OCCIDENTE", rut="X"),
    ]


@respx.mock
def test_listar_instituciones_item_sin_codigo_se_descarta():
    respx.get("https://fake.test/instituciones").mock(
        return_value=httpx.Response(
            200,
            json={"payload": [{"id": 1, "razonSocial": "SIN CODIGO"}]},
        )
    )
    assert listar_instituciones(kpi_url="https://fake.test/instituciones") == []


@respx.mock
def test_listar_instituciones_payload_invalido_retorna_vacio():
    respx.get("https://fake.test/instituciones").mock(return_value=httpx.Response(200, json={"payload": "no-es-lista"}))
    assert listar_instituciones(kpi_url="https://fake.test/instituciones") == []


# ---------------------------------------------------------------------------
# Cliente: parse_pac_csv
# ---------------------------------------------------------------------------


class TestParsePacCsv:
    def test_registro_simple(self):
        csv_text = _HEADER + _fila(
            "MINISTERIO PUBLICO", "224060", "1234567", "Compra de sillas", "10", "5000.0", "50000.0", "3", "1", "Publicado"
        )
        lineas = parse_pac_csv(_build_pac_zip(csv_text))
        assert len(lineas) == 1
        linea = lineas[0]
        assert linea.institucion_nombre == "MINISTERIO PUBLICO"
        assert linea.rut_institucion == "224060"
        assert linea.codigo_producto == "1234567"
        assert linea.descripcion_producto == "Compra de sillas"
        assert linea.cantidad_estimada == 10.0
        assert linea.monto_unitario_clp == 5000.0
        assert linea.monto_estimado_clp == 50000.0
        assert linea.mes_estimado == 3
        assert linea.trimestre_estimado == 1
        assert linea.estado_planificacion == "Publicado"

    def test_descripcion_multilinea_sin_comillas_se_reconstruye(self):
        """Gotcha real del PAC (ver §5-bis b): saltos de línea embebidos SIN comillas."""
        descripcion = (
            "Impresion de libro\nTamano: Media carta.\nPortada y contraportada\ncouche.\n"
            "Encuadernacion: tipo corchete"
        )
        csv_text = _HEADER + _fila(
            "MUNICIPALIDAD  DE  HUECHURABA", "123456", "7654321", descripcion, "1", "3000000.0", "3000000.0", "5", "2", "Publicado"
        )
        lineas = parse_pac_csv(_build_pac_zip(csv_text))
        assert len(lineas) == 1
        assert lineas[0].descripcion_producto == descripcion
        assert lineas[0].monto_estimado_clp == 3000000.0

    def test_no_confia_en_contar_9_punto_y_coma(self):
        """Una descripción con ';' embebido (sin salto de línea) no debe cortar el
        registro antes de tiempo: el algoritmo usa la cola plausible, no el conteo."""
        descripcion = "Sillas; mesas; y estantes de oficina"
        csv_text = _HEADER + _fila(
            "INSTITUCION X", "999", "111", descripcion, "5", "1000.0", "5000.0", "6", "2", "Publicado"
        )
        lineas = parse_pac_csv(_build_pac_zip(csv_text))
        assert len(lineas) == 1
        assert lineas[0].descripcion_producto == descripcion

    def test_bom_utf8_se_decodifica_correctamente_no_latin1(self):
        csv_text = _HEADER + _fila(
            "MINISTERIO PUBLICO", "224060", "111", "Mantención de equipos de gasfitería", "1", "100.0", "100.0", "1", "1", "Publicado"
        )
        lineas = parse_pac_csv(_build_pac_zip(csv_text))
        assert lineas[0].descripcion_producto == "Mantención de equipos de gasfitería"

    def test_estado_no_publicado_se_preserva_tal_cual(self):
        """parse_pac_csv entrega el estado crudo; el mapeo a enum (DESCONOCIDO si no
        se reconoce) es responsabilidad del servicio (estado_planificacion_pac)."""
        csv_text = _HEADER + _fila("X", "1", "1", "desc", "1", "1.0", "1.0", "1", "1", "Anulado")
        lineas = parse_pac_csv(_build_pac_zip(csv_text))
        assert lineas[0].estado_planificacion == "Anulado"

    def test_registro_ambiguo_se_descarta_sin_romper_los_demas(self):
        """Un registro que nunca completa una cola plausible (excede el máximo de
        líneas físicas) se descarta vía log, y el registro siguiente se parsea bien."""
        basura = "".join(f"GARBAGE;{i}\n" for i in range(15))
        csv_text = (
            _HEADER
            + basura
            + _fila("OK", "2", "2", "desc ok", "1", "1.0", "1.0", "1", "1", "Publicado")
        )
        lineas = parse_pac_csv(_build_pac_zip(csv_text))
        assert len(lineas) == 1
        assert lineas[0].institucion_nombre == "OK"

    def test_sin_csv_en_zip_retorna_vacio(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("nota.txt", b"no es un csv")
        assert parse_pac_csv(buf.getvalue()) == []

    def test_zip_vacio_de_datos_retorna_vacio(self):
        lineas = parse_pac_csv(_build_pac_zip(_HEADER))
        assert lineas == []


# ---------------------------------------------------------------------------
# Servicio: get_plan
# ---------------------------------------------------------------------------


def _csv_minimo() -> str:
    return _HEADER + _fila(
        "MINISTERIO PUBLICO", "224060", "1234567", "Compra de sillas", "10", "5000.0", "50000.0", "3", "1", "Publicado"
    )


class TestGetPlan:
    def test_cache_miss_descarga_parsea_y_cachea(self, session, settings):
        url = url_pac(2026, 224060, settings.plan_compra_pac_base_url)
        with respx.mock:
            respx.get(url).mock(return_value=httpx.Response(200, content=_build_pac_zip(_csv_minimo())))
            resultado = get_plan(session, settings, 224060, 2026)

        assert resultado.estado == "ok"
        assert len(resultado.lineas) == 1
        assert resultado.lineas[0].estado_planificacion == EstadoPlanificacionPAC.PUBLICADO.value

        filas = session.execute(select(PlanCompraLinea)).scalars().all()
        assert len(filas) == 1
        sync = session.get(PlanCompraSync, (224060, 2026))
        assert sync is not None
        assert sync.estado == "ok"
        assert sync.n_filas == 1

    def test_cache_hit_no_pega_a_la_red(self, session, settings):
        session.add(
            PlanCompraSync(codigo_entidad=224060, agno=2026, estado="ok", n_filas=1, fetched_at=datetime.now(UTC).replace(tzinfo=None))
        )
        session.add(
            PlanCompraLinea(
                codigo_entidad=224060,
                agno=2026,
                institucion_nombre="MINISTERIO PUBLICO",
                codigo_producto="1",
                descripcion_producto="ya cacheado",
                estado_planificacion=EstadoPlanificacionPAC.PUBLICADO.value,
            )
        )
        session.commit()

        with respx.mock:
            # Ninguna ruta mockeada: si el código intentara descargar, respx fallaría.
            resultado = get_plan(session, settings, 224060, 2026)

        assert resultado.estado == "ok"
        assert len(resultado.lineas) == 1
        assert resultado.lineas[0].descripcion_producto == "ya cacheado"

    def test_403_se_cachea_como_sin_plan(self, session, settings):
        url = url_pac(2024, 7055, settings.plan_compra_pac_base_url)
        with respx.mock:
            respx.get(url).mock(return_value=httpx.Response(403))
            resultado = get_plan(session, settings, 7055, 2024)

        assert resultado.estado == "sin_plan"
        assert resultado.lineas == []
        sync = session.get(PlanCompraSync, (7055, 2024))
        assert sync is not None
        assert sync.estado == "sin_plan"
        assert sync.n_filas == 0

    def test_sin_plan_cacheado_no_pega_a_la_red_de_nuevo(self, session, settings):
        session.add(
            PlanCompraSync(codigo_entidad=7055, agno=2024, estado="sin_plan", n_filas=0, fetched_at=datetime.now(UTC).replace(tzinfo=None))
        )
        session.commit()

        with respx.mock:
            resultado = get_plan(session, settings, 7055, 2024)

        assert resultado.estado == "sin_plan"

    def test_ttl_vencido_vuelve_a_descargar(self, session, settings):
        vencido = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=settings.plan_compra_ttl_dias + 1)
        session.add(PlanCompraSync(codigo_entidad=224060, agno=2026, estado="ok", n_filas=0, fetched_at=vencido))
        session.commit()

        url = url_pac(2026, 224060, settings.plan_compra_pac_base_url)
        with respx.mock:
            respx.get(url).mock(return_value=httpx.Response(200, content=_build_pac_zip(_csv_minimo())))
            resultado = get_plan(session, settings, 224060, 2026)

        assert resultado.estado == "ok"
        assert len(resultado.lineas) == 1

    def test_reconsulta_tras_ttl_no_duplica_filas(self, session, settings):
        """Idempotencia: forzar 2 descargas (TTL=0) no debe duplicar las líneas
        cacheadas — el upsert borra+inserta el par (codigo_entidad, agno)."""
        settings.plan_compra_ttl_dias = 0
        url = url_pac(2026, 224060, settings.plan_compra_pac_base_url)
        with respx.mock:
            respx.get(url).mock(return_value=httpx.Response(200, content=_build_pac_zip(_csv_minimo())))
            get_plan(session, settings, 224060, 2026)
            resultado = get_plan(session, settings, 224060, 2026)

        assert resultado.estado == "ok"
        assert len(resultado.lineas) == 1
        filas = session.execute(select(PlanCompraLinea)).scalars().all()
        assert len(filas) == 1


# ---------------------------------------------------------------------------
# Servicio: sync_instituciones_pac
# ---------------------------------------------------------------------------


class TestSyncInstitucionesPac:
    def test_primera_corrida_descarga_y_cachea(self, session, settings):
        with respx.mock:
            respx.get(settings.plan_compra_kpi_url).mock(
                return_value=httpx.Response(
                    200,
                    json={"payload": [{"codigoEntidad": 224060, "rut": "61.935.400-1", "razonSocial": "MINISTERIO  PUBLICO"}]},
                )
            )
            n = sync_instituciones_pac(session, settings)

        assert n == 1
        instituciones = session.execute(select(InstitucionPAC)).scalars().all()
        assert len(instituciones) == 1
        assert instituciones[0].razon_social == "MINISTERIO  PUBLICO"
        estado = session.get(SyncState, "plan_compra_instituciones")
        assert estado is not None
        assert estado.ultimo_ok is not None

    def test_segunda_corrida_dentro_de_ttl_no_pega_a_la_red(self, session, settings):
        session.add(
            SyncState(
                fuente="plan_compra_instituciones",
                ultima_ejecucion=datetime.now(UTC).replace(tzinfo=None),
                ultimo_ok=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        session.commit()

        with respx.mock:
            n = sync_instituciones_pac(session, settings)

        assert n == 0

    def test_ttl_vencido_vuelve_a_descargar_y_reemplaza_catalogo(self, session, settings):
        vencido = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=settings.plan_compra_ttl_dias + 1)
        session.add(InstitucionPAC(codigo_entidad=1, razon_social="VIEJA", rut=""))
        session.add(SyncState(fuente="plan_compra_instituciones", ultima_ejecucion=vencido, ultimo_ok=vencido))
        session.commit()

        with respx.mock:
            respx.get(settings.plan_compra_kpi_url).mock(
                return_value=httpx.Response(
                    200,
                    json={"payload": [{"codigoEntidad": 2, "rut": "", "razonSocial": "NUEVA"}]},
                )
            )
            n = sync_instituciones_pac(session, settings)

        assert n == 1
        instituciones = session.execute(select(InstitucionPAC)).scalars().all()
        assert len(instituciones) == 1
        assert instituciones[0].razon_social == "NUEVA"
