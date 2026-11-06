"""Respaldo, borrado del legado y compactación de Neon (F-plat-purga-legado, 7-nov-2026).

Lo ejecuta SOLO Boris, desde su PC, contra la base que diga `DATABASE_URL` (en PowerShell se
exporta desde `DATABASE_URL_PROD`). Nada de esto corre en un job, en el deploy ni en los tests
contra Neon.

Qué se borra (decisiones de Boris, 6-nov-2026; ver docs/decisiones.md):
  1. TODAS las licitaciones, con sus `licitacion_items` y `ofertas_competencia`, y las filas con
     `fuente = 'licitaciones'` de `oportunidades_match` (con sus `alertas`),
     `oportunidades_seguidas` (con sus `alertas`) y `match_feedback`.
  2. Las CA fuera del territorio activo, ya no vigentes y sin nada que las proteja (criterio
     único en `app.core.retencion.filtro_ca_fuera_territorio`), con sus `ca_productos`, sus
     `oportunidades_match` (con sus `alertas`) y su `match_feedback`.

Modos (siempre explícitos; sin argumentos solo imprime esta ayuda):
    --medir                         solo lectura: tamaños, filas vivas/muertas y qué se borraría
    --respaldar DIR                 CSV.gz por tabla en DIR/legado_AAAAMMDD/ + manifiesto.json
    --borrar --respaldo DIR/legado_AAAAMMDD
                                    borra por tandas de 5.000, reanudable (pide escribir «si»)
    --compactar                     VACUUM (FULL, ANALYZE) tabla por tabla, solo 22:00–07:00 de
                                    Chile (pide escribir «si»)

Uso (PowerShell, desde la raíz del repo):
    $env:PYTHONIOENCODING = "utf-8"
    $env:DATABASE_URL = $env:DATABASE_URL_PROD
    python scripts\\mantenimiento\\purgar_legado.py --medir
    python scripts\\mantenimiento\\purgar_legado.py --respaldar D:\\respaldos
    python scripts\\mantenimiento\\purgar_legado.py --borrar --respaldo D:\\respaldos\\legado_20261107
    python scripts\\mantenimiento\\purgar_legado.py --compactar

El respaldo contiene `raw_json` (datos públicos de ChileCompra) y queda FUERA del repo: no se
commitea. Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import gzip
import hashlib
import io
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))

from sqlalchemy import (  # noqa: E402
    Select,
    column,
    create_engine,
    delete,
    func,
    insert,
    or_,
    select,
    table,
    text,
)
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.engine import Connection, Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import normalizar_url_driver  # noqa: E402
from app.core.retencion import (  # noqa: E402
    TANDA_BORRADO,
    borrar_cas,
    select_ca_fuera_territorio,
)
from app.core.tiempo import TZ_CHILE, ahora_utc, en_ventana_nocturna  # noqa: E402
from app.models.tables import (  # noqa: E402
    Alerta,
    CaProducto,
    CompraAgil,
    Licitacion,
    LicitacionItem,
    MatchFeedback,
    OfertaCompetencia,
    OportunidadMatch,
    OportunidadSeguida,
)

# Orden de RESTAURACIÓN: primero los padres, después las hijas. El borrado va al revés.
TABLAS_RESPALDO: tuple[str, ...] = (
    "licitaciones",
    "compras_agiles",
    "oportunidades_seguidas",
    "oportunidades_match",
    "licitacion_items",
    "ofertas_competencia",
    "ca_productos",
    "match_feedback",
    "alertas",
)
# Tablas que mide `--medir` (las de la tabla del prompt y las que toca el borrado).
TABLAS_MEDIR: tuple[str, ...] = (
    "compras_agiles",
    "licitaciones",
    "licitacion_items",
    "ofertas_competencia",
    "ca_productos",
    "oportunidades_match",
    "oportunidades_seguidas",
    "alertas",
    "match_feedback",
)
# Orden de `--compactar` (§2 del prompt).
TABLAS_COMPACTAR: tuple[str, ...] = (
    "licitacion_items",
    "licitaciones",
    "ofertas_competencia",
    "ca_productos",
    "compras_agiles",
    "oportunidades_match",
    "alertas",
    "match_feedback",
)
LIMITE_NEON_BYTES = 512 * 1024 * 1024  # 0,5 GB del plan free
MARGEN_COMPACTAR = 1.20
TOLERANCIA_MANIFIESTO = 0.005  # ± 0,5 %
MAX_EDAD_MANIFIESTO = timedelta(hours=24)
ESPERA_LOCK_S = 600  # 10 min
MANIFIESTO = "manifiesto.json"
AVANCE = "avance.json"
RESTAURAR = "RESTAURAR.md"

# Tabla temporal con los códigos de CA a borrar: fija el conjunto una sola vez por respaldo, y
# las consultas del COPY quedan sin parámetros (COPY no los acepta).
_PURGA_CA = table("_purga_ca", column("codigo"))

Salida = Callable[[str], None]
_PG = postgresql.dialect()  # type: ignore[no-untyped-call]


class PurgaError(RuntimeError):
    """El script se niega a seguir; el mensaje dice por qué."""


# ---------------------------------------------------------------------------
# Conjunto a borrar (una sola definición para medir, respaldar, verificar y borrar)
# ---------------------------------------------------------------------------


def consultas(ca_codigos: Select[Any]) -> dict[str, Select[Any]]:
    """`SELECT` de las filas que se borran, por tabla. `ca_codigos` = códigos de CA a borrar
    (el criterio de retención, o la tabla temporal `_purga_ca` en el respaldo)."""
    cond_match = or_(
        OportunidadMatch.fuente == "licitaciones",
        (OportunidadMatch.fuente == "compras_agiles")
        & OportunidadMatch.codigo_oportunidad.in_(ca_codigos),
    )
    cond_seguida = OportunidadSeguida.fuente == "licitaciones"
    cond_feedback = or_(
        MatchFeedback.fuente == "licitaciones",
        (MatchFeedback.fuente == "compras_agiles")
        & MatchFeedback.codigo_oportunidad.in_(ca_codigos),
    )
    return {
        "licitaciones": select(Licitacion.__table__),
        "compras_agiles": select(CompraAgil.__table__).where(CompraAgil.codigo.in_(ca_codigos)),
        "oportunidades_seguidas": select(OportunidadSeguida.__table__).where(cond_seguida),
        "oportunidades_match": select(OportunidadMatch.__table__).where(cond_match),
        "licitacion_items": select(LicitacionItem.__table__),
        "ofertas_competencia": select(OfertaCompetencia.__table__),
        "ca_productos": select(CaProducto.__table__).where(CaProducto.ca_codigo.in_(ca_codigos)),
        "match_feedback": select(MatchFeedback.__table__).where(cond_feedback),
        "alertas": select(Alerta.__table__).where(
            or_(
                Alerta.match_id.in_(select(OportunidadMatch.id).where(cond_match)),
                Alerta.seguimiento_id.in_(select(OportunidadSeguida.id).where(cond_seguida)),
            )
        ),
    }


def contar(conn: Connection | Session, ca_codigos: Select[Any]) -> dict[str, int]:
    """Filas que se borrarían, por tabla (en el orden de `TABLAS_RESPALDO`)."""
    qs = consultas(ca_codigos)
    return {
        t: int(conn.execute(select(func.count()).select_from(qs[t].subquery())).scalar_one())
        for t in TABLAS_RESPALDO
    }


def regiones_protegidas(session: Session) -> list[int]:
    """El territorio que protege el job `retencion`: regiones de las empresas activas ∪
    `MERCADO_REGIONES` (si no hay Settings completos en el entorno, su default)."""
    from app.core.settings import Settings
    from app.mercado.territorio import regiones_activas

    try:
        base = list(Settings().mercado_regiones)  # type: ignore[call-arg]
    except Exception:
        base = list(Settings.model_fields["mercado_regiones"].default or [])
    s = Settings.model_construct(mercado_regiones=base)
    return sorted({int(r) for r in regiones_activas(session, s)} | {int(r) for r in base})


def sql_literal(stmt: Select[Any]) -> str:
    """SQL de Postgres sin parámetros (solo para COPY y el manifiesto). Las consultas de
    `consultas(_purga_ca)` solo llevan los literales fijos 'licitaciones' y 'compras_agiles'."""
    return str(stmt.compile(dialect=_PG, compile_kwargs={"literal_binds": True}))


def host_de(url: str) -> str:
    return urlparse(normalizar_url_driver(url)).hostname or "?"


# ---------------------------------------------------------------------------
# --respaldar
# ---------------------------------------------------------------------------


def validar_dir_fuera_del_repo(dir_: Path, raiz: Path = RAIZ) -> Path:
    """`DIR` resuelto; falla si cae dentro del repo (el respaldo no se commitea)."""
    d = dir_.expanduser().resolve()
    r = raiz.resolve()
    if d == r or r in d.parents:
        raise PurgaError(
            f"DIR no puede estar dentro del repo ({r}): usa una carpeta fuera, ej. D:\\respaldos"
        )
    return d


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def leer_csv_gz(ruta: Path) -> tuple[list[str], int]:
    """(columnas, filas de datos) de un CSV.gz con encabezado. Lee con el módulo csv: un
    campo entre comillas puede traer saltos de línea (descripciones, raw_json)."""
    csv.field_size_limit(2**31 - 1)
    with gzip.open(ruta, "rt", encoding="utf-8", newline="") as f:
        lector = csv.reader(f)
        columnas = next(lector, [])
        filas = sum(1 for _ in lector)
    return columnas, filas


def _texto_restaurar(tablas: Sequence[Mapping[str, Any]]) -> str:
    lineas = [
        "# Restaurar el respaldo del legado (F-plat-purga-legado)",
        "",
        "Respaldo de las filas borradas por `scripts/mantenimiento/purgar_legado.py`.",
        "Fuente de los datos: Dirección ChileCompra.",
        "",
        "## Antes",
        "- La base tiene que estar migrada (`alembic upgrade head`) y con las mismas columnas.",
        "- `oportunidades_match` exige que existan sus `perfiles_busqueda`; `oportunidades_seguidas`",
        "  y `match_feedback`, sus `usuarios`. Si alguno ya no existe, esa fila no entra.",
        "- Pausar los crons GH mientras se restaura.",
        "",
        "## Orden (primero los padres, después las hijas)",
        "",
        "Con psql (`\\copy` lee el archivo desde tu PC; descomprimir antes con 7-Zip o gzip):",
        "",
        "```sql",
    ]
    for t in tablas:
        cols = ", ".join(t["columnas"])
        nombre = str(t["archivo"]).removesuffix(".gz")
        lineas.append(
            f"\\copy {t['tabla']} ({cols}) FROM '{nombre}' WITH (FORMAT csv, HEADER true)"
        )
    lineas += [
        "```",
        "",
        "O con Python (psycopg, sin descomprimir), desde esta carpeta:",
        "",
        "```python",
        "import gzip, json, os, psycopg",
        "man = json.load(open('manifiesto.json', encoding='utf-8'))",
        "url = os.environ['DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://')",
        "with psycopg.connect(url) as conn, conn.cursor() as cur:",
        "    for t in man['tablas']:  # ya vienen en orden de restauración",
        "        cols = ', '.join(t['columnas'])",
        "        sql = f\"COPY {t['tabla']} ({cols}) FROM STDIN WITH (FORMAT csv, HEADER true)\"",
        "        with gzip.open(t['archivo'], 'rb') as f, cur.copy(sql) as cp:",
        "            while bloque := f.read(1 << 20):",
        "                cp.write(bloque)",
        "        print(t['tabla'], t['filas'])",
        "```",
        "",
        "## Después",
        "Ajustar las secuencias de las tablas con `id` autoincremental:",
        "",
        "```sql",
    ]
    for t in tablas:
        if "id" in t["columnas"]:
            lineas.append(
                f"SELECT setval(pg_get_serial_sequence('{t['tabla']}', 'id'), "
                f"(SELECT COALESCE(max(id), 1) FROM {t['tabla']}));"
            )
    lineas += [
        "```",
        "",
        "## Archivos",
        "",
        "| Tabla | Archivo | Filas | sha256 |",
        "|---|---|---|---|",
    ]
    for t in tablas:
        lineas.append(f"| {t['tabla']} | {t['archivo']} | {t['filas']} | `{t['sha256']}` |")
    return "\n".join(lineas) + "\n"


def respaldar(
    engine: Engine,
    dir_: Path,
    *,
    regiones: Sequence[int] | None = None,
    ahora: datetime | None = None,
    salida: Salida = print,
) -> Path:
    """Escribe `DIR/legado_AAAAMMDD/`: un CSV.gz por tabla (COPY … TO STDOUT, psycopg), el
    manifiesto y RESTAURAR.md. Todo en UNA transacción REPEATABLE READ: los conteos y los
    archivos ven la misma foto. Al final relee cada archivo y compara sus filas; si no calza,
    falla. Solo Postgres."""
    base = validar_dir_fuera_del_repo(dir_)
    ahora = ahora or ahora_utc()
    # `ahora` es UTC naive (como la base); la carpeta lleva la fecha de Chile.
    destino = base / f"legado_{ahora.replace(tzinfo=UTC).astimezone(TZ_CHILE):%Y%m%d}"
    destino.mkdir(parents=True, exist_ok=True)
    ca_temp = select(_PURGA_CA.c.codigo)
    qs = consultas(ca_temp)
    tablas: list[dict[str, Any]] = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        if regiones is None:
            with Session(bind=conn) as s:
                regiones = regiones_protegidas(s)
        criterio = select_ca_fuera_territorio(regiones, ahora)
        conn.execute(
            text("CREATE TEMP TABLE _purga_ca (codigo varchar(50) PRIMARY KEY) ON COMMIT DROP")
        )
        conn.execute(insert(_PURGA_CA).from_select(["codigo"], criterio))
        conteos = contar(conn, ca_temp)
        driver = conn.connection.driver_connection
        assert driver is not None
        for t in TABLAS_RESPALDO:
            sql = sql_literal(qs[t])
            archivo = f"{t}.csv.gz"
            ruta = destino / archivo
            copia = f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)"
            with driver.cursor() as cur, gzip.open(ruta, "wb") as gz, cur.copy(copia) as cp:
                for bloque in cp:
                    gz.write(bloque)
            salida(f"  {t}: {conteos[t]} filas → {ruta}")
            tablas.append({"tabla": t, "archivo": archivo, "filas": conteos[t], "consulta": sql})
        conn.rollback()  # solo lectura: la temporal se va con la transacción
    for info in tablas:
        ruta = destino / info["archivo"]
        columnas, filas = leer_csv_gz(ruta)
        if filas != info["filas"]:
            raise PurgaError(
                f"{info['archivo']}: el archivo tiene {filas} filas y el conteo {info['filas']}"
            )
        info["columnas"] = columnas
        info["sha256"] = _sha256(ruta)
    criterio_sql = str(criterio.compile(dialect=_PG))
    manifiesto = {
        "fase": "F-plat-purga-legado",
        "creado_en": ahora_utc().isoformat(),
        "ahora_criterio": ahora.isoformat(),
        "host": host_de(str(engine.url.render_as_string(hide_password=True))),
        "regiones_protegidas": list(regiones),
        "criterio_ca": criterio_sql,
        "criterio_ca_parametros": {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in criterio.compile(dialect=_PG).params.items()
        },
        "tablas": tablas,
    }
    (destino / MANIFIESTO).write_text(
        json.dumps(manifiesto, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (destino / RESTAURAR).write_text(_texto_restaurar(tablas), encoding="utf-8")
    # Verificación final contra el manifiesto escrito (no contra la memoria).
    verificar_archivos(destino)
    return destino


def leer_manifiesto(dir_respaldo: Path) -> dict[str, Any]:
    ruta = dir_respaldo / MANIFIESTO
    if not ruta.is_file():
        raise PurgaError(f"No existe {ruta}: corre --respaldar primero.")
    datos: dict[str, Any] = json.loads(ruta.read_text(encoding="utf-8"))
    return datos


def verificar_archivos(dir_respaldo: Path) -> None:
    """Relee cada CSV.gz y compara filas y sha256 con el manifiesto; si no calza, falla."""
    man = leer_manifiesto(dir_respaldo)
    for t in man["tablas"]:
        ruta = dir_respaldo / t["archivo"]
        if not ruta.is_file():
            raise PurgaError(f"Falta {ruta}")
        _, filas = leer_csv_gz(ruta)
        if filas != t["filas"]:
            raise PurgaError(f"{t['archivo']}: {filas} filas, el manifiesto dice {t['filas']}")
        if _sha256(ruta) != t["sha256"]:
            raise PurgaError(f"{t['archivo']}: el sha256 no calza con el manifiesto")


# ---------------------------------------------------------------------------
# --borrar
# ---------------------------------------------------------------------------


def _parse_iso(valor: str) -> datetime:
    dt = datetime.fromisoformat(valor)
    return dt.replace(tzinfo=None) if dt.tzinfo else dt  # el código guarda UTC naive


def verificar_manifiesto(
    man: Mapping[str, Any],
    conteo_actual: Mapping[str, int],
    *,
    ahora: datetime,
    host: str,
    regiones: Sequence[int],
    reanudando: bool,
) -> list[str]:
    """Motivos para NO borrar (lista vacía = se puede). Pura, testeable.

    - Manifiesto con más de 24 h, de otro host o de otro territorio.
    - Primera corrida: cada tabla tiene que calzar con el manifiesto ± 0,5 %.
    - Reanudando (ya se borró una parte): ninguna tabla puede tener MÁS de lo respaldado
      (+ 0,5 %): lo que queda es un subconjunto de lo respaldado."""
    motivos: list[str] = []
    creado = _parse_iso(str(man.get("creado_en", "")))
    if ahora - creado > MAX_EDAD_MANIFIESTO:
        motivos.append(
            f"el manifiesto tiene más de 24 h (creado {creado:%Y-%m-%d %H:%M} UTC): respalda de nuevo"
        )
    if man.get("host") != host:
        motivos.append(f"el manifiesto es de {man.get('host')} y la base actual es {host}")
    if sorted(int(r) for r in man.get("regiones_protegidas", [])) != sorted(
        int(r) for r in regiones
    ):
        motivos.append(
            f"el territorio cambió: manifiesto {man.get('regiones_protegidas')}, hoy {sorted(regiones)}"
        )
    respaldado = {t["tabla"]: int(t["filas"]) for t in man.get("tablas", [])}
    for tabla in TABLAS_RESPALDO:
        if tabla not in respaldado:
            motivos.append(f"el manifiesto no trae {tabla}")
            continue
        esperado, hoy = respaldado[tabla], int(conteo_actual.get(tabla, 0))
        margen = math.floor(esperado * TOLERANCIA_MANIFIESTO)
        if reanudando:
            if hoy > esperado + margen:
                motivos.append(f"{tabla}: hoy se borrarían {hoy} y el respaldo tiene {esperado}")
        elif abs(hoy - esperado) > margen:
            motivos.append(
                f"{tabla}: hoy se borrarían {hoy} y el respaldo tiene {esperado} (± 0,5 %)"
            )
    return motivos


def _leer_avance(dir_respaldo: Path) -> dict[str, Any] | None:
    ruta = dir_respaldo / AVANCE
    if not ruta.is_file():
        return None
    datos: dict[str, Any] = json.loads(ruta.read_text(encoding="utf-8"))
    return datos


def _guardar_avance(dir_respaldo: Path, avance: Mapping[str, Any]) -> None:
    tmp = dir_respaldo / (AVANCE + ".tmp")
    tmp.write_text(json.dumps(avance, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dir_respaldo / AVANCE)


def _ids_tanda(session: Session, stmt: Select[Any], tanda: int) -> list[Any]:
    return list(session.execute(stmt.limit(tanda)).scalars())


def borrar_por_tandas(
    engine: Engine,
    regiones: Sequence[int],
    ahora_criterio: datetime,
    *,
    tanda: int = TANDA_BORRADO,
    al_avanzar: Callable[[str, dict[str, int]], None] | None = None,
) -> dict[str, int]:
    """Borra el legado por tandas de `tanda` claves: una transacción y un commit por tanda, y
    nunca más que las claves de una tanda en memoria (regla 12). Idempotente: cada tanda
    se elige de lo que QUEDA, así que si se corta, otra corrida sigue desde ahí.

    Orden: primero las referencias de `fuente = 'licitaciones'` (matches y seguidas con sus
    alertas, feedback), después las hijas de las licitaciones (ítems, ofertas) y la licitación;
    al final las CA (cada tanda: matches + alertas, feedback, productos y la CA).
    `al_avanzar(paso, borrados_de_la_tanda)` se llama tras cada commit."""
    total: dict[str, int] = dict.fromkeys(TABLAS_RESPALDO, 0)

    def _registrar(paso: str, n: dict[str, int]) -> None:
        for k, v in n.items():
            total[k] = total.get(k, 0) + v
        if al_avanzar is not None:
            al_avanzar(paso, n)

    # 1. Referencias de licitaciones.
    while True:
        with Session(engine) as s:
            ids = _ids_tanda(
                s,
                select(OportunidadMatch.id)
                .where(OportunidadMatch.fuente == "licitaciones")
                .order_by(OportunidadMatch.id),
                tanda,
            )
            if not ids:
                break
            a = s.execute(delete(Alerta).where(Alerta.match_id.in_(ids))).rowcount  # type: ignore[attr-defined]
            m = s.execute(delete(OportunidadMatch).where(OportunidadMatch.id.in_(ids))).rowcount  # type: ignore[attr-defined]
            s.commit()
        _registrar(
            "matches_licitaciones", {"alertas": int(a or 0), "oportunidades_match": int(m or 0)}
        )
    while True:
        with Session(engine) as s:
            ids = _ids_tanda(
                s,
                select(OportunidadSeguida.id)
                .where(OportunidadSeguida.fuente == "licitaciones")
                .order_by(OportunidadSeguida.id),
                tanda,
            )
            if not ids:
                break
            a = s.execute(delete(Alerta).where(Alerta.seguimiento_id.in_(ids))).rowcount  # type: ignore[attr-defined]
            sg = s.execute(
                delete(OportunidadSeguida).where(OportunidadSeguida.id.in_(ids))
            ).rowcount  # type: ignore[attr-defined]
            s.commit()
        _registrar(
            "seguidas_licitaciones",
            {"alertas": int(a or 0), "oportunidades_seguidas": int(sg or 0)},
        )
    while True:
        with Session(engine) as s:
            ids = _ids_tanda(
                s,
                select(MatchFeedback.id)
                .where(MatchFeedback.fuente == "licitaciones")
                .order_by(MatchFeedback.id),
                tanda,
            )
            if not ids:
                break
            f = s.execute(delete(MatchFeedback).where(MatchFeedback.id.in_(ids))).rowcount  # type: ignore[attr-defined]
            s.commit()
        _registrar("feedback_licitaciones", {"match_feedback": int(f or 0)})

    # 2. Licitaciones: hijas y después el padre, por tanda de códigos.
    while True:
        with Session(engine) as s:
            codigos = _ids_tanda(s, select(Licitacion.codigo).order_by(Licitacion.codigo), tanda)
            if not codigos:
                break
            it = s.execute(
                delete(LicitacionItem).where(LicitacionItem.licitacion_codigo.in_(codigos))
            ).rowcount  # type: ignore[attr-defined]
            of = s.execute(
                delete(OfertaCompetencia).where(OfertaCompetencia.licitacion_codigo.in_(codigos))
            ).rowcount  # type: ignore[attr-defined]
            li = s.execute(delete(Licitacion).where(Licitacion.codigo.in_(codigos))).rowcount  # type: ignore[attr-defined]
            s.commit()
        _registrar(
            "licitaciones",
            {
                "licitacion_items": int(it or 0),
                "ofertas_competencia": int(of or 0),
                "licitaciones": int(li or 0),
            },
        )

    # 3. CA fuera del territorio, con el MISMO instante de corte del respaldo.
    if regiones:
        while True:
            with Session(engine) as s:
                codigos = _ids_tanda(s, select_ca_fuera_territorio(regiones, ahora_criterio), tanda)
                if not codigos:
                    break
                n = borrar_cas(s, codigos)
                s.commit()
            _registrar("compras_agiles", n)
            if n["compras_agiles"] == 0:
                break  # defensivo
    return total


@contextlib.contextmanager
def advisory_lock(engine: Engine, esperar_s: int = ESPERA_LOCK_S) -> Iterator[bool]:
    """El mismo pg_advisory_lock de la ingesta (regla 13). Rinde False si no lo obtuvo en
    `esperar_s` segundos (reintenta cada 30 s, sin retener conexión mientras espera)."""
    from app.ingest.orchestrator import _LOCK_KEY, _adquirir_lock, _pg_try_lock, _pg_unlock

    conn = _adquirir_lock(
        "purgar-legado", engine, _pg_try_lock, esperar_s, time.sleep, time.monotonic
    )
    if conn is None:
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(Exception):
            _pg_unlock(conn, _LOCK_KEY)
        conn.close()


def ejecutar_borrar(
    engine: Engine,
    dir_respaldo: Path,
    *,
    entrada: Callable[[str], str] = input,
    salida: Salida = print,
    lock: Callable[[Engine], contextlib.AbstractContextManager[bool]] = advisory_lock,
    ahora: datetime | None = None,
) -> dict[str, int]:
    """`--borrar`: valida el respaldo, pide «si», toma el lock y borra por tandas."""
    dir_respaldo = dir_respaldo.expanduser().resolve()
    man = leer_manifiesto(dir_respaldo)
    verificar_archivos(dir_respaldo)
    host = host_de(str(engine.url.render_as_string(hide_password=True)))
    ahora_criterio = _parse_iso(str(man["ahora_criterio"]))
    with Session(engine) as s:
        regiones = regiones_protegidas(s)
        conteo = contar(s, select_ca_fuera_territorio(regiones, ahora_criterio))
    avance = _leer_avance(dir_respaldo)
    motivos = verificar_manifiesto(
        man,
        conteo,
        ahora=ahora or ahora_utc(),
        host=host,
        regiones=regiones,
        reanudando=avance is not None,
    )
    if motivos:
        raise PurgaError("No borro nada:\n  - " + "\n  - ".join(motivos))
    salida(f"Base: {host}")
    for t in TABLAS_RESPALDO:
        salida(f"  {t}: {conteo[t]} filas a borrar")
    if entrada("Escribe «si» para BORRAR: ").strip().lower() != "si":
        raise PurgaError("Cancelado: no se borró nada.")
    with lock(engine) as obtenido:
        if not obtenido:
            raise PurgaError(
                "No obtuve el advisory lock en 10 min (¿hay un job corriendo?): no se borró nada."
            )
        estado: dict[str, Any] = avance or {"iniciado_en": ahora_utc().isoformat(), "borrados": {}}
        estado["terminado"] = False
        _guardar_avance(dir_respaldo, estado)

        def _al_avanzar(paso: str, n: dict[str, int]) -> None:
            for k, v in n.items():
                estado["borrados"][k] = estado["borrados"].get(k, 0) + v
            estado["actualizado_en"] = ahora_utc().isoformat()
            _guardar_avance(dir_respaldo, estado)
            salida(f"  {paso}: {n}")

        total = borrar_por_tandas(engine, regiones, ahora_criterio, al_avanzar=_al_avanzar)
        estado["terminado"] = True
        _guardar_avance(dir_respaldo, estado)
    salida(f"Listo. Borrado en esta corrida: {total}")
    return total


# ---------------------------------------------------------------------------
# --compactar
# ---------------------------------------------------------------------------


def _ident(tabla: str) -> str:
    if tabla not in TABLAS_COMPACTAR and tabla not in TABLAS_MEDIR:
        raise PurgaError(f"tabla no permitida: {tabla}")
    return tabla


def tamano_tabla(conn: Connection, tabla: str) -> int:
    return int(
        conn.execute(text("SELECT pg_total_relation_size(to_regclass(:t))"), {"t": tabla}).scalar()
        or 0
    )


def tamano_bd(conn: Connection) -> int:
    return int(conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one())


def filas_vivas_muertas(conn: Connection, tabla: str) -> tuple[int, int]:
    fila = conn.execute(
        text("SELECT n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname = :t"),
        {"t": tabla},
    ).first()
    return (int(fila[0]), int(fila[1])) if fila else (0, 0)


def cabe_compactar(tamano_db: int, tamano_despues: int, limite: int = LIMITE_NEON_BYTES) -> bool:
    """VACUUM FULL escribe una copia nueva antes de soltar la vieja: la base tiene que aguantar
    su tamaño actual + la tabla nueva + 20 % de margen bajo el límite de Neon."""
    return tamano_db + tamano_despues * MARGEN_COMPACTAR <= limite


def estimar_despues(total: int, vivas: int, muertas: int) -> int:
    """Tamaño tras VACUUM FULL ≈ el actual en proporción a las tuplas vivas (cota gruesa;
    `pg_stat_user_tables` se refresca con el ANALYZE que corre justo antes)."""
    if vivas + muertas == 0:
        return total
    return math.ceil(total * vivas / (vivas + muertas))


def _mb(b: int) -> str:
    return f"{b / 1024 / 1024:,.1f} MB"


def ejecutar_compactar(
    engine: Engine,
    *,
    entrada: Callable[[str], str] = input,
    salida: Salida = print,
    lock: Callable[[Engine], contextlib.AbstractContextManager[bool]] = advisory_lock,
    now_fn: Callable[..., datetime] | None = None,
) -> dict[str, tuple[int, int]]:
    """`--compactar`: VACUUM (FULL, ANALYZE) tabla por tabla, solo de noche (regla 5) y con el
    lock. Bloquea cada tabla mientras corre. Devuelve {tabla: (antes, después)}."""
    if not en_ventana_nocturna(now_fn):
        raise PurgaError(
            "--compactar solo corre entre 22:00 y 07:00 de Chile (VACUUM FULL bloquea la tabla)."
        )
    host = host_de(str(engine.url.render_as_string(hide_password=True)))
    salida(f"Base: {host}")
    if (
        entrada("Escribe «si» para COMPACTAR (bloquea cada tabla mientras corre): ").strip().lower()
        != "si"
    ):
        raise PurgaError("Cancelado: no se compactó nada.")
    resultado: dict[str, tuple[int, int]] = {}
    with lock(engine) as obtenido:
        if not obtenido:
            raise PurgaError("No obtuve el advisory lock en 10 min (¿hay un job corriendo?).")
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for tabla in TABLAS_COMPACTAR:
                t = _ident(tabla)
                conn.execute(text(f"ANALYZE {t}"))
                antes = tamano_tabla(conn, t)
                vivas, muertas = filas_vivas_muertas(conn, t)
                despues_est = estimar_despues(antes, vivas, muertas)
                db = tamano_bd(conn)
                if not cabe_compactar(db, despues_est):
                    salida(
                        f"  {t}: SALTADA — no cabe (base {_mb(db)} + {_mb(despues_est)} × 1,2 > {_mb(LIMITE_NEON_BYTES)})"
                    )
                    continue
                inicio = time.monotonic()
                conn.execute(text(f"VACUUM (FULL, ANALYZE) {t}"))
                despues = tamano_tabla(conn, t)
                resultado[t] = (antes, despues)
                salida(f"  {t}: {_mb(antes)} → {_mb(despues)} ({time.monotonic() - inicio:.0f} s)")
            salida(f"Base: {_mb(tamano_bd(conn))}")
    return resultado


# ---------------------------------------------------------------------------
# --medir
# ---------------------------------------------------------------------------


def ejecutar_medir(
    engine: Engine, *, salida: Salida = print, ahora: datetime | None = None
) -> dict[str, Any]:
    """Solo lectura: tamaño de la base, de cada tabla (vivas/muertas) y cuánto borraría cada paso."""
    ahora = ahora or ahora_utc()
    with engine.connect() as conn:
        db = tamano_bd(conn)
        salida(f"Base: {host_de(str(engine.url.render_as_string(hide_password=True)))}")
        salida(
            f"pg_database_size: {_mb(db)} de {_mb(LIMITE_NEON_BYTES)} ({db / LIMITE_NEON_BYTES:.0%})"
        )
        salida("(vivas y muertas: estimaciones de pg_stat_user_tables, se refrescan con ANALYZE)")
        salida(f"{'tabla':<24}{'tamaño':>12}{'vivas':>12}{'muertas':>12}")
        tablas: dict[str, dict[str, int]] = {}
        for t in TABLAS_MEDIR:
            tam = tamano_tabla(conn, _ident(t))
            vivas, muertas = filas_vivas_muertas(conn, t)
            tablas[t] = {"bytes": tam, "vivas": vivas, "muertas": muertas}
            salida(f"{t:<24}{_mb(tam):>12}{vivas:>12,}{muertas:>12,}")
        with Session(bind=conn) as s:
            regiones = regiones_protegidas(s)
            conteo = contar(s, select_ca_fuera_territorio(regiones, ahora))
        salida(f"Territorio protegido: regiones {regiones}")
        salida("Se borraría:")
        for t in TABLAS_RESPALDO:
            salida(f"  {t:<24}{conteo[t]:>12,}")
        conn.rollback()
    return {"bd": db, "tablas": tablas, "regiones": regiones, "borraria": conteo}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Respaldo, borrado del legado y compactación de Neon (F-plat-purga-legado).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    modo = p.add_mutually_exclusive_group()
    modo.add_argument("--medir", action="store_true", help="solo lectura")
    modo.add_argument("--respaldar", metavar="DIR", type=Path, help="carpeta FUERA del repo")
    modo.add_argument("--borrar", action="store_true", help="requiere --respaldo")
    modo.add_argument("--compactar", action="store_true", help="VACUUM FULL, solo de noche")
    p.add_argument("--respaldo", metavar="DIR/legado_AAAAMMDD", type=Path)
    return p


def _engine_desde_entorno() -> Engine:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise PurgaError(
            "Falta DATABASE_URL (en PowerShell: $env:DATABASE_URL = $env:DATABASE_URL_PROD)."
        )
    return create_engine(
        normalizar_url_driver(url), pool_pre_ping=True, pool_size=2, max_overflow=1
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    entrada: Callable[[str], str] = input,
    salida: Salida = print,
    engine: Engine | None = None,
    now_fn: Callable[..., datetime] | None = None,
) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = _parser()
    args = parser.parse_args(argv)
    if not (args.medir or args.respaldar or args.borrar or args.compactar):
        buf = io.StringIO()
        parser.print_help(buf)
        salida(buf.getvalue())
        return 0
    if args.borrar and args.respaldo is None:
        salida("--borrar exige --respaldo DIR/legado_AAAAMMDD")
        return 2
    try:
        if args.respaldar is not None:
            validar_dir_fuera_del_repo(args.respaldar)  # antes de conectarse
        if args.compactar and not en_ventana_nocturna(now_fn):
            raise PurgaError(
                "--compactar solo corre entre 22:00 y 07:00 de Chile (VACUUM FULL bloquea la tabla)."
            )
        eng = engine or _engine_desde_entorno()
        if args.medir:
            ejecutar_medir(eng, salida=salida)
        elif args.respaldar is not None:
            salida(f"Base: {host_de(str(eng.url.render_as_string(hide_password=True)))}")
            destino = respaldar(eng, args.respaldar, salida=salida)
            salida(f"Respaldo verificado en {destino}. Cópialo también a la nube.")
        elif args.borrar:
            ejecutar_borrar(eng, args.respaldo, entrada=entrada, salida=salida)
        else:
            ejecutar_compactar(eng, entrada=entrada, salida=salida, now_fn=now_fn)
    except PurgaError as exc:
        salida(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
