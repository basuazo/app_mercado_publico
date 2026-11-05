"""F-plat-comp: análisis de competencia, mapa de mercado y variables para P(ganar).

Offline (SQLite en memoria). El juez de verdad son las adjudicaciones reales que corre Boris
(`mercado-competencia --completo`); acá, datos sintéticos con resultado conocido.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.query import resumen_evaluacion_modelo
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.mercado import adjudicacion as adj
from app.mercado import competencia as comp
from app.mercado import comprador as comp_mod
from app.mercado import eleccion
from app.mercado import evaluacion as ev
from app.mercado import indice as idx
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaCotizacion,
    CaProducto,
    CompetenciaCompradorMes,
    CompetenciaMes,
    CompraAgil,
    Empresa,
    EvaluacionModelo,
    Usuario,
)

LECHE = "50131700"  # Lácteos y huevos
ASEO = "47131800"  # Aseo
ORG = "69.140.900-7"
AHORA = datetime(2026, 11, 20, 12, 0)  # UTC naive
_PW = "contraseña-segura-test"


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


_ids = iter(range(1, 10**7))


def _ca(s: Session, codigo: str, mes: str, *, org: str = ORG, unidad: str = "Salud",
        region: int = 16, estado: str = "cerrada", productos=(LECHE,), leida: bool = True,
        motivo: str | None = None, dia: int = 5,
        organismo: str = "Municipalidad de Chillán") -> CompraAgil:  # fmt: skip
    cierre = idx.rango_mes_utc(mes)[0] + timedelta(days=dia, hours=12)
    ca = CompraAgil(
        codigo=codigo, nombre=codigo, estado=estado, region=region, organismo_rut=org,
        organismo_nombre=organismo, unidad_compra=unidad, fecha_cierre=cierre,
        fecha_publicacion=cierre - timedelta(days=3), motivo_desierta=motivo,
        cotizaciones_obtenidas_en=cierre if leida else None,
    )  # fmt: skip
    s.add(ca)
    for cod in productos:
        s.add(CaProducto(ca_codigo=codigo, codigo_producto=cod, nombre="p"))
    s.flush()
    return ca


def _of(s: Session, ca: str, rut: str, valor: float | None, sel: bool | None = None, *,
        nombre: str | None = None, emt: bool | None = None) -> None:  # fmt: skip
    s.add(CaCotizacion(ca_codigo=ca, llamado=1, rut_proveedor=rut, razon_social=nombre or f"Prov {rut}",
                       seleccionado=sel, valor_neto=valor, es_emt=emt,
                       id_cotizacion=str(next(_ids))))  # fmt: skip
    s.flush()


def _recalcular(s: Session, desde: str, hasta: str, ahora: datetime = AHORA, regiones=(16,)):
    return comp.recalcular_competencia(s, desde, list(regiones), hasta_mes=hasta, ahora=ahora,
                                       ubicaciones={})  # fmt: skip


def _fila(s: Session, rut: str, mes: str | None = None) -> CompetenciaMes:
    q = select(CompetenciaMes).where(CompetenciaMes.rut == rut)
    if mes:
        q = q.where(CompetenciaMes.mes == mes)
    return s.execute(q).scalar_one()


# ---------------------------------------------------------------------------
# Regla de elección: una sola implementación
# ---------------------------------------------------------------------------


def test_regla_de_eleccion_unica_y_usada_por_todos():
    assert adj.elegir_por_rut is eleccion.elegir_por_rut
    assert idx.elegir_por_rut is eleccion.elegir_por_rut
    assert comp_mod.elegir_por_rut is eleccion.elegir_por_rut
    assert comp.elegir_por_rut is eleccion.elegir_por_rut
    # Las copias anteriores ya no existen.
    assert not hasattr(idx, "_elegir_por_rut")
    assert not hasattr(comp_mod, "_elegir")


def test_regla_de_eleccion_casos():
    def elegir(ofertas):
        return eleccion.elegir_por_rut(ofertas, lambda o: o[0], lambda o: o[1], lambda o: o[2])

    r = elegir([("A", False, 100.0), ("A", True, 300.0), ("A", True, 200.0),  # seleccionada y barata
                ("B", None, 50.0), ("B", False, 40.0),  # sin selección: la más barata
                ("C", False, None), ("C", False, 70.0),  # precio None pierde
                ("", False, 10.0), ("", False, 5.0)])  # fmt: skip
    assert r["A"] == ("A", True, 200.0)
    assert r["B"] == ("B", False, 40.0)
    assert r["C"] == ("C", False, 70.0)
    # El RUT vacío se agrupa (comportamiento de indice/adjudicacion); comprador y competencia lo
    # filtran antes de llamar.
    assert r[""] == ("", False, 5.0)
    assert len(r) == 4


# ---------------------------------------------------------------------------
# Agregado genérico (adaptador §1.5): sin base de datos
# ---------------------------------------------------------------------------


def _p(codigo, ofertas, *, desierta=False, leido=True, comprador="X|salud", familia="F"):
    return comp.Proceso(proceso=codigo, comprador=comprador, organismo_rut="X",
                        comprador_nombre="Comprador X", region=16, familia=familia, mes="2026-10",
                        desierta=desierta, leido=leido,
                        ofertas=tuple(comp.Oferta(r, f"N{r}", None, p, g) for r, p, g in ofertas))  # fmt: skip


def test_agregado_generico_reglas():
    agg = comp.Agregado()
    agg.acumular(_p("P1", [("R1", 1000.0, True), ("R2", 1200.0, False), ("R2", 1100.0, False),
                           ("R3", 900.0, None)]))  # fmt: skip
    agg.acumular(_p("P2", [("R1", 500.0, True)]))  # un oferente
    agg.acumular(_p("P3", [("R4", 800.0, False)], desierta=True))
    agg.acumular(_p("P4", [], leido=True))  # leída sin oferentes
    agg.acumular(_p("P5", [], leido=False))  # aún sin leer
    filas_rut, filas_comp = agg.filas("2026-10", {"R1": ("Chillán", 16)})
    por_rut = {f["rut"]: f for f in filas_rut}
    assert set(por_rut) == {"R1", "R2", "R3"}  # R4 solo ofertó en una desierta
    r1, r2, r3 = por_rut["R1"], por_rut["R2"], por_rut["R3"]
    assert (r1["n_ca"], r1["n_ganadas"], r1["monto_ganado"], r1["n_ca_un_oferente"]) == (
        2,
        2,
        1500,
        1,
    )
    assert r1["n_log_ratio"] == 1 and r1["p50_log_ratio"] == pytest.approx(
        0.0
    )  # P2 fuera del ratio
    assert r1["comuna_proveedor"] == "Chillán" and r1["region_proveedor"] == 16
    # R2: dos filas, un oferente; su precio elegido es el más bajo (1100) frente a la mediana 1000.
    assert (r2["n_ca"], r2["n_ofertas"], r2["n_ganadas"]) == (1, 2, 0)
    assert r2["p50_log_ratio"] == pytest.approx(math.log(1.1))
    # R3: seleccionado nulo = sin dato, no derrota.
    assert (r3["n_sin_dato"], r3["n_ganadas"]) == (1, 0)
    (c,) = filas_comp
    assert c["n_ca"] == 5 and c["n_desiertas"] == 2 and c["n_con_ofertas"] == 2
    assert c["suma_oferentes"] == 4 and c["p50_oferentes"] == 2.0  # [3, 1]: RUT distintos, no filas
    assert c["n_un_oferente"] == 1 and c["n_con_ganador"] == 2
    # P1: ganó 1000 con una oferta de 900 → «no ganó el más barato».
    assert c["n_2mas_con_ganador"] == 1 and c["n_no_gano_mas_barato"] == 1
    assert c["monto_adjudicado"] == 1500


# ---------------------------------------------------------------------------
# Recálculo sobre la base
# ---------------------------------------------------------------------------


def _escenario(s: Session) -> None:
    _ca(s, "A1", "2026-10")
    _of(s, "A1", "76.111.111-1", 1000, True, nombre="Alfa SpA", emt=True)
    _of(s, "A1", "76222222-2", 1200, False, nombre="Beta Ltda")
    _of(s, "A1", "76222222-2", 1100, False, nombre="Beta Ltda")
    _ca(s, "A2", "2026-10")
    _of(s, "A2", "761111111", 700, True, nombre="Alfa SpA")  # mismo RUT sin guion ni puntos
    _ca(s, "D1", "2026-10", estado="desierta")
    _of(s, "D1", "76333333-3", 900, False)
    _ca(s, "OTRA", "2026-10", region=5)
    _of(s, "OTRA", "76999999-9", 5, True)
    s.commit()


def test_recalculo_rut_normalizado_y_cuadratura(s):
    _escenario(s)
    r = _recalcular(s, "2026-10", "2026-10")
    alfa = _fila(s, "76111111-1")
    assert alfa.n_ca == 2 and alfa.n_ganadas == 2 and alfa.monto_ganado == 1700
    assert alfa.razon_social == "Alfa SpA" and alfa.es_emt is True
    assert alfa.clave_comprador == comp_mod.clave_unidad(ORG, "Salud")
    assert alfa.familia == adj.familia_de_ca([(LECHE, "p")])
    beta = _fila(s, "76222222-2")
    assert beta.n_ca == 1 and beta.n_ofertas == 2
    # Desierta: no hay fila del oferente; cuenta en el comprador.
    assert s.execute(select(func.count()).select_from(CompetenciaMes)
                     .where(CompetenciaMes.rut == "76333333-3")).scalar_one() == 0  # fmt: skip
    cm = s.execute(select(CompetenciaCompradorMes)).scalar_one()
    assert cm.n_ca == 3 and cm.n_desiertas == 1 and cm.region == 16
    # Región fuera del territorio: nada.
    assert s.execute(select(func.count()).select_from(CompetenciaMes)
                     .where(CompetenciaMes.rut == "76999999-9")).scalar_one() == 0  # fmt: skip
    (cuad,) = r["cuadratura"]
    assert cuad == {"mes": "2026-10", "n_ganadas": 2, "cas_con_seleccionada": 2, "desvio": 0.0,
                    "cumple": True}  # fmt: skip
    assert r["ruts_forma_distinta"] == 1  # "761111111" → "76111111-1"
    assert r["ruts_no_validos"] == 0


def test_cuadratura_detecta_dos_ganadores(s):
    _ca(s, "A1", "2026-10")
    _of(s, "A1", "76111111-1", 100, True)
    _of(s, "A1", "76222222-2", 110, True)
    s.commit()
    (cuad,) = _recalcular(s, "2026-10", "2026-10")["cuadratura"]
    assert cuad["n_ganadas"] == 2 and cuad["cas_con_seleccionada"] == 1 and cuad["cumple"] is False


def test_rut_no_valido_cae_a_forma_laxa(s):
    _ca(s, "A1", "2026-10")
    _of(s, "A1", "ABC-1", 100, True)
    _of(s, "A1", "", 100, False)  # sin RUT: fuera
    s.commit()
    r = _recalcular(s, "2026-10", "2026-10")
    assert r["ruts_no_validos"] == 1 and r["ofertas_sin_rut"] == 1
    assert _fila(s, "ABC-1").n_ganadas == 1


def test_recalculo_idempotente(s):
    _escenario(s)

    def foto():
        return sorted(
            (f.mes, f.rut, f.clave_comprador, f.familia, f.n_ca, f.n_ganadas, f.monto_ganado)
            for f in s.execute(select(CompetenciaMes)).scalars()
        ), s.execute(select(func.count()).select_from(CompetenciaCompradorMes)).scalar_one()

    _recalcular(s, "2026-10", "2026-10")
    primera = foto()
    _recalcular(s, "2026-10", "2026-10")
    _recalcular(s, "2026-09", "2026-11")
    assert foto() == primera


def test_ca_abierta_o_futura_no_entra(s):
    _ca(s, "FUT", "2026-11", dia=25)  # cierra el 26-nov, después de AHORA (20-nov)
    _of(s, "FUT", "76111111-1", 100, True)
    s.commit()
    _recalcular(s, "2026-11", "2026-11")
    assert s.execute(select(func.count()).select_from(CompetenciaMes)).scalar_one() == 0


def test_rut_competidor():
    assert comp_mod.rut_competidor("76.123.456-7") == ("76123456-7", True)
    assert comp_mod.rut_competidor("761234567") == ("76123456-7", True)
    assert comp_mod.rut_competidor("76123456-k") == ("76123456-K", True)
    assert comp_mod.rut_competidor("ABC-1") == ("ABC-1", False)
    assert comp_mod.rut_competidor(None) == ("", True)


def test_historial_de_la_mesa_usa_el_mismo_rut(s):
    _ca(s, "A1", "2026-10")
    _of(s, "A1", "761111111", 100, True, nombre="Alfa")
    s.commit()
    h = comp_mod.historial_comprador(s, ORG, "Salud", ahora=AHORA)
    assert h.top_proveedores == [("76111111-1", "Alfa", 1)]


# ---------------------------------------------------------------------------
# Wilson, mediana ponderada
# ---------------------------------------------------------------------------


def test_wilson_con_n_chico():
    t0 = comp.wilson(0, 0)
    assert t0.p is None and t0.bajo is None and t0.alto is None
    t = comp.wilson(0, 5)
    assert t.p == 0 and t.bajo == 0 and t.alto == pytest.approx(0.4345, abs=1e-4)
    t1 = comp.wilson(1, 1)
    assert t1.p == 1 and t1.alto == 1 and t1.bajo == pytest.approx(0.2065, abs=1e-4)
    t2 = comp.wilson(50, 100)
    assert t2.bajo == pytest.approx(0.4038, abs=1e-4) and t2.alto == pytest.approx(0.5962, abs=1e-4)


def test_mediana_ponderada():
    assert comp.mediana_ponderada([]) is None
    assert comp.mediana_ponderada([(None, 3), (1.0, 0)]) is None
    assert comp.mediana_ponderada([(0.1, 1), (0.5, 10), (0.9, 1)]) == 0.5
    assert comp.pct_relativo(math.log(1.1)) == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Historial, variables del modelo y juez (sin fuga temporal)
# ---------------------------------------------------------------------------


def _historia(s: Session) -> None:
    # Septiembre: R1 en 3 CA, R2 en 2. Octubre: R3 en 4 CA (no debe verse desde octubre).
    for i in range(3):
        _ca(s, f"S{i}", "2026-09")
        _of(s, f"S{i}", "76111111-1", 100, True)
        if i < 2:
            _of(s, f"S{i}", "76222222-2", 120, False)
    for i in range(4):
        _ca(s, f"O{i}", "2026-10")
        _of(s, f"O{i}", "76333333-3", 90, True)
        _of(s, f"O{i}", "76111111-1", 110, False)
    s.commit()
    _recalcular(s, "2026-09", "2026-11")


def test_esperados_y_variables_sin_fuga(s):
    _historia(s)
    clave, fam = comp_mod.clave_unidad(ORG, "Salud"), adj.familia_de_ca([(LECHE, "p")])
    hist = comp.HistorialCompetencia.cargar(s, "2025-01", "2026-12")
    # Para una CA de octubre solo cuenta septiembre: R3 (que solo aparece en octubre) no está.
    assert [e.rut for e in hist.esperados(clave, fam, "2026-10")] == ["76111111-1", "76222222-2"]
    # En noviembre ya se ve octubre.
    assert [e.rut for e in hist.esperados(clave, fam, "2026-11")] == [
        "76111111-1", "76333333-3", "76222222-2"
    ]  # fmt: skip
    v_oct = hist.variables(clave, fam, "2026-10")
    assert v_oct["comp_n_habituales"] == 2.0
    assert v_oct["comp_tasa_un_oferente"] == pytest.approx(1 / 3)  # S2 tuvo un solo oferente
    # Cambiar octubre no mueve las variables de una CA de octubre.
    _ca(s, "O9", "2026-10")
    _of(s, "O9", "76444444-4", 1, True)
    s.commit()
    _recalcular(s, "2026-10", "2026-10")
    hist2 = comp.HistorialCompetencia.cargar(s, "2025-01", "2026-12")
    assert hist2.variables(clave, fam, "2026-10") == v_oct
    # Sin historial: solo la marca categórica.
    assert hist.variables("otro|x", fam, "2026-10") == {"comp_sin_historial:si": 1.0}
    # variables_competencia de una CA concreta: la de octubre usa septiembre.
    ca = s.get(CompraAgil, "O1")
    assert comp.variables_competencia(s, ca, fam) == v_oct


def test_extractor_registrado_fuera_del_modelo_base():
    assert "competencia" in adj.VARIABLES and "competencia" not in adj.VARIABLES_BASE
    c = adj.ContextoOferta(ca_codigo="x", mes="2026-10", region=16, familia="F", unidad="u",
                           n_ofertas=2, log_ratio=0.0)  # fmt: skip
    assert adj.extraer(c, ["competencia"]) == {"comp_sin_historial:si": 1.0}
    c.extra["competencia"] = {"comp_n_habituales": 2.0}
    assert adj.extraer(c, ["competencia"]) == {"comp_n_habituales": 2.0}


def test_juez_competidores(s):
    _historia(s)
    hist = comp.HistorialCompetencia.cargar(s, "2025-01", "2026-12", regiones=[16])
    (oct_,) = comp.juez_competidores(s, [16], ["2026-10"], hist, AHORA, "2026-11")
    # Octubre: 4 CA de 2 oferentes; esperados (de septiembre) = [R1, R2]; ofertaron R3 y R1.
    assert oct_["cas_2mas"] == 4 and oct_["con_prediccion"] == 4
    assert oct_["top1_oferta"] == 1.0 and oct_["cumple"] is True
    assert oct_["precision_top3"] == 0.5 and oct_["recall_top3"] == 0.5
    assert oct_["datos_insuficientes"] is True and oct_["parcial"] is False
    filas = s.execute(select(EvaluacionModelo.metrica).where(
        EvaluacionModelo.modelo == "competencia", EvaluacionModelo.mes == "2026-10")).scalars().all()  # fmt: skip
    assert sorted(filas) == ["precision_top3", "recall_top3", "top1_oferta"]


def test_aporte_al_modelo_sin_meses_evaluables(s):
    _historia(s)
    hist = comp.HistorialCompetencia.cargar(s, "2025-01", "2026-12", regiones=[16])
    r = comp.aporte_al_modelo(s, [16], ["2026-10"], hist)
    assert r["meses_evaluables"] == 0 and r["en_modelo"] is False and r["mejora"] is None
    assert r["estado"] == "Brier con/sin: pendiente, 0 meses evaluables"
    assert s.execute(select(EvaluacionModelo.metrica).where(
        EvaluacionModelo.modelo == "competencia")).scalars().all() == ["aporte_sin_datos"]  # fmt: skip


def test_mercado_indices_no_borra_ni_cuenta_las_filas_de_competencia(s):
    s.add(
        EvaluacionModelo(
            mes="2026-10", modelo="competencia", metrica="top1_oferta", valor=0.7, n=60
        )
    )
    s.commit()
    ev.run_indices(s, [16], mes_hoy="2026-11")  # reevalúa octubre (sin datos suficientes)
    assert s.execute(select(func.count()).select_from(EvaluacionModelo)
                     .where(EvaluacionModelo.modelo == "competencia")).scalar_one() == 1  # fmt: skip
    # /salud: un mes con solo filas de competencia no es un «mes evaluado» del modelo.
    assert resumen_evaluacion_modelo(s)["ultimo_mes_evaluado"] is None


def test_job_completo(s):
    _historia(s)
    r = comp.run_competencia_mercado(s, [16], completo=True, mes_hoy="2026-11", ahora=AHORA)
    assert r["recalculo"]["meses"][0] == "2026-09"
    assert all(c["cumple"] for c in r["recalculo"]["cuadratura"])
    assert [j["mes"] for j in r["juez_competidores"]][-1] == "2026-11"
    assert r["aporte_modelo"]["estado"].startswith("Brier con/sin: pendiente")
    assert r["bytes"] is None  # SQLite: se mide en Postgres
    assert any("ningún mes" in a for a in r["avisos"])
    assert comp.run_competencia_mercado(s, [], mes_hoy="2026-11") == {
        "regiones": [], "completo": False, "sin_regiones": 1
    }  # fmt: skip


# ---------------------------------------------------------------------------
# Consultas de la UI
# ---------------------------------------------------------------------------


def _empresa(s: Session, rut: str | None = None, regiones=(16,)) -> int:
    e = Empresa(nombre=f"E{next(_ids)}", rut=rut, regiones_prioritarias=list(regiones))
    s.add(e)
    s.commit()
    return e.id


def test_consultas_de_ui(s):
    _historia(s)
    _ca(s, "R5", "2026-10", region=5, org="61.000.000-0")
    _of(s, "R5", "76555555-5", 50, True)
    s.commit()
    _recalcular(s, "2026-10", "2026-10", regiones=(16, 5))
    clave, fam = comp_mod.clave_unidad(ORG, "Salud"), adj.familia_de_ca([(LECHE, "p")])
    p = comp.perfil_competidor(s, "76.111.111-1", [16], mes_hoy="2026-11")
    assert p is not None and p.n_ca == 7 and p.tasa.k == 3 and p.tasa.n == 7
    assert [t["mes"] for t in p.tendencia][-3:] == ["2026-09", "2026-10", "2026-11"]
    assert p.compradores[0]["clave"] == clave and p.familias[0]["clave"] == fam
    assert comp.perfil_competidor(s, "76555555-5", [16], mes_hoy="2026-11") is None  # otra región
    v = comp.competidores_de(s, clave, fam, [16], nosotros="76333333-3", mes_hoy="2026-11")
    assert v["n_con_ofertas"] == 7
    por_rut = {c["rut"]: c for c in v["competidores"]}
    assert por_rut["76111111-1"]["participacion"] == 1.0
    assert por_rut["76333333-3"]["es_nosotros"] and not por_rut["76111111-1"]["es_nosotros"]
    rk = comp.ranking_territorio(s, [16], mes_hoy="2026-11")
    assert {c["rut"] for c in rk} == {"76111111-1", "76222222-2", "76333333-3"}
    assert [c["rut"] for c in comp.buscar_competidores(s, "7611", [16], mes_hoy="2026-11")] == [
        "76111111-1"
    ]
    assert [c["rut"] for c in comp.buscar_competidores(s, "prov 76222", [16], mes_hoy="2026-11")] == [
        "76222222-2"
    ]  # fmt: skip
    assert comp.buscar_competidores(s, "%", [16], mes_hoy="2026-11") == []  # `%` literal
    duelo = comp.cara_a_cara(s, "76111111-1", "76333333-3", [16], mes_hoy="2026-11")
    assert duelo["a"]["n_ca"] == 4 and duelo["b"]["n_ca"] == 4 and duelo["b"]["tasa"].k == 4
    assert duelo["diferencia"] == pytest.approx(110 / 90 - 1)  # A ofertó 110 donde B ofertó 90
    assert comp.cara_a_cara(s, "76111111-1", "76111111-1", [16])["mercados"] == []


def test_mapa_mercado_solo_regiones_de_la_empresa(s):
    _historia(s)
    _ca(s, "R5", "2026-10", region=5, org="61.000.000-0")
    _of(s, "R5", "76555555-5", 50, True)
    s.commit()
    _recalcular(s, "2026-09", "2026-10", regiones=(16, 5))
    e16 = _empresa(s, regiones=(16,))
    m = comp.mapa_mercado(s, e16, mes_hoy="2026-11")
    assert m.regiones == [16] and len(m.filas) == 1
    f = m.filas[0]
    assert f["n_ca"] == 7 and f["dominante"]["rut"] == "76333333-3"  # 360 > 300
    assert f["pct_un_oferente"] == pytest.approx(1 / 7)
    # Octubre: ganó R3 con 90 contra 110 → ganó el más barato en todas.
    assert f["pct_no_mas_barato"] == 0.0
    e5 = _empresa(s, regiones=(5,))
    assert {r["clave"] for r in comp.mapa_mercado(s, e5, mes_hoy="2026-11").filas} == {
        comp_mod.clave_unidad("61.000.000-0", "Salud")
    }
    assert comp.mapa_mercado(s, e16, familia="Aseo", mes_hoy="2026-11").filas == []


def test_rut_empresa_nulo_y_normalizado(s):
    assert comp.rut_empresa(s, _empresa(s)) is None
    assert comp.rut_empresa(s, _empresa(s, rut="76111111-1")) == "76111111-1"
    assert comp.rut_empresa(s, None) is None


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings():
    return Settings(mp_ticket="TICKET_TEST", database_url="sqlite:///:memory:",
                    secret_key="secret-test-key-larga-32chars!!", jobs_token="jobs-token-secreto")  # fmt: skip


@pytest.fixture()
def client(engine, settings):
    return TestClient(create_app(settings, engine))


def _usuario(engine, *, rut: str | None = None, regiones=(16,), con_empresa: bool = True) -> int:
    with Session(engine) as s:
        empresa_id = _empresa(s, rut=rut, regiones=regiones) if con_empresa else None
        u = Usuario(email=f"u{next(_ids)}@t.cl", password_hash=hash_password(_PW),
                    rol=RolUsuario.USUARIO, activo=True, empresa_id=empresa_id)  # fmt: skip
        s.add(u)
        s.commit()
        return u.id


def _cookies(settings, uid):
    token = create_session_token(settings.secret_key, uid)
    nonce = decode_session_token(settings.secret_key, token)[1]
    generate_csrf_token(settings.secret_key, nonce)
    return {COOKIE_NAME: token}


def _datos_recientes(engine) -> tuple[str, str]:
    """CA cerradas el mes pasado (relativo a hoy) en Ñuble y en Valparaíso."""
    mes = idx.sumar_meses(idx.mes_actual(), -1)
    with Session(engine) as s:
        for i in range(2):
            _ca(s, f"N{i}", mes)
            _of(s, f"N{i}", "76111111-1", 100, True, nombre="Alfa SpA")
            _of(s, f"N{i}", "76222222-2", 130, False, nombre="Beta Ltda")
        _ca(
            s,
            "V1",
            mes,
            region=5,
            org="61.000.000-0",
            unidad="Hospital",
            organismo="Servicio de Salud Valparaíso",
        )
        _of(s, "V1", "76555555-5", 50, True, nombre="Valpo SA")
        s.commit()
        comp.recalcular_competencia(s, mes, [16, 5], ahora=ahora_utc(), ubicaciones={})
    return mes, comp_mod.clave_unidad(ORG, "Salud")


def test_sin_empresa_ve_el_aviso(client, engine, settings):
    c = _cookies(settings, _usuario(engine, con_empresa=False))
    for url in ("/competencia", "/competencia/76111111-1", "/estrategia"):
        r = client.get(url, cookies=c)
        assert r.status_code == 200 and "todavía no tiene una empresa" in r.text


def test_rutas_exigen_login(client):
    for url in ("/competencia", "/competencia/76111111-1", "/estrategia"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]


def test_paginas_de_competencia(client, engine, settings):
    _, clave = _datos_recientes(engine)
    c = _cookies(settings, _usuario(engine, rut="76.111.111-1"))
    r = client.get("/competencia", cookies=c)
    assert r.status_code == 200
    assert "Alfa SpA" in r.text and "Beta Ltda" in r.text and "Valpo SA" not in r.text
    assert "Nosotros" in r.text and "Fuente: Dirección ChileCompra" in r.text
    assert 'href="/competencia"' in r.text and 'href="/estrategia"' in r.text  # barra lateral
    r = client.get("/competencia", params={"q": "beta"}, cookies=c)
    assert "Beta Ltda" in r.text and "Resultados" in r.text
    r = client.get("/competencia", params={"comprador": clave}, cookies=c)
    assert "Municipalidad de Chillán" in r.text and "Participación" in r.text
    r = client.get("/competencia/76.222.222-2", cookies=c)
    assert r.status_code == 200 and "Beta Ltda" in r.text and "Tendencia mensual" in r.text
    assert "<svg" in r.text and "Fuente: Dirección ChileCompra" in r.text
    r = client.get("/competencia/76222222-2", params={"vs": "76111111-1"}, cookies=c)
    assert "Cara a cara" in r.text and "ganó 0 de 2" in r.text
    # Competidor de otra región: indistinguible de uno inexistente.
    assert client.get("/competencia/76555555-5", cookies=c).status_code == 404
    assert client.get("/competencia/1-9", cookies=c).status_code == 404


def test_estrategia_solo_regiones_de_la_empresa(client, engine, settings):
    _datos_recientes(engine)
    c16 = _cookies(settings, _usuario(engine, regiones=(16,)))
    r = client.get("/estrategia", cookies=c16)
    assert r.status_code == 200
    assert "Municipalidad de Chillán" in r.text and "Hospital" not in r.text
    assert "Fuente: Dirección ChileCompra" in r.text
    r = client.get("/estrategia", params={"orden": "inventado", "familia": "Aseo"}, cookies=c16)
    assert r.status_code == 200 and "Todavía no hay mapa" in r.text
    c5 = _cookies(settings, _usuario(engine, regiones=(5,)))
    r = client.get("/estrategia", cookies=c5)
    assert "Hospital" in r.text and "Municipalidad de Chillán" not in r.text


def test_sin_rut_de_empresa_no_marca_nosotros(client, engine, settings):
    _datos_recientes(engine)
    r = client.get("/competencia", cookies=_cookies(settings, _usuario(engine, rut=None)))
    assert r.status_code == 200 and "Nosotros</span>" not in r.text
    assert "no tiene RUT registrado" in r.text


def test_la_ui_no_recorre_ca_cotizaciones(client, engine, settings):
    _, clave = _datos_recientes(engine)
    c = _cookies(settings, _usuario(engine, rut="76111111-1"))
    sentencias: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _captura(conn, cursor, statement, parameters, context, executemany):
        sentencias.append(statement)

    for url, params in (("/competencia", {}), ("/competencia", {"q": "alfa"}),
                        ("/competencia", {"comprador": clave}), ("/competencia/76111111-1", {"vs": "76222222-2"}),
                        ("/estrategia", {})):  # fmt: skip
        assert client.get(url, params=params, cookies=c).status_code == 200
    assert any("competencia_mes" in x for x in sentencias)
    assert not [x for x in sentencias if "ca_cotizaciones" in x or "oc_lineas_ca" in x]


def test_mesa_enlaza_a_competidores_del_comprador_y_familia(client, engine, settings):
    _datos_recientes(engine)
    c = _cookies(settings, _usuario(engine))
    with Session(engine) as s:
        for codigo, productos in (("VIG-1", (LECHE,)), ("VIG-2", (LECHE, ASEO))):
            s.add(CompraAgil(codigo=codigo, nombre=codigo, estado="publicada", region=16,
                             organismo_rut=ORG, unidad_compra="Salud",
                             fecha_cierre=ahora_utc() + timedelta(days=1)))  # fmt: skip
            for cod in productos:
                s.add(CaProducto(ca_codigo=codigo, codigo_producto=cod, nombre="p"))
        s.commit()
    r1 = client.get("/mesa/ca/VIG-1/comprador", cookies=c)
    assert "/competencia?comprador=" in r1.text and "familia=" in r1.text
    assert 'href="/competencia/76111111-1"' in r1.text  # top proveedor → su perfil
    r2 = client.get("/mesa/ca/VIG-2/comprador", cookies=c)  # «Varias»: sin familia
    assert "/competencia?comprador=" in r2.text and "familia=" not in r2.text
