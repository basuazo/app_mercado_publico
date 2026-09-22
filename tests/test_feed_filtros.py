"""Tests F-feed-filtros — filtros, facetas, orden por monto, nuevas del día y
paginación del feed.

Casi todo son funciones puras sobre listas fabricadas: ni red ni Postgres. El
único test con base es el que fija la forma del JSON de `/api/oportunidades`,
que esta fase NO debe cambiar aunque el retorno interno pase a `ResultadoFeed`.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.query import (
    AGRUPAR_POR_VALIDOS,
    LIMITE_PAGINA_DEFAULT,
    FiltrosFeed,
    _aplicar_filtros,
    _ordenar,
    agrupar_oportunidades,
    calcular_facetas,
    contar_nuevas_hoy,
    get_oportunidades_usuario,
)
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token
from app.core.settings import Settings
from app.models.base import Base
from app.models.enums import EstadoOportunidad, FamiliaEstado, RolUsuario
from app.models.tables import Licitacion, OportunidadMatch, PerfilBusqueda, Usuario

_PW = "contraseña-segura-test"


# ---------------------------------------------------------------------------
# Item fabricado: las mismas claves que arma `_construir_item`
# ---------------------------------------------------------------------------


def _item(
    codigo: str,
    *,
    score: float = 50,
    fuente: str = "licitaciones",
    nombre: str | None = None,
    monto: float | None = None,
    fecha_cierre: datetime | None = None,
    estado: str = EstadoOportunidad.PUBLICADA.value,
    region: int | None = None,
    fecha_match: datetime | None = None,
) -> dict:
    match = OportunidadMatch(
        perfil_id=1,
        fuente=fuente,
        codigo_oportunidad=codigo,
        score=score,
        razones={},
    )
    if fecha_match is not None:
        match.fecha_match = fecha_match
    return {
        "match": match,
        "nombre": nombre if nombre is not None else f"Oportunidad {codigo}",
        "estado": estado,
        "fecha_cierre": fecha_cierre,
        "dias_al_cierre": None,
        "monto": monto,
        "region": region,
        "region_nombre": None,
    }


def _codigos(items: list[dict]) -> list[str]:
    return [i["match"].codigo_oportunidad for i in items]


# ---------------------------------------------------------------------------
# 3.1 Filtro por monto
# ---------------------------------------------------------------------------


def test_monto_min_y_max_acotan_el_rango() -> None:
    items = [
        _item("BARATA", monto=500_000),
        _item("MEDIA", monto=5_000_000),
        _item("CARA", monto=50_000_000),
    ]
    filtrados = _aplicar_filtros(items, FiltrosFeed(monto_min=1_000_000, monto_max=10_000_000))

    assert _codigos(filtrados) == ["MEDIA"]


def test_monto_no_informado_pasa_por_defecto() -> None:
    """El monto falta seguido en la fuente oficial: excluirlo por omisión
    escondería oportunidades reales."""
    items = [_item("SIN-MONTO", monto=None), _item("CON-MONTO", monto=5_000_000)]
    filtrados = _aplicar_filtros(items, FiltrosFeed(monto_min=1_000_000))

    assert _codigos(filtrados) == ["SIN-MONTO", "CON-MONTO"]


def test_monto_no_informado_se_puede_excluir() -> None:
    items = [_item("SIN-MONTO", monto=None), _item("CON-MONTO", monto=5_000_000)]
    filtrados = _aplicar_filtros(
        items, FiltrosFeed(monto_min=1_000_000, incluir_monto_no_informado=False)
    )

    assert _codigos(filtrados) == ["CON-MONTO"]


def test_monto_sin_filtro_no_descarta_nada() -> None:
    items = [_item("SIN-MONTO", monto=None), _item("CON-MONTO", monto=1)]
    assert len(_aplicar_filtros(items, FiltrosFeed())) == 2


# ---------------------------------------------------------------------------
# 3.2 Filtro por rango de fecha de cierre
# ---------------------------------------------------------------------------


def test_cierre_rango_acota() -> None:
    items = [
        _item("ANTES", fecha_cierre=datetime(2026, 9, 1, 12, 0)),
        _item("DENTRO", fecha_cierre=datetime(2026, 9, 15, 12, 0)),
        _item("DESPUES", fecha_cierre=datetime(2026, 10, 15, 12, 0)),
    ]
    filtrados = _aplicar_filtros(
        items,
        FiltrosFeed(cierre_desde=datetime(2026, 9, 10), cierre_hasta=datetime(2026, 9, 30)),
    )

    assert _codigos(filtrados) == ["DENTRO"]


def test_cierre_sin_fecha_pasa_por_defecto() -> None:
    """Compra Ágil está dejando `fecha_cierre` en NULL incluso en las
    publicadas (deuda de docs/00-estado-actual.md) y el matching ya las trata
    como abiertas: sin este escape, el filtro por rango las borraría TODAS."""
    items = [
        _item("CA-SIN-FECHA", fuente="compras_agiles", fecha_cierre=None),
        _item("LIC", fecha_cierre=datetime(2026, 9, 15, 12, 0)),
    ]
    filtrados = _aplicar_filtros(items, FiltrosFeed(cierre_desde=datetime(2026, 9, 10)))

    assert _codigos(filtrados) == ["CA-SIN-FECHA", "LIC"]


def test_cierre_sin_fecha_se_puede_excluir() -> None:
    items = [
        _item("CA-SIN-FECHA", fuente="compras_agiles", fecha_cierre=None),
        _item("LIC", fecha_cierre=datetime(2026, 9, 15, 12, 0)),
    ]
    filtrados = _aplicar_filtros(
        items,
        FiltrosFeed(cierre_desde=datetime(2026, 9, 10), incluir_sin_fecha_cierre=False),
    )

    assert _codigos(filtrados) == ["LIC"]


def test_cierre_borde_naive_se_interpreta_como_hora_de_chile() -> None:
    """La columna guarda naive en UTC (criterio de `app/core/tiempo`), así que
    un borde naive se convierte igual que cualquier otro instante del proyecto:
    se lee como hora de Chile. El 15-sep-2026 a las 00:00 en Chile son las
    03:00 UTC, así que un cierre guardado a las 02:00 UTC queda ANTES del
    borde."""
    items = [
        _item("JUSTO-ANTES", fecha_cierre=datetime(2026, 9, 15, 2, 0)),
        _item("JUSTO-DESPUES", fecha_cierre=datetime(2026, 9, 15, 4, 0)),
    ]
    filtrados = _aplicar_filtros(items, FiltrosFeed(cierre_desde=datetime(2026, 9, 15, 0, 0)))

    assert _codigos(filtrados) == ["JUSTO-DESPUES"]


# ---------------------------------------------------------------------------
# 3.3 Filtro por familia de estado
# ---------------------------------------------------------------------------


def test_familias_filtra_por_situacion_no_por_estado_crudo() -> None:
    items = [
        _item("ABIERTA", estado=EstadoOportunidad.PUBLICADA.value),
        _item("EVAL-1", estado=EstadoOportunidad.CERRADA.value),
        _item("EVAL-2", estado=EstadoOportunidad.EN_PROCESO.value),
        _item("ADJ", estado=EstadoOportunidad.ADJUDICADA.value),
    ]
    filtrados = _aplicar_filtros(items, FiltrosFeed(familias=frozenset({FamiliaEstado.EN_EVALUACION})))

    assert _codigos(filtrados) == ["EVAL-1", "EVAL-2"]


def test_familias_none_no_filtra() -> None:
    items = [_item("A"), _item("B", estado=EstadoOportunidad.DESIERTA.value)]
    assert len(_aplicar_filtros(items, FiltrosFeed(familias=None))) == 2


# ---------------------------------------------------------------------------
# 3.4 Región: solo discrimina Compra Ágil
# ---------------------------------------------------------------------------


def test_region_no_descarta_licitaciones() -> None:
    """`Licitacion` no guarda región: las licitaciones pasan todas. La
    advertencia al usuario la pone la UI; acá se fija el comportamiento."""
    items = [
        _item("LIC", fuente="licitaciones", region=None),
        _item("CA-13", fuente="compras_agiles", region=13),
        _item("CA-05", fuente="compras_agiles", region=5),
    ]
    filtrados = _aplicar_filtros(items, FiltrosFeed(region=13))

    assert _codigos(filtrados) == ["LIC", "CA-13"]


# ---------------------------------------------------------------------------
# Bloque 4 — orden por monto
# ---------------------------------------------------------------------------


def test_orden_monto_descendente_con_no_informados_al_final() -> None:
    """Un monto que la fuente no entrega no es un monto cero."""
    items = [
        _item("SIN", monto=None),
        _item("CHICA", monto=1_000),
        _item("GRANDE", monto=90_000_000),
        _item("MEDIA", monto=5_000_000),
    ]
    _ordenar(items, "monto")

    assert _codigos(items) == ["GRANDE", "MEDIA", "CHICA", "SIN"]


def test_orden_desconocido_cae_a_score() -> None:
    items = [_item("BAJA", score=10), _item("ALTA", score=90)]
    _ordenar(items, "inventado")

    assert _codigos(items) == ["ALTA", "BAJA"]


# ---------------------------------------------------------------------------
# Bloque 5 — facetas
# ---------------------------------------------------------------------------


def _mixto() -> list[dict]:
    return [
        _item("LIC-1", fuente="licitaciones", estado=EstadoOportunidad.PUBLICADA.value),
        _item("LIC-2", fuente="licitaciones", estado=EstadoOportunidad.ADJUDICADA.value),
        _item("CA-1", fuente="compras_agiles", estado=EstadoOportunidad.PUBLICADA.value, region=13),
        _item("CA-2", fuente="compras_agiles", estado=EstadoOportunidad.PUBLICADA.value, region=5),
    ]


def test_facetas_sin_filtros_cuentan_todo() -> None:
    facetas = calcular_facetas(_mixto(), FiltrosFeed())

    assert facetas["fuente"] == {"compras_agiles": 2, "licitaciones": 2}
    assert facetas["estado"] == {"abierta": 3, "adjudicada": 1}
    # Las licitaciones no guardan región: caen en "sin_region".
    assert facetas["region"] == {"sin_region": 2, "13": 1, "5": 1}


def test_faceta_no_se_aplica_su_propio_filtro() -> None:
    """La regla que hace usable la búsqueda facetada: con "Licitaciones" ya
    elegido, el conteo de Compra Ágil sigue diciendo cuántas habría si soltara
    el filtro. Si bajara a 0, el filtro sería una trampa sin salida."""
    facetas = calcular_facetas(_mixto(), FiltrosFeed(fuente="licitaciones"))

    assert facetas["fuente"] == {"compras_agiles": 2, "licitaciones": 2}


def test_faceta_si_aplica_los_demas_filtros() -> None:
    """La otra mitad de la regla: el conteo de fuente SÍ respeta el filtro de
    estado, así que no promete resultados que el usuario no vería."""
    facetas = calcular_facetas(
        _mixto(),
        FiltrosFeed(fuente="licitaciones", familias=frozenset({FamiliaEstado.ABIERTA})),
    )

    assert facetas["fuente"] == {"compras_agiles": 2, "licitaciones": 1}
    # Y la faceta de estado ignora la suya propia, pero respeta la de fuente.
    assert facetas["estado"] == {"abierta": 1, "adjudicada": 1}


def test_faceta_region_ignora_su_propio_filtro() -> None:
    facetas = calcular_facetas(_mixto(), FiltrosFeed(region=13))

    assert facetas["region"] == {"sin_region": 2, "13": 1, "5": 1}


def test_facetas_de_lista_vacia_tienen_la_forma_completa() -> None:
    facetas = calcular_facetas([], FiltrosFeed())

    assert facetas == {"fuente": {}, "estado": {}, "region": {}}


# ---------------------------------------------------------------------------
# Bloque 6 — nuevas del día, con el borde en America/Santiago
# ---------------------------------------------------------------------------


def test_nuevas_hoy_usa_el_dia_calendario_de_chile_no_el_de_utc() -> None:
    """`fecha_match` se guarda naive en UTC y Render corre en UTC (regla 5).
    En septiembre Chile está en UTC-3:

    - 23-sep 01:00 UTC = 22-sep 22:00 en Chile -> ES de hoy (en UTC sería mañana)
    - 22-sep 02:00 UTC = 21-sep 23:00 en Chile -> NO es de hoy (en UTC sería hoy)

    Un conteo hecho con la fecha del proceso se equivoca en los dos casos, y
    por eso cada uno se cuenta por separado: juntos darían el mismo total con
    las dos implementaciones y el test no probaría nada.
    """
    hoy = date(2026, 9, 22)
    noche_de_hoy = [_item("NOCHE-DE-HOY", fecha_match=datetime(2026, 9, 23, 1, 0))]
    noche_de_ayer = [_item("NOCHE-DE-AYER", fecha_match=datetime(2026, 9, 22, 2, 0))]

    assert contar_nuevas_hoy(noche_de_hoy, hoy=hoy) == 1
    assert contar_nuevas_hoy(noche_de_ayer, hoy=hoy) == 0
    assert contar_nuevas_hoy(noche_de_hoy + noche_de_ayer, hoy=hoy) == 1


def test_nuevas_hoy_sin_matches_del_dia() -> None:
    items = [_item("VIEJA", fecha_match=datetime(2026, 9, 1, 12, 0))]

    assert contar_nuevas_hoy(items, hoy=date(2026, 9, 22)) == 0


# ---------------------------------------------------------------------------
# Bloque 7 — paginación vs. cap por grupo
# ---------------------------------------------------------------------------


def test_ninguno_pagina_y_motivo_capa_por_grupo() -> None:
    """La regla adoptada, en un solo test para que quede documentada: sin
    agrupar manda la paginación (`limit`/`offset` ya aplicados aguas arriba) y
    el grupo único NO se capa; agrupando manda el cap por grupo."""
    items = [_item(f"LIC-{i}", score=80) for i in range(15)]

    grupos, total_unico, total_apariciones = agrupar_oportunidades(items, "ninguno")
    assert len(grupos) == 1
    assert grupos[0]["count"] == 15
    assert len(grupos[0]["items"]) == 15  # sin cap: la página ya venía cortada
    assert total_unico == 15
    assert total_apariciones == 15

    grupos_motivo, _, _ = agrupar_oportunidades(items, "motivo", cap_por_grupo=10)
    assert grupos_motivo[0]["count"] == 15
    assert len(grupos_motivo[0]["items"]) == 10  # el cap sigue mandando


def test_ninguno_es_un_agrupar_por_valido() -> None:
    assert "ninguno" in AGRUPAR_POR_VALIDOS


def test_ninguno_con_lista_vacia_no_rompe() -> None:
    grupos, total_unico, total_apariciones = agrupar_oportunidades([], "ninguno")

    assert len(grupos) == 1
    assert grupos[0]["items"] == []
    assert (total_unico, total_apariciones) == (0, 0)


def test_limite_de_pagina_por_defecto_es_20() -> None:
    assert LIMITE_PAGINA_DEFAULT == 20
    assert inspect.signature(get_oportunidades_usuario).parameters["limit"].default == 20


# ---------------------------------------------------------------------------
# Los defaults que no se pueden cambiar sin romper el feed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parametro", ["incluir_monto_no_informado", "incluir_sin_fecha_cierre"]
)
def test_los_incluir_van_en_true_por_defecto(parametro: str) -> None:
    """Si alguien los da vuelta, el feed se come en silencio las oportunidades
    a las que la fuente no les informa el dato — que son muchas."""
    assert getattr(FiltrosFeed(), parametro) is True
    assert inspect.signature(get_oportunidades_usuario).parameters[parametro].default is True


# ---------------------------------------------------------------------------
# La forma del JSON de /api/oportunidades NO cambia (único test con base)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    import app.models.tables  # noqa: F401

    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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
    return TestClient(create_app(settings, engine), raise_server_exceptions=True)


@pytest.fixture()
def usuario(engine):
    with Session(engine) as s:
        u = Usuario(
            email="dueno@test.cl",
            password_hash=hash_password(_PW),
            rol=RolUsuario.USUARIO,
            activo=True,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_api_oportunidades_conserva_la_forma_del_json(client, usuario, settings, engine) -> None:
    """`get_oportunidades_usuario` pasó a devolver `ResultadoFeed`, pero el
    contrato de la API REST es el de antes: mismas claves, mismos nombres."""
    with Session(engine) as s:
        s.add(
            Licitacion(
                codigo="LIC-001",
                nombre="Licitación LIC-001",
                descripcion="",
                estado="publicada",
                fecha_cierre=datetime(2026, 12, 1, 15, 0),
                monto_clp=1_500_000,
            )
        )
        perfil = PerfilBusqueda(
            owner_id=usuario,
            nombre="Perfil test",
            keywords=["test"],
            keywords_excluir=[],
            regiones=[],
            fuentes=["licitaciones"],
            activo=True,
        )
        s.add(perfil)
        s.flush()
        s.add(
            OportunidadMatch(
                perfil_id=perfil.id,
                fuente="licitaciones",
                codigo_oportunidad="LIC-001",
                score=80,
                razones=[],
            )
        )
        s.commit()

    cookies = {COOKIE_NAME: create_session_token(settings.secret_key, usuario)}
    r = client.get("/api/oportunidades", cookies=cookies)

    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"total", "pagina", "items"}
    assert data["total"] == 1
    assert data["pagina"] == 1
    assert set(data["items"][0]) == {
        "fuente",
        "codigo",
        "nombre",
        "score",
        "estado",
        "fecha_cierre",
        "dias_al_cierre",
        "monto",
        "organismo",
        "url_ficha",
    }
    assert data["items"][0]["codigo"] == "LIC-001"
    assert data["items"][0]["monto"] == 1_500_000
