"""Tests F-feed-ui-2 — panel lateral de filtros, chips, paginación y el cambio
de default de agrupación.

Todo offline: SQLite en memoria y TestClient, sin red y sin Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.query import LIMITE_PAGINA_DEFAULT
from app.api.routes.pages import _monto, _url_alternar, _url_feed
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token
from app.core.settings import Settings
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CompraAgil,
    Licitacion,
    OportunidadMatch,
    PerfilBusqueda,
    Usuario,
)

_PW = "contraseña-segura-test"


# ---------------------------------------------------------------------------
# El armador de URLs (Bloque 4) — sin base ni cliente
# ---------------------------------------------------------------------------


def test_url_feed_no_serializa_lo_que_no_esta_activo() -> None:
    assert _url_feed({}) == "/"


def test_url_feed_codifica_cada_valor() -> None:
    """El bug original de F-ui-fixes: un "&" escrito en el buscador partía el
    querystring y corrompía los filtros al primer clic."""
    url = _url_feed({"texto": "aseo & mantención"}, orden="cierre")

    assert "aseo%20%26%20mantenci%C3%B3n" in url
    assert "&" not in url.split("?")[1].replace("&orden", "|orden")
    assert parse_qs(urlparse(url).query)["texto"] == ["aseo & mantención"]


def test_url_feed_borra_con_none_y_con_vacio() -> None:
    base = {"region": 5, "texto": "aseo"}

    assert "region" not in _url_feed(base, region=None)
    assert "texto" not in _url_feed(base, texto="")
    assert "fuente" not in _url_feed({"fuente": ["licitaciones"]}, fuente=[])


def test_url_feed_resetea_la_paginacion_ante_cualquier_cambio() -> None:
    """Quedarse en la página 3 de un resultado que ahora tiene 10 ítems deja la
    lista vacía sin explicación."""
    con_offset = {"offset": 40, "texto": "aseo"}

    assert "offset" not in _url_feed(con_offset, region=5)
    # Salvo que el cambio sea justamente moverse de página.
    assert "offset=60" in _url_feed(con_offset, offset=60)


def test_url_feed_ordena_siempre_igual() -> None:
    """Dos estados iguales tienen que dar la MISMA URL para ser comparables."""
    uno = _url_feed({"orden": "monto", "texto": "aseo", "region": 5})
    otro = _url_feed({"region": 5, "texto": "aseo", "orden": "monto"})

    assert uno == otro


def test_url_alternar_agrega_y_saca_de_un_multivaluado() -> None:
    con_una = _url_alternar({}, "kw", "aseo")
    assert parse_qs(urlparse(con_una).query)["kw"] == ["aseo"]

    sin_ninguna = _url_alternar({"kw": ["aseo"]}, "kw", "aseo")
    assert "kw" not in sin_ninguna


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [("5.000.000", 5_000_000), ("$5.000.000", 5_000_000), ("5000000", 5_000_000), ("", None), ("abc", None)],
)
def test_monto_acepta_lo_que_el_campo_devuelve(entrada: str, esperado: float | None) -> None:
    """El campo se muestra con punto de miles, así que eso es lo que vuelve."""
    assert _monto(entrada) == esperado


# ---------------------------------------------------------------------------
# Fixtures
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


def _cookie(settings: Settings, user_id: int) -> dict[str, str]:
    return {COOKIE_NAME: create_session_token(settings.secret_key, user_id)}


def _perfil(session: Session, owner_id: int, *, nombre: str = "Perfil test", keywords=None) -> int:
    p = PerfilBusqueda(
        owner_id=owner_id,
        nombre=nombre,
        keywords=keywords if keywords is not None else ["test"],
        keywords_excluir=[],
        regiones=[],
        fuentes=["licitaciones", "compras_agiles"],
        activo=True,
    )
    session.add(p)
    session.flush()
    return int(p.id)


def _crear(
    engine,
    owner_id: int,
    codigo: str,
    *,
    fuente: str = "licitaciones",
    score: float = 80,
    monto: float | None = 1_000_000,
    dias: float | None = 5,
    estado: str = "publicada",
    region: int | None = None,
    perfil_id: int | None = None,
    razones: dict | None = None,
) -> int:
    """Retorna el `perfil_id` usado (nuevo, salvo que se pase uno existente)."""
    ahora = datetime.now(UTC).replace(tzinfo=None)
    cierre = ahora + timedelta(days=dias, hours=1) if dias is not None else None
    with Session(engine) as s:
        if fuente == "licitaciones":
            s.add(
                Licitacion(
                    codigo=codigo,
                    nombre=f"Oportunidad {codigo}",
                    descripcion="",
                    estado=estado,
                    monto_clp=monto,
                    fecha_cierre=cierre,
                )
            )
        else:
            s.add(
                CompraAgil(
                    codigo=codigo,
                    nombre=f"Oportunidad {codigo}",
                    descripcion="",
                    estado=estado,
                    monto_disponible_clp=monto,
                    fecha_cierre=cierre,
                    region=region,
                )
            )
        pid = perfil_id if perfil_id is not None else _perfil(s, owner_id)
        s.add(
            OportunidadMatch(
                perfil_id=pid,
                fuente=fuente,
                codigo_oportunidad=codigo,
                score=score,
                razones=razones if razones is not None else {},
            )
        )
        s.commit()
        return pid


# ---------------------------------------------------------------------------
# La ruta arma los filtros desde el querystring
# ---------------------------------------------------------------------------


def test_filtro_de_monto_desde_el_querystring(client, settings, usuario, engine) -> None:
    _crear(engine, usuario, "BARATA", monto=500_000)
    _crear(engine, usuario, "CARA", monto=50_000_000)

    html = client.get(
        "/?monto_min=1.000.000", cookies=_cookie(settings, usuario)
    ).text

    assert "Oportunidad CARA" in html
    assert "Oportunidad BARATA" not in html


def test_filtro_de_estado_ya_no_filtra_y_se_ignora(client, settings, usuario, engine) -> None:
    """F-vigencia: la sección "Estado" del panel se sacó (con solo_vigentes=True
    lo único que queda siempre es "Abierta"). Un enlace viejo con `?estado=...`
    no rompe ni filtra: ADJUDICADA no aparece porque dejó de ser vigente, no
    porque el parámetro la haya excluido."""
    _crear(engine, usuario, "ABIERTA", estado="publicada")
    _crear(engine, usuario, "ADJUDICADA", estado="adjudicada")

    html = client.get("/?estado=adjudicada", cookies=_cookie(settings, usuario)).text

    assert "Oportunidad ABIERTA" in html
    assert "Oportunidad ADJUDICADA" not in html


def test_filtro_de_fuente_desde_el_querystring(client, settings, usuario, engine) -> None:
    _crear(engine, usuario, "LIC-1", fuente="licitaciones")
    _crear(engine, usuario, "CA-1", fuente="compras_agiles")

    html = client.get("/?fuente=compras_agiles", cookies=_cookie(settings, usuario)).text

    assert "Oportunidad CA-1" in html
    assert "Oportunidad LIC-1" not in html


def test_las_dos_fuentes_marcadas_es_sin_filtro(client, settings, usuario, engine) -> None:
    """El modelo tiene una fuente por match: el conjunto vacío no existe, así
    que marcar las dos equivale a no filtrar."""
    _crear(engine, usuario, "LIC-1", fuente="licitaciones")
    _crear(engine, usuario, "CA-1", fuente="compras_agiles")

    html = client.get(
        "/?fuente=licitaciones&fuente=compras_agiles", cookies=_cookie(settings, usuario)
    ).text

    assert "Oportunidad LIC-1" in html
    assert "Oportunidad CA-1" in html


def test_el_monto_no_informado_se_incluye_salvo_que_se_pida_lo_contrario(
    client, settings, usuario, engine
) -> None:
    _crear(engine, usuario, "SIN-MONTO", monto=None)

    incluye = client.get("/", cookies=_cookie(settings, usuario)).text
    assert "Oportunidad SIN-MONTO" in incluye

    # Desde un enlace (forma canónica).
    excluye = client.get("/?excluir_sin_monto=1", cookies=_cookie(settings, usuario)).text
    assert "Oportunidad SIN-MONTO" not in excluye

    # Y desde el panel, donde la casilla ausente significa desmarcada.
    del_panel = client.get("/?panel=1", cookies=_cookie(settings, usuario)).text
    assert "Oportunidad SIN-MONTO" not in del_panel


def test_las_casillas_incluir_van_marcadas_sin_parametros(client, settings, usuario, engine) -> None:
    """Si alguien las da vuelta, el feed se come en silencio las oportunidades
    a las que la fuente no informa el dato."""
    _crear(engine, usuario, "LIC-1")
    html = client.get("/", cookies=_cookie(settings, usuario)).text

    for id_casilla in ("filtros-escritorio-sin-monto", "filtros-escritorio-sin-cierre"):
        i = html.index(f'id="{id_casilla}"')
        # El `checked` va después del id, dentro de la misma etiqueta.
        assert "checked" in html[i : html.index(">", i)]


def test_la_busqueda_con_ampersand_sobrevive_a_los_enlaces(client, settings, usuario, engine) -> None:
    _crear(engine, usuario, "LIC-1")
    html = client.get(
        "/", params={"texto": "aseo & mantención"}, cookies=_cookie(settings, usuario)
    ).text

    assert "texto=aseo%20%26%20mantenci%C3%B3n" in html
    assert "texto=aseo & mantención" not in html


# ---------------------------------------------------------------------------
# Chips de filtro activo
# ---------------------------------------------------------------------------


def test_sin_filtros_no_se_renderiza_la_fila_de_chips(client, settings, usuario, engine) -> None:
    _crear(engine, usuario, "LIC-1")
    html = client.get("/", cookies=_cookie(settings, usuario)).text

    assert "chip-filtro" not in html
    assert "Limpiar todo" not in html


def test_los_chips_reflejan_exactamente_los_filtros_activos(
    client, settings, usuario, engine
) -> None:
    _crear(engine, usuario, "CA-1", fuente="compras_agiles", region=5)

    html = client.get(
        "/?fuente=compras_agiles&region=5&monto_min=1000000&monto_max=50000000",
        cookies=_cookie(settings, usuario),
    ).text

    assert "Fuente: Compra Ágil" in html
    assert "Región: Valparaíso" in html
    assert "Monto: $1.000.000 – $50.000.000" in html
    # Tres, no cuatro: el mínimo y el máximo de monto son UN rango, un chip.
    assert "Limpiar todo (3)" in html


def test_el_chip_quita_solo_su_filtro(client, settings, usuario, engine) -> None:
    _crear(engine, usuario, "CA-1", fuente="compras_agiles", region=5)
    html = client.get("/?region=5&monto_min=1000000", cookies=_cookie(settings, usuario)).text

    # El enlace del chip de región conserva el monto y suelta la región.
    i = html.index("Quitar filtro Región")
    href = html[html.rindex('href="', 0, i) + 6 : html.index('"', html.rindex('href="', 0, i) + 6)]
    query = parse_qs(urlparse(href).query)

    assert "region" not in query
    assert query["monto_min"] == ["1000000"]


# ---------------------------------------------------------------------------
# Agrupación, orden y paginación
# ---------------------------------------------------------------------------


def test_el_default_de_agrupacion_es_ninguno(client, settings, usuario, engine) -> None:
    """Agrupando por motivo la misma oportunidad aparece en varios grupos; sin
    agrupar aparece una vez y sus motivos viven como chips en la tarjeta."""
    _crear(engine, usuario, "LIC-1")
    html = client.get("/", cookies=_cookie(settings, usuario)).text

    assert 'id="feed-agrupado"' not in html
    i = html.index('id="agrupar-select"')
    bloque = html[i : html.index("</select>", i)]
    assert bloque.index("Sin agrupar") < bloque.index("Por motivo")
    assert "selected" in bloque[: bloque.index("Sin agrupar")]


def test_el_orden_por_monto_esta_ofrecido(client, settings, usuario, engine) -> None:
    _crear(engine, usuario, "LIC-1")
    html = client.get("/", cookies=_cookie(settings, usuario)).text

    assert "Monto mayor" in html


def test_ordenar_por_monto_pone_los_no_informados_al_final(
    client, settings, usuario, engine
) -> None:
    _crear(engine, usuario, "SIN-MONTO", monto=None)
    _crear(engine, usuario, "CHICA", monto=1_000)
    _crear(engine, usuario, "GRANDE", monto=90_000_000)

    html = client.get("/?orden=monto", cookies=_cookie(settings, usuario)).text

    assert html.index("Oportunidad GRANDE") < html.index("Oportunidad CHICA")
    assert html.index("Oportunidad CHICA") < html.index("Oportunidad SIN-MONTO")


def test_la_paginacion_aparece_sin_agrupar(client, settings, usuario, engine) -> None:
    for i in range(LIMITE_PAGINA_DEFAULT + 5):
        _crear(engine, usuario, f"LIC-{i:02d}")

    html = client.get("/", cookies=_cookie(settings, usuario)).text

    assert "quedan 5" in html
    assert f"offset={LIMITE_PAGINA_DEFAULT}" in html


def test_la_paginacion_no_aparece_agrupando(client, settings, usuario, engine) -> None:
    """Agrupando manda el cap por grupo y el offset no participa."""
    for i in range(LIMITE_PAGINA_DEFAULT + 5):
        _crear(engine, usuario, f"LIC-{i:02d}")

    html = client.get("/?agrupar_por=motivo", cookies=_cookie(settings, usuario)).text

    assert "Paginación del feed" not in html
    assert "quedan" not in html


def test_la_segunda_pagina_trae_las_siguientes(client, settings, usuario, engine) -> None:
    for i in range(LIMITE_PAGINA_DEFAULT + 5):
        _crear(engine, usuario, f"LIC-{i:02d}")

    html = client.get(
        f"/?offset={LIMITE_PAGINA_DEFAULT}", cookies=_cookie(settings, usuario)
    ).text

    assert "Ver las 20 anteriores" in html
    assert "Oportunidad LIC-00" not in html


# ---------------------------------------------------------------------------
# El panel
# ---------------------------------------------------------------------------


def test_el_panel_muestra_los_conteos_por_faceta(client, settings, usuario, engine) -> None:
    _crear(engine, usuario, "LIC-1", fuente="licitaciones")
    _crear(engine, usuario, "LIC-2", fuente="licitaciones")
    _crear(engine, usuario, "CA-1", fuente="compras_agiles")

    html = client.get("/", cookies=_cookie(settings, usuario)).text
    i = html.index('id="filtros-escritorio-sec-fuente"')
    seccion = html[i : html.index("accordion-item", i + 10)]

    assert ">2<" in seccion
    assert ">1<" in seccion


def test_la_faceta_de_la_fuente_filtrada_no_cae_a_cero(client, settings, usuario, engine) -> None:
    """Regla leave-one-out: con "Compra Ágil" ya elegido, el conteo de
    Licitaciones sigue diciendo cuántas habría si soltara el filtro."""
    _crear(engine, usuario, "LIC-1", fuente="licitaciones")
    _crear(engine, usuario, "LIC-2", fuente="licitaciones")
    _crear(engine, usuario, "CA-1", fuente="compras_agiles")

    html = client.get("/?fuente=compras_agiles", cookies=_cookie(settings, usuario)).text
    i = html.index('id="filtros-escritorio-fuente-licitaciones"')
    fin = html.index("</div>", html.index("</label>", i))

    assert ">2<" in html[i:fin]


def test_el_panel_advierte_que_la_region_solo_aplica_a_compra_agil(
    client, settings, usuario, engine
) -> None:
    """Obligatoria: sin ella el filtro miente sobre lo que hace."""
    _crear(engine, usuario, "LIC-1")
    html = client.get("/", cookies=_cookie(settings, usuario)).text

    assert "El filtro de región aplica solo a Compra Ágil" in html


def test_el_panel_no_escribe_los_cortes_de_relevancia_a_mano(
    client, settings, usuario, engine
) -> None:
    """Los cortes vienen de la ruta (`_RELEVANCIA_ALTA` y el default de
    settings), no de números escritos en la plantilla."""
    _crear(engine, usuario, "LIC-1")
    html = client.get("/", cookies=_cookie(settings, usuario)).text

    assert 'name="min_score" value="60"' in html
    assert f'name="min_score" value="{settings.feed_min_score_default}"' in html


def test_el_panel_existe_dos_veces_con_ids_distintos(client, settings, usuario, engine) -> None:
    """Barra lateral y offcanvas comparten marcado pero no ids: dos elementos
    con el mismo id rompen el `for` de las etiquetas."""
    _crear(engine, usuario, "LIC-1")
    html = client.get("/", cookies=_cookie(settings, usuario)).text

    assert html.count('id="filtros-escritorio-form"') == 1
    assert html.count('id="filtros-movil-form"') == 1
    assert 'id="panel-filtros-movil"' in html


def test_el_boton_de_filtros_movil_cuenta_los_activos(client, settings, usuario, engine) -> None:
    _crear(engine, usuario, "CA-1", fuente="compras_agiles", region=5)
    html = client.get("/?region=5&monto_min=1000000", cookies=_cookie(settings, usuario)).text

    i = html.index('id="boton-filtros"')
    assert "Filtros (2)" in html[i : html.index("</button>", i)]


def test_el_feed_vacio_con_filtros_ofrece_limpiarlos(client, settings, usuario, engine) -> None:
    _crear(engine, usuario, "LIC-1", monto=1_000)
    html = client.get("/?monto_min=99000000", cookies=_cookie(settings, usuario)).text

    assert "No hay oportunidades con estos filtros." in html
    assert "Limpiar los filtros" in html


# ---------------------------------------------------------------------------
# F-vigencia 3-bis: perfil (selección múltiple) y palabra clave
# ---------------------------------------------------------------------------


def test_sin_seccion_estado_en_el_panel(client, settings, usuario, engine) -> None:
    """La sección "Estado" del panel se saca por completo (F-vigencia): con
    solo_vigentes=True lo único que quedaría siempre es "Abierta"."""
    _crear(engine, usuario, "LIC-1")
    html = client.get("/", cookies=_cookie(settings, usuario)).text

    assert 'id="filtros-escritorio-sec-estado"' not in html


def test_filtro_de_perfil_admite_varios_seleccionados(client, settings, usuario, engine) -> None:
    """Reproduce el pedido de Boris (27-sep): antes solo se podía elegir UN
    perfil a la vez. Con selección múltiple, marcar dos perfiles muestra las
    oportunidades de ambos y deja fuera las del tercero."""
    p1 = _crear(engine, usuario, "P1")
    p2 = _crear(engine, usuario, "P2")
    _crear(engine, usuario, "P3")

    html = client.get(
        f"/?perfil_id={p1}&perfil_id={p2}", cookies=_cookie(settings, usuario)
    ).text

    assert "Oportunidad P1" in html
    assert "Oportunidad P2" in html
    assert "Oportunidad P3" not in html


def test_filtro_de_perfil_de_otro_usuario_se_ignora(client, settings, usuario, engine) -> None:
    """Regla 17: un `perfil_id` que no es del usuario no filtra nada (ni
    rompe), no se cuela desde otra cuenta."""
    with Session(engine) as s:
        otro = Usuario(
            email="otro@test.cl",
            password_hash=hash_password(_PW),
            rol=RolUsuario.USUARIO,
            activo=True,
        )
        s.add(otro)
        s.commit()
        s.refresh(otro)
        perfil_ajeno = _perfil(s, otro.id)
        s.commit()

    _crear(engine, usuario, "PROPIA")

    html = client.get(
        f"/?perfil_id={perfil_ajeno}", cookies=_cookie(settings, usuario)
    ).text

    assert "Oportunidad PROPIA" in html


def test_faceta_de_palabra_clave_filtra_y_cuenta(client, settings, usuario, engine) -> None:
    """`kw` se valida contra las keywords CONFIGURADAS del perfil (no contra
    `razones`), así que el perfil de la fixture necesita "aseo" entre las
    suyas para que el filtro no se descarte como ajena (regla 17)."""
    with Session(engine) as s:
        perfil_id = _perfil(s, usuario, keywords=["aseo"])
        s.commit()

    _crear(engine, usuario, "CON-ASEO", perfil_id=perfil_id, razones={"keywords_hit": ["aseo"]})
    _crear(engine, usuario, "SIN-ASEO", perfil_id=perfil_id, razones={})

    html = client.get("/?kw=aseo", cookies=_cookie(settings, usuario)).text

    assert "Oportunidad CON-ASEO" in html
    assert "Oportunidad SIN-ASEO" not in html


def test_una_kw_que_no_es_de_mis_perfiles_se_ignora(client, settings, usuario, engine) -> None:
    _crear(engine, usuario, "LIC-1")
    html = client.get("/?kw=palabra-inventada", cookies=_cookie(settings, usuario)).text

    assert "Oportunidad LIC-1" in html
