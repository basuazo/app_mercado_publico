"""Tests F-merc-2: órdenes de compra de Compra Ágil desde datos abiertos (`mercado-oc`).

Fixture SINTÉTICA de `oc-da` (Latin-1, `;`, campo multilínea, UNSPSC de 9 dígitos, región con
espacio al final, `NA`). Red siempre mockeada (respx). SQLite en archivo con el esquema completo.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import io
import itertools
import sys
import zipfile
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.query import resumen_mercado
from app.catalogos.mercado import (
    G_ALIM,
    codigo_oc_normalizado,
    grupo_de,
    moneda_codigo,
    normalizar,
)
from app.catalogos.regiones import region_codigo
from app.clients.datos_abiertos import COLS_OC, iterar_oc_da
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.ingest.mercado_oc import cargar_mes, meses_a_cargar, sync_mercado_oc
from app.mercado.territorio import regiones_activas_engine
from app.models.tables import (
    CaCotizacion,
    CompraAgil,
    OcLineaCa,
    PrecioReferencia,
    SyncState,
)

_BASE = "https://transparenciachc.blob.core.windows.net"
_NUBLE = "Región del Ñuble "  # con espacio al final, como viene en oc-da [V]
_RM = "Región Metropolitana de Santiago"


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine(f"sqlite:///{tmp_path / 'oc.db'}")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MP_TICKET", "ticket-de-test-oc-9876")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SECRET_KEY", "clave-de-test-32bytesxxxxxxxxxx")
    monkeypatch.setenv("JOBS_TOKEN", "token-de-test-jobs-abcdefgh1234")
    return Settings(_env_file=None)  # type: ignore[call-arg]


_IDS = itertools.count(1)


def _linea(**kw: str) -> dict[str, str]:
    base = {c: "" for c in COLS_OC}
    base.update(
        {
            "Codigo": "1000-1-AG26",
            "IDItem": f"I{next(_IDS)}",
            "EsCompraAgil": "Si",
            "FechaCreacion": "2026-03-05 10:11:12",
            "codigoEstado": "6",
            "OrganismoPublico": "Municipalidad de Prueba",
            "sector": "Municipalidades",
            "RutUnidadCompra": "69.000.100-1",
            "UnidadCompra": "Depto Salud",
            "CiudadUnidadCompra": "Chillán",
            "RegionUnidadCompra": _NUBLE,
            "RutSucursal": "76.111.222-3",
            "NombreProveedor": "Proveedor Local SpA",
            "ComunaProveedor": "Chillán",
            "RegionProveedor": _NUBLE,
            "codigoProductoONU": "50131700",
            "NombreroductoGenerico": "Leche",
            "EspecificacionComprador": "Leche entera",
            "cantidad": "10",
            "UnidadMedida": "Litro",
            "precioNeto": "1000",
            "totalLineaNeto": "10000",
            "TipoMonedaOC": "CLP",
        }
    )
    base.update(kw)
    return base


def _filas_mes(mes: str) -> list[dict[str, str]]:
    """Un mes con casos de borde. Los códigos llevan el mes para no chocar entre archivos."""
    s = mes[-2:]
    filas = [
        _linea(Codigo=f"A-{s}", precioNeto="1000", totalLineaNeto="10000"),
        # Misma OC, segunda línea: envase con formato en la especificación (1 L).
        _linea(
            Codigo=f"A-{s}",
            UnidadMedida="Botella",
            EspecificacionComprador="Jugo 1 L",
            precioNeto="900",
            totalLineaNeto="9000",
            NombreroductoGenerico="Jugo",
            codigoProductoONU="50202300",
        ),
        # Campo multilínea, región con espacio al final y proveedor de otra región.
        _linea(
            Codigo=f"B-{s}",
            EspecificacionComprador="Línea 1\nlínea 2",
            RegionProveedor=_RM,
            RutUnidadCompra="69.000.200-2",
            UnidadCompra="Escuela X",
            totalLineaNeto="3000000",
            precioNeto="1500",
        ),
        # Preparado (grupo aparte).
        _linea(
            Codigo=f"C-{s}",
            NombreroductoGenerico="Empanadas de pino",
            UnidadMedida="Unidad",
            precioNeto="800",
            totalLineaNeto="80000",
            RegionProveedor="NA",
        ),
        # No es Compra Ágil.
        _linea(Codigo=f"D-{s}", EsCompraAgil="No"),
        # UNSPSC de 9 dígitos (pseudo-código): fuera de GRUPOS.
        _linea(Codigo=f"E-{s}", codigoProductoONU="123456789"),
        # Región del comprador sin dato.
        _linea(Codigo=f"F-{s}", RegionUnidadCompra="NA"),
        # Compradores de otra región: no se guardan, pero alimentan precio_referencia.
        *[
            _linea(Codigo=f"R{i}-{s}", RegionUnidadCompra=_RM, precioNeto=str(900 + 100 * i))
            for i in range(4)
        ],
        # Otro grupo (aseo) de Ñuble: se guarda, no es alimento.
        _linea(Codigo=f"G-{s}", codigoProductoONU="47131800", NombreroductoGenerico="Cloro"),
    ]
    for i, f in enumerate(filas):
        f["IDItem"] = f"{s}{i:02d}"
    return filas


def _zip(destino: Path, filas: list[dict[str, str]], extra: dict[str, str] | None = None) -> Path:
    cols = list(COLS_OC) + list(extra or {})
    buf = io.StringIO(newline="")
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(cols)
    for f in filas:
        w.writerow([f.get(c, (extra or {}).get(c, "")) for c in cols])
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("oc.csv", buf.getvalue().encode("latin-1"))
    return destino


def _cargar(engine: Engine, ruta: Path, mes: str, regiones: list[int] | None = None) -> dict[str, int]:
    with Session(engine) as s:
        r = cargar_mes(s, str(ruta), mes, regiones or [16])
        s.commit()
    return r


# ---------------------------------------------------------------------------
# Cliente y parseo
# ---------------------------------------------------------------------------
def test_iterar_oc_da_lee_latin1_multilinea_y_columnas_faltantes(tmp_path: Path) -> None:
    filas = [_linea(EspecificacionComprador="a\nb;c")]
    z = _zip(tmp_path / "x.zip", filas, extra={"tieneItems": "1"})
    extra: list[str] = []
    out = list(iterar_oc_da(str(z), extra))
    assert len(out) == 1
    assert out[0]["RegionUnidadCompra"] == _NUBLE
    assert out[0]["EspecificacionComprador"] == "a\nb;c"
    assert extra == ["tieneItems"]
    assert out[0]["IDItem"].startswith("I")
    assert set(out[0]) == set(COLS_OC)  # solo las columnas de la plataforma


@pytest.mark.parametrize(
    ("texto", "codigo"),
    [
        ("Región del Ñuble", 16),
        ("Región del Ñuble ", 16),
        ("REGION DE NUBLE", 16),  # sin tilde ni Ñ
        ("Región Metropolitana de Santiago", 13),
        ("Región del Libertador Gral. Bernardo O'Higgins", 6),
        ("Región del Biobío", 8),
        ("Región de La Araucanía", 9),
        ("Región de Los Ríos", 14),
        ("Región de Los Lagos", 10),
        ("Región de Aysén del Gral. Carlos Ibáñez del Campo", 11),
        ("Región de Magallanes y de la Antártica Chilena", 12),
        ("Región de Arica y Parinacota", 15),
        ("NA", None),
        ("", None),
        (None, None),
        ("Marte", None),
    ],
)
def test_region_codigo(texto: str | None, codigo: int | None) -> None:
    assert region_codigo(texto) == codigo


def test_moneda_codigo() -> None:
    assert moneda_codigo("CLP") == "CLP"
    assert moneda_codigo("Peso Chileno") == "CLP"
    assert moneda_codigo("") == "CLP"
    assert moneda_codigo("UTM") == "UTM"
    assert moneda_codigo("CLF") == "CLF"
    assert moneda_codigo("Unidad de Fomento") == "CLF"


def test_normalizar_unidad_fisica_y_envase() -> None:
    assert normalizar(_linea()) == ("50131700|L", 1000.0)
    nz = normalizar(
        _linea(UnidadMedida="Botella", EspecificacionComprador="Jugo 500 ml", precioNeto="500")
    )
    assert nz is not None and nz[0] == "50131700|L" and nz[1] == pytest.approx(1000.0)
    assert normalizar(_linea(precioNeto="0")) is None
    assert grupo_de("50131700", "Leche") in G_ALIM


# ---------------------------------------------------------------------------
# Carga de un mes
# ---------------------------------------------------------------------------
def test_cargar_mes_filtra_y_normaliza(engine: Engine, tmp_path: Path) -> None:
    z = _zip(tmp_path / "m.zip", _filas_mes("2026-03"))
    r = _cargar(engine, z, "2026-03")
    # No CA, 9 dígitos y NA no entran; RM no se guarda (solo precio_referencia).
    assert r["lineas_guardadas"] == 5
    with Session(engine) as s:
        lineas = list(s.execute(select(OcLineaCa).order_by(OcLineaCa.codigo_oc, OcLineaCa.id_item)).scalars())
        assert [(x.codigo_oc, x.id_item) for x in lineas] == [
            ("A-03", "0300"), ("A-03", "0301"), ("B-03", "0302"), ("C-03", "0303"), ("G-03", "0311"),
        ]  # fmt: skip
        a1, a2, b, c, g = lineas
        assert a1.region_compra == 16 and a1.proveedor_local is True
        assert a1.base == "L" and a1.precio_base == 1000.0 and a1.moneda == "CLP"
        assert a1.rut_unidad == "69000100-1" and a1.rut_proveedor == "76111222-3"
        assert a1.fecha_creacion is not None and a1.fecha_creacion.year == 2026
        assert a2.base == "L" and a2.precio_base == pytest.approx(900.0)
        assert b.proveedor_local is False and b.region_proveedor == 13
        assert b.especificacion == "Línea 1\nlínea 2"
        assert c.grupo != a1.grupo and c.region_proveedor is None and c.proveedor_local is None
        assert g.grupo == grupo_de("47131800", "Cloro")
        # precio_referencia nacional: 50131700|L con Ñuble (2 líneas de leche de A/B) + 4 de RM.
        ref = s.get(PrecioReferencia, ("50131700|L", "2026-03"))
        assert ref is not None and ref.n >= 4
        assert ref.p25 <= ref.mediana <= ref.p75


def test_cargar_mes_es_idempotente_y_reemplaza(engine: Engine, tmp_path: Path) -> None:
    z = _zip(tmp_path / "m.zip", _filas_mes("2026-03"))
    _cargar(engine, z, "2026-03")
    _cargar(engine, z, "2026-03")
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(OcLineaCa)).scalar_one() == 5
        n_refs = s.execute(select(func.count()).select_from(PrecioReferencia)).scalar_one()
    # Un archivo corregido (menos filas) reemplaza al mes, sin dejar restos.
    z2 = _zip(tmp_path / "m2.zip", _filas_mes("2026-03")[:1])
    _cargar(engine, z2, "2026-03")
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(OcLineaCa)).scalar_one() == 1
        assert n_refs >= 1
        assert s.execute(select(func.count()).select_from(PrecioReferencia)).scalar_one() == 0


def test_moneda_no_clp_se_guarda_sin_precio_base(engine: Engine, tmp_path: Path) -> None:
    filas = [
        _linea(Codigo="U-1", TipoMonedaOC="UTM", totalLineaNeto="5", precioNeto="5"),
        _linea(Codigo="P-1"),
    ]
    _cargar(engine, _zip(tmp_path / "m.zip", filas), "2026-03")
    with Session(engine) as s:
        utm = s.execute(select(OcLineaCa).where(OcLineaCa.codigo_oc == "U-1")).scalar_one()
        assert utm.moneda == "UTM" and utm.precio_base is None and utm.base is None
        r = resumen_mercado(s, 16, "2026-03", "2026-03")
    assert r["total12"] == 10000.0  # la línea en UTM no suma
    assert r["oc12"] == 1


def test_region_configurable(engine: Engine, tmp_path: Path, settings: Settings) -> None:
    assert regiones_activas_engine(engine, settings) == [16]
    _cargar(engine, _zip(tmp_path / "m.zip", _filas_mes("2026-03")), "2026-03", regiones=[13])
    with Session(engine) as s:
        regs = set(s.execute(select(OcLineaCa.region_compra)).scalars())
    assert regs == {13}


# ---------------------------------------------------------------------------
# resumen_mercado == analizar_alimentos() del observatorio
# ---------------------------------------------------------------------------
def _observatorio() -> Any:
    ruta = Path(__file__).resolve().parent.parent / "scripts" / "mercado" / "mercado_observatorio.py"
    spec = importlib.util.spec_from_file_location("mercado_observatorio_oc", ruta)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _escribir_gz(carpeta: Path, mes: str, filas: list[dict[str, str]]) -> None:
    with gzip.open(carpeta / f"oc_ca2_{mes}.csv.gz", "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(list(COLS_OC) + ["grupo", "archivo"])
        for r in filas:
            gr = grupo_de(r["codigoProductoONU"], r["NombreroductoGenerico"])
            if r["EsCompraAgil"].lower() == "si" and gr:
                w.writerow([r[c] for c in COLS_OC] + [gr, mes])


def test_resumen_coincide_con_analizar_alimentos(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    obs = _observatorio()
    carpeta = tmp_path / "gz"
    carpeta.mkdir()
    monkeypatch.setattr(obs, "OUT", carpeta)
    for mes in ("2026-03", "2026-04"):
        filas = _filas_mes(mes)
        _escribir_gz(carpeta, mes, filas)
        _cargar(engine, _zip(tmp_path / f"{mes}.zip", filas), mes)

    esperado = obs.analizar_alimentos()
    with Session(engine) as s:
        real = resumen_mercado(s, 16, "2026-03", "2026-04")

    assert real["total12"] == pytest.approx(esperado["total12"])
    assert real["total12"] > 0
    assert real["local12"] == pytest.approx(esperado["local12"])
    assert real["oc12"] == esperado["oc12"]
    assert real["serie"] == esperado["serie"]
    assert real["familias"] == esperado["familias"]
    assert real["n_compradores"] == esperado["n_compradores"]
    assert real["n_proveedores"] == esperado["n_proveedores"]
    assert real["n_proveedores_locales"] == esperado["n_proveedores_locales"]
    assert real["hhi"] == pytest.approx(esperado["hhi"])
    assert real["top5_cuota"] == pytest.approx(esperado["top5_cuota"])
    assert real["tickets"] == esperado["tickets"]
    assert real["compradores"] == esperado["compradores"]
    assert [(p["proveedor"], p["monto"]) for p in real["proveedores"]] == [
        (p["proveedor"], p["monto"]) for p in esperado["proveedores"]
    ]
    assert [(c["producto"], c["monto"], c["oc"]) for c in real["canasta"]] == [
        (c["producto"], c["monto"], c["oc"]) for c in esperado["canasta"]
    ]


def test_resumen_respeta_region_y_rango(engine: Engine, tmp_path: Path) -> None:
    for mes in ("2026-03", "2026-04"):
        _cargar(engine, _zip(tmp_path / f"{mes}.zip", _filas_mes(mes)), mes)
    with Session(engine) as s:
        solo_marzo = resumen_mercado(s, 16, "2026-03", "2026-03")
        otra_region = resumen_mercado(s, 13, "2026-03", "2026-04")
    assert [m[0] for m in solo_marzo["serie"]] == ["2026-03"]
    assert otra_region["total12"] == 0 and otra_region["oc12"] == 0


# ---------------------------------------------------------------------------
# Job: HEAD + Last-Modified, meses, errores por mes
# ---------------------------------------------------------------------------
def test_meses_a_cargar() -> None:
    assert meses_a_cargar(None, date(2026, 11, 1)) == [(2026, 9), (2026, 10), (2026, 11)]
    assert meses_a_cargar(None, date(2026, 1, 1)) == [(2025, 11), (2025, 12), (2026, 1)]
    assert meses_a_cargar("2026-09", date(2026, 11, 1))[0] == (2026, 9)
    assert len(meses_a_cargar("2024-10", date(2026, 9, 1))) == 24
    with pytest.raises(ValueError):
        meses_a_cargar("2026-13", date(2026, 11, 1))


def _bytes_zip(tmp_path: Path, mes: str) -> bytes:
    return _zip(tmp_path / f"srv-{mes}.zip", _filas_mes(mes)).read_bytes()


@respx.mock
def test_sync_carga_cursor_y_no_repite(engine: Engine, settings: Settings, tmp_path: Path) -> None:
    cuerpo = _bytes_zip(tmp_path, "2026-03")
    lm = {"Last-Modified": "Wed, 01 Apr 2026 03:00:00 GMT"}
    respx.head(f"{_BASE}/oc-da/2026-3.zip").mock(return_value=httpx.Response(200, headers=lm))
    get = respx.get(f"{_BASE}/oc-da/2026-3.zip").mock(return_value=httpx.Response(200, content=cuerpo))

    r1 = sync_mercado_oc(settings, engine, desde="2026-03", hoy=date(2026, 3, 1))
    assert r1["meses"]["2026-03"]["lineas_guardadas"] == 5
    assert get.call_count == 1
    with Session(engine) as s:
        estado = s.get(SyncState, "mercado_oc:2026-03")
        assert estado is not None and estado.cursor == "2026-04-01T03:00:00"

    r2 = sync_mercado_oc(settings, engine, desde="2026-03", hoy=date(2026, 3, 1))
    assert r2["meses"]["2026-03"] == "sin cambios"
    assert get.call_count == 1  # no bajó otra vez
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(OcLineaCa)).scalar_one() == 5


@respx.mock
def test_sync_un_mes_fallido_no_aborta_el_resto(
    engine: Engine, settings: Settings, tmp_path: Path
) -> None:
    cuerpo = _bytes_zip(tmp_path, "2026-04")
    respx.head(f"{_BASE}/oc-da/2026-3.zip").mock(return_value=httpx.Response(403))
    respx.get(f"{_BASE}/oc-da/2026-3.zip").mock(return_value=httpx.Response(403))
    respx.head(f"{_BASE}/oc-da/2026-4.zip").mock(return_value=httpx.Response(404))
    respx.get(f"{_BASE}/oc-da/2026-4.zip").mock(return_value=httpx.Response(200, content=cuerpo))

    r = sync_mercado_oc(settings, engine, desde="2026-03", hoy=date(2026, 4, 1))

    assert r["meses"]["2026-03"] == "no disponible (403)"
    assert r["meses"]["2026-04"]["lineas_guardadas"] == 5
    assert r["errores"] == []  # 403 es "no disponible", no error del job
    with Session(engine) as s:
        assert s.get(SyncState, "mercado_oc:2026-03") is None  # sin cursor: se reintenta


@respx.mock
def test_sync_error_de_servidor_se_informa(engine: Engine, settings: Settings) -> None:
    respx.head(f"{_BASE}/oc-da/2026-3.zip").mock(return_value=httpx.Response(500))
    respx.get(f"{_BASE}/oc-da/2026-3.zip").mock(return_value=httpx.Response(500))
    r = sync_mercado_oc(settings, engine, desde="2026-03", hoy=date(2026, 3, 1))
    assert r["errores"] == ["2026-03: HTTP 500"]


@pytest.mark.parametrize(
    ("columna", "avisa"),
    [("tieneItems", False), ("TIENEITEMS", False), ("IDItemNuevo", True)],
)
def test_columnas_de_item_conocidas_no_avisan(
    engine: Engine,
    settings: Settings,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    columna: str,
    avisa: bool,
) -> None:
    """F-merc-1c §4: `tieneItems` es una marca sí/no, no una clave de línea."""
    cuerpo = _zip(tmp_path / "srv.zip", _filas_mes("2026-03"), extra={columna: "1"}).read_bytes()
    with respx.mock:
        respx.head(f"{_BASE}/oc-da/2026-3.zip").mock(return_value=httpx.Response(200))
        respx.get(f"{_BASE}/oc-da/2026-3.zip").mock(return_value=httpx.Response(200, content=cuerpo))
        with caplog.at_level("WARNING"):
            r = sync_mercado_oc(settings, engine, desde="2026-03", hoy=date(2026, 3, 1))
    assert r["meses"]["2026-03"]["lineas_guardadas"] == 5
    assert ("columnas_item" in r) is avisa
    assert ("columnas de ítem" in caplog.text) is avisa


# ---------------------------------------------------------------------------
# Retención, cruce con F-merc-1, CLI
# ---------------------------------------------------------------------------
def test_retencion_no_purga_oc_lineas_ni_precio_referencia(
    engine: Engine, settings: Settings, tmp_path: Path
) -> None:
    from app.ingest.orchestrator import run_retencion

    _cargar(engine, _zip(tmp_path / "m.zip", _filas_mes("2026-03")), "2026-03")
    with Session(engine) as s:
        antes = (
            s.execute(select(func.count()).select_from(OcLineaCa)).scalar_one(),
            s.execute(select(func.count()).select_from(PrecioReferencia)).scalar_one(),
        )
    run_retencion(engine, settings)
    with Session(engine) as s:
        despues = (
            s.execute(select(func.count()).select_from(OcLineaCa)).scalar_one(),
            s.execute(select(func.count()).select_from(PrecioReferencia)).scalar_one(),
        )
    assert antes == despues and antes[0] > 0


def test_id_oc_de_cotizaciones_se_une_con_oc_lineas(engine: Engine, tmp_path: Path) -> None:
    """`ca_cotizaciones.id_oc` es el `ID` numérico de oc-da, no el `Codigo` [V, 5-nov-2026]."""
    filas = _filas_mes("2026-03")
    for f in filas:
        f["ID"] = "55672374" if f["Codigo"] == "A-03" else "99"
    _cargar(engine, _zip(tmp_path / "m.zip", filas), "2026-03")
    with Session(engine) as s:
        s.add(CompraAgil(codigo="CA-1", nombre="x", estado="proveedor_seleccionado", region=16,
                         actualizado_en=ahora_utc() - timedelta(days=1)))  # fmt: skip
        s.flush()
        s.add(CaCotizacion(ca_codigo="CA-1", llamado=1, rut_proveedor="1-9", id_oc=" 55672374 "))
        s.commit()
        filas = s.execute(
            select(CaCotizacion.id_oc, func.count(OcLineaCa.id_item))
            .join(OcLineaCa, OcLineaCa.id_oc == func.upper(func.trim(CaCotizacion.id_oc)))
            .group_by(CaCotizacion.id_oc)
        ).all()
    assert codigo_oc_normalizado(" a-03 ") == "A-03"
    assert filas == [(" 55672374 ", 2)]


def test_job_mercado_oc_en_el_cli_sin_api() -> None:
    from app.ingest.__main__ import _JOBS, _JOBS_CON_API

    assert "mercado-oc" in _JOBS
    assert "mercado-oc" not in _JOBS_CON_API  # datos abiertos: sin ticket ni cuota ni pausa de API


def test_salud_muestra_tablas_de_mercado(
    engine: Engine, settings: Settings, tmp_path: Path
) -> None:
    from app.api.salud_data import get_salud_data

    _cargar(engine, _zip(tmp_path / "m.zip", _filas_mes("2026-03")), "2026-03")
    with Session(engine) as s:
        datos = get_salud_data(s, settings)
    por_tabla = {t["tabla"]: t for t in datos["tablas_mercado"]}
    assert por_tabla["oc_lineas_ca"]["filas"] == 5
    # F-plat-7 extendió la lista (todas las tablas del pivote): aquí basta con que estén las de mercado.
    assert set(por_tabla) >= {
        "oc_lineas_ca",
        "precio_referencia",
        "precio_indice",  # F-plat-4
        "ca_cotizaciones",
        "ca_cotizacion_productos",
        "demanda_series",  # F-plat-6
        "pronosticos",
        "recomendaciones",
    }


# ---------------------------------------------------------------------------
# IDItem, OC repetida entre meses, textos largos y moneda por ítem
# ---------------------------------------------------------------------------
def test_id_item_es_la_pk_y_sin_id_se_descarta(engine: Engine, tmp_path: Path) -> None:
    filas = [
        _linea(Codigo="X-1", IDItem="101"),
        _linea(Codigo="X-1", IDItem="102"),  # misma OC, otro ítem: dos filas
        _linea(Codigo="X-1", IDItem="102"),  # repetida en el mismo archivo: una sola
        _linea(Codigo="X-2", IDItem=""),  # sin IDItem: se descarta y se cuenta
    ]
    r = _cargar(engine, _zip(tmp_path / "m.zip", filas), "2026-03")
    assert r["lineas_guardadas"] == 2
    assert r["sin_id_item"] == 1
    assert r["repetidas_en_el_mes"] == 1
    with Session(engine) as s:
        pks = {(x.codigo_oc, x.id_item) for x in s.execute(select(OcLineaCa)).scalars()}
    assert pks == {("X-1", "101"), ("X-1", "102")}


@pytest.mark.parametrize("orden", [("2026-03", "2026-04"), ("2026-04", "2026-03")])
def test_oc_repetida_en_dos_meses_queda_en_el_mas_reciente(
    engine: Engine, tmp_path: Path, orden: tuple[str, str]
) -> None:
    def archivo(mes: str) -> Path:
        filas = [_linea(Codigo="2582-359-SE22", IDItem="555", precioNeto="1000" if mes == "2026-03" else "1200")]
        return _zip(tmp_path / f"{mes}.zip", filas)

    resultados = {mes: _cargar(engine, archivo(mes), mes) for mes in orden}
    with Session(engine) as s:
        filas_db = list(s.execute(select(OcLineaCa)).scalars())
    assert len(filas_db) == 1
    assert filas_db[0].mes == "2026-04"
    assert filas_db[0].precio_neto == 1200.0
    if orden[0] == "2026-03":  # el mes nuevo movió la fila
        assert resultados["2026-04"]["movidas_de_otro_mes"] == 1
    else:  # el mes viejo llega tarde y no pisa la fila más reciente
        assert resultados["2026-03"]["conservadas_de_mes_posterior"] == 1
        assert resultados["2026-03"]["movidas_de_otro_mes"] == 0


def test_especificacion_larga_no_rompe_la_carga(engine: Engine, tmp_path: Path) -> None:
    largo = "x" * 2000
    filas = [
        _linea(Codigo="L-1", IDItem="1", EspecificacionComprador=largo, UnidadCompra="U" * 600,
               OrganismoPublico="O" * 600, NombreProveedor="P" * 600, NombreroductoGenerico="N" * 600),
        # columnas con tope: nada más largo que su String(n) llega a la base
        _linea(Codigo="L-2", IDItem="2", sector="S" * 500, CiudadUnidadCompra="C" * 500,
               codigoEstado="9" * 50, UnidadMedida="M" * 200),
    ]  # fmt: skip
    r = _cargar(engine, _zip(tmp_path / "m.zip", filas), "2026-03")
    assert r["lineas_guardadas"] == 2
    with Session(engine) as s:
        a = s.get(OcLineaCa, ("L-1", "1"))
        b = s.get(OcLineaCa, ("L-2", "2"))
    assert a is not None and b is not None
    assert a.especificacion is not None and 0 < len(a.especificacion) <= 500
    assert a.unidad_compra == "U" * 600 and a.organismo == "O" * 600  # Text: sin recorte
    assert a.proveedor == "P" * 600 and a.producto_generico == "N" * 600
    assert b.sector is not None and len(b.sector) == 100
    assert b.codigo_estado is not None and len(b.codigo_estado) == 10


def test_moneda_por_item_manda_sobre_la_de_la_oc(engine: Engine, tmp_path: Path) -> None:
    filas = [
        _linea(Codigo="M-1", IDItem="1", TipoMonedaOC="CLP", monedaItem="UTM"),  # difiere
        _linea(Codigo="M-2", IDItem="2", TipoMonedaOC="UTM", monedaItem=""),  # sin monedaItem: la de la OC
        _linea(Codigo="M-3", IDItem="3", TipoMonedaOC="CLP", monedaItem="CLP"),
        _linea(Codigo="M-4", IDItem="4", TipoMonedaOC="UTM", monedaItem="CLP"),  # difiere
    ]
    r = _cargar(engine, _zip(tmp_path / "m.zip", filas), "2026-03")
    assert r["moneda_distinta"] == 2
    with Session(engine) as s:
        monedas = {x.id_item: (x.moneda, x.precio_base) for x in s.execute(select(OcLineaCa)).scalars()}
    assert monedas["1"] == ("UTM", None)
    assert monedas["2"] == ("UTM", None)
    assert monedas["3"][0] == "CLP" and monedas["3"][1] is not None
    assert monedas["4"][0] == "CLP" and monedas["4"][1] is not None


# ---------------------------------------------------------------------------
# F-plat-6-cruce-2: `ID` de la OC (id_oc) y recarga con --forzar
# ---------------------------------------------------------------------------
def _zip_id_primero(destino: Path, filas: list[dict[str, str]], bom: bool) -> Path:
    """oc-da real: `ID` es la PRIMERA columna; con BOM UTF-8 (bytes EF BB BF) pegado a ella."""
    cols = ["ID"] + [c for c in COLS_OC if c != "ID"]
    buf = io.StringIO(newline="")
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(cols)
    for f in filas:
        w.writerow([f.get(c, "") for c in cols])
    datos = buf.getvalue().encode("latin-1")
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("oc.csv", (b"\xef\xbb\xbf" if bom else b"") + datos)
    return destino


@pytest.mark.parametrize("bom", [False, True])
def test_id_oc_se_guarda_con_y_sin_bom_y_cuenta_las_filas_sin_id(
    engine: Engine, tmp_path: Path, bom: bool
) -> None:
    filas = [
        _linea(Codigo="2724-973-AG26", IDItem="1", ID=" 55672374 "),
        _linea(Codigo="2724-973-AG26", IDItem="2", ID="55672374"),
        _linea(Codigo="2724-974-AG26", IDItem="1", ID=""),  # sin ID: se carga igual
    ]
    r = _cargar(engine, _zip_id_primero(tmp_path / "m.zip", filas, bom), "2026-03")
    assert r["lineas_guardadas"] == 3 and r["lineas_sin_id"] == 1
    with Session(engine) as s:
        ids = dict(s.execute(select(OcLineaCa.codigo_oc, OcLineaCa.id_oc).distinct()).all())
    assert ids == {"2724-973-AG26": "55672374", "2724-974-AG26": None}


def test_nombre_columna_quita_el_bom_en_ambas_formas() -> None:
    from app.clients.datos_abiertos import _nombre_columna

    assert _nombre_columna("\u00ef\u00bb\u00bfID") == "ID"  # EF BB BF leído como Latin-1
    assert _nombre_columna("\ufeffID") == "ID"  # ya decodificado
    assert _nombre_columna(" Codigo ") == "Codigo"


@respx.mock
def test_forzar_ignora_el_cursor_y_sin_el_se_respeta(
    engine: Engine, settings: Settings, tmp_path: Path
) -> None:
    cuerpo = _bytes_zip(tmp_path, "2026-03")
    lm = {"Last-Modified": "Wed, 01 Apr 2026 03:00:00 GMT"}
    respx.head(f"{_BASE}/oc-da/2026-3.zip").mock(return_value=httpx.Response(200, headers=lm))
    get = respx.get(f"{_BASE}/oc-da/2026-3.zip").mock(return_value=httpx.Response(200, content=cuerpo))
    hoy = date(2026, 3, 1)

    sync_mercado_oc(settings, engine, desde="2026-03", hoy=hoy)
    assert get.call_count == 1
    r = sync_mercado_oc(settings, engine, desde="2026-03", hoy=hoy)
    assert r["meses"]["2026-03"] == "sin cambios" and get.call_count == 1
    r = sync_mercado_oc(settings, engine, desde="2026-03", hoy=hoy, forzar=True)
    assert r["meses"]["2026-03"]["lineas_guardadas"] == 5 and get.call_count == 2
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(OcLineaCa)).scalar_one() == 5


@pytest.mark.parametrize(("extra", "esperado"), [([], False), (["--forzar"], True)])
def test_forzar_llega_al_job_del_cli(
    monkeypatch: pytest.MonkeyPatch, extra: list[str], esperado: bool
) -> None:
    import app.ingest.__main__ as cli

    visto: dict[str, Any] = {}
    monkeypatch.setattr(cli, "cmd_run_once", lambda job, **kw: visto.update(job=job, **kw))
    monkeypatch.setattr(sys, "argv", ["app.ingest", "run-once", "--job", "mercado-oc", *extra])
    cli.main()
    assert visto["job"] == "mercado-oc" and visto["forzar"] is esperado
