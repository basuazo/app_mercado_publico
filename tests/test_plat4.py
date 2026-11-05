"""F-plat-4: índice de precios, probabilidad de adjudicación y evaluación walk-forward.

Todo offline (SQLite en memoria). Datos sintéticos con signo conocido: el juez de verdad son las
adjudicaciones reales que corre Boris (`mercado-indices --completo`).
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.catalogos.territorio_nuble import comuna_canonica, comuna_en_texto, provincia_de
from app.mercado import adjudicacion as adj
from app.mercado import evaluacion as ev
from app.mercado import indice as idx
from app.models.base import Base
from app.models.tables import (
    CaAnexo,
    CaCotizacion,
    CaCotizacionProducto,
    CaLinea,
    CaProducto,
    CompraAgil,
    Empresa,
    EvaluacionModelo,
    ModeloAdjudicacion,
    OcLineaCa,
    PrecioIndice,
    PrecioReferencia,
    ProductoCanonico,
    Proveedor,
    ProveedorProducto,
    TarifaLogistica,
)

UNSPSC = "50131701"  # leche (familia 5013, alimentos)


@pytest.fixture()
def engine():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)  # fmt: skip
    Base.metadata.create_all(e)
    yield e


@pytest.fixture()
def s(engine):
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Constructores
# ---------------------------------------------------------------------------


def _producto(s: Session, nombre: str = "Leche entera", ub: str = "L", unspsc: str | None = UNSPSC):
    p = ProductoCanonico(nombre=nombre, unidad_base=ub, unspsc_sugerido=unspsc, activo=True)
    s.add(p)
    s.flush()
    return p


def _cierre(mes: str, dia: int = 10) -> datetime:
    return idx.rango_mes_utc(mes)[0] + timedelta(days=dia, hours=12)


def _ca(s: Session, codigo: str, mes: str = "2026-08", *, region: int = 16,
        organismo: str = "Municipalidad de Chillán", org_rut: str = "69.140.900-7",
        direccion: str | None = None, detalle: str = "completo", productos=((UNSPSC, "Leche"),),
        publicada: datetime | None = None) -> CompraAgil:  # fmt: skip
    ca = CompraAgil(
        codigo=codigo, nombre=codigo, estado="cerrada", region=region, organismo_nombre=organismo,
        organismo_rut=org_rut, unidad_compra="Depto. Salud", direccion_entrega=direccion,
        fecha_cierre=_cierre(mes), fecha_publicacion=publicada or _cierre(mes, 1),
        detalle_lineas=detalle,
    )  # fmt: skip
    s.add(ca)
    for cod, nombre in productos:
        s.add(
            CaProducto(
                ca_codigo=codigo, codigo_producto=cod, nombre=nombre, cantidad=10, unidad="Litro"
            )
        )
    s.flush()
    return ca


def _linea(s: Session, ca: str, n: int, producto: ProductoCanonico | None, codigo: str | None = UNSPSC,
           *, cantidad: float = 10, cantidad_base: float = 10, estado: str = "ok",
           anexo_id: int | None = None) -> CaLinea:  # fmt: skip
    ln = CaLinea(
        ca_codigo=ca, n=n, producto_id=producto.id if producto else None, cantidad=cantidad,
        cantidad_base=cantidad_base, texto_origen="x", codigo_unspsc=codigo, estado=estado,
        origen="anexo" if anexo_id else "api", anexo_id=anexo_id,
    )  # fmt: skip
    s.add(ln)
    s.flush()
    return ln


_ids = iter(range(1, 10**7))


def _oferta(s: Session, ca: str, rut: str, sel: bool | None, valor: float,
            productos=((UNSPSC, None),), emt: bool | None = None) -> CaCotizacion:  # fmt: skip
    c = CaCotizacion(ca_codigo=ca, llamado=1, rut_proveedor=rut, seleccionado=sel, valor_neto=valor,
                     es_emt=emt, id_cotizacion=str(next(_ids)))  # fmt: skip
    s.add(c)
    s.flush()
    for cod, precio in productos:
        s.add(CaCotizacionProducto(cotizacion_id=c.id, codigo_producto=cod, cantidad=10,
                                   precio_unitario=precio if precio is not None else valor / 10))  # fmt: skip
    s.flush()
    return c


# ---------------------------------------------------------------------------
# Territorio y comuna de una CA
# ---------------------------------------------------------------------------


def test_mapa_de_comunas_y_alias():
    assert comuna_canonica("CHILLAN VIEJO") == "Chillán Viejo"
    assert comuna_canonica("Trehuaco") == "Treguaco"
    assert provincia_de("trehuaco") == "Itata"
    assert provincia_de("Ñiquén") == "Punilla"
    assert provincia_de("Concepción") is None
    # La más larga gana: "Chillán Viejo" no es "Chillán".
    assert comuna_en_texto("Av. O'Higgins 123, Chillán Viejo") == "Chillán Viejo"
    assert comuna_en_texto("Escuela de San Carlos") == "San Carlos"
    assert comuna_en_texto("Pintura y aseo") is None


def test_comuna_de_ca_los_cuatro_casos():
    oc = {"69140900-7": "Chillán"}
    # 1) Por OC del mismo comprador (RUT con o sin puntos).
    assert idx.comuna_de_ca("69.140.900-7", "Liceo de Coihueco", None, oc) == ("Chillán", "oc")
    # 2) Por texto: la dirección de entrega manda sobre el organismo.
    assert idx.comuna_de_ca("1-9", "Municipalidad de Coihueco", "Calle 1, Yungay", oc) == (
        "Yungay",
        "texto",
    )
    # 3) Por texto en el organismo.
    assert idx.comuna_de_ca(None, "Municipalidad de Coihueco", None, oc) == ("Coihueco", "texto")
    # 4) Sin nada: None (la referencia cae a provincia o región).
    assert idx.comuna_de_ca("1-9", "Servicio de Salud", "Bodega central", oc) == (None, None)


def test_inferir_comunas_persiste_sin_tocar_actualizado_en(s):
    a = _ca(s, "CA-1", organismo="Hospital", org_rut="61.000.000-1")
    b = _ca(s, "CA-2", organismo="Municipalidad de Quirihue", org_rut="2-7")
    c = _ca(s, "CA-3", organismo="Servicio", org_rut="3-5")
    s.add(OcLineaCa(codigo_oc="OC-1", id_item="1", mes="2026-07", rut_unidad="61000000-1",
                    comuna_compra="CHILLAN", region_compra=16, grupo="g"))  # fmt: skip
    s.commit()
    antes = {x.codigo: x.actualizado_en for x in (a, b, c)}
    stats = idx.inferir_comunas(s, [16], todas=True)
    s.expire_all()
    assert (s.get(CompraAgil, "CA-1").comuna_inferida, s.get(CompraAgil, "CA-1").comuna_fuente) == (
        "Chillán",
        "oc",
    )
    assert (s.get(CompraAgil, "CA-2").comuna_inferida, s.get(CompraAgil, "CA-2").comuna_fuente) == (
        "Quirihue",
        "texto",
    )
    assert s.get(CompraAgil, "CA-3").comuna_inferida is None
    assert stats["comuna_oc"] == 1 and stats["comuna_texto"] == 1 and stats["sin_comuna"] == 1
    for cod, t in antes.items():
        assert s.get(CompraAgil, cod).actualizado_en == t  # no cuenta como cambio de la CA


# ---------------------------------------------------------------------------
# Unión ofertas × líneas e índice
# ---------------------------------------------------------------------------


def test_union_descarta_lineas_ambiguas_y_de_anexo_y_aplica_regla_de_eleccion(s):
    p1, p2, p3, p4 = (_producto(s, f"P{i}") for i in range(4))
    _ca(s, "CA-U", productos=(("A1", "a"), ("B2", "b"), ("B2", "b2")))
    s.add(CaAnexo(id=1, ca_codigo="CA-U", mime="application/pdf", tamano=1, sha256="x"))
    _linea(s, "CA-U", 1, p1, "A1", cantidad=10, cantidad_base=5)  # 2 unidades por... factor 2
    _linea(s, "CA-U", 2, p2, "B2")
    _linea(s, "CA-U", 3, p3, "B2")  # comparte código con la 2: ambas fuera
    _linea(s, "CA-U", 1000, p4, None, anexo_id=1)  # leída de un anexo: fuera, contada aparte
    _oferta(s, "CA-U", "1-9", True, 2000, (("A1", 100), ("B2", 50)))
    _oferta(
        s, "CA-U", "1-9", False, 1500, (("A1", 80), ("B2", 40))
    )  # mismo RUT: vale la seleccionada
    _oferta(s, "CA-U", "2-7", False, 1800, (("A1", 90), ("B2", 60)))
    _oferta(s, "CA-U", "2-7", False, 1900, (("A1", 95), ("B2", 65)))  # su más barata: 90
    s.commit()
    conteo = idx.ConteoUnion()
    obs = idx.observaciones_de_cas(s, [("CA-U", "Chillán", 16)], conteo)
    assert len(obs) == 1
    ob = obs[0]
    assert ob.producto_id == p1.id
    assert ob.ganador == pytest.approx(200)  # 100 por unidad pedida × 2 unidades por litro
    assert sorted(ob.ofertas) == pytest.approx([180, 200])  # una por RUT
    assert conteo.descartadas_ambiguas == 2
    assert conteo.descartadas_anexo == 1
    assert conteo.cas_con_anexo == {"CA-U"}


def test_recalcular_indice_por_ambito_y_respaldo_de_oc(s):
    p = _producto(s)
    for i in range(3):
        _ca(s, f"CA-{i}")
        _linea(s, f"CA-{i}", 1, p)
        _oferta(s, f"CA-{i}", "1-9", True, 1000 + 100 * i)
        _oferta(s, f"CA-{i}", "2-7", False, 1500)
    # Otro producto sin cotizaciones, solo con OC → ganador desde oc-da.
    q = _producto(s, "Arroz", ub="kg", unspsc="50221101")
    s.add(OcLineaCa(codigo_oc="OC-9", id_item="1", mes="2026-08", comuna_compra="San Carlos",
                    region_compra=16, grupo="g", codigo_producto="50221101", precio_base=1200.0,
                    base="kg"))  # fmt: skip
    s.commit()
    idx.inferir_comunas(s, [16], todas=True)
    r = idx.recalcular_indice(s, ["2026-08"], [16])
    filas = {(f.producto_id, f.ambito): f for f in s.execute(select(PrecioIndice)).scalars()}
    assert set(a for pid, a in filas if pid == p.id) == {
        "comuna:Chillán",
        "provincia:Diguillín",
        "region:16",
    }
    f = filas[(p.id, "comuna:Chillán")]
    assert (f.n_ganador, f.mediana_ganador, f.n_ofertas, f.fuente_ganador) == (3, 110.0, 6, "cot")
    g = filas[(q.id, "provincia:Punilla")]
    assert (g.n_ganador, g.mediana_ganador, g.n_ofertas, g.fuente_ganador) == (1, 1200.0, 0, "oc")
    assert r["filas_desde_oc"] == 3 and r["lineas_oc_usadas"] == 1
    # Idempotente: recalcular no duplica.
    idx.recalcular_indice(s, ["2026-08"], [16])
    assert s.execute(select(func.count()).select_from(PrecioIndice)).scalar_one() == len(filas)


def _fila_indice(s, pid, ambito, mes, n, med=100.0):
    s.add(PrecioIndice(producto_id=pid, ambito=ambito, mes=mes, n_ganador=n, p25_ganador=med * 0.9,
                       mediana_ganador=med, p75_ganador=med * 1.1, n_ofertas=2 * n,
                       p25_ofertas=med, mediana_ofertas=med * 1.05, p75_ofertas=med * 1.2))  # fmt: skip


def test_referencia_cae_de_comuna_a_provincia_region_y_pais_sin_usar_el_mes_propio(s):
    p = _producto(s)
    meses = ("2026-06", "2026-07", "2026-08")
    for m in meses:
        _fila_indice(s, p.id, "comuna:Chillán", m, 1, 90)  # 3 < 5
        _fila_indice(s, p.id, "provincia:Diguillín", m, 2, 100)  # 6 ≥ 5
        _fila_indice(s, p.id, "region:16", m, 4, 110)
    _fila_indice(s, p.id, "comuna:Chillán", "2026-09", 50, 999)  # el mes de la CA: no se usa
    s.add(PrecioReferencia(clave=f"{UNSPSC}|L", mes="2026-08", n=40, p25=80, mediana=95, p75=120))
    s.commit()
    r = idx.referencia_precio(s, p.id, "Chillán", "2026-09")
    assert (r.ambito, r.n, r.suficiente, r.mediana_ganador) == ("provincia:Diguillín", 6, True, 100)
    assert r.meses == meses
    r2 = idx.referencia_precio(s, p.id, "Ninhue", "2026-09")  # Itata: sin datos → región
    assert (r2.ambito, r2.n) == ("region:16", 12)
    r3 = idx.referencia_precio(s, p.id, "Ninhue", "2026-09", region=8)  # otra región → país
    assert (r3.ambito, r3.n, r3.mediana_ganador, r3.mediana_ofertas) == ("pais", 40, 95, None)
    assert r3.mediana_para_ratio == 95
    q = _producto(s, "Sin datos", unspsc=None)
    vacia = idx.referencia_precio(s, q.id, "Chillán", "2026-09")
    assert (vacia.ambito, vacia.n, vacia.suficiente) == (None, 0, False)


def test_referencia_con_n_insuficiente_devuelve_el_mejor_ambito_marcado():
    mem = idx.IndiceMemoria(
        {(1, "comuna:Pinto", "2026-08"): (2, 9.0, 10.0, 11.0, 4, 10.0, 11.0, 12.0)},
        {1: (None, "kg")},
        {},
    )
    r = mem.referencia(1, "Pinto", "2026-09", region=16)
    assert (r.ambito, r.n, r.suficiente) == ("comuna:Pinto", 2, False)
    lo, hi = idx.intervalo_80(r)
    assert lo < 10.0 < hi


# ---------------------------------------------------------------------------
# Logística, línea base y curva
# ---------------------------------------------------------------------------


def _filas_sinteticas(n_cas: int, *, semilla: int = 7, mes: str = "2026-05", b_ratio: float = -8.0,
                      b_local: float = 0.8) -> list[adj.CaDataset]:  # fmt: skip
    """CA con 2–6 oferentes; el ganador sale de una logística conocida (ratio ↑ → p ↓)."""
    rnd = random.Random(semilla)
    cas = []
    for i in range(n_cas):
        n = rnd.randint(2, 6)
        cod = f"S{semilla}-{mes}-{i}"
        ratios = [rnd.uniform(-0.3, 0.3) for _ in range(n)]
        locales = [rnd.random() < 0.5 for _ in range(n)]
        utilidad = [
            b_ratio * r + b_local * loc + rnd.gauss(0, 0.3)
            for r, loc in zip(ratios, locales, strict=True)
        ]
        gana = utilidad.index(max(utilidad))
        filas = [
            adj.FilaOferta(
                adj.ContextoOferta(
                    ca_codigo=cod,
                    mes=mes,
                    region=16,
                    familia="Lácteos y huevos",
                    unidad="u|x",
                    n_ofertas=float(n),
                    log_ratio=r,
                    es_local=loc,
                    es_emt=None,
                ),
                j == gana,
                1000 * math.exp(r),
            )  # fmt: skip
            for j, (r, loc) in enumerate(zip(ratios, locales, strict=True))
        ]
        cas.append(adj.CaDataset(cod, mes, 16, "Chillán", "Lácteos y huevos", "u|x", n, filas, 0,
                                 filas[gana].valor_neto, None))  # fmt: skip
    return cas


def test_logistica_recupera_el_signo_y_la_base_queda_peor():
    train = [f for c in _filas_sinteticas(800) for f in c.filas]
    test = [f for c in _filas_sinteticas(400, semilla=8) for f in c.filas]
    m = adj.entrenar_logistico(train, variables=("precio", "n_ofertas", "local"))
    assert m.convergio
    coef = {c: b for c, b in zip(m.columnas, m.beta, strict=True)}
    assert coef["log_ratio"] < 0  # precio relativo ↑ → p ↓
    assert coef["local:si"] > 0
    assert coef["log_n"] < 0  # más oferentes → menos chance
    # Coeficiente en escala original cerca del verdadero (la logística condicional no es exacta).
    assert -14 < coef["log_ratio"] / m.desv["log_ratio"] < -4
    base = adj.entrenar_base(train)
    assert base.b < 0
    b_log, _ = adj.brier((m.p(f.contexto), f.gano) for f in test)
    b_base, _ = adj.brier((base.p(f.contexto), f.gano) for f in test)
    assert b_log < b_base  # la variable "local" no la ve la base
    assert ev.elegir_activo({"logistico": b_log, "base": b_base}) == "logistico"
    assert ev.elegir_activo({"logistico": b_base, "base": b_log}) == "base"
    assert ev.elegir_activo({"logistico": None, "base": 0.2}) == "base"
    # Serialización ida y vuelta.
    m2 = adj.modelo_desde_json(m.a_json())
    assert m2.p(test[0].contexto) == pytest.approx(m.p(test[0].contexto))


def test_categoricas_con_pocas_ca_quedan_fuera():
    cas = _filas_sinteticas(30)
    for c in cas[:5]:
        for f in c.filas:
            f.contexto.unidad = "rara|y"
    m = adj.entrenar_logistico([f for c in cas for f in c.filas])
    assert "unidad:u|x" in m.columnas  # 25 CA ≥ 20
    assert "unidad:rara|y" not in m.columnas  # 5 CA < 20: "otra"


def test_curva_monotona_decreciente_en_precio():
    m = adj.entrenar_logistico([f for c in _filas_sinteticas(300) for f in c.filas],
                               variables=("precio", "n_ofertas"))  # fmt: skip
    ref = idx.ReferenciaPrecio(producto_id=1, ambito="comuna:Chillán", n=10, p25_ganador=90,
                               mediana_ganador=100, p75_ganador=115, n_ofertas=20, p25_ofertas=95,
                               mediana_ofertas=108, p75_ofertas=125, suficiente=True)  # fmt: skip
    precios = adj.precios_de_lineas([(10.0, ref)])
    base = adj.ContextoOferta(ca_codigo="X", mes="2026-09", region=16, familia="f", unidad="u",
                              n_ofertas=3, log_ratio=0.0)  # fmt: skip
    for costo in (None, 800.0):
        puntos = adj.curva(m, base, precios, costo)
        assert [p.nombre for p in puntos] == ["agresivo", "sugerido", "comodo"]
        assert all(a.precio <= b.precio for a, b in zip(puntos, puntos[1:], strict=False))
        assert all(a.p >= b.p for a, b in zip(puntos, puntos[1:], strict=False))
    # Un modelo absurdo (p crece con el precio) se recorta para no mostrar una curva al revés.
    puntos = adj.curva(adj.ModeloBase(b=+5.0), base, precios)
    assert all(a.p >= b.p for a, b in zip(puntos, puntos[1:], strict=False))


# ---------------------------------------------------------------------------
# Walk-forward sin fuga
# ---------------------------------------------------------------------------


def test_walk_forward_no_usa_datos_del_mes_evaluado_ni_posteriores():
    cas = [
        c
        for m in ("2026-03", "2026-04", "2026-05")
        for c in _filas_sinteticas(60, mes=m, semilla=int(m[-2:]))
    ]
    ent = ev.entrenar_para_mes(cas, "2026-06")
    futuros = _filas_sinteticas(80, mes="2026-06", semilla=3, b_ratio=+9) + _filas_sinteticas(
        80, mes="2026-07", semilla=4
    )
    ent2 = ev.entrenar_para_mes(cas + futuros, "2026-06")
    prueba = futuros[0].filas[0].contexto
    assert ent2.logistico.p(prueba) == ent.logistico.p(prueba)
    assert ent2.base.b == ent.base.b
    assert ent2.tabla.a_json() == ent.tabla.a_json()


def test_elegibilidad_y_meses_sin_datos_suficientes():
    cas = (_filas_sinteticas(25, mes="2026-02") + _filas_sinteticas(25, mes="2026-03")
           + _filas_sinteticas(25, mes="2026-04") + _filas_sinteticas(55, mes="2026-05"))  # fmt: skip
    ok, info = ev.elegibilidad(cas, "2026-05")
    assert ok and info == {"cas_2mas": 55, "meses_previos_con_datos": 3}
    ok, info = ev.elegibilidad(cas, "2026-04")  # 25 < 50 y solo 2 meses previos
    assert not ok and info["cas_2mas"] == 25


# ---------------------------------------------------------------------------
# Job completo (SQLite) y evaluar_ca
# ---------------------------------------------------------------------------


def _poblar_meses(
    s: Session, producto: ProductoCanonico, meses: list[str], por_mes: int = 55
) -> None:
    rnd = random.Random(11)
    for m in meses:
        for i in range(por_mes):
            cod = f"CA-{m}-{i}"
            _ca(s, cod, m, organismo=f"Municipalidad de {'Chillán' if i % 2 else 'Coihueco'}",
                org_rut=f"{i % 7 + 1}-{i % 7 + 1}")  # fmt: skip
            _linea(s, cod, 1, producto)
            n = rnd.randint(2, 4)
            precios = [rnd.uniform(800, 1300) for _ in range(n)]
            gana = precios.index(min(precios)) if rnd.random() < 0.7 else rnd.randrange(n)
            for j, pr in enumerate(precios):
                _oferta(s, cod, f"{j + 10}-{j}", j == gana, pr * 10, emt=j % 2 == 0)
    s.commit()


def test_run_indices_de_punta_a_punta_con_meses_sin_datos(s):
    p = _producto(s)
    empresa = Empresa(nombre="E", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True)
    s.add(empresa)
    s.commit()
    _poblar_meses(s, p, ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08"])
    r = ev.run_indices(s, [16], completo=True, mes_hoy="2026-09")
    estados = {e["mes"]: e["estado"] for e in r["evaluacion"]}
    assert estados == {"2026-03": "sin_datos_suficientes", "2026-04": "sin_datos_suficientes",
                       "2026-05": "sin_datos_suficientes", "2026-06": "sin_datos_suficientes",
                       "2026-07": "evaluado", "2026-08": "evaluado"}  # fmt: skip
    filas = (
        s.execute(select(EvaluacionModelo).where(EvaluacionModelo.mes == "2026-08")).scalars().all()
    )
    met = {(f.modelo, f.metrica, f.empresa_id): f for f in filas}
    assert met[("logistico", "brier_2mas", None)].valor is not None
    assert met[("base", "brier_2mas", None)].detalle["confiabilidad"]
    assert met[("indice", "intervalo_80", None)].n > 0
    assert met[("indice", "cobertura_monto", None)].valor == pytest.approx(1.0)
    gan = met[("logistico", "ganables", empresa.id)]
    assert gan.valor is None and gan.detalle == {"estado": "sin_costos"}  # sin costos ≠ 0 %
    assert s.execute(select(func.count()).select_from(EvaluacionModelo)
                     .where(EvaluacionModelo.metrica == "sin_datos_suficientes")).scalar_one() == 4  # fmt: skip
    modelos = s.execute(select(ModeloAdjudicacion)).scalars().all()
    assert {m.tipo for m in modelos} == {"logistico", "base"}
    assert sum(m.activo for m in modelos) == 1
    assert r["modelo_activo"] in ("logistico", "base")
    assert any("sin costos" in a for a in r["avisos"])
    assert r["metas"]["mes"] == "2026-08"
    # Re-ejecutar no duplica la evaluación ni deja dos activos.
    n_filas = len(filas)
    s.expunge_all()  # SQLite reutiliza ids tras borrar: que no queden filas viejas en la sesión
    ev.run_indices(s, [16], completo=False, mes_hoy="2026-09")
    assert s.execute(select(func.count()).select_from(EvaluacionModelo).where(
        EvaluacionModelo.mes == "2026-08")).scalar_one() == n_filas  # fmt: skip
    assert s.execute(select(func.count()).select_from(ModeloAdjudicacion)
                     .where(ModeloAdjudicacion.activo.is_(True))).scalar_one() == 1  # fmt: skip


def _ca_abierta(s: Session, producto: ProductoCanonico) -> CompraAgil:
    ca = _ca(s, "CA-NUEVA", "2026-09", organismo="Municipalidad de Chillán",
             publicada=_cierre("2026-09", 2))  # fmt: skip
    ca.comuna_inferida, ca.comuna_fuente = "Chillán", "texto"
    _linea(s, "CA-NUEVA", 1, producto, cantidad=10, cantidad_base=10)
    for m in ("2026-06", "2026-07", "2026-08"):
        _fila_indice(s, producto.id, "comuna:Chillán", m, 4, 100)
    s.add(ModeloAdjudicacion(tipo="base", datos_hasta_mes="2026-08", n_filas=1,
                             coeficientes={"modelo": {"tipo": "base", "b": -6.0},
                                           "n_esperado": {"por_clave": {}, "por_familia": {}}},
                             metricas={}, activo=True))  # fmt: skip
    s.commit()
    return ca


def test_evaluar_ca_sin_costos_deja_el_margen_en_none(s):
    p = _producto(s)
    _ca_abierta(s, p)
    e = Empresa(
        nombre="Sin costos", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True
    )
    s.add(e)
    s.commit()
    r = adj.evaluar_ca(s, "CA-NUEVA", e.id)
    assert r.completa and r.modelo == "base"
    assert r.margen_esperado is None and r.margen_pct is None and r.costo_total is None
    assert r.cobertura == 0.0
    assert set(r.p_ganar) == {"agresivo", "sugerido", "comodo"}
    assert r.p_ganar["agresivo"] >= r.p_ganar["sugerido"] >= r.p_ganar["comodo"]
    assert r.precio_sugerido == pytest.approx(1000.0)  # Σ 10 L × mediana 100
    assert r.lineas[0].referencia.ambito == "comuna:Chillán"
    assert any("Sin costos" in a for a in r.avisos)


def test_evaluar_ca_con_costos_usa_solo_los_de_la_empresa_y_muestra_su_fuente(s):
    p = _producto(s, ub="kg")
    _ca_abierta(s, p)
    a = Empresa(nombre="A", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True)
    b = Empresa(nombre="B", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True,
                comuna_base="Chillán")  # fmt: skip
    s.add_all([a, b])
    s.flush()
    prov = Proveedor(empresa_id=b.id, nombre="Mayorista", rubros=[], activo=True)
    s.add(prov)
    s.flush()
    s.add(ProveedorProducto(proveedor_id=prov.id, producto_id=p.id, producto_texto="Leche",
                            formato_texto="1 kg", factor_base=1.0, unidad_base="kg",
                            precio_neto_actual=50.0, vigente_desde=date(2026, 11, 5),
                            fuente="planilla"))  # fmt: skip
    s.add(TarifaLogistica(empresa_id=b.id, ambito="provincia", nombre="Diguillín",
                          costo_fijo_clp=100, costo_por_kg_clp=2.0))  # fmt: skip
    s.commit()
    rb = adj.evaluar_ca(s, "CA-NUEVA", b.id, costo_financiero_mensual=0.015)
    assert rb.lineas[0].costo.fuente == "planilla"
    assert rb.lineas[0].costo.vigente_desde == date(2026, 11, 5)
    assert rb.logistica.tarifa == "provincia:Diguillín" and rb.logistica.kg == 10
    assert rb.costo_total == pytest.approx((500 + 100 + 20) * 1.015)
    assert rb.margen_pct is not None and rb.margen_esperado is not None
    assert rb.cobertura == pytest.approx(1.0)
    ra = adj.evaluar_ca(s, "CA-NUEVA", a.id)  # A no ve los costos de B (regla 26)
    assert ra.lineas[0].costo is None and ra.margen_esperado is None


def test_p_ganar_none_sin_modelo_o_con_ca_incompleta(s):
    p = _producto(s)
    ca = _ca(s, "CA-INC", "2026-09", detalle="incompleto")
    _linea(s, "CA-INC", 1, p, estado="pendiente")
    s.commit()
    assert adj.p_ganar(s, ca, None, 1000.0) is None  # sin modelo activo
    ca2 = _ca_abierta(s, p)
    assert adj.p_ganar(s, ca, None, 1000.0) is None  # CA incompleta
    p1 = adj.p_ganar(s, ca2, None, 900.0)
    p2 = adj.p_ganar(s, ca2, None, 1200.0)
    assert p1 is not None and p2 is not None and p1 > p2
