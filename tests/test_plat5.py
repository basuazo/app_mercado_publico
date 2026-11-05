"""F-plat-5: precio, margen y P(ganar) en la mesa; mi oferta; modo sombra.

Todo offline (SQLite en memoria). `ON CONFLICT` e inmutabilidad sobre Postgres: test_plat5_pg.py.
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.retencion import purgar_terminales
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.mercado import adjudicacion as adj
from app.mercado import equipo as eq
from app.mercado import sombra
from app.mercado.indice import mes_de, sumar_meses
from app.mercado.mesa_dia import Evaluada, FiltrosMesa, buscar_codigos, filtrar_precio, valor_orden
from app.models.base import Base
from app.models.tables import (
    CaCotizacion,
    CaEvento,
    CaLinea,
    CaProducto,
    CaTrabajo,
    CompraAgil,
    Empresa,
    EvaluacionCache,
    ModeloAdjudicacion,
    OfertaPropia,
    PrecioIndice,
    Prediccion,
    ProductoCanonico,
    Proveedor,
    ProveedorProducto,
    TarifaLogistica,
    Usuario,
)

UNSPSC = "50131701"  # leche (alimentos)
RUT_NUESTRO = "76.123.456-0"
RUT_OTRO = "77.777.777-7"
_PW = "contraseña-segura-test"


@pytest.fixture()
def engine():
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(e)
    yield e


@pytest.fixture()
def settings():
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url="sqlite:///:memory:",
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
    )


@pytest.fixture()
def client(engine, settings):
    return TestClient(create_app(settings, engine))


def _auth(settings, uid):
    token = create_session_token(settings.secret_key, uid)
    nonce = decode_session_token(settings.secret_key, token)[1]
    csrf = generate_csrf_token(settings.secret_key, nonce)
    return {COOKIE_NAME: token}, {"X-CSRF-Token": csrf, "HX-Request": "true"}, csrf


# ---------------------------------------------------------------------------
# Escenario: una empresa con costos, otra sin; producto con índice; modelo base activo
# ---------------------------------------------------------------------------


def _modelo(s, b=-6.0):
    m = ModeloAdjudicacion(
        tipo="base", datos_hasta_mes="2026-08", n_filas=1,
        coeficientes={"modelo": {"tipo": "base", "b": b},
                      "n_esperado": {"por_clave": {}, "por_familia": {}}},
        metricas={}, activo=True,
    )  # fmt: skip
    s.add(m)
    s.flush()
    return m


def _ca(s, codigo, pid, *, cierre=None, region=16, con_indice=True, estado="publicada", **kw):
    ahora = ahora_utc()
    publicada = ahora - timedelta(days=1)
    s.add(
        CompraAgil(
            codigo=codigo, nombre=kw.pop("nombre", f"Leche {codigo}"), estado=estado, region=region,
            organismo_nombre="Municipalidad de Chillán", organismo_rut="69.140.900-7",
            unidad_compra="Depto. Salud", fecha_publicacion=publicada,
            fecha_cierre=cierre if cierre is not None else ahora + timedelta(days=2),
            detalle_lineas="completo", comuna_inferida="Chillán", comuna_fuente="texto", **kw,
        )
    )  # fmt: skip
    s.add(CaProducto(ca_codigo=codigo, codigo_producto=UNSPSC, nombre="Leche", cantidad=10,
                     unidad="Kilo"))  # fmt: skip
    s.add(CaLinea(ca_codigo=codigo, n=1, producto_id=pid, cantidad=10, cantidad_base=10,
                  texto_origen="Leche entera 10 kg", codigo_unspsc=UNSPSC, estado="ok"))  # fmt: skip
    s.flush()
    if con_indice:
        mes = mes_de(publicada)
        for k in (1, 2, 3):
            m = sumar_meses(mes, -k)
            if s.get(PrecioIndice, (pid, "comuna:Chillán", m)) is None:
                s.add(PrecioIndice(producto_id=pid, ambito="comuna:Chillán", mes=m, n_ganador=4,
                                   p25_ganador=90, mediana_ganador=100, p75_ganador=110,
                                   n_ofertas=8, p25_ofertas=100, mediana_ofertas=105,
                                   p75_ofertas=120))  # fmt: skip
        s.flush()


@pytest.fixture()
def esc(engine):
    """Empresa «Nuestra» (con RUT y costos), «Otra»; usuarios; producto; modelo; CA-V vigente."""
    with Session(engine) as s:
        p = ProductoCanonico(nombre="Leche entera", unidad_base="kg", unspsc_sugerido=UNSPSC,
                             activo=True)  # fmt: skip
        s.add(p)
        nuestra = Empresa(nombre="Nuestra", rut=RUT_NUESTRO, regiones_prioritarias=[16],
                          comunas_prioritarias=[], activa=True, comuna_base="Chillán")  # fmt: skip
        otra = Empresa(nombre="Otra", rut="11.111.111-1", regiones_prioritarias=[16],
                       comunas_prioritarias=[], activa=True)  # fmt: skip
        s.add_all([nuestra, otra])
        s.flush()
        prov = Proveedor(empresa_id=nuestra.id, nombre="Mayorista", rubros=[], activo=True)
        prov_otra = Proveedor(empresa_id=otra.id, nombre="De la otra", rubros=[], activo=True)
        s.add_all([prov, prov_otra])
        s.flush()
        pp = ProveedorProducto(proveedor_id=prov.id, producto_id=p.id, producto_texto="Leche",
                               formato_texto="1 kg", factor_base=1.0, unidad_base="kg",
                               precio_neto_actual=50.0, vigente_desde=date(2026, 11, 1),
                               fuente="planilla")  # fmt: skip
        pp2 = ProveedorProducto(proveedor_id=prov.id, producto_id=p.id, producto_texto="Leche",
                                formato_texto="caja 12 kg", factor_base=12.0, unidad_base="kg",
                                precio_neto_actual=720.0, vigente_desde=date(2026, 11, 1),
                                fuente="planilla")  # fmt: skip  60/kg
        # De la otra empresa y sin producto canónico: Otra queda sin costos para la leche.
        pp_otra = ProveedorProducto(proveedor_id=prov_otra.id, producto_id=None,
                                    producto_texto="Leche", formato_texto="1 kg", factor_base=1.0,
                                    unidad_base="kg", precio_neto_actual=10.0,
                                    vigente_desde=date(2026, 11, 1), fuente="manual")  # fmt: skip
        s.add_all([pp, pp2, pp_otra])
        s.add(TarifaLogistica(empresa_id=nuestra.id, ambito="provincia", nombre="Diguillín",
                              costo_fijo_clp=100, costo_por_kg_clp=2.0))  # fmt: skip
        users = {}
        for clave, email, emp, nombre in (("boris", "boris@t.cl", nuestra.id, "Boris"),
                                          ("mario", "mario@t.cl", nuestra.id, "Mario"),
                                          ("ana", "ana@t.cl", otra.id, "Ana")):  # fmt: skip
            u = Usuario(email=email, nombre=nombre, password_hash=hash_password(_PW),
                        empresa_id=emp)  # fmt: skip
            s.add(u)
            s.flush()
            users[clave] = u.id
        modelo = _modelo(s)
        _ca(s, "CA-V", p.id)
        s.commit()
        return {"p": p.id, "emp": nuestra.id, "otra": otra.id, "pp": pp.id, "pp2": pp2.id,
                "pp_otra": pp_otra.id, "modelo": modelo.id, **users}  # fmt: skip


def _preds(s):
    return list(s.execute(select(Prediccion).order_by(Prediccion.id)).scalars())


# ---------------------------------------------------------------------------
# Job mercado-mesa: caché y predicciones
# ---------------------------------------------------------------------------


def test_job_escribe_cache_y_crea_prediccion_inmutable(engine, esc):
    with Session(engine) as s:
        r = sombra.run_mesa(s)
        assert r["predicciones_nuevas"] == 1 and r["modelo"] == "base"
        c = s.get(EvaluacionCache, ("CA-V", esc["emp"]))
        assert c is not None and c.datos["costo_total"] is not None
        assert c.datos["cobertura"] == pytest.approx(1.0)
        # La otra empresa no tiene costos: caché sin margen y sin predicción (cobertura 0).
        c2 = s.get(EvaluacionCache, ("CA-V", esc["otra"]))
        assert c2.datos["costo_total"] is None and c2.datos["margen_pct"] is None
        (p,) = _preds(s)
        assert (p.empresa_id, p.modelo_id, p.modelo_tipo, p.estado) == (
            esc["emp"], esc["modelo"], "base", "abierta"
        )
        congelada = (p.precio_sugerido, p.p_ganar_sugerido, p.margen_esperado, p.detalle)

        # Segunda corrida y un modelo nuevo entrenado (otra fila activa): ni otra ni distinta.
        s.execute(ModeloAdjudicacion.__table__.update().values(activo=False))
        _modelo(s, b=-1.0)
        s.add(ProveedorProducto(proveedor_id=s.get(ProveedorProducto, esc["pp"]).proveedor_id,
                                producto_id=esc["p"], producto_texto="Leche barata",
                                formato_texto="1 kg", factor_base=1.0, unidad_base="kg",
                                precio_neto_actual=20.0, vigente_desde=date(2026, 11, 2)))  # fmt: skip
        s.commit()
        r2 = sombra.run_mesa(s)
        assert r2["predicciones_nuevas"] == 0
        (p,) = _preds(s)
        assert (p.precio_sugerido, p.p_ganar_sugerido, p.margen_esperado, p.detalle) == congelada
        assert p.modelo_id == esc["modelo"]
        # La caché sí se reescribe con el modelo y el costo nuevos.
        c = s.get(EvaluacionCache, ("CA-V", esc["emp"]))
        assert c.modelo_id != esc["modelo"]


def test_no_predice_con_cobertura_baja_ni_a_menos_de_2h(engine, esc):
    with Session(engine) as s:
        p2 = ProductoCanonico(nombre="Arroz", unidad_base="kg", unspsc_sugerido=UNSPSC, activo=True)
        s.add(p2)
        s.flush()
        _ca(s, "CA-SIN-COSTO", p2.id)  # sin costo de nadie → cobertura 0
        _ca(s, "CA-CERCA", esc["p"], cierre=ahora_utc() + timedelta(hours=1, minutes=50))
        s.commit()
        sombra.run_mesa(s)
        assert [p.ca_codigo for p in _preds(s)] == ["CA-V"]
        ev = adj.evaluar_ca(s, "CA-SIN-COSTO", esc["emp"])
        assert ev.cobertura == 0.0
        assert s.get(EvaluacionCache, ("CA-CERCA", esc["emp"])) is not None  # sí evaluada


def test_cache_de_ca_que_ya_no_esta_vigente_se_borra(engine, esc):
    with Session(engine) as s:
        sombra.run_mesa(s)
        s.get(CompraAgil, "CA-V").estado = "cerrada"
        s.commit()
        r = sombra.run_mesa(s)
        assert r["cache_borradas"] == 2
        assert s.execute(select(func.count()).select_from(EvaluacionCache)).scalar_one() == 0


# ---------------------------------------------------------------------------
# Cierre de predicciones
# ---------------------------------------------------------------------------


def _cerrar_ca(s, codigo, ofertas, estado="cerrada"):
    ca = s.get(CompraAgil, codigo)
    ca.fecha_cierre = ahora_utc() - timedelta(hours=1)
    ca.estado = estado
    ca.cotizaciones_obtenidas_en = ahora_utc()
    for i, (rut, sel, valor) in enumerate(ofertas):
        s.add(CaCotizacion(ca_codigo=codigo, llamado=1, rut_proveedor=rut, seleccionado=sel,
                           valor_neto=valor, id_cotizacion=f"{codigo}-{i}"))  # fmt: skip
    s.commit()


def test_cierre_con_ganador_en_y_fuera_del_intervalo_y_multi_ganador(engine, esc):
    with Session(engine) as s:
        _ca(s, "CA-2", esc["p"])
        s.commit()
        sombra.run_mesa(s)
        preds = {p.ca_codigo: p for p in _preds(s)}
        lo, hi = preds["CA-V"].intervalo_80_min, preds["CA-V"].intervalo_80_max
        assert lo is not None and hi is not None
        _cerrar_ca(s, "CA-V", [(RUT_OTRO, True, (lo + hi) / 2), ("88.888.888-8", False, hi * 2)])
        # CA-2: dos ganadores (CA multi-adjudicada): se suman y se marca.
        _cerrar_ca(s, "CA-2", [(RUT_OTRO, True, hi), ("88.888.888-8", True, hi)])
        r = sombra.cerrar_predicciones(s, ahora_utc())
        assert r["cerradas"] == 2
        s.expire_all()
        preds = {p.ca_codigo: p for p in _preds(s)}
        v, dos = preds["CA-V"], preds["CA-2"]
        assert v.estado == "cerrada" and v.resultado_en_intervalo is True
        assert v.resultado_propio is None  # no la enviamos
        assert dos.resultado_precio_ganador == pytest.approx(2 * hi)
        assert dos.resultado_en_intervalo is False
        assert dos.detalle["multi_ganador"] == 2 and "lineas" in dos.detalle


def test_cierre_sin_adjudicacion_y_espera_si_falta_la_seleccion(engine, esc):
    with Session(engine) as s:
        _ca(s, "CA-2", esc["p"])
        s.commit()
        sombra.run_mesa(s)
        _cerrar_ca(s, "CA-V", [], estado="desierta")
        _cerrar_ca(s, "CA-2", [(RUT_OTRO, None, 900.0)])  # ofertas sin selección todavía
        sombra.cerrar_predicciones(s, ahora_utc())
        s.expire_all()
        preds = {p.ca_codigo: p for p in _preds(s)}
        assert preds["CA-V"].estado == "sin_adjudicacion"
        assert preds["CA-2"].estado == "abierta"  # todavía no se puede decir
        s.get(CompraAgil, "CA-2").estado = "adjudicada"  # terminó sin seleccionada
        s.commit()
        sombra.cerrar_predicciones(s, ahora_utc())
        s.expire_all()
        assert s.get(Prediccion, preds["CA-2"].id).estado == "sin_adjudicacion"


def test_resultado_propio_y_margen_realizado_con_las_funciones_de_f_plat_4(engine, esc):
    with Session(engine) as s:
        sombra.run_mesa(s)
        boris = s.get(Usuario, esc["boris"])
        ca = s.get(CompraAgil, "CA-V")
        ln = s.execute(select(CaLinea).where(CaLinea.ca_codigo == "CA-V")).scalar_one()
        s.add(OfertaPropia(ca_codigo="CA-V", empresa_id=esc["emp"], usuario_id=boris.id,
                           lineas=[{"linea_id": ln.id, "precio_unitario": 95.0,
                                    "proveedor_producto_id": esc["pp2"]}],
                           total_neto=950.0, version=1, guardada_en=ahora_utc()))  # fmt: skip
        eq.cambiar_estado(s, esc["emp"], boris, "CA-V", eq.ENVIADA, 0)
        s.commit()
        _cerrar_ca(s, "CA-V", [(RUT_NUESTRO, True, 940.0), (RUT_OTRO, False, 990.0)])
        sombra.run_mesa(s)
        s.expire_all()
        (p,) = _preds(s)
        assert p.resultado_propio == eq.resultado_de(s, "76123456-0", ca, ahora_utc()) == "ganada"
        o = s.execute(select(OfertaPropia)).scalar_one()
        assert o.precio_ganador_real == pytest.approx(940.0)
        # Costo con el proveedor elegido (caja: 60/kg) + logística de Diguillín + financiero.
        cc = adj.costo_ca(600.0, 10.0, adj.cargar_tarifas(s, esc["emp"]), "Chillán",
                          Settings().costo_financiero_mensual)  # fmt: skip
        assert o.margen_realizado == pytest.approx(940.0 - cc.total)


def test_el_cierre_no_carga_raw_json(engine, esc):
    """Regla 12: los SELECT de cerrar_predicciones y cerrar_ofertas no traen compras_agiles.raw_json."""
    from sqlalchemy import event

    with Session(engine) as s:
        sombra.run_mesa(s)
        ln = s.execute(select(CaLinea).where(CaLinea.ca_codigo == "CA-V")).scalar_one()
        s.add(OfertaPropia(ca_codigo="CA-V", empresa_id=esc["emp"], total_neto=950.0,
                           lineas=[{"linea_id": ln.id, "precio_unitario": 95.0}]))  # fmt: skip
        s.get(CompraAgil, "CA-V").raw_json = {"pesado": "x" * 1000}
        s.commit()
        _cerrar_ca(s, "CA-V", [(RUT_OTRO, True, 900.0)])
    sql: list[str] = []

    def _capturar(conn, cursor, statement, *a):
        sql.append(statement)

    event.listen(engine, "before_cursor_execute", _capturar)
    try:
        with Session(engine) as s:
            sombra.cerrar_predicciones(s, ahora_utc())
            sombra.cerrar_ofertas(s, ahora_utc())
    finally:
        event.remove(engine, "before_cursor_execute", _capturar)
    joins = [q for q in sql if "JOIN compras_agiles" in q]
    assert len(joins) >= 3  # abiertas, cerradas sin resultado propio, ofertas
    assert not [q for q in joins if "compras_agiles.raw_json" in q]


def test_cerrar_ofertas_no_revisa_ca_cerradas_hace_mas_de_dias_espera(engine, esc, monkeypatch):
    with Session(engine) as s:
        _ca(s, "CA-VIEJA", esc["p"], con_indice=False)
        for cod in ("CA-V", "CA-VIEJA"):
            s.add(OfertaPropia(ca_codigo=cod, empresa_id=esc["emp"], lineas=[], total_neto=1.0))
        s.commit()
        _cerrar_ca(s, "CA-V", [(RUT_OTRO, True, 900.0), (RUT_NUESTRO, False, 950.0)])
        _cerrar_ca(s, "CA-VIEJA", [(RUT_OTRO, True, 900.0), (RUT_NUESTRO, False, 950.0)])
        s.get(CompraAgil, "CA-VIEJA").fecha_cierre = ahora_utc() - timedelta(
            days=sombra.DIAS_ESPERA + 1
        )
        s.commit()
        revisadas: list[str] = []
        original = eq.resultado_de

        def _espia(session, rut, ca, ahora):
            revisadas.append(ca.codigo)
            return original(session, rut, ca, ahora)

        monkeypatch.setattr(eq, "resultado_de", _espia)
        sombra.cerrar_ofertas(s, ahora_utc())
        assert revisadas == ["CA-V"]  # perdida y reciente: se revisa; la vieja ya no


def test_resultado_propio_se_completa_si_marcan_enviada_despues_del_cierre(engine, esc):
    with Session(engine) as s:
        _ca(s, "CA-2", esc["p"])
        s.commit()
        sombra.run_mesa(s)
        _cerrar_ca(s, "CA-V", [(RUT_NUESTRO, True, 940.0), (RUT_OTRO, False, 990.0)])
        _cerrar_ca(s, "CA-2", [(RUT_NUESTRO, True, 940.0), (RUT_OTRO, False, 990.0)])
        sombra.cerrar_predicciones(s, ahora_utc() - timedelta(minutes=30))
        s.expire_all()
        assert all(p.estado == "cerrada" and p.resultado_propio is None for p in _preds(s))
        # CA-2 se cerró hace más de DIAS_ESPERA días: fuera de la ventana.
        p2 = next(p for p in _preds(s) if p.ca_codigo == "CA-2")
        p2.cerrada_en = ahora_utc() - timedelta(days=sombra.DIAS_ESPERA + 1)
        boris = s.get(Usuario, esc["boris"])
        eq.cambiar_estado(s, esc["emp"], boris, "CA-V", eq.ENVIADA, 0)  # se registra tarde
        eq.cambiar_estado(s, esc["emp"], boris, "CA-2", eq.ENVIADA, 0)
        s.commit()
        r = sombra.cerrar_predicciones(s, ahora_utc())
        assert r["resultado_propio_tardio"] == 1
        s.expire_all()
        preds = {p.ca_codigo: p for p in _preds(s)}
        assert preds["CA-V"].resultado_propio == eq.GANADA
        assert preds["CA-2"].resultado_propio is None
        # Lo demás de la predicción no se toca.
        assert preds["CA-V"].resultado_precio_ganador == pytest.approx(940.0)


# ---------------------------------------------------------------------------
# Rutas: ficha, simular, guardar, enviar, exportar
# ---------------------------------------------------------------------------


def _form(engine, codigo="CA-V", precio="95", prov=""):
    with Session(engine) as s:
        ln = s.execute(select(CaLinea).where(CaLinea.ca_codigo == codigo)).scalar_one()
        return {f"precio_{ln.id}": precio, f"prov_{ln.id}": prov}


def test_ficha_precio_muestra_referencia_costo_y_curva(client, engine, settings, esc):
    cookies, headers, _ = _auth(settings, esc["boris"])
    r = client.get("/mesa/ca/CA-V/precio", cookies=cookies, headers=headers)
    assert r.status_code == 200
    assert "Precio y mi oferta" in r.text and "comuna:Chillán" in r.text
    assert "<svg" in r.text and "línea base" in r.text
    assert "Mayorista" in r.text and "Fuente: Dirección ChileCompra" in r.text
    # La ficha interna carga la sección.
    r = client.get("/oportunidad/compras_agiles/CA-V", cookies=cookies)
    assert "/mesa/ca/CA-V/precio" in r.text


def test_simular_no_persiste(client, engine, settings, esc):
    cookies, headers, _ = _auth(settings, esc["boris"])
    r = client.post("/mesa/ca/CA-V/simular", data=_form(engine, precio="95"), cookies=cookies,
                    headers=headers)  # fmt: skip
    assert r.status_code == 200 and "$950" in r.text and "P(ganar)" in r.text
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(OfertaPropia)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(CaTrabajo)).scalar_one() == 0
    r = client.post("/mesa/ca/CA-V/simular", data=_form(engine, precio="-3"), cookies=cookies,
                    headers=headers)  # fmt: skip
    assert r.status_code == 422


def test_simular_y_guardar_exigen_csrf(client, engine, settings, esc):
    cookies, _, _ = _auth(settings, esc["boris"])
    for ruta in ("simular", "oferta", "enviar", "exportar"):
        r = client.post(f"/mesa/ca/CA-V/{ruta}", data=_form(engine), cookies=cookies)
        assert r.status_code == 403, ruta


def test_guardar_mueve_priorizada_a_cotizando_y_enviar_a_enviada(client, engine, settings, esc):
    cookies, headers, _ = _auth(settings, esc["boris"])
    with Session(engine) as s:
        eq.cambiar_estado(s, esc["emp"], s.get(Usuario, esc["boris"]), "CA-V", eq.PRIORIZADA, 0)
        s.commit()
    data = {**_form(engine, precio="95", prov=str(esc["pp2"])), "version": "0", "plazo": "3",
            "notas": "flete incluido"}  # fmt: skip
    r = client.post("/mesa/ca/CA-V/oferta", data=data, cookies=cookies, headers=headers)
    assert r.status_code == 200 and "Oferta guardada" in r.text
    with Session(engine) as s:
        o = s.execute(select(OfertaPropia)).scalar_one()
        assert (o.version, o.total_neto, o.plazo_entrega_dias, o.usuario_id) == (1, 950.0, 3, esc["boris"])
        assert o.lineas[0]["proveedor_producto_id"] == esc["pp2"]
        assert eq.trabajo_de(s, esc["emp"], "CA-V").estado == eq.COTIZANDO
    r = client.post("/mesa/ca/CA-V/enviar", data={}, cookies=cookies, headers=headers)
    assert r.status_code == 200 and "Marcada como enviada" in r.text
    with Session(engine) as s:
        assert eq.trabajo_de(s, esc["emp"], "CA-V").estado == eq.ENVIADA
        eventos = [e.datos for e in s.execute(select(CaEvento).order_by(CaEvento.id)).scalars()]
        assert eventos == [{"de": "nueva", "a": "priorizada"}, {"de": "priorizada", "a": "cotizando"},
                           {"de": "cotizando", "a": "enviada"}]  # fmt: skip
    # Guardar otra vez no la saca de «enviada».
    data["version"] = "1"
    assert client.post("/mesa/ca/CA-V/oferta", data=data, cookies=cookies, headers=headers).status_code == 200
    with Session(engine) as s:
        assert eq.trabajo_de(s, esc["emp"], "CA-V").estado == eq.ENVIADA


def test_guardar_con_conflicto_de_version_no_pisa(client, engine, settings, esc):
    cb, hb, _ = _auth(settings, esc["boris"])
    cm, hm, _ = _auth(settings, esc["mario"])
    base = {**_form(engine, precio="95"), "version": "0"}
    assert client.post("/mesa/ca/CA-V/oferta", data=base, cookies=cb, headers=hb).status_code == 200
    # Mario abrió la ficha antes (versión 0) y guarda después: conflicto, no pisa.
    r = client.post("/mesa/ca/CA-V/oferta", data={**base, **_form(engine, precio="80")},
                    cookies=cm, headers=hm)  # fmt: skip
    assert r.status_code == 409 and "Boris" in r.text and "No se guardó" in r.text
    with Session(engine) as s:
        o = s.execute(select(OfertaPropia)).scalar_one()
        assert (o.version, o.total_neto) == (1, 950.0)


def test_enviar_exige_rut_de_la_empresa(client, engine, settings, esc):
    with Session(engine) as s:
        s.get(Empresa, esc["emp"]).rut = None
        s.commit()
    cookies, headers, _ = _auth(settings, esc["boris"])
    r = client.post("/mesa/ca/CA-V/enviar", data={}, cookies=cookies, headers=headers)
    assert r.status_code == 422 and "RUT" in r.text and "/admin/empresas" in r.text
    with Session(engine) as s:
        assert eq.trabajo_de(s, esc["emp"], "CA-V") is None


def test_exportar_xlsx_con_columnas_y_fuente(client, engine, settings, esc):
    from openpyxl import load_workbook

    cookies, headers, csrf = _auth(settings, esc["boris"])
    sin = client.post("/mesa/ca/CA-V/exportar", data={"csrf_token": csrf}, cookies=cookies)
    assert sin.status_code == 404  # nada guardado todavía
    client.post("/mesa/ca/CA-V/oferta", data={**_form(engine, precio="95"), "version": "0",
                                              "plazo": "5"}, cookies=cookies, headers=headers)  # fmt: skip
    r = client.post("/mesa/ca/CA-V/exportar", data={"csrf_token": csrf}, cookies=cookies)
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    filas = [list(f) for f in load_workbook(io.BytesIO(r.content)).active.iter_rows(values_only=True)]
    assert ["N°", "Producto solicitado", "Cantidad", "Precio unitario neto", "Total neto"] in [
        f[:5] for f in filas
    ]
    assert [1, "Leche entera 10 kg", 10, 95, 950] in [f[:5] for f in filas]
    assert any(f[1] == "Plazo de entrega (días)" and f[2] == 5 for f in filas)
    assert [v for v in filas[-1] if v is not None] == ["Fuente: Dirección ChileCompra"]


# ---------------------------------------------------------------------------
# Mesa: solo lee la caché; orden, filtros, tarjeta
# ---------------------------------------------------------------------------


def test_la_mesa_no_llama_evaluar_ca(client, engine, settings, esc, monkeypatch):
    with Session(engine) as s:
        sombra.run_mesa(s)

    def _prohibido(*a, **k):
        raise AssertionError("la mesa no debe evaluar en el request de la lista")

    monkeypatch.setattr(adj, "evaluar_ca", _prohibido)
    monkeypatch.setattr("app.api.query.evaluar_ca", _prohibido)
    cookies, _, _ = _auth(settings, esc["boris"])
    r = client.get("/mesa", cookies=cookies)
    assert r.status_code == 200
    assert "Precio sugerido" in r.text and "Modelo: línea base" in r.text
    assert "Con predicción" in r.text
    assert client.get("/mesa/conteo", params={"con_costo": "1"}, cookies=cookies).status_code == 200


def test_tarjeta_sin_costo_lo_dice_y_no_inventa_margen(client, engine, settings, esc):
    with Session(engine) as s:
        sombra.run_mesa(s)
    cookies, _, _ = _auth(settings, esc["ana"])  # la otra empresa: sin costos
    r = client.get("/mesa", cookies=cookies)
    assert "Falta costo de 1 producto" in r.text
    assert "Sin calcular: falta costo" in r.text


def test_mesa_sin_evaluaciones_avisa(client, engine, settings, esc):
    cookies, _, _ = _auth(settings, esc["boris"])
    r = client.get("/mesa", cookies=cookies)
    assert "Todavía no hay evaluaciones de precio" in r.text
    assert "Sin evaluación de precio todavía" in r.text


def test_orden_por_margen_esperado_y_filtros_nuevos(engine, esc):
    ahora = ahora_utc()
    with Session(engine) as s:
        for cod, horas in (("A", 30), ("B", 40), ("C", 50), ("D", 60)):
            _ca(s, cod, esc["p"], cierre=ahora + timedelta(hours=horas), con_indice=False)
        s.commit()
        cache = {
            "B": Evaluada(ahora, {"margen_esperado": 100.0, "margen_pct": 0.05, "costo_total": 1.0}),
            "C": Evaluada(ahora, {"margen_esperado": 300.0, "margen_pct": 0.20, "costo_total": 1.0}),
            "D": Evaluada(ahora, {"margen_esperado": None, "margen_pct": None, "costo_total": None}),
        }
        f = FiltrosMesa()
        codigos = buscar_codigos(s, [16], [], f, ahora, valor=valor_orden(cache))
        # Con valor primero (mayor a menor); sin evaluación al final, por cierre (CA-V: 48 h).
        assert codigos == ["C", "B", "A", "CA-V", "D"]
        assert buscar_codigos(s, [16], [], f, ahora) == ["A", "B", "CA-V", "C", "D"]  # por cierre
        assert filtrar_precio(codigos, FiltrosMesa(con_costo=True), cache, set()) == ["C", "B"]
        assert filtrar_precio(codigos, FiltrosMesa(margen_min=0.1), cache, set()) == ["C"]
        assert filtrar_precio(codigos, FiltrosMesa(con_prediccion=True), cache, {"B"}) == ["B"]


def test_comunas_prioritarias_siguen_primero(engine, esc):
    ahora = ahora_utc()
    with Session(engine) as s:
        _ca(s, "LEJOS", esc["p"], con_indice=False, direccion_entrega="Calle 1, Pinto")
        _ca(s, "CERCA", esc["p"], con_indice=False, direccion_entrega="Calle 2, Chillán")
        s.commit()
        cache = {"LEJOS": Evaluada(ahora, {"margen_esperado": 999.0})}
        codigos = buscar_codigos(s, [16], ["Chillán"], FiltrosMesa(), ahora,
                                 valor=valor_orden(cache))  # fmt: skip
        assert codigos[0] == "CERCA" and codigos[1] == "LEJOS"


def test_filtros_por_url_y_conteo(client, engine, settings, esc):
    with Session(engine) as s:
        sombra.run_mesa(s)
    cookies, _, _ = _auth(settings, esc["boris"])
    assert ">1<" in client.get("/mesa/conteo", params={"con_prediccion": "1"}, cookies=cookies).text
    assert "Sin CA vigentes" in client.get("/mesa/conteo", params={"margen_min": "99"}, cookies=cookies).text
    r = client.get("/mesa", params={"margen_min": "abc", "orden": "raro"}, cookies=cookies)
    assert r.status_code == 200
    ca, _, _ = _auth(settings, esc["ana"])
    assert "Sin CA vigentes" in client.get("/mesa/conteo", params={"con_prediccion": "1"}, cookies=ca).text


# ---------------------------------------------------------------------------
# Ownership, retención, resultados
# ---------------------------------------------------------------------------


def test_ownership_cruzado(client, engine, settings, esc):
    cb, hb, _ = _auth(settings, esc["boris"])
    ca_, ha, csrf_a = _auth(settings, esc["ana"])
    client.post("/mesa/ca/CA-V/oferta", data={**_form(engine, precio="95"), "version": "0"},
                cookies=cb, headers=hb)  # fmt: skip
    # Ana (otra empresa) no ve la oferta de Boris, ni sus proveedores, ni puede exportarla.
    r = client.get("/mesa/ca/CA-V/precio", cookies=ca_, headers=ha)
    assert r.status_code == 200 and "Mayorista" not in r.text and "oferta guardada" not in r.text
    assert client.post("/mesa/ca/CA-V/exportar", data={"csrf_token": csrf_a}, cookies=ca_).status_code == 404
    # Un proveedor de la otra empresa en el formulario → 404.
    r = client.post("/mesa/ca/CA-V/simular", data=_form(engine, prov=str(esc["pp_otra"])),
                    cookies=cb, headers=hb)  # fmt: skip
    assert r.status_code == 404
    # CA fuera del territorio activo → 404.
    with Session(engine) as s:
        _ca(s, "CA-RM", esc["p"], region=13, con_indice=False)
        s.commit()
    for ruta in ("precio", "competidores"):
        assert client.get(f"/mesa/ca/CA-RM/{ruta}", cookies=cb).status_code == 404
    assert client.post("/mesa/ca/CA-RM/oferta", data={"version": "0"}, cookies=cb,
                       headers=hb).status_code == 404  # fmt: skip
    # Sin empresa: 403.
    with Session(engine) as s:
        u = Usuario(email="sin@t.cl", password_hash=hash_password(_PW))
        s.add(u)
        s.commit()
        sin = u.id
    cs, hs, _ = _auth(settings, sin)
    assert client.get("/mesa/ca/CA-V/precio", cookies=cs, headers=hs).status_code == 403
    assert client.get("/mesa/resultados", cookies=cs).status_code == 403


def test_retencion_no_purga_ca_con_prediccion_u_oferta(engine, esc):
    viejo = ahora_utc() - timedelta(days=200)
    with Session(engine) as s:
        for cod in ("PRED", "OFER", "NADA"):
            _ca(s, cod, esc["p"], con_indice=False, estado="adjudicada", region=13)
        s.add(Prediccion(ca_codigo="PRED", empresa_id=esc["emp"], modelo_tipo="base",
                         precio_sugerido=1.0, detalle={}, estado="cerrada"))  # fmt: skip
        s.add(OfertaPropia(ca_codigo="OFER", empresa_id=esc["emp"], lineas=[], total_neto=0.0))
        s.flush()
        s.execute(CompraAgil.__table__.update().where(CompraAgil.codigo.in_(["PRED", "OFER", "NADA"]))
                  .values(actualizado_en=viejo))  # fmt: skip
        s.commit()
        purgar_terminales(s, dias=90, regiones_protegidas=[16])
        s.commit()
        quedan = set(s.execute(select(CaProducto.ca_codigo)).scalars())
        assert {"PRED", "OFER"} <= quedan and "NADA" not in quedan


def test_resultados_con_cero_filas(client, settings, esc):
    cookies, _, _ = _auth(settings, esc["boris"])
    r = client.get("/mesa/resultados", cookies=cookies)
    assert r.status_code == 200
    assert "Todavía no hay predicciones cerradas" in r.text and "n = 0" in r.text


def test_resultados_con_filas_metricas_y_solo_de_la_empresa(client, engine, settings, esc):
    with Session(engine) as s:
        _ca(s, "CA-2", esc["p"])
        s.commit()
        sombra.run_mesa(s)
        boris = s.get(Usuario, esc["boris"])
        eq.cambiar_estado(s, esc["emp"], boris, "CA-V", eq.ENVIADA, 0)
        s.commit()
        preds = {p.ca_codigo: p for p in _preds(s)}
        lo, hi = preds["CA-V"].intervalo_80_min, preds["CA-V"].intervalo_80_max
        _cerrar_ca(s, "CA-V", [(RUT_OTRO, True, (lo + hi) / 2), (RUT_NUESTRO, False, hi)])
        _cerrar_ca(s, "CA-2", [], estado="desierta")
        sombra.cerrar_predicciones(s, ahora_utc())
        r = sombra.resultados(s, esc["emp"], ahora_utc())
        assert (r["n"], r["n_cerradas"], r["n_sin_adjudicacion"]) == (2, 1, 1)
        assert (r["aciertos_intervalo"], r["n_intervalo"]) == (1, 1)
        assert r["n_brier"] == 1 and r["brier"] == pytest.approx(preds["CA-V"].p_ganar_sugerido ** 2)
        assert r["error_mediano"] is not None
        assert sombra.resultados(s, esc["otra"], ahora_utc())["n"] == 0
    cookies, _, _ = _auth(settings, esc["boris"])
    t = client.get("/mesa/resultados", cookies=cookies).text
    assert "Perdida" in t and "sin adjudicación" in t and "dentro" in t
    ca_, _, _ = _auth(settings, esc["ana"])
    assert "Todavía no hay predicciones cerradas" in client.get("/mesa/resultados", cookies=ca_).text


def test_competidores_de_la_tarjeta_son_historial(client, engine, settings, esc):
    cookies, _, _ = _auth(settings, esc["boris"])
    r = client.get("/mesa/ca/CA-V/competidores", cookies=cookies)
    assert r.status_code == 200
    assert "Es historial" in r.text and "probables" not in r.text
