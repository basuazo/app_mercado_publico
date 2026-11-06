"""F-plat-purga-legado: criterio de borrado de CA, borrado por tandas, negativas del script,
entrada de `PURGAS` y «Archivo» con las tablas vacías. SQLite en memoria (sin COPY);
el respaldo con COPY está en test_plat_purga_legado_pg.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token
from app.core import retencion
from app.core.retencion import (
    PURGAS,
    TOPE_CA_FUERA_TERRITORIO,
    purgar_ca_fuera_territorio,
    purgar_terminales,
    select_ca_fuera_territorio,
)
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    Alerta,
    CaAnexo,
    CaCotizacion,
    CaLinea,
    CaProducto,
    CaRequisito,
    CaTrabajo,
    CompraAgil,
    Empresa,
    EvaluacionCache,
    Licitacion,
    LicitacionItem,
    Lote,
    LoteCa,
    MatchFeedback,
    OfertaCompetencia,
    OfertaPropia,
    OportunidadMatch,
    OportunidadSeguida,
    PerfilBusqueda,
    Prediccion,
    Usuario,
    Venta,
)

RAIZ = Path(__file__).resolve().parent.parent
TERRITORIO = [16]
OTRA = 2  # Antofagasta: fuera del territorio


def _cargar() -> ModuleType:
    ruta = RAIZ / "scripts" / "mantenimiento" / "purgar_legado.py"
    spec = importlib.util.spec_from_file_location("purgar_legado", ruta)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pl = _cargar()


@pytest.fixture()
def engine() -> Engine:
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url="sqlite:///:memory:",
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
        mercado_regiones=[16],
    )


def _base(s: Session) -> dict[str, int]:
    """Empresa activa de Ñuble, un usuario, un perfil y un lote."""
    e = Empresa(nombre="Aralia", regiones_prioritarias=TERRITORIO, comunas_prioritarias=[], activa=True)
    s.add(e)
    s.flush()
    u = Usuario(email="a@t.cl", password_hash="x", rol=RolUsuario.ADMIN, activo=True, empresa_id=e.id)
    s.add(u)
    s.flush()
    p = PerfilBusqueda(owner_id=u.id, nombre="P")
    s.add(p)
    lote = Lote(empresa_id=e.id, nombre="L")
    s.add(lote)
    s.flush()
    return {"empresa": e.id, "usuario": u.id, "perfil": p.id, "lote": lote.id}


def _ca(
    s: Session,
    codigo: str,
    *,
    region: int | None = OTRA,
    estado: str = "adjudicada",
    cierre: datetime | None = None,
    sin_cierre: bool = False,
) -> str:
    if cierre is None and not sin_cierre:
        cierre = ahora_utc() - timedelta(days=200)
    s.add(
        CompraAgil(
            codigo=codigo,
            nombre=f"CA {codigo}",
            estado=estado,
            region=region,
            fecha_cierre=cierre,
        )
    )
    s.add(CaProducto(ca_codigo=codigo, codigo_producto="50131701", nombre="Leche"))
    s.flush()
    return codigo


def _a_borrar(s: Session, regiones: list[int] = TERRITORIO) -> set[str]:
    return set(s.execute(select_ca_fuera_territorio(regiones, ahora_utc())).scalars())


# ---------------------------------------------------------------------------
# §1 Criterio
# ---------------------------------------------------------------------------


def test_criterio_borra_solo_lo_no_protegido(engine: Engine) -> None:
    ahora = ahora_utc()
    with Session(engine) as s:
        b = _base(s)
        borrables = {
            _ca(s, "B-TERMINAL"),  # otra región, adjudicada, vieja
            _ca(s, "B-NULL", region=None),  # región NULL = fuera del territorio
            _ca(s, "B-RECIENTE-TERMINAL", cierre=ahora - timedelta(days=3)),  # terminal aunque reciente
            _ca(s, "B-VIEJA-NO-TERMINAL", estado="cerrada", cierre=ahora - timedelta(days=91)),
        }
        protegidas: dict[str, str] = {}

        def proteger(nombre: str, fn: Any = None, **kw: Any) -> None:
            codigo = _ca(s, f"P-{nombre}", **kw)
            if fn is not None:
                for fila in fn(codigo):
                    s.add(fila)
            s.flush()
            protegidas[nombre] = codigo

        proteger("REGION-ACTIVA", region=16)
        proteger("VIGENTE", estado="publicada", cierre=ahora + timedelta(days=2))
        proteger("VIGENTE-SIN-CIERRE", estado="publicada", sin_cierre=True)
        proteger("SEGUIDA", lambda c: [OportunidadSeguida(owner_id=b["usuario"], fuente="compras_agiles", codigo_oportunidad=c)])  # fmt: skip
        proteger("TRABAJO", lambda c: [CaTrabajo(empresa_id=b["empresa"], ca_codigo=c, estado="priorizada")])  # fmt: skip
        proteger("PREDICCION", lambda c: [Prediccion(ca_codigo=c, empresa_id=b["empresa"], modelo_tipo="base", precio_sugerido=1)])  # fmt: skip
        proteger("OFERTA", lambda c: [OfertaPropia(ca_codigo=c, empresa_id=b["empresa"])])
        proteger("COTIZACION", lambda c: [CaCotizacion(ca_codigo=c)])
        proteger("LINEA", lambda c: [CaLinea(ca_codigo=c, n=1)])
        proteger("ANEXO", lambda c: [CaAnexo(ca_codigo=c, mime="application/pdf", tamano=1, sha256="x" * 64)])  # fmt: skip
        proteger("LOTE", lambda c: [LoteCa(lote_id=b["lote"], ca_codigo=c)])
        proteger("REQUISITO", lambda c: [CaRequisito(empresa_id=b["empresa"], ca_codigo=c, texto="t")])  # fmt: skip
        proteger("VENTA", lambda c: [Venta(empresa_id=b["empresa"], ca_codigo=c)])
        proteger("EVALUACION", lambda c: [EvaluacionCache(ca_codigo=c, empresa_id=b["empresa"])])
        # Alerta pendiente: el match se crea aparte para tener su id.
        codigo = _ca(s, "P-ALERTA-PENDIENTE")
        m = OportunidadMatch(perfil_id=b["perfil"], fuente="compras_agiles", codigo_oportunidad=codigo)
        s.add(m)
        s.flush()
        s.add(Alerta(match_id=m.id, tipo="nuevo", estado="pendiente"))
        protegidas["ALERTA-PENDIENTE"] = codigo
        # Una alerta ya ENVIADA no protege.
        codigo = _ca(s, "B-ALERTA-ENVIADA")
        m2 = OportunidadMatch(perfil_id=b["perfil"], fuente="compras_agiles", codigo_oportunidad=codigo)
        s.add(m2)
        s.flush()
        s.add(Alerta(match_id=m2.id, tipo="nuevo", estado="enviada"))
        borrables.add(codigo)
        s.commit()

        assert _a_borrar(s) == borrables
        for nombre, codigo in protegidas.items():
            assert codigo not in _a_borrar(s), nombre


def test_criterio_sin_territorio_no_borra_nada(engine: Engine) -> None:
    with Session(engine) as s:
        _ca(s, "B-1")
        s.commit()
        assert _a_borrar(s, []) == set()
        assert purgar_ca_fuera_territorio(s, ahora_utc(), []) == 0
        assert purgar_ca_fuera_territorio(s, ahora_utc(), None) == 0


# ---------------------------------------------------------------------------
# §3 Entrada de PURGAS con su tope
# ---------------------------------------------------------------------------


def test_purga_ca_fuera_territorio_registrada_con_regiones() -> None:
    entrada = next(p for p in PURGAS if p.nombre == "ca_fuera_territorio")
    assert entrada.con_regiones is True
    assert TOPE_CA_FUERA_TERRITORIO == 20_000


def test_purga_respeta_el_tope_y_borra_hijas_y_referencias(engine: Engine, monkeypatch: Any) -> None:
    monkeypatch.setattr(retencion, "TANDA_BORRADO", 2)
    with Session(engine) as s:
        b = _base(s)
        for i in range(5):
            _ca(s, f"B-{i}")
        m = OportunidadMatch(perfil_id=b["perfil"], fuente="compras_agiles", codigo_oportunidad="B-0")
        s.add(m)
        s.flush()
        s.add(Alerta(match_id=m.id, tipo="nuevo", estado="enviada"))
        s.add(MatchFeedback(usuario_id=b["usuario"], fuente="compras_agiles", codigo_oportunidad="B-0", valor="no"))  # fmt: skip
        _ca(s, "P-NUBLE", region=16)
        s.commit()

        assert purgar_ca_fuera_territorio(s, ahora_utc(), TERRITORIO, tope=3) == 3
        s.commit()
        assert s.scalar(select(func.count()).select_from(CompraAgil)) == 3  # 2 ajenas + Ñuble
        # B-0 es la primera por orden de código: se fue con su match, alerta, feedback y productos.
        assert s.get(CompraAgil, "B-0") is None
        assert s.scalar(select(func.count()).select_from(OportunidadMatch)) == 0
        assert s.scalar(select(func.count()).select_from(Alerta)) == 0
        assert s.scalar(select(func.count()).select_from(MatchFeedback)) == 0
        assert s.scalar(select(func.count()).select_from(CaProducto)) == 3


def test_job_retencion_cuenta_ca_fuera_territorio(engine: Engine, settings: Settings) -> None:
    from app.ingest.orchestrator import run_retencion

    with Session(engine) as s:
        _base(s)
        _ca(s, "B-1")
        _ca(s, "P-NUBLE", region=16)
        s.commit()
    r = run_retencion(engine, settings)
    assert r["ca_fuera_territorio_borrados"] == 1
    with Session(engine) as s:
        assert s.get(CompraAgil, "B-1") is None
        assert s.get(CompraAgil, "P-NUBLE") is not None


def test_purgar_terminales_sin_regiones_no_borra_ca(engine: Engine) -> None:
    with Session(engine) as s:
        _ca(s, "B-1")
        s.commit()
        r = purgar_terminales(s)
        s.commit()
        assert r["ca_fuera_territorio_borrados"] == 0
        assert s.get(CompraAgil, "B-1") is not None


# ---------------------------------------------------------------------------
# Borrado por tandas (script)
# ---------------------------------------------------------------------------


def _legado(s: Session, b: dict[str, int], n_lic: int = 7) -> None:
    for i in range(n_lic):
        c = f"L-{i:02d}"
        s.add(Licitacion(codigo=c, nombre=c))
        s.add(LicitacionItem(licitacion_codigo=c, nombre="i"))
        s.add(OfertaCompetencia(licitacion_codigo=c))
    s.flush()
    m = OportunidadMatch(perfil_id=b["perfil"], fuente="licitaciones", codigo_oportunidad="L-00")
    sg = OportunidadSeguida(owner_id=b["usuario"], fuente="licitaciones", codigo_oportunidad="L-01")
    s.add_all([m, sg])
    s.flush()
    s.add(Alerta(match_id=m.id, tipo="nuevo", estado="pendiente"))  # pendiente: igual se va
    s.add(Alerta(seguimiento_id=sg.id, tipo="cambio_estado", estado="pendiente"))
    s.add(MatchFeedback(usuario_id=b["usuario"], fuente="licitaciones", codigo_oportunidad="L-02", valor="si"))  # fmt: skip
    for i in range(5):
        _ca(s, f"B-{i}")
    _ca(s, "P-NUBLE", region=16)
    s.commit()


def _conteos(s: Session) -> dict[str, int]:
    modelos = {
        "licitaciones": Licitacion,
        "licitacion_items": LicitacionItem,
        "ofertas_competencia": OfertaCompetencia,
        "oportunidades_match": OportunidadMatch,
        "oportunidades_seguidas": OportunidadSeguida,
        "match_feedback": MatchFeedback,
        "alertas": Alerta,
        "compras_agiles": CompraAgil,
        "ca_productos": CaProducto,
    }
    return {k: int(s.scalar(select(func.count()).select_from(m)) or 0) for k, m in modelos.items()}


def test_contar_coincide_con_lo_que_borra_el_script(engine: Engine) -> None:
    with Session(engine) as s:
        _legado(s, _base(s))
        ahora = ahora_utc()
        previsto = pl.contar(s, select_ca_fuera_territorio(TERRITORIO, ahora))
    assert previsto == {
        "licitaciones": 7,
        "compras_agiles": 5,
        "oportunidades_seguidas": 1,
        "oportunidades_match": 1,
        "licitacion_items": 7,
        "ofertas_competencia": 7,
        "ca_productos": 5,
        "match_feedback": 1,
        "alertas": 2,
    }
    total = pl.borrar_por_tandas(engine, TERRITORIO, ahora, tanda=2)
    assert total == previsto
    with Session(engine) as s:
        quedan = _conteos(s)
    assert quedan["licitaciones"] == quedan["licitacion_items"] == quedan["ofertas_competencia"] == 0
    assert quedan["compras_agiles"] == 1 and quedan["ca_productos"] == 1  # Ñuble intacta


def test_borrado_por_tandas_hace_un_commit_por_tanda_y_es_reanudable(engine: Engine) -> None:
    with Session(engine) as s:
        _legado(s, _base(s))
    ahora = ahora_utc()
    tandas: list[tuple[str, dict[str, int]]] = []

    class Corte(Exception):
        pass

    def cortar_tras_dos_tandas_de_licitaciones(paso: str, n: dict[str, int]) -> None:
        tandas.append((paso, n))
        if sum(1 for p, _ in tandas if p == "licitaciones") == 2:
            raise Corte

    with pytest.raises(Corte):
        pl.borrar_por_tandas(engine, TERRITORIO, ahora, tanda=3, al_avanzar=cortar_tras_dos_tandas_de_licitaciones)
    with Session(engine) as s:
        # Lo commiteado quedó: 6 de 7 licitaciones, ninguna CA todavía.
        assert _conteos(s)["licitaciones"] == 1
        assert _conteos(s)["compras_agiles"] == 6
        assert _conteos(s)["oportunidades_match"] == 0
    # Segunda corrida: sigue desde donde quedó.
    resto = pl.borrar_por_tandas(engine, TERRITORIO, ahora, tanda=3)
    assert resto["licitaciones"] == 1 and resto["compras_agiles"] == 5
    # Tercera: no queda nada que borrar (idempotente).
    assert sum(pl.borrar_por_tandas(engine, TERRITORIO, ahora, tanda=3).values()) == 0
    with Session(engine) as s:
        assert _conteos(s)["compras_agiles"] == 1


# ---------------------------------------------------------------------------
# Respaldo: validaciones puras
# ---------------------------------------------------------------------------


def test_dir_del_respaldo_no_puede_estar_dentro_del_repo(tmp_path: Path) -> None:
    with pytest.raises(pl.PurgaError):
        pl.validar_dir_fuera_del_repo(RAIZ)
    with pytest.raises(pl.PurgaError):
        pl.validar_dir_fuera_del_repo(RAIZ / "data" / "respaldos")
    assert pl.validar_dir_fuera_del_repo(tmp_path, raiz=tmp_path / "repo") == tmp_path.resolve()


def test_respaldar_dentro_del_repo_falla_antes_de_conectarse() -> None:
    salida: list[str] = []
    rc = pl.main(["--respaldar", str(RAIZ / "data")], salida=salida.append)
    assert rc == 1
    assert "dentro del repo" in salida[-1]


def test_sin_argumentos_solo_imprime_la_ayuda() -> None:
    salida: list[str] = []
    assert pl.main([], salida=salida.append) == 0
    assert "--medir" in salida[0] and "--compactar" in salida[0]


# ---------------------------------------------------------------------------
# Negativas de --borrar y --compactar
# ---------------------------------------------------------------------------


def _manifiesto(dir_: Path, filas: dict[str, int], *, creado: datetime, host: str = "localhost") -> None:
    """Manifiesto + CSV.gz de mentira con exactamente `filas` filas por tabla."""
    import gzip

    tablas = []
    for t in pl.TABLAS_RESPALDO:
        archivo = f"{t}.csv.gz"
        with gzip.open(dir_ / archivo, "wt", encoding="utf-8", newline="") as f:
            f.write("id\n" + "".join(f"{i}\n" for i in range(filas.get(t, 0))))
        tablas.append({"tabla": t, "archivo": archivo, "filas": filas.get(t, 0), "consulta": "",
                       "columnas": ["id"], "sha256": pl._sha256(dir_ / archivo)})  # fmt: skip
    (dir_ / pl.MANIFIESTO).write_text(
        json.dumps({"creado_en": creado.isoformat(), "ahora_criterio": creado.isoformat(),
                    "host": host, "regiones_protegidas": TERRITORIO, "tablas": tablas}),
        encoding="utf-8",
    )  # fmt: skip


def _no_lock(_engine: Engine) -> Any:
    raise AssertionError("no debe llegar a tomar el lock")


def test_borrar_sin_manifiesto_se_niega(engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(pl.PurgaError, match="No existe"):
        pl.ejecutar_borrar(engine, tmp_path, entrada=lambda _: "si", lock=_no_lock)


def test_borrar_con_manifiesto_viejo_se_niega(engine: Engine, tmp_path: Path) -> None:
    with Session(engine) as s:
        _legado(s, _base(s))
        conteo = pl.contar(s, select_ca_fuera_territorio(TERRITORIO, ahora_utc()))
    _manifiesto(tmp_path, conteo, creado=ahora_utc() - timedelta(hours=25), host="?")
    with pytest.raises(pl.PurgaError, match="más de 24 h"):
        pl.ejecutar_borrar(engine, tmp_path, entrada=lambda _: "si", lock=_no_lock)
    with Session(engine) as s:
        assert _conteos(s)["licitaciones"] == 7  # no borró nada


def test_borrar_con_manifiesto_que_no_cuadra_se_niega(engine: Engine, tmp_path: Path) -> None:
    with Session(engine) as s:
        _legado(s, _base(s))
        conteo = pl.contar(s, select_ca_fuera_territorio(TERRITORIO, ahora_utc()))
    _manifiesto(tmp_path, {**conteo, "licitaciones": 5}, creado=ahora_utc(), host="?")
    with pytest.raises(pl.PurgaError, match="licitaciones: hoy se borrarían 7"):
        pl.ejecutar_borrar(engine, tmp_path, entrada=lambda _: "si", lock=_no_lock)


def test_borrar_con_archivo_alterado_se_niega(engine: Engine, tmp_path: Path) -> None:
    with Session(engine) as s:
        _legado(s, _base(s))
        conteo = pl.contar(s, select_ca_fuera_territorio(TERRITORIO, ahora_utc()))
    _manifiesto(tmp_path, conteo, creado=ahora_utc(), host="?")
    (tmp_path / "licitaciones.csv.gz").write_bytes(b"")
    with pytest.raises(pl.PurgaError):
        pl.ejecutar_borrar(engine, tmp_path, entrada=lambda _: "si", lock=_no_lock)


def test_borrar_completo_y_reanudable_con_manifiesto_valido(engine: Engine, tmp_path: Path) -> None:
    import contextlib

    with Session(engine) as s:
        _legado(s, _base(s))
        conteo = pl.contar(s, select_ca_fuera_territorio(TERRITORIO, ahora_utc()))
    _manifiesto(tmp_path, conteo, creado=ahora_utc(), host="?")  # SQLite en memoria: host «?»

    @contextlib.contextmanager
    def lock_ok(_engine: Engine) -> Any:
        yield True

    # Sin «si» no borra.
    with pytest.raises(pl.PurgaError, match="Cancelado"):
        pl.ejecutar_borrar(engine, tmp_path, entrada=lambda _: "no", lock=lock_ok)
    with Session(engine) as s:
        assert _conteos(s)["licitaciones"] == 7

    pl.ejecutar_borrar(engine, tmp_path, entrada=lambda _: "si", lock=lock_ok, salida=lambda _: None)
    avance = json.loads((tmp_path / pl.AVANCE).read_text(encoding="utf-8"))
    assert avance["terminado"] is True
    assert avance["borrados"]["licitaciones"] == 7 and avance["borrados"]["compras_agiles"] == 5
    # Re-correr (reanudando): ya no queda nada y no se niega.
    pl.ejecutar_borrar(engine, tmp_path, entrada=lambda _: "si", lock=lock_ok, salida=lambda _: None)


def test_borrar_sin_lock_no_borra(engine: Engine, tmp_path: Path) -> None:
    import contextlib

    with Session(engine) as s:
        _legado(s, _base(s))
        conteo = pl.contar(s, select_ca_fuera_territorio(TERRITORIO, ahora_utc()))
    _manifiesto(tmp_path, conteo, creado=ahora_utc(), host="?")

    @contextlib.contextmanager
    def lock_ocupado(_engine: Engine) -> Any:
        yield False

    with pytest.raises(pl.PurgaError, match="advisory lock"):
        pl.ejecutar_borrar(engine, tmp_path, entrada=lambda _: "si", lock=lock_ocupado, salida=lambda _: None)
    with Session(engine) as s:
        assert _conteos(s)["licitaciones"] == 7


def test_verificar_manifiesto_tolerancia_y_reanudacion() -> None:
    ahora = ahora_utc()
    man = {"creado_en": ahora.isoformat(), "host": "h", "regiones_protegidas": [16],
           "tablas": [{"tabla": t, "filas": 1000} for t in pl.TABLAS_RESPALDO]}  # fmt: skip
    ok = dict.fromkeys(pl.TABLAS_RESPALDO, 1000)
    kw = {"ahora": ahora, "host": "h", "regiones": [16]}
    assert pl.verificar_manifiesto(man, {**ok, "compras_agiles": 1005}, reanudando=False, **kw) == []
    assert pl.verificar_manifiesto(man, {**ok, "compras_agiles": 1006}, reanudando=False, **kw)
    assert pl.verificar_manifiesto(man, {**ok, "compras_agiles": 994}, reanudando=False, **kw)
    # Reanudando, lo que queda puede ser mucho menos, pero nunca más que lo respaldado.
    assert pl.verificar_manifiesto(man, {**ok, "compras_agiles": 3}, reanudando=True, **kw) == []
    assert pl.verificar_manifiesto(man, {**ok, "compras_agiles": 1006}, reanudando=True, **kw)
    assert pl.verificar_manifiesto(man, ok, reanudando=False, ahora=ahora, host="otro", regiones=[16])
    assert pl.verificar_manifiesto(man, ok, reanudando=False, ahora=ahora, host="h", regiones=[16, 8])


def test_compactar_fuera_de_horario_se_niega(engine: Engine) -> None:
    def a_las_15(tz: Any) -> datetime:
        return datetime(2026, 11, 7, 15, 0, tzinfo=TZ_CHILE)

    with pytest.raises(pl.PurgaError, match="22:00 y 07:00"):
        pl.ejecutar_compactar(engine, entrada=lambda _: "si", lock=_no_lock, now_fn=a_las_15)
    salida: list[str] = []
    assert pl.main(["--compactar"], salida=salida.append, engine=engine, now_fn=a_las_15) == 1
    assert "22:00 y 07:00" in salida[-1]


def test_compactar_estimacion_y_espacio() -> None:
    assert pl.estimar_despues(100_000_000, vivas=10, muertas=90) == 10_000_000
    assert pl.estimar_despues(5, 0, 0) == 5
    mb = 1024 * 1024
    assert pl.cabe_compactar(376 * mb, 100 * mb) is True  # 376 + 120 = 496 ≤ 512
    assert pl.cabe_compactar(376 * mb, 120 * mb) is False  # 376 + 144 > 512


# ---------------------------------------------------------------------------
# §4 «Archivo» con las tablas vacías
# ---------------------------------------------------------------------------


def test_archivo_sin_errores_con_tablas_vacias(engine: Engine, settings: Settings) -> None:
    with Session(engine) as s:
        u = Usuario(email="admin@t.cl", password_hash=hash_password("contraseña-segura-test"),
                    rol=RolUsuario.ADMIN, activo=True)  # fmt: skip
        s.add(u)
        s.commit()
        uid = u.id
    client = TestClient(create_app(settings, engine))
    cookies = {COOKIE_NAME: create_session_token(settings.secret_key, uid)}
    for ruta in ("/archivo", "/perfiles", "/registro", "/compras-agiles", "/plan-anual",
                 "/registro?tab=cerradas&fuente=licitaciones"):  # fmt: skip
        r = client.get(ruta, cookies=cookies, follow_redirects=False)
        assert r.status_code == 200, (ruta, r.status_code)
    for ruta in ("/archivo", "/registro"):
        r = client.get(ruta, cookies=cookies)
        assert "Licitaciones archivadas el 7-nov-2026 (respaldo local)" in r.text, ruta
    # Una licitación que ya no existe da 404, no 500.
    r = client.get("/oportunidad/licitaciones/1234-5-LE26", cookies=cookies)
    assert r.status_code == 404
