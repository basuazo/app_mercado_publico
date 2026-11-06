"""F-plat-purga-legado sobre Postgres local (`TEST_DATABASE_URL`), nunca contra producción:
respaldo con COPY (manifiesto, conteos, relectura), borrado con el advisory lock real,
restauración con COPY FROM y compactación."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.models.tables import (
    Alerta,
    CaProducto,
    CaTrabajo,
    CompraAgil,
    Empresa,
    Licitacion,
    LicitacionItem,
    MatchFeedback,
    OfertaCompetencia,
    OportunidadMatch,
    OportunidadSeguida,
    PerfilBusqueda,
    Usuario,
)
from tests import pg_url

_DB_URL = pg_url.DB_URL
pytestmark = pytest.mark.skipif(
    not pg_url.es_postgres(_DB_URL),
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)
RAIZ = Path(__file__).resolve().parent.parent
_P = "ZZPURGA-"
_EMAIL = "zzpurga@t.cl"
_DESC = 'Línea 1\nLínea 2, con coma y "comillas"'


def _cargar() -> ModuleType:
    ruta = RAIZ / "scripts" / "mantenimiento" / "purgar_legado.py"
    spec = importlib.util.spec_from_file_location("purgar_legado_pg", ruta)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pl = _cargar()


def _limpiar(e: Engine) -> None:
    with Session(e) as s:
        s.execute(delete(Usuario).where(Usuario.email == _EMAIL))  # perfiles, matches, alertas...
        s.execute(delete(Empresa).where(Empresa.nombre.like(f"{_P}%")))  # ca_trabajo
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like(f"{_P}%")))
        s.execute(delete(Licitacion).where(Licitacion.codigo.like(f"{_P}%")))
        s.commit()


@pytest.fixture()
def pg_engine() -> Iterator[Engine]:
    e = create_engine(normalizar_url_driver(_DB_URL))
    _limpiar(e)
    with Session(e) as s:
        # El script borra TODAS las licitaciones y las CA ajenas: solo en una base de tests limpia.
        ajenas = s.scalar(
            select(func.count()).select_from(Licitacion).where(~Licitacion.codigo.like(f"{_P}%"))
        ) + s.scalar(
            select(func.count()).select_from(CompraAgil).where(~CompraAgil.codigo.like(f"{_P}%"))
        )
        if ajenas:
            pytest.fail(f"La base de tests tiene {ajenas} licitaciones/CA ajenas: no corro el borrado.")
    yield e
    _limpiar(e)
    e.dispose()


def _sembrar(e: Engine) -> None:
    viejo = ahora_utc() - timedelta(days=200)
    with Session(e) as s:
        emp = Empresa(nombre=f"{_P}Emp", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True)
        s.add(emp)
        s.flush()
        u = Usuario(email=_EMAIL, password_hash="x", empresa_id=emp.id)
        s.add(u)
        s.flush()
        perfil = PerfilBusqueda(owner_id=u.id, nombre="P")
        s.add(perfil)
        for i in range(3):
            c = f"{_P}L{i}"
            s.add(Licitacion(codigo=c, nombre=c, descripcion=_DESC, raw_json={"k": [1, "á"]}))
        s.flush()
        for i in range(3):
            c = f"{_P}L{i}"
            s.add(LicitacionItem(licitacion_codigo=c, nombre="ítem"))
            s.add(OfertaCompetencia(licitacion_codigo=c, nombre_proveedor="Prov"))
        for i in range(3):
            c = f"{_P}CA{i}"
            s.add(CompraAgil(codigo=c, nombre=c, descripcion=_DESC, estado="adjudicada", region=2,
                             fecha_cierre=viejo, raw_json={"x": i}))  # fmt: skip
        s.add(CompraAgil(codigo=f"{_P}NUBLE", nombre="n", estado="adjudicada", region=16, fecha_cierre=viejo))  # fmt: skip
        s.add(CompraAgil(codigo=f"{_P}TRABAJO", nombre="t", estado="adjudicada", region=2, fecha_cierre=viejo))  # fmt: skip
        s.flush()
        for c in (f"{_P}CA0", f"{_P}CA1", f"{_P}CA2", f"{_P}NUBLE", f"{_P}TRABAJO"):
            s.add(CaProducto(ca_codigo=c, codigo_producto="50131701", nombre="Leche"))
        s.add(CaTrabajo(empresa_id=emp.id, ca_codigo=f"{_P}TRABAJO", estado="priorizada"))
        m_lic = OportunidadMatch(perfil_id=perfil.id, fuente="licitaciones", codigo_oportunidad=f"{_P}L0")
        m_ca = OportunidadMatch(perfil_id=perfil.id, fuente="compras_agiles", codigo_oportunidad=f"{_P}CA0")
        sg = OportunidadSeguida(owner_id=u.id, fuente="licitaciones", codigo_oportunidad=f"{_P}L1")
        s.add_all([m_lic, m_ca, sg])
        s.flush()
        s.add(Alerta(match_id=m_lic.id, tipo="nuevo", estado="pendiente"))
        s.add(Alerta(match_id=m_ca.id, tipo="nuevo", estado="enviada"))
        s.add(Alerta(seguimiento_id=sg.id, tipo="cambio_estado", estado="pendiente"))
        s.add(MatchFeedback(usuario_id=u.id, fuente="licitaciones", codigo_oportunidad=f"{_P}L2", valor="si"))  # fmt: skip
        s.add(MatchFeedback(usuario_id=u.id, fuente="compras_agiles", codigo_oportunidad=f"{_P}CA1", valor="no"))  # fmt: skip
        s.commit()


ESPERADO = {
    "licitaciones": 3,
    "compras_agiles": 3,
    "oportunidades_seguidas": 1,
    "oportunidades_match": 2,
    "licitacion_items": 3,
    "ofertas_competencia": 3,
    "ca_productos": 3,
    "match_feedback": 2,
    "alertas": 3,
}


def test_medir_respaldar_borrar_restaurar_y_compactar(pg_engine: Engine, tmp_path: Path) -> None:
    _sembrar(pg_engine)
    salida: list[str] = []

    medido = pl.ejecutar_medir(pg_engine, salida=salida.append)
    assert medido["borraria"] == ESPERADO
    assert medido["bd"] > 0 and "compras_agiles" in medido["tablas"]

    # --- Respaldo ---
    destino = pl.respaldar(pg_engine, tmp_path, salida=salida.append)
    assert destino.parent == tmp_path.resolve()
    assert destino.name == f"legado_{datetime.now(TZ_CHILE):%Y%m%d}"
    man = json.loads((destino / pl.MANIFIESTO).read_text(encoding="utf-8"))
    assert [t["tabla"] for t in man["tablas"]] == list(pl.TABLAS_RESPALDO)
    assert {t["tabla"]: t["filas"] for t in man["tablas"]} == ESPERADO
    assert man["host"] == pl.host_de(_DB_URL)
    for t in man["tablas"]:
        columnas, filas = pl.leer_csv_gz(destino / t["archivo"])
        assert filas == t["filas"] and columnas == t["columnas"] and len(t["sha256"]) == 64
        assert t["consulta"].startswith("SELECT")
    assert "raw_json" in man["tablas"][0]["columnas"]
    restaurar = (destino / pl.RESTAURAR).read_text(encoding="utf-8")
    assert restaurar.index("\\copy licitaciones ") < restaurar.index("\\copy licitacion_items ")
    assert restaurar.index("\\copy compras_agiles ") < restaurar.index("\\copy ca_productos ")
    # La descripción con salto de línea, coma y comillas sobrevive al CSV.
    with gzip.open(destino / "compras_agiles.csv.gz", "rt", encoding="utf-8", newline="") as f:
        import csv

        filas_ca = list(csv.DictReader(f))
    assert {r["codigo"] for r in filas_ca} == {f"{_P}CA0", f"{_P}CA1", f"{_P}CA2"}
    assert all(r["descripcion"] == _DESC for r in filas_ca)

    # --- Borrado: un «no» no borra; con «si» y el lock real, borra ---
    with pytest.raises(pl.PurgaError, match="Cancelado"):
        pl.ejecutar_borrar(pg_engine, destino, entrada=lambda _: "no", salida=salida.append)
    total = pl.ejecutar_borrar(pg_engine, destino, entrada=lambda _: "si", salida=salida.append)
    assert total == ESPERADO
    with Session(pg_engine) as s:
        assert s.scalar(select(func.count()).select_from(Licitacion)) == 0
        quedan = set(s.scalars(select(CompraAgil.codigo).where(CompraAgil.codigo.like(f"{_P}%"))))
        assert quedan == {f"{_P}NUBLE", f"{_P}TRABAJO"}
        assert s.scalar(select(func.count()).select_from(OportunidadMatch)) == 0
        assert s.scalar(select(func.count()).select_from(Alerta)) == 0
    avance = json.loads((destino / pl.AVANCE).read_text(encoding="utf-8"))
    assert avance["terminado"] is True
    # Re-correr no falla y no borra nada más (idempotente).
    assert sum(pl.ejecutar_borrar(pg_engine, destino, entrada=lambda _: "si", salida=salida.append).values()) == 0

    # --- Restauración con COPY FROM, en el orden del manifiesto ---
    _restaurar(pg_engine, destino, man, solo=("licitaciones", "compras_agiles", "licitacion_items", "ca_productos"))
    with Session(pg_engine) as s:
        assert s.scalar(select(func.count()).select_from(Licitacion)) == 3
        assert s.scalar(select(func.count()).select_from(LicitacionItem)) == 3
        assert s.get(CompraAgil, f"{_P}CA1").raw_json == {"x": 1}  # type: ignore[union-attr]
        assert s.get(Licitacion, f"{_P}L0").descripcion == _DESC  # type: ignore[union-attr]

    # --- Compactación (de noche) ---
    def a_las_23(tz: Any) -> datetime:
        return datetime(2026, 11, 7, 23, 0, tzinfo=TZ_CHILE)

    res = pl.ejecutar_compactar(pg_engine, entrada=lambda _: "si", salida=salida.append, now_fn=a_las_23)
    assert set(res) <= set(pl.TABLAS_COMPACTAR)
    assert any(linea.startswith("Base: ") for linea in salida)


def _restaurar(e: Engine, destino: Path, man: dict[str, Any], solo: tuple[str, ...]) -> None:
    with e.connect() as conn:
        driver = conn.connection.driver_connection
        assert driver is not None
        with driver.cursor() as cur:  # type: ignore[attr-defined]
            for t in man["tablas"]:
                if t["tabla"] not in solo:
                    continue
                cols = ", ".join(t["columnas"])
                sql = f"COPY {t['tabla']} ({cols}) FROM STDIN WITH (FORMAT csv, HEADER true)"
                with gzip.open(destino / t["archivo"], "rb") as f, cur.copy(sql) as cp:
                    while bloque := f.read(1 << 20):
                        cp.write(bloque)
        driver.commit()  # el COPY va por psycopg directo: el commit también
