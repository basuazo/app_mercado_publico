"""F-plat-7: salud del pivote, informe mensual, aviso de ofertas sin resultado y retención.

Todo offline (SQLite en memoria). El cierre de predicciones y de ofertas (`sin_adjudicacion`,
`DIAS_ESPERA`) ya lo prueba test_plat5: acá solo lo nuevo de la fase.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.alerts import email as email_mod
from app.api.main import create_app
from app.api.salud_data import get_salud_data
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token
from app.clients.base import QuotaTracker, requests_hoy
from app.core import retencion
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.ingest import orchestrator
from app.mercado import equipo as eq
from app.mercado import informe, salud_pivote, sombra
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaAnexo,
    CaLinea,
    CaProducto,
    CaTrabajo,
    CompraAgil,
    Empresa,
    EvaluacionCache,
    EvaluacionModelo,
    JobRun,
    LlmCache,
    LlmUso,
    OfertaPropia,
    Prediccion,
    ProductoAlias,
    ProductoCanonico,
    Recomendacion,
    SyncState,
    Usuario,
)

_PW = "contraseña-segura-test"
_LOCK_LIBRE = lambda conn, key: True  # noqa: E731
_UNLOCK = lambda conn, key: None  # noqa: E731


@pytest.fixture()
def engine():
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(e)
    yield e
    e.dispose()


def _settings(**kw: Any) -> Settings:
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url="sqlite:///:memory:",
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
        **kw,
    )


@pytest.fixture()
def settings():
    return _settings()


@pytest.fixture()
def client(engine, settings):
    return TestClient(create_app(settings, engine))


def _cookies(settings, uid):
    return {COOKIE_NAME: create_session_token(settings.secret_key, uid)}


def _job(s, job, requests=None, *, estado="ok", horas_atras=0.0, **extra):
    res = dict(extra)
    if requests is not None:
        res["requests"] = requests
    s.add(
        JobRun(
            job=job,
            iniciado_en=ahora_utc() - timedelta(hours=horas_atras),
            estado=estado,
            resultado_json=res or None,
        )
    )


# ---------------------------------------------------------------------------
# requests por job
# ---------------------------------------------------------------------------


def test_run_with_lock_graba_los_requests_del_job(engine):
    """El delta de `quota_log` entre el inicio y el fin queda en `job_runs.resultado_json`."""
    cuota = QuotaTracker(engine, budget=9000)

    def job():
        cuota.consume(3)
        return {"x": 1}

    r = orchestrator._run_with_lock(
        "mercado-ca", job, engine, try_lock_fn=_LOCK_LIBRE, unlock_fn=_UNLOCK
    )
    assert r == {"x": 1}  # el valor que devuelve el job no cambia
    with Session(engine) as s:
        fila = s.execute(select(JobRun)).scalar_one()
        assert fila.resultado_json == {"requests": 3, "x": 1}
    assert requests_hoy(engine) == 3


def test_run_with_lock_respeta_el_requests_propio_y_no_inventa(engine):
    cuota = QuotaTracker(engine, budget=9000)

    def job():
        cuota.consume(2)
        return {"requests": 7}  # el backfill trae su propio conteo

    orchestrator._run_with_lock(
        "mercado-backfill", job, engine, try_lock_fn=_LOCK_LIBRE, unlock_fn=_UNLOCK
    )
    with Session(engine) as s:
        assert s.execute(select(JobRun)).scalar_one().resultado_json == {"requests": 7}


def test_run_with_lock_graba_los_requests_aunque_el_job_falle(engine):
    """Un job que cae tras gastar ticket igual se ve en la suma de /salud (alerta R-p1)."""
    cuota = QuotaTracker(engine, budget=9000)

    def job():
        cuota.consume(4)
        raise RuntimeError("boom")

    assert orchestrator._run_with_lock(
        "ca", job, engine, try_lock_fn=_LOCK_LIBRE, unlock_fn=_UNLOCK
    ) is None
    with Session(engine) as s:
        fila = s.execute(select(JobRun)).scalar_one()
        assert fila.estado == "error" and fila.resultado_json == {"requests": 4}
        assert salud_pivote.cuota_por_job(s, ahora_utc())["alerta_no_mercado"] == ["ca"]


def test_run_with_lock_sin_quota_log_no_agrega_requests():
    """Base sin tabla `quota_log` (tests, bases viejas): la corrida se graba igual, sin `requests`."""
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(e)
    assert requests_hoy(e) is None
    orchestrator._run_with_lock(
        "retencion", lambda: {"a": 1}, e, try_lock_fn=_LOCK_LIBRE, unlock_fn=_UNLOCK
    )
    with Session(e) as s:
        assert s.execute(select(JobRun)).scalar_one().resultado_json == {"a": 1}


# ---------------------------------------------------------------------------
# /salud → Pivote
# ---------------------------------------------------------------------------


def test_cuota_por_job_suma_el_dia_y_alerta_si_no_es_mercado(engine):
    with Session(engine) as s:
        _job(s, "mercado-ca", 100)
        _job(s, "mercado-ca", 50)
        _job(s, "mercado-backfill", 400)
        _job(s, "mercado-oc", 0)
        _job(s, "ca", 30)  # un job viejo gastó ticket: R-p1
        _job(s, "nocturno", 9, estado="error")  # un job que cayó después de gastar ticket cuenta
        _job(s, "mercado-oc", estado="omitido")  # omitido por lock: sin requests, no suma
        _job(s, "mercado-ca", 9999, horas_atras=48)  # de otro día
        s.commit()
        c = salud_pivote.cuota_por_job(s, ahora_utc())
    assert (c["mercado_ca"], c["mercado_backfill"], c["total"]) == (150, 400, 589)
    assert c["otros"] == {"ca": 30, "nocturno": 9}
    assert c["alerta_no_mercado"] == ["ca", "nocturno"]


def test_cuota_sin_alerta_si_solo_corre_mercado(engine):
    with Session(engine) as s:
        _job(s, "mercado-ca", 10)
        _job(s, "mercado-backfill", 5)
        s.commit()
        assert salud_pivote.cuota_por_job(s, ahora_utc())["alerta_no_mercado"] == []


def test_pausa_429_vigente_en_hora_de_chile(engine):
    ahora = ahora_utc()
    with Session(engine) as s:
        assert salud_pivote.pausa_429(s, ahora) is None
        hasta = (ahora + timedelta(minutes=20)).isoformat(timespec="seconds")
        s.add(SyncState(fuente="api_pausa", cursor=hasta, notas="429 de tasa"))
        s.commit()
        p = salud_pivote.pausa_429(s, ahora)
        assert p is not None and p["minutos"] in (19, 20) and p["causa"] == "429 de tasa"
        assert len(p["hasta_chile"]) == 5
        # Una pausa vencida no se muestra.
        assert salud_pivote.pausa_429(s, ahora + timedelta(hours=1)) is None


def test_tamano_pivote_alerta_al_70_por_ciento():
    mb = 1024 * 1024
    chico = salud_pivote.tamano_pivote([{"tamano_bytes": 100 * mb}, {"tamano_bytes": None}], 200 * mb)
    assert not chico["alerta_tablas"] and not chico["alerta_bd"]
    grande = salud_pivote.tamano_pivote([{"tamano_bytes": 351 * mb}], 360 * mb)
    assert grande["alerta_tablas"] and grande["alerta_bd"]


def _empresa(s, nombre="Nuestra", rut="76.123.456-0", regiones=(16,)):
    e = Empresa(nombre=nombre, rut=rut, regiones_prioritarias=list(regiones),
                comunas_prioritarias=[], activa=True)  # fmt: skip
    s.add(e)
    s.flush()
    return e


def _usuario(s, email, empresa_id, *, dias_resumen=3, activo=True, rol=RolUsuario.USUARIO):
    u = Usuario(email=email, password_hash=hash_password(_PW), empresa_id=empresa_id,
                dias_resumen=dias_resumen, activo=activo, rol=rol)  # fmt: skip
    s.add(u)
    s.flush()
    return u


def _ca(s, codigo, *, region=16, publicada=None, cierre=None, estado="publicada",
        detalle="completo", monto=100_000.0, organismo="Municipalidad de Chillán"):  # fmt: skip
    ahora = ahora_utc()
    s.add(CompraAgil(
        codigo=codigo, nombre=f"CA {codigo}", estado=estado, region=region,
        organismo_nombre=organismo, monto_disponible_clp=monto,
        fecha_publicacion=publicada or ahora - timedelta(days=1),
        fecha_cierre=cierre or ahora + timedelta(days=2), detalle_lineas=detalle,
    ))  # fmt: skip
    s.flush()


def test_territorio_catalogo_llm_y_sombra(engine, settings):
    ahora = ahora_utc()
    with Session(engine) as s:
        e1 = _empresa(s)
        e2 = _empresa(s, "Otra", "11.111.111-1", regiones=(16, 8))
        u = _usuario(s, "mario@t.cl", e1.id)
        p = ProductoCanonico(nombre="Leche", unidad_base="kg", activo=True)
        s.add(p)
        s.flush()
        _ca(s, "A", detalle="completo")
        _ca(s, "B", detalle="sin_detalle")
        _ca(s, "C", detalle="incompleto")
        _ca(s, "D", region=5, detalle="sin_detalle")  # fuera del territorio: no cuenta
        _ca(s, "E", publicada=ahora - timedelta(days=60), detalle="sin_detalle")  # fuera de 30 días
        s.add(CaLinea(ca_codigo="A", n=1, texto_origen="leche", estado="ok", producto_id=p.id,
                      revisado_por=u.id, revisado_en=ahora))  # fmt: skip
        s.add(CaLinea(ca_codigo="B", n=1, texto_origen="pan", estado="pendiente"))
        s.add(CaLinea(ca_codigo="B", n=2, texto_origen="queso", estado="dudosa"))
        s.add(ProductoAlias(texto_normalizado="leche", producto_id=p.id, fuente="humano"))
        s.add(ProductoAlias(texto_normalizado="pan", producto_id=p.id, fuente="auto"))
        s.add(LlmUso(mes=ahora.strftime("%Y-%m"), requests=4, costo_usd=1.25, estimado=False))
        for costo in (0.0, 0.0, 0.05):  # 2 de 3 salieron de la caché
            s.add(CaAnexo(ca_codigo="A", mime="application/pdf", tamano=1, sha256="x" * 64,
                          extraccion_estado="hecha", extraccion_costo_usd=costo))  # fmt: skip
        s.add(Prediccion(ca_codigo="A", empresa_id=e1.id, modelo_tipo="base", precio_sugerido=1.0,
                         detalle={}, estado="cerrada", resultado_en_intervalo=True))  # fmt: skip
        s.add(Prediccion(ca_codigo="B", empresa_id=e1.id, modelo_tipo="base", precio_sugerido=1.0,
                         detalle={}, estado="cerrada", resultado_en_intervalo=False))  # fmt: skip
        s.add(Prediccion(ca_codigo="C", empresa_id=e1.id, modelo_tipo="base", precio_sugerido=1.0,
                         detalle={}, estado="abierta"))  # fmt: skip
        s.add(Prediccion(ca_codigo="D", empresa_id=e2.id, modelo_tipo="base", precio_sugerido=1.0,
                         detalle={}, estado="sin_adjudicacion"))  # fmt: skip
        s.add(EvaluacionModelo(mes="2026-10", modelo="demanda", metrica="mape", valor=0.4, n=30))
        s.commit()
        t = salud_pivote.territorio(s)
        assert len(t["regiones"]) == 2  # la unión de las dos empresas: 16 y 8
        assert [x["empresa"] for x in t["empresas"]] == ["Nuestra", "Otra"]
        assert t["ultima_mercado_ca"] is None
        c = salud_pivote.catalogo(s, [16], ahora)
        assert c["ca"] == {"sin_detalle": 1, "incompleto": 1, "completo": 1}
        assert (c["lineas_pendientes"], c["alias_aprendidos"]) == (2, 1)
        assert c["correcciones"] == [{"usuario": "mario@t.cl", "correcciones": 1}]
        ll = salud_pivote.llm(s, _settings(llm_presupuesto_mes_usd=5.0), ahora)
        assert (ll["gasto_usd"], ll["pct_presupuesto"], ll["requests"]) == (1.25, 25.0, 4)
        assert ll["pct_cache"] == pytest.approx(66.7, abs=0.1) and ll["extracciones"] == 3
        sh = {x["empresa"]: x for x in salud_pivote.sombra(s)}
        assert sh["Nuestra"]["abiertas"] == 1 and sh["Nuestra"]["cerradas"] == 2
        assert sh["Nuestra"]["pct_acierto"] == 50.0
        assert sh["Otra"]["sin_adjudicacion"] == 1 and sh["Otra"]["pct_acierto"] is None
        d = salud_pivote.demanda(s)
        assert d == {"mes": "2026-10", "metricas": [{"metrica": "mape", "valor": 0.4, "n": 30}]}


def test_territorio_trae_la_ultima_corrida_buena_de_mercado_ca(engine):
    with Session(engine) as s:
        _job(s, "mercado-ca", 12, horas_atras=3, ca_listadas=50, secreto_no_listado="x")
        _job(s, "mercado-ca", 7, estado="error", horas_atras=1)
        _job(s, "mercado-ca", 20, horas_atras=2, ca_listadas=80, anexos_bajados=2)
        s.commit()
        u = salud_pivote.territorio(s)["ultima_mercado_ca"]
    assert u["ca_listadas"] == 80 and u["anexos_bajados"] == 2 and u["requests"] == 20
    assert "secreto_no_listado" not in u


def test_salud_data_incluye_pivote_y_las_tablas_nuevas(engine, settings):
    with Session(engine) as s:
        _job(s, "match", 5)
        s.commit()
        data = get_salud_data(s, settings)
    assert data["pivote"]["cuota"]["alerta_no_mercado"] == ["match"]
    nombres = {t["tabla"] for t in data["tablas_mercado"]}
    assert {"competencia_mes", "competencia_comprador_mes", "demanda_series", "pronosticos",
            "demanda_nombres", "recomendaciones", "evaluaciones_cache", "ca_trabajo", "ca_eventos",
            "ca_comentarios", "empresa_productos", "empresa_producto_movimientos", "ca_lineas",
            "ca_anexos", "predicciones", "producto_alias", "proveedores"} <= nombres  # fmt: skip
    assert len(nombres) == len(data["tablas_mercado"])  # ninguna repetida
    for secreto in ("TICKET_TEST", "secret-test-key", "jobs-token-secreto"):
        assert secreto not in json.dumps(data, default=str)


def test_pagina_salud_pinta_el_pivote_y_la_alerta(client, engine, settings):
    with Session(engine) as s:
        admin = _usuario(s, "admin@t.cl", None, rol=RolUsuario.ADMIN)
        _job(s, "ca", 40)
        _job(s, "mercado-ca", 10)
        s.commit()
        uid = admin.id
    r = client.get("/salud", cookies=_cookies(settings, uid))
    assert r.status_code == 200
    assert "Cuota por job" in r.text and "alerta-cuota-no-mercado" in r.text
    assert "Modo sombra" in r.text and "LLM (OpenRouter" in r.text


def test_pagina_salud_sin_alerta_si_solo_mercado(client, engine, settings):
    with Session(engine) as s:
        admin = _usuario(s, "admin@t.cl", None, rol=RolUsuario.ADMIN)
        _job(s, "mercado-ca", 10)
        s.commit()
        uid = admin.id
    assert "alerta-cuota-no-mercado" not in client.get("/salud", cookies=_cookies(settings, uid)).text


def test_salud_solo_admin(client, engine, settings):
    with Session(engine) as s:
        e = _empresa(s)
        u = _usuario(s, "u@t.cl", e.id)
        s.commit()
        uid = u.id
    assert client.get("/salud", cookies=_cookies(settings, uid)).status_code == 403


# ---------------------------------------------------------------------------
# Informe mensual
# ---------------------------------------------------------------------------

MES = informe.mes_anterior(ahora_utc())


def _escenario_informe(s):
    """Dos empresas con usuarios; la primera con CA del mes, ofertas, predicciones y recomendaciones."""
    ini, _ = informe.rango_mes_utc(MES)
    dentro = ini + timedelta(days=5)
    e1 = _empresa(s)
    e2 = _empresa(s, "Otra", "11.111.111-1")
    u1 = _usuario(s, "boris@t.cl", e1.id)
    u2 = _usuario(s, "mario@t.cl", e1.id)
    _usuario(s, "off@t.cl", e1.id, dias_resumen=0)  # no recibe
    _usuario(s, "baja@t.cl", e1.id, activo=False)  # no recibe
    u3 = _usuario(s, "ana@t.cl", e2.id)
    _ca(s, "M1", publicada=dentro, cierre=dentro + timedelta(days=2), monto=200_000.0,
        organismo="Municipalidad de Chillán", detalle="sin_detalle")  # fmt: skip
    _ca(s, "M2", publicada=dentro, cierre=dentro + timedelta(days=3), monto=300_000.0,
        organismo="Municipalidad de Chillán")  # fmt: skip
    _ca(s, "M3", publicada=dentro, cierre=dentro + timedelta(days=3), monto=50_000.0,
        organismo="Hospital de Chillán")  # fmt: skip
    _ca(s, "FUERA", publicada=dentro - timedelta(days=90), monto=9_000_000.0)
    _ca(s, "OTRAREG", region=5, publicada=dentro, monto=9_000_000.0)
    for cod in ("M1", "M2", "M3"):
        s.add(CaProducto(ca_codigo=cod, codigo_producto="50131701", nombre="Leche", cantidad=1,
                         unidad="Kilo"))  # fmt: skip
        s.add(CaLinea(ca_codigo=cod, n=1, texto_origen="leche", estado="ok"))
    s.add(CaTrabajo(empresa_id=e1.id, ca_codigo="M1", estado=eq.ENVIADA, resultado=eq.GANADA))
    s.add(CaTrabajo(empresa_id=e1.id, ca_codigo="M2", estado=eq.ENVIADA, resultado=eq.PERDIDA))
    s.add(CaTrabajo(empresa_id=e1.id, ca_codigo="M3", estado=eq.ENVIADA))
    s.add(OfertaPropia(ca_codigo="M1", empresa_id=e1.id, lineas=[], total_neto=180_000.0,
                       precio_ganador_real=180_000.0, margen_realizado=27_000.0))  # fmt: skip
    # La otra empresa no debe ver nada de esto.
    s.add(CaTrabajo(empresa_id=e2.id, ca_codigo="M1", estado=eq.ENVIADA, resultado=eq.PERDIDA))
    s.add(Prediccion(ca_codigo="M1", empresa_id=e1.id, modelo_tipo="base", precio_sugerido=1.0,
                     detalle={}, estado="cerrada", cierre_ca=dentro, resultado_en_intervalo=True,
                     p_ganar_sugerido=0.6, resultado_propio=eq.GANADA))  # fmt: skip
    s.add(Prediccion(ca_codigo="M2", empresa_id=e1.id, modelo_tipo="base", precio_sugerido=1.0,
                     detalle={}, estado="cerrada", cierre_ca=dentro, resultado_en_intervalo=False,
                     p_ganar_sugerido=0.4, resultado_propio=eq.PERDIDA))  # fmt: skip
    for i in range(7):
        s.add(Recomendacion(empresa_id=e1.id, tipo="precio_firme", nivel="producto", item=f"i{i}",
                            comprador="", mes_objetivo=MES, texto=f"Recomendación {i}",
                            datos={"monto": 1000 * i}, estado="nueva"))  # fmt: skip
    s.add(Recomendacion(empresa_id=e1.id, tipo="precio_firme", nivel="producto", item="x",
                        comprador="", mes_objetivo=MES, texto="Ya descartada", datos={},
                        estado="descartada"))  # fmt: skip
    s.add(Recomendacion(empresa_id=e2.id, tipo="precio_firme", nivel="producto", item="z",
                        comprador="", mes_objetivo=MES, texto="De la otra empresa", datos={},
                        estado="nueva"))  # fmt: skip
    s.commit()
    return {"e1": e1.id, "e2": e2.id, "u1": u1.id, "u2": u2.id, "u3": u3.id}


def test_informe_construye_el_contenido(engine):
    with Session(engine) as s:
        ids = _escenario_informe(s)
        i = informe.construir(s, ids["e1"], MES)
    assert i["ca"]["n"] == 3 and i["ca"]["monto"] == "$550.000"
    assert i["compradores"][0] == {"nombre": "Municipalidad de Chillán", "n": 2, "monto": "$500.000"}
    assert i["cotizadas"] == {"n": 3, "ganadas": 1, "perdidas": 1, "sin_resultado": 1}
    assert i["margen"]["total"] == "$27.000" and i["margen"]["vendido"] == "$180.000"
    assert [f["margen"] for f in i["margen"]["por_familia"]] == ["$27.000"]
    p = i["predicciones"]
    assert (p["n"], p["aciertos"], p["n_intervalo"], p["pct_intervalo"]) == (2, 1, 2, "50 %")
    assert p["brier"] == "0,160" and p["p_media"] == "50 %" and p["tasa_ganadas"] == "50 %"
    assert len(i["recomendaciones"]) == 5  # el top 5, solo las vigentes y solo de la empresa
    assert all("descartada" not in r["texto"] and "otra empresa" not in r["texto"]
               for r in i["recomendaciones"])  # fmt: skip
    assert i["recomendaciones"][0]["texto"] == "Recomendación 6"
    assert i["catalogo"] == {"lineas_confirmadas": 3, "ca_sin_detalle": 1}
    assert i["pie"] == "Fuente: Dirección ChileCompra"


def test_informe_de_empresa_sin_datos_no_falla(engine):
    with Session(engine) as s:
        e = _empresa(s)
        s.commit()
        i = informe.construir(s, e.id, MES)
    assert i["ca"]["n"] == 0 and i["margen"]["total"] == "—" and i["predicciones"]["brier"] == "—"
    assert i["compradores"] == [] and i["recomendaciones"] == []


@pytest.fixture()
def correos(monkeypatch):
    enviados: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(email_mod, "_smtp_send", lambda st, to, subj, txt, html:
                        enviados.append((to, subj, txt, html)))  # fmt: skip
    return enviados


def test_un_informe_por_usuario_elegible_y_cada_empresa_el_suyo(engine, correos):
    st = _settings(brevo_api_key="k", smtp_from="a@t.cl", app_base_url="https://app.test")
    with Session(engine) as s:
        _escenario_informe(s)
        r = informe.enviar_informes(s, st)
    assert r["informes_enviados"] == 3 and r["empresas"] == 2 and r["errores"] == 0
    assert sorted(c[0] for c in correos) == ["ana@t.cl", "boris@t.cl", "mario@t.cl"]
    para_boris = next(c for c in correos if c[0] == "boris@t.cl")
    assert "Nuestra" in para_boris[1] and "Recomendación 6" in para_boris[2]
    assert "Fuente: Dirección ChileCompra" in para_boris[2] and "Fuente: Dirección ChileCompra" in para_boris[3]
    assert f"https://app.test/mesa/informe/{MES}" in para_boris[3]
    para_ana = next(c for c in correos if c[0] == "ana@t.cl")
    assert "Otra" in para_ana[1] and "Recomendación" not in para_ana[2]  # nada de la otra empresa


def test_redisparar_el_mismo_mes_no_duplica(engine, correos):
    st = _settings(brevo_api_key="k", smtp_from="a@t.cl")
    with Session(engine) as s:
        _escenario_informe(s)
        informe.enviar_informes(s, st)
        n = len(correos)
        r2 = informe.enviar_informes(s, st)
        marca = s.get(SyncState, f"mercado_informe:1:{MES}")
    assert len(correos) == n == 3
    assert r2["informes_enviados"] == 0 and r2["ya_enviados"] == 2
    assert marca is not None and marca.cursor == "1,2" and marca.ultimo_ok is not None


def test_un_usuario_nuevo_recibe_solo_su_informe(engine, correos):
    st = _settings(brevo_api_key="k", smtp_from="a@t.cl")
    with Session(engine) as s:
        ids = _escenario_informe(s)
        informe.enviar_informes(s, st)
        _usuario(s, "nuevo@t.cl", ids["e1"])
        s.commit()
        informe.enviar_informes(s, st)
    assert [c[0] for c in correos].count("nuevo@t.cl") == 1 and len(correos) == 4


def test_tope_diario_deja_pendiente_y_no_marca(engine, correos):
    st = _settings(brevo_api_key="k", smtp_from="a@t.cl", email_daily_limit=2)
    with Session(engine) as s:
        _escenario_informe(s)
        r = informe.enviar_informes(s, st)
        assert r["informes_enviados"] == 2 and r["pospuestos"] == 1
        assert "avisos" in r
    assert len(correos) == 2
    # Al día siguiente (cupo nuevo) se completa lo pendiente, sin repetir lo enviado.
    st2 = _settings(brevo_api_key="k", smtp_from="a@t.cl", email_daily_limit=250)
    with Session(engine) as s:
        s.execute(SyncState.__table__.update().where(SyncState.fuente == "alerts_email")
                  .values(requests_usadas_hoy=0))  # fmt: skip
        s.commit()
        r2 = informe.enviar_informes(s, st2)
    assert r2["informes_enviados"] == 1 and len(correos) == 3
    assert len({c[0] for c in correos}) == 3


def test_un_correo_que_falla_no_corta_ni_se_marca(engine, monkeypatch):
    llamadas: list[str] = []

    def malo(st, to, subj, txt, html):
        llamadas.append(to)
        if to == "boris@t.cl":
            raise RuntimeError("Brevo caído")

    monkeypatch.setattr(email_mod, "_smtp_send", malo)
    st = _settings(brevo_api_key="k", smtp_from="a@t.cl")
    with Session(engine) as s:
        _escenario_informe(s)
        r = informe.enviar_informes(s, st)
        assert r["errores"] == 1 and r["informes_enviados"] == 2
        assert s.get(SyncState, f"mercado_informe:1:{MES}").cursor == "2"  # solo Mario quedó marcado


def test_sin_proveedor_de_correo_no_falla_y_lo_dice(engine, correos):
    with Session(engine) as s:
        _escenario_informe(s)
        r = informe.enviar_informes(s, _settings())  # sin BREVO_API_KEY ni SMTP
        assert s.execute(select(func.count()).select_from(SyncState)).scalar_one() == 0
    assert r["sin_proveedor_correo"] is True and r["informes_enviados"] == 0
    assert "BREVO_API_KEY" in r["avisos"][0] and correos == []


def test_runner_del_job_y_cli(engine, correos, settings):
    from app.ingest.__main__ import _JOBS

    assert "mercado-informe" in _JOBS
    r = orchestrator.run_mercado_informe(settings, engine)
    assert r["sin_proveedor_correo"] is True


def test_vista_web_del_informe_con_ownership(client, engine, settings):
    with Session(engine) as s:
        ids = _escenario_informe(s)
    boris = _cookies(settings, ids["u1"])
    r = client.get(f"/mesa/informe/{MES}", cookies=boris)
    assert r.status_code == 200
    assert "Municipalidad de Chillán" in r.text and "Recomendación 6" in r.text
    assert "De la otra empresa" not in r.text and "Fuente: Dirección ChileCompra" in r.text
    # La empresa sale de la sesión: Ana ve su propio informe (vacío), no el de Nuestra.
    ana = client.get(f"/mesa/informe/{MES}", cookies=_cookies(settings, ids["u3"]))
    assert ana.status_code == 200
    # Las CA son públicas (mismo territorio); lo comercial es de cada empresa (regla 26).
    assert "Recomendación" not in ana.text and "$27.000" not in ana.text
    assert "De la otra empresa" in ana.text and "$27.000" in r.text


def test_vista_web_mes_invalido_futuro_y_sin_sesion(client, engine, settings):
    with Session(engine) as s:
        ids = _escenario_informe(s)
    c = _cookies(settings, ids["u1"])
    for malo in ("2026-13", "26-10", "abc", "2999-01"):
        assert client.get(f"/mesa/informe/{malo}", cookies=c).status_code == 404
    assert client.get(f"/mesa/informe/{MES}", follow_redirects=False).status_code in (302, 303, 401)


# ---------------------------------------------------------------------------
# §1.3: aviso de ofertas enviadas sin resultado
# ---------------------------------------------------------------------------


def test_ofertas_sin_resultado_solo_enviadas_viejas_y_de_la_empresa(engine, client, settings):
    ahora = ahora_utc()
    with Session(engine) as s:
        e1 = _empresa(s)
        e2 = _empresa(s, "Otra", "11.111.111-1")
        u = _usuario(s, "boris@t.cl", e1.id)
        _usuario(s, "ana@t.cl", e2.id)
        _ca(s, "VIEJA", cierre=ahora - timedelta(days=20))
        _ca(s, "RECIENTE", cierre=ahora - timedelta(days=5))
        _ca(s, "RESUELTA", cierre=ahora - timedelta(days=20))
        _ca(s, "NOENVIADA", cierre=ahora - timedelta(days=20))
        for cod in ("VIEJA", "RECIENTE", "RESUELTA"):
            s.add(CaTrabajo(empresa_id=e1.id, ca_codigo=cod, estado=eq.ENVIADA,
                            resultado=eq.PERDIDA if cod == "RESUELTA" else None))  # fmt: skip
        s.add(CaTrabajo(empresa_id=e1.id, ca_codigo="NOENVIADA", estado=eq.COTIZANDO))
        s.add(CaTrabajo(empresa_id=e2.id, ca_codigo="VIEJA", estado=eq.ENVIADA))
        s.add(OfertaPropia(ca_codigo="VIEJA", empresa_id=e1.id, lineas=[], total_neto=123_000.0))
        s.commit()
        r = sombra.ofertas_sin_resultado(s, e1.id, ahora)
        assert r["total"] == 1 and r["filas"][0]["codigo"] == "VIEJA"
        assert r["filas"][0]["oferta_total"] == 123_000.0 and r["filas"][0]["dias"] == 20
        assert sombra.ofertas_sin_resultado(s, e2.id, ahora)["total"] == 1
        uid = u.id
    t = client.get("/mesa/resultados", cookies=_cookies(settings, uid)).text
    assert "sin-resultado" in t and "VIEJA" in t and "RECIENTE" not in t and "RESUELTA" not in t


def test_resultados_sin_aviso_si_no_hay_pendientes(client, engine, settings):
    with Session(engine) as s:
        e = _empresa(s)
        u = _usuario(s, "boris@t.cl", e.id)
        s.commit()
        uid = u.id
    assert 'id="sin-resultado"' not in client.get("/mesa/resultados", cookies=_cookies(settings, uid)).text


# ---------------------------------------------------------------------------
# §1.4: retención
# ---------------------------------------------------------------------------


def test_retencion_llm_cache_180_dias(engine):
    ahora = ahora_utc()
    with Session(engine) as s:
        s.add(LlmCache(clave="vieja", modelo="m", respuesta={}, creado_en=ahora - timedelta(days=181)))
        s.add(LlmCache(clave="justa", modelo="m", respuesta={}, creado_en=ahora - timedelta(days=170)))
        s.commit()
        r = retencion.purgar_terminales(s)
        s.commit()
        assert r["llm_cache_borrados"] == 1
        assert set(s.execute(select(LlmCache.clave)).scalars()) == {"justa"}


def test_retencion_evaluaciones_cache_de_ca_cerradas_hace_mas_de_90_dias(engine):
    ahora = ahora_utc()
    with Session(engine) as s:
        e = _empresa(s)
        _ca(s, "CERRADA-VIEJA", cierre=ahora - timedelta(days=100), estado="adjudicada")
        _ca(s, "CERRADA-RECIENTE", cierre=ahora - timedelta(days=30), estado="adjudicada")
        _ca(s, "VIGENTE")
        for cod in ("CERRADA-VIEJA", "CERRADA-RECIENTE", "VIGENTE"):
            s.add(EvaluacionCache(ca_codigo=cod, empresa_id=e.id, datos={}))
        s.commit()
        r = retencion.purgar_terminales(s)
        s.commit()
        assert r["evaluaciones_cache_borrados"] == 1
        assert set(s.execute(select(EvaluacionCache.ca_codigo)).scalars()) == {
            "CERRADA-RECIENTE", "VIGENTE"
        }  # fmt: skip


def test_retencion_no_purga_ca_con_oferta_ni_prediccion_y_deja_conteos_en_el_job(engine):
    viejo = ahora_utc() - timedelta(days=200)
    with Session(engine) as s:
        e = _empresa(s)
        for cod in ("PRED", "OFER", "NADA"):
            _ca(s, cod, region=13, estado="adjudicada", cierre=viejo)
            s.add(CaProducto(ca_codigo=cod, codigo_producto="1", nombre="x", cantidad=1, unidad="u"))
        s.add(Prediccion(ca_codigo="PRED", empresa_id=e.id, modelo_tipo="base", precio_sugerido=1.0,
                         detalle={}, estado="cerrada"))  # fmt: skip
        s.add(OfertaPropia(ca_codigo="OFER", empresa_id=e.id, lineas=[], total_neto=0.0))
        s.flush()
        s.execute(CompraAgil.__table__.update().values(actualizado_en=viejo))
        s.commit()
    # Por el job `retencion` real: los conteos quedan en `job_runs`.
    r = orchestrator._run_with_lock(
        "retencion", lambda: orchestrator.run_retencion(engine, _settings()), engine,
        try_lock_fn=_LOCK_LIBRE, unlock_fn=_UNLOCK,
    )  # fmt: skip
    assert r is not None
    with Session(engine) as s:
        quedan = set(s.execute(select(CaProducto.ca_codigo)).scalars())
        assert {"PRED", "OFER"} <= quedan and "NADA" not in quedan
        res = s.execute(select(JobRun).where(JobRun.job == "retencion")).scalar_one().resultado_json
    for clave in ("llm_cache_borrados", "evaluaciones_cache_borrados", "eventos_equipo_borrados",
                  "job_runs_borrados", "ca_purgadas"):  # fmt: skip
        assert clave in res


def test_registrar_purga_suma_una_regla_sin_tocar_el_codigo(engine):
    """Cómo F-plat-lote y F-plat-seguimiento agregan sus tablas: una entrada en `PURGAS`."""
    antes = list(retencion.PURGAS)
    try:
        retencion.registrar_purga("tabla_futura", lambda s, ahora: 7)
        retencion.registrar_purga("tabla_futura", lambda s, ahora: 9)  # idempotente por nombre
        with Session(engine) as s:
            r = retencion.purgar_terminales(s)
        assert r["tabla_futura_borrados"] == 9
        assert [p.nombre for p in retencion.PURGAS].count("tabla_futura") == 1
    finally:
        retencion.PURGAS[:] = antes
