"""F-plat-6: demanda por comprador, pronóstico, backtest y recomendaciones de abastecimiento.

Offline (SQLite en memoria). El juez de verdad es el backtest con las OC reales que corre Boris
(`mercado-demanda --completo`); acá, series sintéticas con resultado conocido.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.catalogos.canonico import normalizar_texto
from app.core.settings import Settings
from app.mercado import abastecimiento as ab
from app.mercado import demanda as dm
from app.mercado import evaluacion as ev
from app.mercado import indice as idx
from app.mercado.comprador import clave_unidad
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaCotizacion,
    CaCotizacionProducto,
    CaLinea,
    CompetenciaCompradorMes,
    CompraAgil,
    DemandaNombre,
    DemandaSerie,
    Empresa,
    EvaluacionModelo,
    OcLineaCa,
    ProductoAlias,
    ProductoCanonico,
    Pronostico,
    Proveedor,
    ProveedorProducto,
    Recomendacion,
    Usuario,
)

LECHE = "50131701"  # Lácteos y huevos
ASEO = "47131801"
ORG = "69.140.900-7"
ORG_OC = "69140900-7"  # la OC trae el RUT sin puntos
HOY = "2026-11"
AHORA = datetime(2026, 11, 20, 12, 0)  # UTC naive
_PW = "contraseña-segura-test"
_ids = iter(range(1, 10**7))


@pytest.fixture()
def engine():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)  # fmt: skip
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def s(engine):
    with Session(engine) as session:
        yield session


def _oc(s: Session, codigo: str, mes: str, *, rut_u: str = ORG_OC, unidad: str = "Salud",
        cod: str = LECHE, base: str | None = "L", cantidad: float = 10, precio: float = 1000,
        region: int = 16, espec: str = "leche entera", rut_p: str = "76111111-1",
        total: float | None = None, precio_base: float | None = None,
        fecha: datetime | None = None, id_oc: str | None = "") -> None:  # fmt: skip
    """`id_oc` = el `ID` numérico de oc-da: "" (default) = igual al código; None = línea sin `ID`."""
    y, m = (int(x) for x in mes.split("-"))
    s.add(OcLineaCa(
        codigo_oc=codigo, id_oc=codigo if id_oc == "" else id_oc, id_item=str(next(_ids)), mes=mes,
        fecha_creacion=fecha or datetime(y, m, 10), rut_unidad=rut_u, unidad_compra=unidad,
        organismo="Municipalidad de Chillán", comuna_compra="Chillán", region_compra=region,
        rut_proveedor=rut_p, proveedor="Alfa SpA", codigo_producto=cod,
        producto_generico="Leche" if cod == LECHE else "Detergente", grupo="Alimentos",
        especificacion=espec, cantidad=cantidad, unidad_medida="Litro", precio_neto=precio,
        total_linea_neto=cantidad * precio if total is None else total,
        precio_base=(precio if precio_base is None else precio_base) if base else None,
        base=base, moneda="CLP",
    ))  # fmt: skip
    s.flush()


def _ca(s: Session, codigo: str, mes: str, *, estado: str = "cerrada", id_oc: str | None = None,
        region: int = 16, unidad: str = "Salud", org: str = ORG) -> CompraAgil:  # fmt: skip
    cierre = idx.rango_mes_utc(mes)[0] + timedelta(days=5, hours=12)
    ca = CompraAgil(codigo=codigo, nombre=codigo, estado=estado, region=region,
                    organismo_rut=org, organismo_nombre="Municipalidad de Chillán",
                    unidad_compra=unidad, fecha_cierre=cierre, id_orden_compra=id_oc)  # fmt: skip
    s.add(ca)
    s.flush()
    return ca


def _producto(s: Session, nombre: str = "Leche entera", base: str = "L",
              alias: str = "leche entera") -> int:  # fmt: skip
    p = ProductoCanonico(nombre=nombre, unidad_base=base, unspsc_sugerido=LECHE,
                         familia="Lácteos y huevos", activo=True)  # fmt: skip
    s.add(p)
    s.flush()
    s.add(ProductoAlias(texto_normalizado=normalizar_texto(alias), producto_id=p.id,
                        fuente="humano", confianza=1.0))  # fmt: skip
    s.flush()
    return p.id


def _filas(s: Session, **filtro) -> list[DemandaSerie]:
    q = select(DemandaSerie)
    for k, v in filtro.items():
        q = q.where(getattr(DemandaSerie, k) == v)
    return list(s.execute(q).scalars())


def _recalcular(s: Session, desde: str, hasta: str, modo: str = dm.MODO_UNIDAD, regiones=(16,)):
    _, dir_ = dm.recorrer(s, list(regiones), HOY)
    return dm.recalcular_series(s, list(regiones), desde, hasta, modo, dir_, ahora=AHORA)


def _meses(desde: str, n: int) -> list[str]:
    return [idx.sumar_meses(desde, k) for k in range(n)]


# ---------------------------------------------------------------------------
# Claves y cantidades
# ---------------------------------------------------------------------------


def test_clave_del_comprador_y_su_modo():
    assert dm.clave_comprador(ORG, "Salud", dm.MODO_UNIDAD) == clave_unidad(ORG, "Salud")
    assert dm.clave_comprador(ORG, "Salud", dm.MODO_RUT) == "69140900-7"
    assert dm.clave_comprador(None, "Salud", dm.MODO_UNIDAD) == ""
    assert dm.modo_de_clave(clave_unidad(ORG, "DAEM")) == dm.MODO_UNIDAD
    assert dm.modo_de_clave("69140900-7") == dm.MODO_RUT


def test_cantidad_en_base():
    # 25 cajas de 0,17 kg a 1.100 c/u: precio_base = 1.100 / 0,17.
    pb = 1100 / 0.17
    assert dm.cantidad_en_base(25, 1100, 27500, pb) == pytest.approx(4.25)
    assert dm.cantidad_en_base(None, None, 27500, pb) == pytest.approx(4.25)  # sin cantidad
    assert dm.cantidad_en_base(25, 1100, 27500, None) is None


def test_mes_de_la_oc_es_la_fecha_local_de_creacion():
    assert dm.mes_oc(datetime(2026, 3, 31, 0, 0), "2026-05") == "2026-03"
    assert dm.mes_oc(None, "2026-05") == "2026-05"


# ---------------------------------------------------------------------------
# Medición (§1.0) y elección de la clave
# ---------------------------------------------------------------------------


def test_medicion_elige_clave_unidad_solo_con_evidencia(s):
    for i in range(dm.MIN_CA_CLAVE):
        _oc(s, f"OC{i}", "2026-10")
        _ca(s, f"CA{i}", "2026-10", id_oc=f"OC{i}")
    _oc(s, "OCX", "2026-10", rut_u="61000000-0", unidad="DAEM")
    _oc(s, "OCY", "2026-10", rut_u="61000000-0", unidad="Salud")
    s.commit()
    med, dir_ = dm.recorrer(s, [16], HOY)
    d = med.como_dict()
    assert d["c_cas_con_oc"] == dm.MIN_CA_CLAVE and d["c_pct_misma_clave_unidad"] == 1.0
    assert med.modo() == dm.MODO_UNIDAD
    assert d["b_ruts"] == 2 and d["b_ruts_con_varias_unidades"] == 1
    # (a): de 32 líneas, 30 son del RUT que también es organismo de alguna CA.
    assert d["a_lineas_oc"] == 32 and d["a_pct_rut_unidad_es_organismo_de_ca"] == pytest.approx(30 / 32)
    assert dir_.oc_a_ca["OC0"] == "CA0"


def test_medicion_sin_coincidencia_usa_el_rut(s):
    for i in range(dm.MIN_CA_CLAVE):
        _oc(s, f"OC{i}", "2026-10", unidad="Departamento de Salud Municipal")
        _ca(s, f"CA{i}", "2026-10", id_oc=f"OC{i}", unidad="Salud")
    s.commit()
    med, _ = dm.recorrer(s, [16], HOY)
    assert med.pct_misma_clave == 0.0 and med.modo() == dm.MODO_RUT


def test_medicion_con_pocas_ca_usa_el_rut(s):
    _oc(s, "OC1", "2026-10")
    _ca(s, "CA1", "2026-10", id_oc="OC1")
    s.commit()
    med, _ = dm.recorrer(s, [16], HOY)
    assert med.pct_misma_clave == 1.0 and med.modo() == dm.MODO_RUT  # < 30 CA medibles


# ---------------------------------------------------------------------------
# Series: tres niveles, bases, doble conteo y borrado acotado
# ---------------------------------------------------------------------------


def test_tres_niveles_de_producto(s):
    pid = _producto(s)
    _oc(s, "OC1", "2026-10", espec="leche entera", cantidad=10, precio=1000)
    _oc(s, "OC2", "2026-10", espec="leche con chocolate en polvo", cantidad=5, precio=1000)
    s.commit()
    r = _recalcular(s, "2026-10", "2026-10")
    clave = clave_unidad(ORG, "Salud")
    (prod,) = _filas(s, nivel="producto")
    assert (prod.comprador, prod.item, prod.base, prod.cantidad_base) == (clave, str(pid), "L", 10)
    (un,) = _filas(s, nivel="unspsc")
    assert (un.item, un.cantidad_base, un.n_oc, un.monto_neto) == (f"{LECHE}|L", 15, 2, 15000)
    (fam,) = _filas(s, nivel="familia")
    assert fam.item == "Lácteos y huevos" and fam.cantidad_base is None and fam.base is None
    assert fam.monto_neto == 15000 and fam.n_proveedores == 1
    cob = r["d_cobertura_monto"]
    assert cob["familia"] == 1.0 and cob["unspsc"] == 1.0 and cob["producto"] == pytest.approx(10 / 15, abs=1e-4)
    assert r["e_pct_cantidad_coincide"] == 1.0


def test_producto_exige_misma_unidad_base(s):
    _producto(s, base="kg")  # el catálogo dice kg y la OC viene en litros
    _oc(s, "OC1", "2026-10", espec="leche entera")
    s.commit()
    _recalcular(s, "2026-10", "2026-10")
    assert _filas(s, nivel="producto") == []


def test_cantidades_nunca_mezclan_bases(s):
    _oc(s, "OC1", "2026-10", cod=ASEO, base="kg", cantidad=3, precio=2000)
    _oc(s, "OC2", "2026-10", cod=ASEO, base="u", cantidad=7, precio=500)
    _oc(s, "OC3", "2026-10", cod=ASEO, base=None, cantidad=99, precio=10)  # sin base: solo familia
    s.commit()
    _recalcular(s, "2026-10", "2026-10")
    por_item = {f.item: f for f in _filas(s, nivel="unspsc")}
    assert set(por_item) == {f"{ASEO}|kg", f"{ASEO}|u"}
    assert por_item[f"{ASEO}|kg"].cantidad_base == 3 and por_item[f"{ASEO}|kg"].base == "kg"
    assert por_item[f"{ASEO}|u"].cantidad_base == 7 and por_item[f"{ASEO}|u"].base == "u"
    (fam,) = _filas(s, nivel="familia")
    assert fam.cantidad_base is None and fam.monto_neto == 6000 + 3500 + 990


def test_sin_doble_conteo_oc_ca_sin_oc_y_desierta(s):
    pid = _producto(s)
    # CA con OC cargada: cuenta la OC (10 L); sus líneas de CA NO suman.
    _oc(s, "OC1", "2026-10", cantidad=10)
    _ca(s, "CON-OC", "2026-10", id_oc="OC1")
    s.add(CaLinea(ca_codigo="CON-OC", n=1, producto_id=pid, cantidad=10, cantidad_base=10,
                  codigo_unspsc=LECHE, estado="ok", texto_origen="leche"))  # fmt: skip
    # CA cerrada sin OC cargada: cuentan sus líneas ok (4 L) con el precio de la seleccionada.
    _ca(s, "SIN-OC", "2026-10", id_oc="OC-NO-CARGADA")
    s.add(CaLinea(ca_codigo="SIN-OC", n=1, producto_id=pid, cantidad=4, cantidad_base=4,
                  codigo_unspsc=LECHE, estado="ok", texto_origen="leche"))  # fmt: skip
    s.add(CaLinea(ca_codigo="SIN-OC", n=2, producto_id=pid, cantidad=50, cantidad_base=50,
                  codigo_unspsc=LECHE, estado="pendiente", texto_origen="leche"))  # fmt: skip
    cot = CaCotizacion(ca_codigo="SIN-OC", llamado=1, rut_proveedor="76111111-1",
                       seleccionado=True, id_cotizacion="1")  # fmt: skip
    s.add(cot)
    s.flush()
    s.add(CaCotizacionProducto(cotizacion_id=cot.id, codigo_producto=LECHE, precio_unitario=900))
    # Desierta: no suma aunque tenga líneas ok.
    _ca(s, "DES", "2026-10", estado="desierta")
    s.add(CaLinea(ca_codigo="DES", n=1, producto_id=pid, cantidad=99, cantidad_base=99,
                  codigo_unspsc=LECHE, estado="ok", texto_origen="leche"))  # fmt: skip
    s.commit()
    r = _recalcular(s, "2026-10", "2026-10")
    (un,) = _filas(s, nivel="unspsc")
    assert un.cantidad_base == 14  # 10 de la OC + 4 de la CA sin OC
    assert un.n_oc == 1 and un.n_ca == 2  # la CA con OC y la CA sin OC
    assert un.monto_neto == 10_000 + 3_600
    (prod,) = _filas(s, nivel="producto")
    assert prod.cantidad_base == 14
    assert (r["cas_con_oc"], r["cas_sin_oc"], r["cas_desiertas_no_suman"]) == (1, 1, 1)
    assert r["lineas_ca_ok_sumadas"] == 1


def _cot(s: Session, ca: str, id_oc: str | None, *, seleccionado: bool) -> None:
    s.add(CaCotizacion(ca_codigo=ca, llamado=1, rut_proveedor="76111111-1", id_oc=id_oc,
                       seleccionado=seleccionado, id_cotizacion=str(next(_ids))))  # fmt: skip
    s.flush()


def test_oc_de_cas_usa_id_orden_compra_o_la_oferta_seleccionada(s):
    """F-plat-6-cruce: la ingesta vigente guarda la OC en `ca_cotizaciones.id_oc`."""
    _ca(s, "CON-IOC", "2026-10", id_oc=" oc1 ")
    _cot(s, "CON-IOC", "OTRA", seleccionado=True)  # id_orden_compra manda
    _ca(s, "POR-COT", "2026-10")
    _cot(s, "POR-COT", "OC2", seleccionado=True)
    _cot(s, "POR-COT", "OC-NO", seleccionado=False)  # no seleccionada: no cuenta
    _ca(s, "DOS-SEL", "2026-10")
    _cot(s, "DOS-SEL", "OC3", seleccionado=True)
    _cot(s, "DOS-SEL", "oc4", seleccionado=True)
    _ca(s, "NADA", "2026-10")
    _cot(s, "NADA", None, seleccionado=True)
    s.commit()
    cas = {"CON-IOC": " oc1 ", "POR-COT": None, "DOS-SEL": None, "NADA": None}
    assert dm.oc_de_cas(s, cas) == {
        "CON-IOC": {"OC1"}, "POR-COT": {"OC2"}, "DOS-SEL": {"OC3", "OC4"},
    }  # fmt: skip


def test_cruce_por_la_oc_de_la_oferta_seleccionada_sin_doble_conteo(s):
    """Cruce y doble conteo por el `ID` de oc-da en ambos lados (F-plat-6-cruce-2): el `codigo_oc`
    AG de la línea es distinto del `ID` que traen las CA."""
    pid = _producto(s)

    def _linea(ca: str, cantidad: float) -> None:
        s.add(CaLinea(ca_codigo=ca, n=1, producto_id=pid, cantidad=cantidad,
                      cantidad_base=cantidad, codigo_unspsc=LECHE, estado="ok",
                      texto_origen="leche"))  # fmt: skip

    # OC solo en ca_cotizaciones.id_oc (minúsculas y espacios: se normaliza) → cruza.
    _oc(s, "1234-5-AG26", "2026-10", cantidad=10, id_oc="55672374")
    _ca(s, "POR-COT", "2026-10")
    _cot(s, "POR-COT", " 55672374 ", seleccionado=True)
    _linea("POR-COT", 10)  # no suma: ya contó la OC
    # OC en compras_agiles.id_orden_compra (job viejo) → cruza.
    _oc(s, "1234-6-AG26", "2026-10", cantidad=20, id_oc="55672375")
    _ca(s, "POR-IOC", "2026-10", id_oc="55672375")
    _linea("POR-IOC", 20)
    # Sin ninguna OC → sin OC: suman sus líneas ok.
    _ca(s, "NADA", "2026-10")
    _linea("NADA", 4)
    s.commit()
    med, dir_ = dm.recorrer(s, [16], HOY)
    assert med.cas_con_oc == 2 and med.pct_misma_clave == 1.0
    assert dir_.oc_a_ca == {"55672374": "POR-COT", "55672375": "POR-IOC"}
    r = dm.recalcular_series(s, [16], "2026-10", "2026-10", dm.MODO_UNIDAD, dir_, ahora=AHORA)
    assert (r["cas_con_oc"], r["cas_sin_oc"], r["lineas_ca_ok_sumadas"]) == (2, 1, 1)
    (un,) = _filas(s, nivel="unspsc")
    assert un.cantidad_base == 34  # 10 + 20 de las OC + 4 de la CA sin OC; nunca 64
    assert un.n_oc == 2 and un.n_ca == 3


def test_sin_cruce_avisa_y_c_queda_en_none(s):
    # Líneas de OC sin `ID` (antes de la recarga, o fila sin la columna): no cruzan.
    _historia(s, meses=6)
    _ca(s, "SIN", "2026-10")
    _cot(s, "SIN", "55672374", seleccionado=True)
    s.commit()
    r = dm.run_demanda(s, [16], mes_hoy=HOY, ahora=AHORA)
    assert r["medicion"]["c_cas_con_oc"] == 0
    assert r["medicion"]["c_pct_misma_clave_unidad"] is None
    assert any("cruce CA↔OC no disponible" in a for a in r["avisos"])


def test_borrado_acotado_a_las_regiones_que_recalcula(s):
    _oc(s, "OC16", "2026-10")
    _oc(s, "OC5", "2026-10", region=5, rut_u="61000000-0")
    s.commit()
    _recalcular(s, "2026-10", "2026-10", regiones=(16, 5))
    assert {f.region for f in _filas(s, mes="2026-10")} == {16, 5}
    # Recalcular solo Ñuble no toca Valparaíso (no repetir el error de recalcular_competencia).
    _recalcular(s, "2026-10", "2026-10", regiones=(16,))
    assert {f.region for f in _filas(s, mes="2026-10")} == {16, 5}
    assert len(_filas(s, mes="2026-10", region=5)) == 2  # unspsc + familia, intactas


def test_recalculo_idempotente(s):
    _oc(s, "OC1", "2026-10")
    _oc(s, "OC2", "2026-09")
    s.commit()
    _recalcular(s, "2026-09", "2026-10")
    antes = sorted((f.comprador, f.nivel, f.item, f.mes, f.cantidad_base) for f in _filas(s))
    _recalcular(s, "2026-09", "2026-10")
    despues = sorted((f.comprador, f.nivel, f.item, f.mes, f.cantidad_base) for f in _filas(s))
    assert antes == despues and len(despues) == 4


def test_otra_moneda_no_suma(s):
    _oc(s, "OC1", "2026-10")
    s.execute(OcLineaCa.__table__.update().values(moneda="UTM"))
    s.commit()
    r = _recalcular(s, "2026-10", "2026-10")
    assert _filas(s) == [] and r["lineas_oc_otra_moneda"] == 1


# ---------------------------------------------------------------------------
# Pronóstico v1
# ---------------------------------------------------------------------------


def _serie(meses: list[str], valor: float = 100.0) -> dict[str, float]:
    return {m: valor for m in meses}


def test_compra_cada_mes():
    v = _serie(_meses("2024-11", 24))  # nov-2024 a oct-2026
    (f,) = dm.pronosticar(v, HOY, [HOY])
    assert f.metodo == "v1" and f.p_compra >= dm.P_COMPRA
    assert f.cantidad == pytest.approx(100) and f.intervalo == (pytest.approx(100), pytest.approx(100))
    assert f.compra_base and not f.debil


def test_compra_cada_dos_meses():
    v = _serie(_meses("2024-11", 24)[::2])  # nov, ene, mar, ...
    sigue = dm.pronosticar(v, HOY, [HOY, "2026-12"])
    assert sigue[0].p_compra >= dm.P_COMPRA  # noviembre: compró los dos noviembres
    assert sigue[1].p_compra < 0.3  # diciembre: nunca compró en diciembre


def test_solo_marzo_y_septiembre():
    v = {"2024-03": 300.0, "2024-09": 100.0, "2025-03": 300.0, "2025-09": 100.0, "2026-03": 300.0,
         "2026-09": 100.0}  # fmt: skip
    marzo, abril = dm.pronosticar(v, "2027-03", ["2027-03", "2027-04"])
    assert marzo.p_compra >= dm.P_COMPRA and abril.p_compra < 0.2
    # Estacionalidad: marzo compra el triple que septiembre (promedio 200 → 300).
    assert marzo.cantidad == pytest.approx(300)
    (sept,) = dm.pronosticar(v, "2026-11", ["2027-09"])
    assert sept.p_compra >= dm.P_COMPRA and sept.cantidad == pytest.approx(100)


def test_una_sola_compra_e_insuficiente():
    (f,) = dm.pronosticar({"2026-03": 50.0}, HOY, [HOY])
    assert f.metodo == "insuficiente" and f.p_compra is None and f.cantidad is None
    (f,) = dm.pronosticar(_serie(["2026-07", "2026-08", "2026-09"]), HOY, [HOY])
    assert f.metodo == "insuficiente" and f.n_meses_con_compra == 3
    (f,) = dm.pronosticar(_serie(["2026-06", "2026-07", "2026-08", "2026-09"]), HOY, [HOY])
    assert f.metodo == "v1"


def test_debil_con_un_solo_anio():
    v = _serie(_meses("2026-01", 10))  # ene a oct de 2026
    (f,) = dm.pronosticar(v, HOY, [HOY])
    assert f.debil  # noviembre nunca se observó


def test_pronostico_no_mira_el_mes_evaluado_ni_posteriores():
    v = _serie(_meses("2024-11", 24))
    base = dm.pronosticar(v, "2026-10", ["2026-10"])
    trampa = {**v, "2026-10": 1e9, "2026-11": 1e9, "2027-01": 1e9}
    assert dm.pronosticar(trampa, "2026-10", ["2026-10"]) == base
    sin_octubre = {m: x for m, x in v.items() if m != "2026-10"}
    assert dm.pronosticar(sin_octubre, "2026-10", ["2026-10"]) == base


def test_linea_base():
    # Compró en noviembre del año pasado, no el mes pasado.
    v = {"2025-11": 70.0, "2026-01": 10.0, "2026-04": 10.0, "2026-07": 10.0}
    (f,) = dm.pronosticar(v, HOY, [HOY])
    assert f.compra_base and f.cantidad_linea_base == 70.0
    v2 = {"2026-01": 10.0, "2026-04": 10.0, "2026-07": 10.0, "2026-10": 20.0}
    (f2,) = dm.pronosticar(v2, HOY, [HOY])
    assert f2.compra_base and f2.cantidad_linea_base == 20.0  # el mes pasado
    (f3,) = dm.pronosticar({"2026-01": 10.0, "2026-04": 10.0, "2026-07": 10.0, "2026-08": 1.0},
                           HOY, [HOY])  # fmt: skip
    assert not f3.compra_base


def test_tendencia_acotada():
    v = {**_serie(_meses("2025-10", 9), 10.0), **_serie(["2026-08", "2026-09", "2026-10"], 1000.0)}
    (f,) = dm.pronosticar(v, HOY, [HOY])
    promedio = sum(v.values()) / len(v)
    assert f.cantidad <= promedio * dm.TOPE_MAX * dm.TOPE_MAX + 1e-6


# ---------------------------------------------------------------------------
# Juez: backtest
# ---------------------------------------------------------------------------


def _series(**valores: dict[str, float]) -> dict[dm.Clave, dm.Serie]:
    out = {}
    for k, v in valores.items():
        out[(k, "unspsc", f"{LECHE}|L")] = dm.Serie(16, "L", dict(v), dict(v))
    return out


def test_backtest_no_mira_el_mes_evaluado():
    mensual = _serie(_meses("2024-11", 23))  # nov-2024 a sep-2026; NO compró en octubre
    series = _series(a=mensual)
    r = dm.backtest(series, ["2026-10"])["2026-10"]["unspsc"]
    assert (r.tp, r.fp, r.fn) == (0, 1, 0)  # predijo compra y no compró
    # Agregar compras POSTERIORES a octubre no cambia la predicción de octubre.
    series2 = _series(a={**mensual, "2026-11": 5.0, "2026-12": 5.0})
    r2 = dm.backtest(series2, ["2026-10"])["2026-10"]["unspsc"]
    assert (r2.tp, r2.fp, r2.fn, r2.tp_base, r2.fp_base) == (r.tp, r.fp, r.fn, r.tp_base, r.fp_base)


def test_backtest_cuenta_y_linea_base_en_la_misma_poblacion():
    series = _series(
        mensual=_serie(_meses("2024-11", 24)),  # compra en octubre: tp para ambos
        corta=_serie(["2026-08", "2026-09"]),  # < 4 meses: fuera de la población
        marzo={"2024-03": 1.0, "2024-09": 1.0, "2025-03": 1.0, "2025-09": 1.0, "2026-10": 1.0},
    )
    r = dm.backtest(series, ["2026-10"])["2026-10"]["unspsc"]
    assert r.elegibles == 2
    assert (r.tp, r.fn) == (1, 1)  # «marzo» compró en octubre sin aviso
    assert (r.tp_base, r.fn_base) == (1, 1)
    assert r.pares_mape == 2


def test_guardar_backtest_no_borra_otros_modelos(s):
    for modelo in ("indice", "base", "logistico", "competencia"):
        s.add(EvaluacionModelo(mes="2026-10", modelo=modelo, metrica="x", valor=1.0, n=1))
    s.commit()
    series = _series(a=_serie(_meses("2024-11", 24)))
    dm.guardar_backtest(s, dm.backtest(series, ["2026-10"]), AHORA)
    dm.guardar_backtest(s, dm.backtest(series, ["2026-10"]), AHORA)  # reevaluar
    filas = s.execute(select(EvaluacionModelo.modelo, func.count()).group_by(EvaluacionModelo.modelo)).all()
    por = dict(filas)
    assert por == {"indice": 1, "base": 1, "logistico": 1, "competencia": 1,
                   "demanda": len(dm.NIVELES) * 7}  # fmt: skip
    # mercado-indices tampoco borra las de demanda al reevaluar un mes.
    assert ev.MODELO_DEMANDA in ev.MODELOS_AJENOS


def test_veredicto_exige_meta_y_ganarle_a_la_base(s):
    series = _series(a=_serie(_meses("2024-11", 24)), b=_serie(_meses("2024-11", 24)))
    dm.guardar_backtest(s, dm.backtest(series, ["2026-10"]), AHORA)
    v = dm.veredicto(s)["unspsc"]
    assert v.cumple_meta and v.base_cumple_meta
    assert not v.gana  # empatan en F1: no le gana → se muestra la base
    assert dm.veredicto(s)["familia"].elegibles == 0 and not dm.veredicto(s)["familia"].gana
    v2 = dm.Veredicto("unspsc", (), 10, {"precision": 0.8, "recall": 0.6, "f1": 0.68,
                                         "precision_base": 0.5, "recall_base": 0.9, "f1_base": 0.64})  # fmt: skip
    assert v2.gana
    v3 = dm.Veredicto("unspsc", (), 10, {"precision": 0.65, "recall": 0.9, "f1": 0.75,
                                         "precision_base": 0.3, "recall_base": 0.3, "f1_base": 0.3})  # fmt: skip
    assert not v3.gana  # no cumple la meta de precisión


# ---------------------------------------------------------------------------
# Job de punta a punta
# ---------------------------------------------------------------------------


def _historia(s: Session, meses: int = 24, region: int = 16, rut_u: str = ORG_OC) -> None:
    for i, m in enumerate(_meses("2024-11", meses)):
        _oc(s, f"OC-{rut_u}-{i}", m, cantidad=100, precio=1000, region=region, rut_u=rut_u,
            id_oc=None)  # fmt: skip
    s.commit()


def test_job_de_punta_a_punta_e_idempotente(s):
    _historia(s)
    s.add(Empresa(nombre="E", regiones_prioritarias=[16]))
    s.commit()
    r = ab.run_mercado_demanda(s, [16], mes_hoy=HOY, ahora=AHORA)
    assert r["medicion"]["clave_elegida"] == dm.MODO_RUT  # sin CA: no hay evidencia
    assert r["series"]["meses"][0] == "2024-11"  # primera corrida: todo
    assert r["backtest"]["unspsc"]["elegibles"] > 0
    assert r["pronosticos"]["filas"] > 0
    # F-plat-6-fix: tiempo por etapa en el resultado (y un log INFO por etapa).
    assert set(r["segundos_por_etapa"]) == {"recorrido", "series", "nombres", "carga_series",
                                            "backtest", "pronosticos", "recomendaciones"}  # fmt: skip
    assert r["duracion_s"] >= sum(r["segundos_por_etapa"].values()) - 0.5
    n_series = s.execute(select(func.count()).select_from(DemandaSerie)).scalar_one()
    n_pron = s.execute(select(func.count()).select_from(Pronostico)).scalar_one()
    n_rec = s.execute(select(func.count()).select_from(Recomendacion)).scalar_one()
    assert n_rec > 0
    r2 = ab.run_mercado_demanda(s, [16], mes_hoy=HOY, ahora=AHORA + timedelta(hours=1))
    assert r2["series"]["meses"] == ["2026-09", "2026-10", "2026-11"]  # diario: 3 meses
    assert s.execute(select(func.count()).select_from(DemandaSerie)).scalar_one() == n_series
    assert s.execute(select(func.count()).select_from(Pronostico)).scalar_one() == n_pron
    assert s.execute(select(func.count()).select_from(Recomendacion)).scalar_one() == n_rec
    (rec_stats,) = r2["recomendaciones"].values()
    assert rec_stats["nuevas"] == 0 and rec_stats["actualizadas"] == n_rec
    nombres = {n.clave: n for n in s.execute(select(DemandaNombre)).scalars()}
    assert nombres["69140900-7"].nombre == "Municipalidad de Chillán"
    assert nombres["69140900-7"].datos["comuna"] == "Chillán"
    assert nombres[f"unspsc:{LECHE}"].nombre == "Leche"


def test_cambio_de_clave_recalcula_todo(s):
    _historia(s, meses=6)
    s.add(DemandaSerie(comprador=clave_unidad(ORG, "Salud"), nivel="familia", item="x",
                       mes="2025-01", region=16, monto_neto=1))  # fmt: skip
    s.commit()
    r = dm.run_demanda(s, [16], mes_hoy=HOY, ahora=AHORA)  # la medición dice RUT
    assert any("clave del comprador" in a for a in r["avisos"])
    assert r["series"]["meses"][0] <= "2024-11"
    claves = {f.comprador for f in _filas(s)}
    assert claves == {"69140900-7"}


# ---------------------------------------------------------------------------
# Recomendaciones
# ---------------------------------------------------------------------------


def _empresa(s: Session, *, regiones=(16,), comunas=()) -> int:
    e = Empresa(nombre=f"E{next(_ids)}", regiones_prioritarias=list(regiones),
                comunas_prioritarias=list(comunas))  # fmt: skip
    s.add(e)
    s.commit()
    return e.id


def _pron(s: Session, comprador: str, *, item: str = f"{LECHE}|L", nivel: str = "unspsc",
          base: str | None = "L", meses=(HOY, "2026-12", "2027-01"), cantidad: float = 120.0,
          monto: float = 120_000.0, region: int = 16) -> None:  # fmt: skip
    for m in meses:
        s.add(Pronostico(comprador=comprador, nivel=nivel, item=item, region=region, base=base,
                         mes_objetivo=m, p_compra=0.9, cantidad_esperada=cantidad,
                         monto_esperado=monto, compra_base=True, cantidad_linea_base=cantidad,
                         monto_linea_base=monto, n_meses_con_compra=12, metodo="v1",
                         generado_en=AHORA))  # fmt: skip
    s.flush()


def _nombre_comprador(s: Session, clave: str, nombre: str, comuna: str) -> None:
    s.add(DemandaNombre(tipo="comprador", clave=clave, nombre=nombre, region=16,
                        datos={"comuna": comuna, "organismo_rut": clave}))  # fmt: skip
    s.flush()


def _proveedor(s: Session, empresa_id: int, precio: float = 800.0) -> ProveedorProducto:
    p = Proveedor(empresa_id=empresa_id, nombre="Lácteos Sur", activo=True)
    s.add(p)
    s.flush()
    pp = ProveedorProducto(proveedor_id=p.id, producto_texto="Leche", codigo_unspsc=LECHE,
                           unidad_base="L", factor_base=1.0, precio_neto_actual=precio,
                           vigente_desde=date(2026, 11, 1))  # fmt: skip
    s.add(pp)
    s.flush()
    return pp


def test_recomendaciones_con_y_sin_proveedor(s):
    con, sin = _empresa(s), _empresa(s)
    _proveedor(s, con)
    _nombre_comprador(s, "A", "Hospital A", "Chillán")
    _pron(s, "A")  # 3 × 120 L = 360 L ≥ 300 L
    s.add(DemandaSerie(comprador="A", nivel="unspsc", item=f"{LECHE}|L", mes="2026-10", region=16,
                       monto_neto=1, precio_mediana=1000.0, n_oc=3))  # fmt: skip
    s.commit()
    ab.recomendar(s, con, mes_hoy=HOY, ver={}, ahora=AHORA)
    ab.recomendar(s, sin, mes_hoy=HOY, ver={}, ahora=AHORA)
    pf = s.execute(select(Recomendacion).where(Recomendacion.empresa_id == con,
                                               Recomendacion.tipo == "precio_firme")).scalar_one()  # fmt: skip
    assert "Lácteos Sur" in pf.texto and "360 L" in pf.texto
    assert pf.datos["costo_unitario"] == 800 and pf.datos["precio_referencia"] == 1000
    assert pf.datos["margen_pct"] == pytest.approx(0.2) and pf.datos["metodo"] == "linea_base"
    hueco = s.execute(select(Recomendacion).where(Recomendacion.empresa_id == sin,
                                                  Recomendacion.tipo == "hueco_proveedor")).scalar_one()  # fmt: skip
    assert "Buscar proveedor" in hueco.texto
    tipos_sin = {r.tipo for r in s.execute(select(Recomendacion).where(
        Recomendacion.empresa_id == sin)).scalars()}  # fmt: skip
    assert "precio_firme" not in tipos_sin and "proxima_compra" in tipos_sin


def test_bajo_el_umbral_no_hay_precio_firme(s):
    e = _empresa(s)
    _proveedor(s, e)
    _pron(s, "A", cantidad=50)  # 150 L < 300 L
    s.commit()
    ab.recomendar(s, e, mes_hoy=HOY, ver={}, ahora=AHORA)
    tipos = {r.tipo for r in s.execute(select(Recomendacion)).scalars()}
    assert tipos == {"proxima_compra"}
    ab.recomendar(s, e, mes_hoy=HOY, ver={}, ahora=AHORA + timedelta(hours=1),
                  umbrales={"L": 100, "kg": 1, "u": 1})  # fmt: skip
    tipos = {r.tipo for r in s.execute(select(Recomendacion)).scalars()}
    assert "precio_firme" in tipos


def test_upsert_conserva_el_estado(s):
    e = _empresa(s)
    _pron(s, "A")
    s.commit()
    r1 = ab.recomendar(s, e, mes_hoy=HOY, ver={}, ahora=AHORA)
    assert r1["nuevas"] == r1["generadas"] > 0
    hueco = s.execute(select(Recomendacion).where(Recomendacion.tipo == "hueco_proveedor")).scalar_one()
    hecha = s.execute(select(Recomendacion).where(Recomendacion.tipo == "proxima_compra")).scalar_one()
    ab.cambiar_estado(hueco, "descartada", 1)
    ab.cambiar_estado(hecha, "hecha", 1)
    s.commit()
    r2 = ab.recomendar(s, e, mes_hoy=HOY, ver={}, ahora=AHORA + timedelta(hours=1))
    assert r2["nuevas"] == 0 and r2["actualizadas"] == r1["generadas"]
    s.expire_all()
    assert s.get(Recomendacion, hueco.id).estado == "descartada"  # no resucita
    assert s.get(Recomendacion, hecha.id).estado == "hecha"
    assert s.execute(select(func.count()).select_from(Recomendacion)).scalar_one() == r1["generadas"]


def test_recomendacion_obsoleta_se_borra_salvo_la_cerrada(s):
    e = _empresa(s)
    _pron(s, "A")
    _pron(s, "B", item=f"{ASEO}|u", base="u", cantidad=1000)
    s.commit()
    ab.recomendar(s, e, mes_hoy=HOY, ver={}, ahora=AHORA)
    hecha = s.execute(select(Recomendacion).where(Recomendacion.comprador == "B")).scalar_one()
    ab.cambiar_estado(hecha, "hecha", 1)
    s.execute(Pronostico.__table__.delete().where(Pronostico.comprador == "B"))
    s.commit()
    ab.recomendar(s, e, mes_hoy=HOY, ver={}, ahora=AHORA + timedelta(hours=1))
    items = {(r.item, r.estado) for r in s.execute(select(Recomendacion)).scalars()}
    assert (f"{ASEO}|u", "hecha") in items  # la hecha queda
    assert not any(i == f"{ASEO}|u" and est == "nueva" for i, est in items)


def test_comunas_prioritarias_primero(s):
    e = _empresa(s, comunas=["Bulnes"])
    _nombre_comprador(s, "GRANDE", "Hospital de Chillán", "Chillán")
    _nombre_comprador(s, "CHICO", "Posta de Bulnes", "Bulnes")
    _pron(s, "GRANDE", monto=900_000)
    _pron(s, "CHICO", monto=1_000)
    s.commit()
    ab.recomendar(s, e, mes_hoy=HOY, ver={}, ahora=AHORA)
    prox = ab.recomendaciones_ui(s, e)["proxima_compra"]
    assert [r.comprador for r in prox] == ["CHICO", "GRANDE"]
    assert prox[0].datos["prioritaria"] is True


def test_recomendaciones_solo_en_regiones_de_la_empresa(s):
    e = _empresa(s, regiones=(5,))
    _pron(s, "A")  # región 16
    s.commit()
    r = ab.recomendar(s, e, mes_hoy=HOY, ver={}, ahora=AHORA)
    assert r["generadas"] == 0


def test_demanda_desierta(s):
    e = _empresa(s)
    for mes in ("2026-08", "2026-10"):
        s.add(CompetenciaCompradorMes(mes=mes, region=16, clave_comprador="X|salud",
                                      organismo_rut="X", comprador_nombre="Hospital X",
                                      familia="Aseo", n_ca=2, n_desiertas=1))  # fmt: skip
    s.add(CompetenciaCompradorMes(mes="2026-10", region=16, clave_comprador="Y|daem",
                                  organismo_rut="Y", comprador_nombre="DAEM Y", familia="Aseo",
                                  n_ca=1, n_desiertas=1))  # fmt: skip
    s.commit()
    ab.recomendar(s, e, mes_hoy=HOY, ver={}, ahora=AHORA)
    (r,) = ab.recomendaciones_ui(s, e)["demanda_desierta"]
    assert r.comprador == "X|salud" and r.item == "Aseo" and "2 CA" in r.texto


def test_con_v1_usa_p_por_cantidad(s):
    e = _empresa(s)
    _pron(s, "A", cantidad=200)  # p 0,9 × 200 × 3 = 540 L
    s.commit()
    ver = {"unspsc": dm.Veredicto("unspsc", (), 1, {"precision": 0.9, "recall": 0.9, "f1": 0.9,
                                                    "precision_base": 0.5, "recall_base": 0.5,
                                                    "f1_base": 0.5})}  # fmt: skip
    ab.recomendar(s, e, mes_hoy=HOY, ver=ver, ahora=AHORA)
    hueco = s.execute(select(Recomendacion).where(Recomendacion.tipo == "hueco_proveedor")).scalar_one()
    assert hueco.cantidad == pytest.approx(540) and hueco.datos["metodo"] == "v1"


# ---------------------------------------------------------------------------
# Rutas y ownership
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings():
    return Settings(mp_ticket="TICKET_TEST", database_url="sqlite:///:memory:",
                    secret_key="secret-test-key-larga-32chars!!", jobs_token="jobs-token-secreto")  # fmt: skip


@pytest.fixture()
def client(engine, settings):
    return TestClient(create_app(settings, engine))


def _usuario(engine, empresa_id: int | None) -> int:
    with Session(engine) as s:
        u = Usuario(email=f"u{next(_ids)}@t.cl", password_hash=hash_password(_PW),
                    rol=RolUsuario.USUARIO, activo=True, empresa_id=empresa_id)  # fmt: skip
        s.add(u)
        s.commit()
        return u.id


def _auth(settings, uid):
    token = create_session_token(settings.secret_key, uid)
    nonce = decode_session_token(settings.secret_key, token)[1]
    return {COOKIE_NAME: token}, generate_csrf_token(settings.secret_key, nonce)


@pytest.fixture()
def esc(engine):
    with Session(engine) as s:
        _historia(s)
        a = _empresa(s, comunas=["Chillán"])
        b = _empresa(s)
        r = ab.run_mercado_demanda(s, [16], mes_hoy=idx.mes_actual(), ahora=AHORA)
        assert r["recomendaciones"]
        rec_a = s.execute(select(Recomendacion.id).where(Recomendacion.empresa_id == a)).scalars().first()
    return {"a": a, "b": b, "rec_a": rec_a}


def test_paginas_de_abastecimiento(client, engine, settings, esc):
    cookies, _ = _auth(settings, _usuario(engine, esc["a"]))
    for tab in ("recomendaciones", "calendario", "compradores"):
        r = client.get("/abastecimiento", params={"tab": tab}, cookies=cookies)
        assert r.status_code == 200, tab
        assert "Fuente: Dirección ChileCompra" in r.text
    r = client.get("/abastecimiento", params={"tab": "compradores", "comprador": "69140900-7"},
                   cookies=cookies)  # fmt: skip
    assert r.status_code == 200 and "Municipalidad de Chillán" in r.text
    r = client.get("/abastecimiento", params={"tab": "item", "nivel": "unspsc", "item": f"{LECHE}|L"},
                   cookies=cookies)  # fmt: skip
    assert r.status_code == 200 and "<svg" in r.text and "Estacionalidad" in r.text
    r = client.get("/abastecimiento", params={"tab": "calendario", "nivel": "familia"}, cookies=cookies)
    assert r.status_code == 200
    # Sin series en el territorio → 404 (no se filtra nada de otras regiones).
    assert client.get("/abastecimiento", params={"tab": "compradores", "comprador": "NO"},
                      cookies=cookies).status_code == 404  # fmt: skip
    assert client.get("/abastecimiento", params={"tab": "item", "item": "NO"},
                      cookies=cookies).status_code == 404  # fmt: skip
    # Sin backtest que le gane a la base, la página lo dice.
    r = client.get("/abastecimiento", cookies=cookies)
    assert "línea base" in r.text


def test_menu_incluye_abastecimiento(client, engine, settings, esc):
    cookies, _ = _auth(settings, _usuario(engine, esc["a"]))
    assert 'href="/abastecimiento"' in client.get("/cuenta", cookies=cookies).text


def test_sin_empresa_y_sin_login(client, engine, settings):
    cookies, _ = _auth(settings, _usuario(engine, None))
    r = client.get("/abastecimiento", cookies=cookies)
    assert r.status_code == 200 and "todavía no tiene una empresa" in r.text
    r = client.get("/abastecimiento", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]


def test_marcar_recomendacion_ownership_y_csrf(client, engine, settings, esc):
    ca, csrf_a = _auth(settings, _usuario(engine, esc["a"]))
    cb, csrf_b = _auth(settings, _usuario(engine, esc["b"]))
    url = f"/abastecimiento/recomendaciones/{esc['rec_a']}"
    # Sin CSRF → 403.
    assert client.post(url, data={"estado": "hecha"}, cookies=ca).status_code == 403
    # Otra empresa → 404 (indistinguible de inexistente).
    r = client.post(url, data={"estado": "hecha", "csrf_token": csrf_b}, cookies=cb)
    assert r.status_code == 404
    # Estado inválido → 422.
    r = client.post(url, data={"estado": "borrada", "csrf_token": csrf_a}, cookies=ca)
    assert r.status_code == 422
    # Dueña → hecha; htmx recibe 200 vacío (la fila sale del DOM).
    r = client.post(url, data={"estado": "hecha", "csrf_token": csrf_a}, cookies=ca,
                    headers={"HX-Request": "true"})  # fmt: skip
    assert r.status_code == 200 and r.text == ""
    with Session(engine) as s:
        rec = s.get(Recomendacion, esc["rec_a"])
        assert rec.estado == "hecha" and rec.usuario_id is not None
    # La otra empresa no ve las recomendaciones de la primera.
    texto_b = client.get("/abastecimiento", cookies=cb).text
    assert f"/abastecimiento/recomendaciones/{esc['rec_a']}" not in texto_b
