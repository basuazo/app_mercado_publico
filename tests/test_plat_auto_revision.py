"""F-plat-auto-revision: líneas de anexo confirmadas solas; humano solo en excepciones.

Puntaje puro con casos de cada señal; umbral desde settings (y el subido por el job); anexo
totalmente auto → resuelto (las de la API pasan a `reemplazada`, regresión de anexos-2); segunda
lectura solo cuando hace falta; muestreo de control determinista; precisión observada y subida del
umbral; cola de excepciones. Sin red: el extractor es un doble (SQLite en memoria).
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.catalogos.canonico import normalizar_texto
from app.clients.types import ExtraccionAnexo, LineaExtraida
from app.mercado import anexos
from app.mercado import auto_revision as ar
from app.mercado import catalogo as cat
from app.models.base import Base
from app.models.tables import (
    AutoControlSemana,
    CaLinea,
    CompraAgil,
    Empresa,
    ProductoCanonico,
    SyncState,
)
from tests.test_plat2 import _ANEXO, _ca, _lineas, _sesion, _settings, _usuario

# ---------------------------------------------------------------------------
# Fixtures y dobles
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(e)
    yield e


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


class _Extractor:
    """Doble del cliente de OpenRouter: devuelve siempre las mismas líneas."""

    habilitado = True

    def __init__(self, lineas: list[LineaExtraida], *, respaldo: bool = False) -> None:
        self._lineas = lineas
        self._respaldo = respaldo
        self.llamadas = 0

    def extraer_lineas(self, texto, imagenes=(), *, esquema="lineas") -> ExtraccionAnexo:
        self.llamadas += 1
        return ExtraccionAnexo(
            lineas=list(self._lineas), modelo="prueba/principal", respaldo=self._respaldo
        )


class _Segunda:
    def __init__(self, lineas: list[LineaExtraida]) -> None:
        self._lineas = lineas
        self.llamadas = 0

    def __call__(self, texto, imagenes) -> ExtraccionAnexo:
        self.llamadas += 1
        return ExtraccionAnexo(lineas=list(self._lineas), modelo="prueba/respaldo")


def _xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Producto", "Cantidad", "Unidad"])
    ws.append(["Hallulla", 12, "kg"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pid(session: Session, nombre: str) -> int:
    return session.execute(
        select(ProductoCanonico.id).where(ProductoCanonico.nombre == nombre)
    ).scalar_one()


_HALLULLA = LineaExtraida(texto="Hallulla", cantidad=12, unidad="kg")
_ARROZ = LineaExtraida(texto="Arroz grado 1", cantidad=20, unidad="kg")
_RARO = LineaExtraida(texto="Servicio de banquetería", cantidad=1, unidad="servicio")


def _alias(session: Session, linea: LineaExtraida, nombre: str) -> None:
    cat._upsert_alias(
        session, normalizar_texto(anexos.texto_de_linea(linea)), _pid(session, nombre), None, 1
    )


def _catalogo_con_alias(session: Session) -> None:
    cat.sembrar_canasta(session)
    _alias(session, _HALLULLA, "Hallulla")
    _alias(session, _ARROZ, "Arroz grado 1")
    session.commit()


def _ca_placeholder(session: Session) -> CompraAgil:
    """CA cuyo detalle de la API es «según anexo» (placeholder `sin_detalle`)."""
    _catalogo_con_alias(session)
    ca = _ca(session, productos=_ANEXO)
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    session.commit()
    return ca


def _ca_con_api(session: Session) -> CompraAgil:
    """Caso D de anexos-2: línea pobre de la API ya confirmada (`api|ok`) + anexo."""
    _catalogo_con_alias(session)
    ca = _ca(session, productos=[("50181901", "Pan", "pan batido de campo", 10, "Kilogramo")])
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    (pan,) = _lineas(session)
    cat.confirmar_linea(
        session, pan, producto_id=_pid(session, "Hallulla"), formato_id=None, usuario_id=1
    )
    session.commit()
    return ca


def _procesar(session, ca, extractor, auto) -> anexos.ResultadoCarga:
    res = anexos.procesar_anexo_ca(
        session, ca, _xlsx(), "anexo.xlsx", usuario_id=None, extractor=extractor, auto=auto
    )
    session.commit()
    return res


# ---------------------------------------------------------------------------
# §1 Puntaje (puro): casos de cada señal
# ---------------------------------------------------------------------------

_BASE = ar.Senales(producto_id=1, via="alias", cantidad=5, tipo_doc="texto")


def _p(**kw: Any) -> float:
    return ar.puntaje_linea(replace(_BASE, **kw)).valor


def test_sin_producto_o_sin_cantidad_puntaje_cero():
    assert ar.puntaje_linea(replace(_BASE, producto_id=None)).valor == 0.0
    assert ar.puntaje_linea(replace(_BASE, cantidad=None)).valor == 0.0
    assert ar.puntaje_linea(replace(_BASE, cantidad=0)).valor == 0.0


def test_senal_catalogo_alias_contenido_regla_y_unicidad():
    assert _p(via="alias") > _p(via="contenido") > _p(via="regla") > _p(via=None)
    assert _p(sugerencia_unica=True) > _p()
    assert _p(formato_coherente=True) > _p()


def test_senal_api_calza_suma_y_contradiccion_resta():
    assert _p(api_calza=True) > _p() > _p(api_calza=False)
    assert _p(api_calza=True, api_cantidad_orden=True) > _p(api_calza=True)
    assert _p(monto_cuadra=True) > _p() > _p(monto_cuadra=False)


def test_senal_cantidad_unidad_plausible():
    assert _p(unidad_reconocida=True) > _p()
    assert _p(plausible=True) > _p() > _p(plausible=False)


def test_senal_acuerdo_de_dos_lecturas():
    assert _p(acuerdo=True) - _p() == pytest.approx(ar.PESOS["acuerdo"])
    assert _p(acuerdo=False) < _p()


def test_senal_tipo_documento_y_calidad():
    assert _p(tipo_doc="docx-xlsx") > _p(tipo_doc="texto") > _p(tipo_doc="escaneado")
    assert _p(paginas_omitidas=True) < _p()
    assert _p(respaldo=True) < _p()


def test_puntaje_recortado_entre_cero_y_uno():
    todo = ar.Senales(
        producto_id=1, via="alias", sugerencia_unica=True, formato_coherente=True, cantidad=1,
        unidad_reconocida=True, plausible=True, api_calza=True, api_cantidad_orden=True,
        monto_cuadra=True, acuerdo=True, tipo_doc="docx-xlsx",
    )  # fmt: skip
    nada = ar.Senales(
        producto_id=1, cantidad=1, plausible=False, api_calza=False, monto_cuadra=False,
        acuerdo=False, tipo_doc="escaneado", paginas_omitidas=True, respaldo=True,
    )  # fmt: skip
    assert ar.puntaje_linea(todo).valor == 1.0
    assert ar.puntaje_linea(nada).valor == 0.0
    assert "cat_alias" in ar.puntaje_linea(todo).motivos


def test_alcanza_con_acuerdo():
    s = replace(_BASE, tipo_doc="docx-xlsx")  # 0,35 + 0,10 = 0,45; con acuerdo 0,65
    assert ar.alcanza_con_acuerdo(s, 0.6)
    assert not ar.alcanza_con_acuerdo(s, 0.4)  # ya llega sin acuerdo
    assert not ar.alcanza_con_acuerdo(s, 0.9)  # ni con acuerdo llega
    assert not ar.alcanza_con_acuerdo(replace(s, acuerdo=True), 0.6)


def test_acuerdo_lecturas_texto_cantidad_y_unidad():
    a = [_HALLULLA, _ARROZ, _RARO]
    b = [
        LineaExtraida(texto="hallulla", cantidad=12.0, unidad="kilos"),
        LineaExtraida(texto="Arroz grado 1", cantidad=25, unidad="kg"),  # otra cantidad
    ]
    assert ar.acuerdo_lecturas(a, b) == [True, False, False]


def test_monto_cuadra_y_unidad_reconocida():
    ls = [LineaExtraida(texto="x", cantidad=10, precio=1000), LineaExtraida(texto="y", cantidad=2, precio=500)]
    assert ar.monto_cuadra(ls, 11_000) is True
    assert ar.monto_cuadra(ls, 50_000) is False
    assert ar.monto_cuadra([_HALLULLA], 11_000) is None  # sin precio: no opina
    assert ar.unidad_reconocida("Kilos") and ar.unidad_reconocida("cajas")
    assert not ar.unidad_reconocida("servicio") and not ar.unidad_reconocida(None)


def test_senales_plausibilidad_con_historial(session):
    _catalogo_con_alias(session)
    from app.mercado.catalogo import cargar_catalogo

    catalogo = cargar_catalogo(session)
    pid = _pid(session, "Hallulla")
    ctx = ar.ContextoCA(rangos={pid: (5.0, 50.0)})
    ok = ar.senales_de_linea(_HALLULLA, catalogo, ctx, tipo_doc="texto")
    fuera = ar.senales_de_linea(replace(_HALLULLA, cantidad=900), catalogo, ctx, tipo_doc="texto")
    assert ok.producto_id == pid and ok.via == "alias" and ok.sugerencia_unica
    assert ok.plausible is True and fuera.plausible is False


# ---------------------------------------------------------------------------
# Umbral desde settings (y el que sube el job)
# ---------------------------------------------------------------------------


def test_umbral_desde_settings_vacio_es_apagado():
    assert _settings().auto_confirmar_umbral is None
    assert _settings(auto_confirmar_umbral="").auto_confirmar_umbral is None
    assert _settings(auto_confirmar_umbral="0.7").auto_confirmar_umbral == 0.7
    with pytest.raises(ValueError):
        _settings(auto_confirmar_umbral=1.5)


def test_config_desde_settings_y_umbral_subido(session):
    assert ar.config_desde_settings(session, _settings(), None).umbral is None
    cfg = ar.config_desde_settings(session, _settings(auto_confirmar_umbral=0.7), None)
    assert cfg.umbral == 0.7 and cfg.segunda is None  # sin cliente real ni respaldo
    session.add(SyncState(fuente=ar.FUENTE_ESTADO, notas='{"umbral": 0.8}'))
    session.commit()
    assert ar.config_desde_settings(session, _settings(auto_confirmar_umbral=0.7), None).umbral == 0.8
    # El configurado manda si es mayor que el subido.
    assert ar.umbral_efectivo(session, 0.9) == 0.9


def test_config_desde_entorno(session, monkeypatch):
    for k, v in {
        "MP_TICKET": "t", "SECRET_KEY": "secret-test-key-larga-32chars!!", "JOBS_TOKEN": "j",
        "DATABASE_URL": "sqlite:///:memory:", "AUTO_CONFIRMAR_UMBRAL": "0.65",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    assert ar.resolver_config(session, None).umbral == 0.65
    monkeypatch.setenv("AUTO_CONFIRMAR_UMBRAL", "")
    assert ar.resolver_config(session, None).umbral is None


def test_segunda_lectura_con_cliente_real_y_respaldo(session, engine):
    from app.clients.openrouter import OpenRouterClient

    s = _settings(
        auto_confirmar_umbral=0.7, openrouter_api_key="sk-or-v1-x", llm_modelo_extraccion="a/b",
        llm_modelo_respaldo="c/d",
    )  # fmt: skip
    cfg = ar.config_desde_settings(session, s, OpenRouterClient(s, engine))
    assert cfg.segunda is not None and cfg.modelo_segunda == "c/d"


# ---------------------------------------------------------------------------
# §3 Aplicación al extraer
# ---------------------------------------------------------------------------


def test_apagado_todas_quedan_dudosa(session):
    ca = _ca_placeholder(session)
    res = _procesar(session, ca, _Extractor([_HALLULLA, _ARROZ]), ar.ConfigAuto(umbral=None))
    assert [ln.estado for ln in res.lineas] == ["dudosa", "dudosa"]
    assert all(ln.confirmada_por is None and ln.puntaje_auto is None for ln in res.lineas)


def test_anexo_totalmente_auto_queda_resuelto(session):
    ca = _ca_placeholder(session)
    res = _procesar(session, ca, _Extractor([_HALLULLA, _ARROZ]), ar.ConfigAuto(umbral=0.55))
    lineas = _lineas(session)
    assert [(ln.estado, ln.confirmada_por) for ln in lineas] == [("ok", "auto"), ("ok", "auto")]
    assert all(ln.puntaje_auto is not None and ln.puntaje_auto >= 0.55 for ln in lineas)
    assert all(ln.revisado_por is None for ln in lineas)  # nadie la revisó
    session.refresh(ca)
    assert ca.anexo_resuelto_en is not None and ca.detalle_lineas == "completo"
    assert "todas se confirmaron solas" in res.aviso


def test_auto_total_reemplaza_las_de_la_api_regresion_anexos2(session):
    ca = _ca_con_api(session)
    _procesar(session, ca, _Extractor([_HALLULLA]), ar.ConfigAuto(umbral=0.55))
    api, hallulla = _lineas(session)
    assert (api.origen, api.estado) == ("api", "reemplazada")
    assert (hallulla.estado, hallulla.confirmada_por) == ("ok", "auto")
    # Señales 2: calza con la línea de la API (mismo producto, misma escala de cantidad).
    assert hallulla.puntaje_auto == pytest.approx(0.75)


def test_auto_parcial_deja_dudosas_y_no_toca_la_api(session):
    ca = _ca_con_api(session)
    res = _procesar(session, ca, _Extractor([_HALLULLA, _RARO]), ar.ConfigAuto(umbral=0.55))
    api, hallulla, raro = _lineas(session)
    assert (api.origen, api.estado) == ("api", "ok")  # sin resolver: nada reemplazado aún
    assert (hallulla.estado, raro.estado) == ("ok", "dudosa")
    assert raro.puntaje_auto == 0.0 and raro.confirmada_por is None
    session.refresh(ca)
    assert ca.detalle_lineas == "incompleto"
    assert "1 se confirmaron solas" in res.aviso


def test_no_toca_lineas_ok_por_humano_ni_las_manuales(session):
    ca = _ca_placeholder(session)
    manual = cat.agregar_linea_manual(
        session, ca, producto_id=_pid(session, "Azúcar"), formato_id=None, cantidad=3, usuario_id=1
    )
    session.commit()
    _procesar(session, ca, _Extractor([_HALLULLA, _RARO]), ar.ConfigAuto(umbral=0.55))
    session.refresh(manual)
    assert (manual.estado, manual.confirmada_por, manual.puntaje_auto) == ("ok", None, None)


def test_segunda_lectura_solo_cuando_hace_falta(session):
    ca = _ca_placeholder(session)
    # 0,60 sin acuerdo; con umbral 0,55 no hace falta la segunda lectura.
    segunda = _Segunda([_HALLULLA, _ARROZ])
    _procesar(session, ca, _Extractor([_HALLULLA, _ARROZ]), ar.ConfigAuto(0.55, segunda))
    assert segunda.llamadas == 0


def test_segunda_lectura_acuerda_y_confirma(session):
    ca = _ca_placeholder(session)
    segunda = _Segunda([_HALLULLA, replace(_ARROZ, cantidad=99)])
    _procesar(session, ca, _Extractor([_HALLULLA, _ARROZ]), ar.ConfigAuto(0.75, segunda))
    assert segunda.llamadas == 1
    hallulla, arroz = _lineas(session)
    assert (hallulla.estado, hallulla.confirmada_por) == ("ok", "auto")  # 0,60 + 0,20
    assert arroz.estado == "dudosa"  # las lecturas no coinciden en la cantidad
    session.refresh(ca)
    assert ca.anexo_resuelto_en is not None  # el placeholder ya lo resolvió la extracción
    assert ca.detalle_lineas == "incompleto"


def test_sin_segunda_lectura_si_ya_leyo_el_respaldo(session):
    ca = _ca_placeholder(session)
    segunda = _Segunda([_HALLULLA])
    _procesar(session, ca, _Extractor([_HALLULLA], respaldo=True), ar.ConfigAuto(0.7, segunda))
    assert segunda.llamadas == 0
    (h,) = _lineas(session)
    assert h.estado == "dudosa"  # 0,60 − 0,05 (respaldo) < 0,7


# ---------------------------------------------------------------------------
# Muestreo de control, precisión observada y subida del umbral
# ---------------------------------------------------------------------------

_LUNES = datetime(2026, 11, 2, 15, 0)  # lunes 2-nov-2026, 12:00 Chile (UTC naive)


def _autos(session: Session, n: int, cuando: datetime, codigo: str = "9-9-COT26") -> list[int]:
    if session.get(CompraAgil, codigo) is None:
        _ca(session, codigo)
    base = len(_lineas(session, codigo))
    ids = []
    for i in range(n):
        ln = CaLinea(
            ca_codigo=codigo, n=1000 + base + i, origen="anexo", estado="ok", texto_origen="x",
            confirmada_por=ar.AUTO, puntaje_auto=0.9, creado_en=cuando,
        )  # fmt: skip
        session.add(ln)
        session.flush()
        ids.append(ln.id)
    session.commit()
    return ids


def _controladas(session: Session) -> set[int]:
    return set(
        session.execute(select(CaLinea.id).where(CaLinea.control_en.isnot(None))).scalars()
    )


def test_semana_iso_en_hora_de_chile():
    assert ar.semana_iso(_LUNES) == "2026-W45"
    # Domingo 1-nov 23:30 Chile = lunes 2-nov 02:30 UTC: sigue siendo la semana 44 en Chile.
    assert ar.semana_iso(datetime(2026, 11, 2, 2, 30)) == "2026-W44"


def test_muestreo_5_por_ciento_idempotente_y_determinista(engine, session):
    _autos(session, 40, _LUNES)
    assert ar.muestrear_control(session, _LUNES) == 2  # ceil(5 % de 40)
    session.commit()
    primeras = _controladas(session)
    assert len(primeras) == 2
    assert ar.muestrear_control(session, _LUNES) == 0  # idempotente
    _autos(session, 20, _LUNES + timedelta(days=1))
    assert ar.muestrear_control(session, _LUNES) == 1  # ceil(5 % de 60) = 3: falta 1
    session.commit()
    assert primeras <= _controladas(session)
    # Misma semilla y mismas líneas en otra base → la misma muestra.
    e2 = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(e2)
    with Session(e2) as s2:
        _autos(s2, 40, _LUNES)
        ar.muestrear_control(s2, _LUNES)
        assert _controladas(s2) == primeras
    with Session(e2) as s3:
        s3.execute(CaLinea.__table__.update().values(control_en=None))
        ar.muestrear_control(s3, _LUNES, semilla="otra-semilla")
        assert len(_controladas(s3)) == 2


def test_muestreo_no_mezcla_semanas(session):
    _autos(session, 10, _LUNES - timedelta(days=7))
    assert ar.muestrear_control(session, _LUNES) == 0


def _linea_control(session: Session) -> CaLinea:
    ca = _ca_placeholder(session)
    _procesar(session, ca, _Extractor([_HALLULLA, _RARO]), ar.ConfigAuto(umbral=0.55))
    hallulla = _lineas(session)[0]
    assert ar.muestrear_control(session, hallulla.creado_en) == 1
    session.commit()
    session.refresh(hallulla)
    assert ar.es_control_pendiente(hallulla)
    return hallulla


def test_control_confirmado_sin_cambios_es_acierto(session):
    ln = _linea_control(session)
    cat.confirmar_linea(
        session, ln, producto_id=ln.producto_id, formato_id=ln.formato_id, usuario_id=1,
        cantidad=ln.cantidad,
    )  # fmt: skip
    session.commit()
    fila = session.get(AutoControlSemana, ar.semana_iso(ln.creado_en))
    assert (fila.controles, fila.errores) == (1, 0)
    assert ln.confirmada_por == "humano" and not ar.es_control_pendiente(ln)
    assert ar.precision_semana(fila) == 1.0


def test_control_corregido_o_eliminado_es_error(session):
    ln = _linea_control(session)
    semana = ar.semana_iso(ln.creado_en)
    cat.confirmar_linea(
        session, ln, producto_id=ln.producto_id, formato_id=ln.formato_id, usuario_id=1,
        cantidad=15,
    )  # fmt: skip
    session.commit()
    assert (session.get(AutoControlSemana, semana).errores) == 1
    # Una auto-confirmada en control que se elimina también cuenta como error.
    ln.confirmada_por, ln.control_en = ar.AUTO, ln.creado_en
    session.commit()
    cat.quitar_linea_manual(session, ln)
    session.commit()
    fila = session.get(AutoControlSemana, semana)
    assert (fila.controles, fila.errores) == (2, 2)
    assert ar.precision_semana(fila) == 0.0


def test_confirmar_linea_de_anexo_la_marca_humana_sin_contar_control(session):
    ca = _ca_placeholder(session)
    _procesar(session, ca, _Extractor([_HALLULLA, _RARO]), ar.ConfigAuto(umbral=None))
    h, _ = _lineas(session)
    cat.confirmar_linea(session, h, producto_id=h.producto_id, formato_id=None, usuario_id=1)
    session.commit()
    assert h.confirmada_por == "humano"
    assert session.execute(select(AutoControlSemana)).first() is None


def _semana(session: Session, momento: datetime, controles: int, errores: int) -> None:
    session.add(
        AutoControlSemana(semana=ar.semana_iso(momento), controles=controles, errores=errores)
    )
    session.commit()


def test_precision_baja_dos_semanas_sube_el_umbral_una_vez(session, caplog):
    ahora = _LUNES + timedelta(days=2)
    _semana(session, ahora - timedelta(days=7), 20, 2)  # 90 %
    _semana(session, ahora - timedelta(days=14), 10, 1)  # 90 %
    with caplog.at_level("WARNING"):
        r = ar.evaluar_subida(session, 0.7, ahora)
    session.commit()
    assert r["subido"] and r["umbral"] == pytest.approx(0.75)
    assert "umbral sube" in caplog.text
    assert ar.umbral_efectivo(session, 0.7) == pytest.approx(0.75)
    # Mismo par de semanas: no vuelve a subir (ni al día siguiente).
    assert not ar.evaluar_subida(session, 0.7, ahora + timedelta(days=1))["subido"]
    # Una semana nueva mala sola tampoco: pide dos semanas nuevas.
    _semana(session, ahora, 10, 5)
    assert not ar.evaluar_subida(session, 0.7, ahora + timedelta(days=7))["subido"]
    _semana(session, ahora + timedelta(days=7), 10, 5)
    r2 = ar.evaluar_subida(session, 0.7, ahora + timedelta(days=14))
    assert r2["subido"] and r2["umbral"] == pytest.approx(0.8)


def test_una_semana_baja_o_apagado_no_sube(session):
    ahora = _LUNES + timedelta(days=2)
    _semana(session, ahora - timedelta(days=7), 20, 2)  # 90 %
    _semana(session, ahora - timedelta(days=14), 20, 0)  # 100 %
    assert not ar.evaluar_subida(session, 0.7, ahora)["subido"]
    assert ar.evaluar_subida(session, None, ahora) == {"umbral": None}


def test_ciclo_semanal_y_resumen_de_salud(session):
    _autos(session, 30, _LUNES)
    r = ar.ciclo_semanal(session, 0.7, _LUNES + timedelta(days=1))
    session.commit()
    assert r["control_muestreadas"] == 2 and not r["subido"]
    _semana(session, _LUNES - timedelta(days=7), 10, 2)
    salud = ar.resumen_salud(session, 0.7, _LUNES + timedelta(days=1))
    actual, anterior = salud["semanas"][0], salud["semanas"][1]
    assert (actual["auto"], actual["control"], actual["control_pendiente"]) == (30, 2, 2)
    assert anterior["precision"] == 80.0 and salud["alerta"]
    assert salud["umbral_efectivo"] == 0.7


# ---------------------------------------------------------------------------
# §2 Calibración (pura)
# ---------------------------------------------------------------------------


def test_curva_y_eleccion_del_umbral():
    alto = replace(_BASE, tipo_doc="docx-xlsx", sugerencia_unica=True)  # 0,50
    bajo = replace(_BASE, via="regla")  # 0,17
    docs = [
        ar.DocumentoCalibracion("docx-xlsx", (alto,) * 30, None, (True,) * 30),
        ar.DocumentoCalibracion("texto", (bajo,) * 10, None, (False,) * 10),
    ]
    puntos = {p.umbral: p for p in ar.curva(docs, [0.1, 0.5, 0.9])}
    assert (puntos[0.1].auto, puntos[0.1].correctas) == (40, 30)
    assert puntos[0.5].precision == 1.0 and puntos[0.5].cobertura == 0.75
    assert puntos[0.9].auto == 0
    assert ar.elegir_umbral(list(puntos.values())) == 0.5
    assert ar.elegir_umbral(list(puntos.values()), min_auto=50) is None


def test_curva_simula_la_segunda_lectura_como_produccion():
    s = replace(_BASE, tipo_doc="docx-xlsx")  # 0,45; con acuerdo 0,65
    doc = ar.DocumentoCalibracion("docx-xlsx", (s, s), (True, False), (True, False))
    assert ar.puntajes_simulados(doc, 0.6) == pytest.approx([0.65, 0.15])
    assert ar.puntajes_simulados(doc, 0.4) == pytest.approx([0.45, 0.45])  # no hacía falta


# ---------------------------------------------------------------------------
# Cola de excepciones (/catalogo/revision)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_y_usuario(engine):
    from fastapi.testclient import TestClient

    settings = _settings()
    with Session(engine) as s:
        e1 = Empresa(nombre="E1", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True)
        s.add(e1)
        s.flush()
        uid = _usuario(s, "con@t.cl", e1.id)
        s.commit()
    return TestClient(create_app(settings, engine)), settings, uid


def test_cola_muestra_dudosas_y_control_y_confirmar_todas_vuelve(engine, client_y_usuario):
    client, settings, uid = client_y_usuario
    with Session(engine) as s:
        ca = _ca_placeholder(s)
        ca.fecha_cierre = datetime(2099, 1, 1)
        s.commit()
        _procesar(s, ca, _Extractor([_HALLULLA, _ARROZ, _RARO]), ar.ConfigAuto(umbral=None))
        h, a, _r = _lineas(s)
        # Hallulla auto-confirmada y en control; Arroz dudosa con producto; lo raro sin producto.
        h.estado, h.confirmada_por, h.control_en = "ok", ar.AUTO, datetime(2026, 11, 3)
        s.commit()
        codigo = ca.codigo
    cookies, headers = _sesion(settings, uid)
    client.cookies.update(cookies)
    r = client.get("/catalogo/revision")
    assert r.status_code == 200
    assert codigo in r.text and "control" in r.text and "Confirmar todas (1)" in r.text
    assert r.text.count('id="linea-') == 3
    r = client.post(
        f"/catalogo/ca/{codigo}/anexo/confirmar-todas",
        data={"csrf_token": headers["X-CSRF-Token"], "volver": "revision"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/catalogo/revision?")
    with Session(engine) as s:
        h, a, raro = _lineas(s)
        assert (a.estado, a.confirmada_por) == ("ok", "humano")
        assert ar.es_control_pendiente(h)  # «Confirmar todas» no despacha los controles
        assert raro.estado == "dudosa"
    r = client.get("/catalogo/revision")
    assert r.text.count('id="linea-') == 2


def test_cola_vacia(client_y_usuario):
    client, settings, uid = client_y_usuario
    client.cookies.update(_sesion(settings, uid)[0])
    r = client.get("/catalogo/revision")
    assert r.status_code == 200 and "No hay excepciones" in r.text
