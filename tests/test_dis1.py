"""F-dis-1: sistema visual Rastrillo (tokens, átomos, macros), menú nuevo y preferencias.

SQLite en memoria: render de `base.html` en los tres temas, menú con `aria-current`, «Hoy» en vez de
«Inicio», preferencias (apariencia y vistas recordadas), `/admin/diseno` solo admin, contadores del
menú y humo de todas las plantillas y rutas HTML. Lo de Postgres (JSONB, UNIQUE) en test_dis1_pg.py.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.presentacion import registrar_filtros
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.mercado import preferencias as pf
from app.mercado.lote import contadores_menu
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import CaTrabajo, CompraAgil, Empresa, Lote, LoteCa, Usuario, UsuarioVista

_RAIZ = Path(__file__).resolve().parent.parent
_TEMPLATES = _RAIZ / "app" / "api" / "templates"
_ESTATICOS = _RAIZ / "app" / "api" / "static"


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


def _usuario(engine, email, rol=RolUsuario.USUARIO, con_empresa=True, **extra) -> int:
    with Session(engine) as s:
        empresa_id = None
        if con_empresa:
            e = Empresa(nombre="Emp " + email, regiones_prioritarias=[16], comunas_prioritarias=[])
            s.add(e)
            s.flush()
            empresa_id = e.id
        u = Usuario(
            email=email,
            password_hash=hash_password("contraseña-segura-test"),
            rol=rol,
            activo=True,
            empresa_id=empresa_id,
            **extra,
        )
        s.add(u)
        s.commit()
        return u.id


def _sesion(settings, uid):
    token = create_session_token(settings.secret_key, uid)
    nonce = decode_session_token(settings.secret_key, token)[1]
    return {COOKIE_NAME: token}, {"X-CSRF-Token": generate_csrf_token(settings.secret_key, nonce)}


def _menu(html: str) -> str:
    """Solo el menú lateral de escritorio."""
    return html[html.index('<aside class="app-lateral m-lateral"') : html.index("</aside>")]


def _activos(html: str) -> list[str]:
    return re.findall(r'<a class="m-item[^"]*" href="([^"]+)" aria-current="page"', _menu(html))


# ---------------------------------------------------------------------------
# base.html: temas, densidad, menú
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tema", ["claro", "oscuro", "sistema"])
def test_base_en_los_tres_temas(client, engine, settings, tema):
    uid = _usuario(engine, f"{tema}@t.cl", tema=tema, densidad="compacta")
    ck, _ = _sesion(settings, uid)
    r = client.get("/?hoy=1", cookies=ck)
    assert r.status_code == 200
    assert f'<html lang="es" data-tema="{tema}" data-densidad="compacta"' in r.text
    # Bootstrap (pantallas no migradas) sigue al tema: oscuro desde el servidor, sistema por script.
    assert ('data-bs-theme="dark"' in r.text.split("<head>")[0]) is (tema == "oscuro")
    assert ("prefers-color-scheme: dark" in r.text) is (tema == "sistema")
    for css in ("rastrillo/tokens.css", "rastrillo/atomos.css", "rastrillo/estacion.css"):
        assert f"/static/{css}?v=" in r.text
    # Los estilos nuevos van después de Bootstrap y app.css.
    assert (
        r.text.index("bootstrap.min.css")
        < r.text.index("/static/app.css")
        < r.text.index("rastrillo/tokens.css")
    )
    assert "/static/rastrillo/rastrillo.js" in r.text
    assert 'href="/static/fonts/geist-latin-400.woff2" as="font"' in r.text


def test_login_sin_sesion_usa_el_tema_por_defecto(client):
    r = client.get("/login")
    assert r.status_code == 200


def test_hoy_reemplaza_a_inicio_en_menu_y_titulo(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    ck, _ = _sesion(settings, uid)
    r = client.get("/?hoy=1", cookies=ck)
    assert "<title>Hoy — Rastrillo</title>" in r.text
    menu = _menu(r.text)
    assert "<span>Hoy</span>" in menu and "Inicio" not in menu
    assert _activos(r.text) == ["/?hoy=1"]


def test_menu_completo_con_empresa_y_grupos(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    ck, _ = _sesion(settings, uid)
    menu = _menu(client.get("/mesa", cookies=ck).text)
    textos = re.findall(
        r'<a class="m-item[^"]*" href="[^"]+"[^>]*>.*?<span>([^<]+)</span>', menu, re.S
    )
    assert textos == [
        "Hoy", "Mesa", "En trabajo", "Ventas", "Actividad", "Competencia", "Estrategia",
        "Abastecimiento", "Demanda MP", "Proveedores", "Inventario", "Logística", "Documentos",
        "Configuración",
    ]  # fmt: skip
    assert 'id="nav-catalogo">Catálogo<' in menu and 'id="nav-admin">Administración<' in menu
    # Usuario común: sin «Archivo» ni enlaces de admin.
    assert 'href="/archivo"' not in menu and 'href="/admin/empresas"' not in menu
    # Campana y botón de paleta con su atajo visible.
    assert 'aria-controls="campana-panel"' in menu and 'aria-expanded="false"' in menu
    assert "data-abre-paleta" in menu and "⌘K" in menu
    # Pie: avatar, nombre y rol, enlace a Configuración.
    assert 'class="m-pie__perfil" href="/cuenta"' in menu and "Usuario · preferencias" in menu


def test_menu_admin_ve_archivo_y_sin_empresa_solo_hoy_y_configuracion(client, engine, settings):
    uid = _usuario(engine, "admin@t.cl", rol=RolUsuario.ADMIN, con_empresa=False)
    ck, _ = _sesion(settings, uid)
    menu = _menu(client.get("/cuenta", cookies=ck).text)
    assert 'href="/archivo"' in menu and "Administrador · preferencias" in menu
    assert 'href="/?hoy=1"' in menu and 'href="/mesa"' not in menu
    assert "campana-panel" not in menu  # sin empresa no hay actividad del equipo


def test_menu_usuario_sin_empresa_no_ve_pantallas_comerciales(client, engine, settings):
    uid = _usuario(engine, "sin@t.cl", con_empresa=False)
    ck, _ = _sesion(settings, uid)
    menu = _menu(client.get("/cuenta", cookies=ck).text)
    assert 'href="/mesa"' not in menu and 'href="/?hoy=1"' not in menu
    assert 'href="/cuenta" aria-current="page"' in menu


@pytest.mark.parametrize(
    ("ruta", "activo"),
    [
        ("/?hoy=1", "/?hoy=1"),
        ("/mesa", "/mesa"),
        ("/trabajo", "/trabajo"),
        ("/ventas", "/ventas"),
        ("/caja", "/ventas"),
        ("/actividad", "/actividad"),
        ("/competencia", "/competencia"),
        ("/estrategia", "/estrategia"),
        ("/abastecimiento", "/abastecimiento"),
        ("/catalogo", "/catalogo"),
        ("/proveedores", "/proveedores"),
        ("/inventario", "/inventario"),
        ("/proveedores/logistica", "/proveedores/logistica"),
        ("/cuenta", "/cuenta"),
        ("/admin/empresas", "/cuenta"),
        ("/admin/usuarios", "/cuenta"),
        ("/salud", "/cuenta"),
        ("/admin/diseno", "/cuenta"),
        ("/archivo", "/archivo"),
    ],
)
def test_menu_marca_aria_current_en_cada_ruta(client, engine, settings, ruta, activo):
    uid = _usuario(engine, "admin@t.cl", rol=RolUsuario.ADMIN)
    ck, _ = _sesion(settings, uid)
    r = client.get(ruta, cookies=ck)
    assert r.status_code == 200, ruta
    assert _activos(r.text) == [activo]


def test_menu_movil_es_dialog_con_el_mismo_menu(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    ck, _ = _sesion(settings, uid)
    html = client.get("/mesa", cookies=ck).text
    assert (
        '<dialog class="m-dialogo-menu" id="menu-movil" aria-label="Menú principal" data-cerrable>'
        in html
    )
    assert 'aria-controls="menu-movil"' in html and "data-abre-menu" in html
    assert "offcanvas-start" not in html and 'data-bs-toggle="dropdown"' not in html
    # Mismo menú, ids distintos (sin duplicados).
    assert 'id="nav-catalogo-m"' in html and 'id="campana-n-m"' in html
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids))


def test_paleta_lista_las_pantallas_segun_permisos(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    ck, _ = _sesion(settings, uid)
    html = client.get("/mesa", cookies=ck).text
    paleta = html[
        html.index('<dialog class="m-paleta"') : html.index(
            "</dialog>", html.index('<dialog class="m-paleta"')
        )
    ]
    assert 'role="combobox"' in paleta and 'aria-controls="paleta-lista"' in paleta
    for href in ("/?hoy=1", "/mesa", "/trabajo", "/ventas", "/catalogo", "/competencia"):
        assert f'href="{href}"' in paleta
    # Las cinco del atajo G se ven sin escribir; las demás, solo al buscar.
    assert paleta.count("data-solo-busqueda hidden") == len(
        [x for x in __import__("app.api.nav", fromlist=["x"]).PALETA_IR_A if not x.get("inicial")]
    )
    sin = _usuario(engine, "sin@t.cl", con_empresa=False)
    ck, _ = _sesion(settings, sin)
    html = client.get("/cuenta", cookies=ck).text
    paleta = html[html.index('<dialog class="m-paleta"') :]
    assert 'href="/mesa"' not in paleta.split("</dialog>")[0]


def test_contadores_del_menu(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    ahora = ahora_utc()
    with Session(engine) as s:
        emp = s.get(Usuario, uid).empresa_id
        for codigo, horas in [("CA-1", 5), ("CA-2", 48), ("CA-3", 10), ("CA-4", -2)]:
            s.add(CompraAgil(codigo=codigo, nombre=codigo, estado="publicada", region=16,
                             fecha_cierre=ahora + timedelta(hours=horas)))  # fmt: skip
            s.add(CaTrabajo(empresa_id=emp, ca_codigo=codigo, estado="cotizando"))
        lote = Lote(empresa_id=emp, nombre="L1")
        s.add(lote)
        s.flush()
        s.add(LoteCa(lote_id=lote.id, ca_codigo="CA-3"))
        s.commit()
        # Hoy: cierran en < 24 h sin enviar (CA-1, CA-3). En trabajo: vigentes sin lote (CA-1, CA-2).
        assert contadores_menu(s, emp, ahora) == {"hoy": 2, "trabajo": 2}
        # Otra empresa no cuenta (regla 26).
        otra = Empresa(nombre="Otra", regiones_prioritarias=[16], comunas_prioritarias=[])
        s.add(otra)
        s.commit()
        assert contadores_menu(s, otra.id, ahora) == {"hoy": 0, "trabajo": 0}
    ck, _ = _sesion(settings, uid)
    menu = _menu(client.get("/mesa", cookies=ck).text)
    assert re.search(
        r'href="/\?hoy=1">.*?<span class="r-contador"[^>]*>2</span><span class="r-oculto">cierran',
        menu,
        re.S,
    )
    assert re.search(
        r'href="/trabajo">.*?r-contador--atencion"[^>]*>2</span><span class="r-oculto">CA en curso sin lote',
        menu,
        re.S,
    )


def test_parcial_htmx_no_calcula_contadores(client, engine, settings, monkeypatch):
    """Los contadores se piden desde base.html: un parcial que no lo extiende no los paga."""
    import app.mercado.lote as lt

    llamadas: list[int] = []
    original = lt.contadores_menu
    monkeypatch.setattr(
        lt, "contadores_menu", lambda *a, **k: llamadas.append(1) or original(*a, **k)
    )
    uid = _usuario(engine, "u@t.cl")
    ck, _ = _sesion(settings, uid)
    client.get("/actividad/campana", cookies=ck)
    assert llamadas == []
    client.get("/mesa", cookies=ck)
    assert llamadas == [1]


# ---------------------------------------------------------------------------
# Campana sin dropdown de Bootstrap
# ---------------------------------------------------------------------------


def test_campana_boton_con_aria_expanded_y_panel_propio(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    ck, _ = _sesion(settings, uid)
    html = client.get("/mesa", cookies=ck).text
    assert (
        'hx-get="/actividad/campana" hx-target="#campana-lista" hx-trigger="abrir-campana"' in html
    )
    assert (
        'id="campana-panel" role="region" aria-label="Actividad del equipo" hidden data-cerrable'
        in html
    )
    # Sondeo único (60 s), igual que antes.
    assert html.count('hx-get="/actividad/contador"') == 1
    lista = client.get("/actividad/campana", cookies=ck).text
    assert 'hx-target="closest [data-campana-lista]"' not in lista  # sin eventos no hay «marcar»
    assert "Nada nuevo. Estás al día." in lista and "Fuente: Dirección ChileCompra" in lista
    cont = client.get("/actividad/contador", cookies=ck).text
    assert (
        'id="campana-n" hx-swap-oob="true" class="r-contador m-campana__n" data-no-leidos="0" hidden'
        in cont
    )


# ---------------------------------------------------------------------------
# Preferencias de apariencia
# ---------------------------------------------------------------------------


def test_apariencia_se_guarda_con_htmx_y_refresca(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    ck, h = _sesion(settings, uid)
    r = client.post("/cuenta/apariencia", cookies=ck, headers={**h, "HX-Request": "true"},
                    data={"tema": "oscuro", "densidad": "compacta", "inicio": "mesa"})  # fmt: skip
    assert r.status_code == 200 and r.headers["HX-Refresh"] == "true"
    with Session(engine) as s:
        u = s.get(Usuario, uid)
        assert (u.tema, u.densidad, u.inicio) == ("oscuro", "compacta", "mesa")
    r = client.post("/cuenta/apariencia", cookies=ck, headers=h, follow_redirects=False,
                    data={"tema": "sistema", "densidad": "comoda", "inicio": "hoy"})  # fmt: skip
    assert r.status_code == 303 and "mensaje=" in r.headers["location"]


@pytest.mark.parametrize(
    "datos",
    [
        {"tema": "rosado", "densidad": "comoda", "inicio": "hoy"},
        {"tema": "claro", "densidad": "apretada", "inicio": "hoy"},
        {"tema": "claro", "densidad": "comoda", "inicio": "/admin"},
        {"tema": "", "densidad": "", "inicio": ""},
    ],
)
def test_apariencia_rechaza_valores_invalidos(client, engine, settings, datos):
    uid = _usuario(engine, "u@t.cl")
    ck, h = _sesion(settings, uid)
    r = client.post(
        "/cuenta/apariencia", cookies=ck, headers={**h, "HX-Request": "true"}, data=datos
    )
    assert r.status_code == 422
    r = client.post("/cuenta/apariencia", cookies=ck, headers=h, data=datos, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    with Session(engine) as s:
        u = s.get(Usuario, uid)
        assert (u.tema, u.densidad, u.inicio) == ("claro", "comoda", "hoy")


def test_apariencia_exige_csrf(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    ck, _ = _sesion(settings, uid)
    r = client.post("/cuenta/apariencia", cookies=ck,
                    data={"tema": "oscuro", "densidad": "comoda", "inicio": "hoy"})  # fmt: skip
    assert r.status_code == 403


def test_cuenta_muestra_el_bloque_apariencia_con_lo_guardado(client, engine, settings):
    uid = _usuario(engine, "u@t.cl", tema="sistema", densidad="compacta", inicio="ventas")
    ck, _ = _sesion(settings, uid)
    html = client.get("/cuenta", cookies=ck).text
    bloque = html[
        html.index("data-apariencia") : html.index("</section>", html.index("data-apariencia"))
    ]
    assert 'hx-post="/cuenta/apariencia"' in bloque and 'name="csrf_token"' in bloque
    assert re.search(r'name="tema" id="[^"]+" value="sistema" checked', bloque)
    assert re.search(r'name="densidad" id="[^"]+" value="compacta" checked', bloque)
    assert re.search(r'name="inicio" id="[^"]+" value="ventas" checked', bloque)
    assert bloque.count(" checked") == 3


def test_inicio_redirige_salvo_con_hoy_1(client, engine, settings):
    uid = _usuario(engine, "u@t.cl", inicio="trabajo")
    ck, _ = _sesion(settings, uid)
    r = client.get("/", cookies=ck, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/trabajo"
    assert client.get("/?hoy=1", cookies=ck, follow_redirects=False).status_code == 200
    # «Hoy» como inicio: la portada no redirige.
    uid2 = _usuario(engine, "h@t.cl")
    ck2, _ = _sesion(settings, uid2)
    assert client.get("/", cookies=ck2, follow_redirects=False).status_code == 200


def test_inicio_sin_empresa_no_redirige(client, engine, settings):
    uid = _usuario(engine, "admin@t.cl", rol=RolUsuario.ADMIN, con_empresa=False, inicio="mesa")
    ck, _ = _sesion(settings, uid)
    assert client.get("/", cookies=ck, follow_redirects=False).status_code == 200


def test_apariencia_con_valor_viejo_cae_al_defecto():
    u = Usuario(email="x@t.cl", password_hash="x", tema="morado", densidad=None, inicio="nada")
    assert pf.apariencia(u) == {"tema": "claro", "densidad": "comoda", "inicio": "hoy"}
    assert pf.apariencia(None)["tema"] == "claro"


def test_guardar_apariencia_valida_todo_o_nada():
    u = Usuario(email="x@t.cl", password_hash="x", tema="claro", densidad="comoda", inicio="hoy")
    with pytest.raises(pf.PreferenciaInvalida):
        pf.guardar_apariencia(u, "oscuro", "compacta", "nada")
    assert (u.tema, u.densidad, u.inicio) == ("claro", "comoda", "hoy")
    pf.guardar_apariencia(u, "oscuro", "compacta", "ventas")
    assert pf.url_inicio(u) == "/ventas"


# ---------------------------------------------------------------------------
# Vistas recordadas por pantalla
# ---------------------------------------------------------------------------


def test_guardar_vista_lista_blanca_y_reemplazo(engine):
    a = _usuario(engine, "a@t.cl")
    with Session(engine) as s:
        guardado = pf.guardar_vista(s, a, "hoy", {"periodo": "semana", "filtro": "cierres",
                                                  "empresa_id": 99, "sql": "DROP"})  # fmt: skip
        s.commit()
        assert guardado == {"periodo": "semana", "filtro": "cierres"}
        assert pf.vista_de(s, a, "hoy") == {"periodo": "semana", "filtro": "cierres"}
        # Valores no simples se descartan; reemplaza la fila (una por usuario y pantalla).
        pf.guardar_vista(s, a, "hoy", {"periodo": "mes", "filtro": {"anidado": 1}})
        s.commit()
        assert pf.vista_de(s, a, "hoy") == {"periodo": "mes"}
        assert s.query(UsuarioVista).count() == 1
        # Vacía: borra la fila.
        assert pf.guardar_vista(s, a, "hoy", {"otra": 1}) == {}
        s.commit()
        assert pf.vista_de(s, a, "hoy") == {} and s.query(UsuarioVista).count() == 0


def test_guardar_vista_pantalla_desconocida_y_tamano_maximo(engine):
    a = _usuario(engine, "a@t.cl")
    with Session(engine) as s:
        with pytest.raises(pf.PreferenciaInvalida):
            pf.guardar_vista(s, a, "no-existe", {"x": 1})
        with pytest.raises(pf.PreferenciaInvalida, match="máximo"):
            pf.guardar_vista(s, a, "hoy", {"filtro": "x" * (pf.MAX_BYTES_VISTA + 1)})
        assert s.query(UsuarioVista).count() == 0
        # Justo bajo el límite sí entra.
        texto = "x" * (pf.MAX_BYTES_VISTA - len('{"filtro": ""}'))
        assert pf.guardar_vista(s, a, "hoy", {"filtro": texto}) == {"filtro": texto}


def test_otro_usuario_no_lee_la_vista_del_primero(engine):
    a = _usuario(engine, "a@t.cl")
    b = _usuario(engine, "b@t.cl")
    with Session(engine) as s:
        pf.guardar_vista(s, a, "hoy", {"periodo": "mes"})
        s.commit()
        assert pf.vista_de(s, b, "hoy") == {}
        pf.guardar_vista(s, b, "hoy", {"periodo": "semana"})
        s.commit()
        assert pf.vista_de(s, a, "hoy") == {"periodo": "mes"}
        assert pf.vista_de(s, b, "hoy") == {"periodo": "semana"}


def test_vista_guardada_con_clave_que_salio_de_la_lista_no_se_entrega(engine):
    a = _usuario(engine, "a@t.cl")
    with Session(engine) as s:
        s.add(UsuarioVista(usuario_id=a, pantalla="hoy", datos={"periodo": "mes", "vieja": 1}))
        s.commit()
        assert pf.vista_de(s, a, "hoy") == {"periodo": "mes"}


# ---------------------------------------------------------------------------
# /admin/diseno
# ---------------------------------------------------------------------------


def test_admin_diseno_solo_admin(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    ck, _ = _sesion(settings, uid)
    assert client.get("/admin/diseno", cookies=ck).status_code == 403
    assert client.get("/admin/diseno", follow_redirects=False).status_code in (302, 303)
    adm = _usuario(engine, "admin@t.cl", rol=RolUsuario.ADMIN)
    ck, _ = _sesion(settings, adm)
    r = client.get("/admin/diseno", cookies=ck)
    assert r.status_code == 200
    html = r.text
    # Los dos lienzos y cada átomo/patrón.
    assert (
        'data-tema="claro" aria-label="Lienzo claro"' in html
        and 'data-tema="oscuro" aria-label="Lienzo oscuro"' in html
    )
    for clase in ("r-btn--primario", "r-btn--secundario", "r-btn--terciario", "r-btn--peligro", "r-btn--icono",
                  "r-btn--sm", "r-estado--curso", "r-estado--ok", "r-estado--sin-punto", "r-senal--urgente",
                  "r-senal--atencion", "r-senal--nuevo", "r-contador--atencion", "r-tecla", "r-seg", "r-filtro",
                  "r-brillo", "r-dato--xl", "r-dato--fila", "r-delta--sube", "r-delta--baja", "r-etiqueta",
                  "r-codigo", "r-icono", "m-vacio", "m-aviso", "m-panel", "m-modal", "m-cabecera-lista"):  # fmt: skip
        assert clase in html, clase
    # No va en el menú.
    assert 'href="/admin/diseno"' not in _menu(html)


def test_explicativo_de_senal_es_accesible(client, engine, settings):
    adm = _usuario(engine, "admin@t.cl", rol=RolUsuario.ADMIN)
    ck, _ = _sesion(settings, adm)
    html = client.get("/admin/diseno", cookies=ck).text
    for m in re.finditer(
        r'<span class="m-ayuda[^"]*" tabindex="0" aria-describedby="([^"]+)">', html
    ):
        assert f'role="tooltip" id="{m.group(1)}"' in html
    assert len(re.findall(r'aria-describedby="ayuda-\d+"', html)) == 8  # 4 señales × 2 lienzos


# ---------------------------------------------------------------------------
# Macros y estáticos
# ---------------------------------------------------------------------------


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    registrar_filtros(env)
    return env


def test_macros_texto_en_un_solo_span_y_escapado():
    r = _env().from_string('{% import "_r.html" as r %}{{ r.boton(t, "primario", icono="flecha", tecla="⌘K") }}|'
                           '{{ r.estado(t, "ok") }}|{{ r.boton("Buscar", "icono", icono="buscar") }}|'
                           '{{ r.contador(150) }}|{{ r.boton("Ver", href="/x", tam="sm") }}')  # fmt: skip
    html = r.render(t="<b>Ganada & lista</b>")
    assert "<span>&lt;b&gt;Ganada &amp; lista&lt;/b&gt;</span>" in html
    assert 'class="r-btn r-btn--primario" type="button">' in html
    assert '<span class="r-tecla" data-tecla-mod aria-hidden="true">⌘K</span>' in html
    assert 'aria-label="Buscar" title="Buscar"' in html and "<span>Buscar</span>" not in html
    assert ">99+<" in html
    assert '<a class="r-btn r-btn--secundario r-btn--sm" href="/x">' in html


def test_macro_boton_con_attrs():
    html = (
        _env()
        .from_string(
            '{% import "_r.html" as r %}{{ r.boton("Enviar", "primario", attrs={"type": "submit", "disabled": true, '
            '"hx-post": "/x", "data-off": false, "class": "extra"}) }}'
        )
        .render()
    )
    assert (
        html
        == '<button class="r-btn r-btn--primario extra" type="submit" disabled hx-post="/x"><span>Enviar</span></button>'
    )


def test_iconos_lucide_compatibles():
    env = _env()
    html = env.from_string(
        '{% from "_iconos.html" import icono %}{{ icono("mesa") }}{{ icono("buscar", 18) }}{{ icono("no-existe") }}'
    ).render()
    assert html.count('stroke-width="1.75"') == 3
    assert 'width="16" height="16"' in html and 'width="18" height="18"' in html
    assert "ISC" in (_TEMPLATES / "_iconos.html").read_text(encoding="utf-8")


def test_tokens_sin_valores_sueltos_en_atomos():
    """atomos.css y estacion.css toman los colores de los tokens (sin hex ni rgb sueltos)."""
    for nombre in ("atomos.css", "estacion.css"):
        css = (_ESTATICOS / "rastrillo" / nombre).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", css), nombre
        assert "rgba(" not in css, nombre
    tokens = (_ESTATICOS / "rastrillo" / "tokens.css").read_text(encoding="utf-8")
    assert '[data-tema="oscuro"]' in tokens and '[data-tema="sistema"]' in tokens
    assert "prefers-color-scheme: dark" in tokens


def test_fuentes_geist_autoalojadas_con_licencia():
    carpeta = _ESTATICOS / "fonts"
    for nombre in (
        "geist-latin-400",
        "geist-latin-500",
        "geist-latin-600",
        "geist-mono-latin-400",
        "geist-mono-latin-500",
    ):
        archivo = carpeta / f"{nombre}.woff2"
        assert archivo.read_bytes()[:4] == b"wOF2", nombre
    assert "SIL Open Font License" in (carpeta / "geist-LICENSE.txt").read_text(encoding="utf-8")
    tokens = (_ESTATICOS / "rastrillo" / "tokens.css").read_text(encoding="utf-8")
    assert tokens.count("font-display: swap") == 5


def test_brillo_respeta_movimiento_reducido():
    css = (_ESTATICOS / "rastrillo" / "atomos.css").read_text(encoding="utf-8")
    bloque = css[css.index("@media (prefers-reduced-motion: reduce)") :]
    assert ".r-brillo" in bloque and "animation: none" in bloque


def test_rastrillo_js_atajos_no_se_disparan_en_campos():
    js = (_ESTATICOS / "rastrillo" / "rastrillo.js").read_text(encoding="utf-8")
    assert (
        'tag === "input" || tag === "textarea" || tag === "select"' in js
        and "isContentEditable" in js
    )
    for clave in ('h: "/?hoy=1"', 'm: "/mesa"', 't: "/trabajo"', 'v: "/ventas"', 'd: "/catalogo"',
                  "[data-buscar]", "[data-cerrable]", "[data-fila]", "[data-abrir]", '"aviso"'):  # fmt: skip
        assert clave in js, clave


# ---------------------------------------------------------------------------
# Humo: todas las plantillas compilan y todas las rutas HTML renderizan con el base nuevo
# ---------------------------------------------------------------------------


def test_todas_las_plantillas_compilan():
    env = _env()
    nombres = sorted(p.name for p in _TEMPLATES.glob("*.html"))
    assert len(nombres) >= 95
    for nombre in nombres:
        env.get_template(nombre)


@pytest.mark.parametrize("tema", ["claro", "oscuro"])
def test_humo_de_todas_las_rutas_html(client, engine, settings, tema):
    """Cada GET sin parámetros de ruta renderiza (admin con empresa, sin datos)."""
    adm = _usuario(engine, "admin@t.cl", rol=RolUsuario.ADMIN, tema=tema)
    ck, _ = _sesion(settings, adm)
    # FastAPI 0.138 ya no aplana include_router: app.routes trae _IncludedRouter con
    # .original_router. Se recorre recursivamente; en main.py ningún include_router usa
    # prefijo, así que el path de cada ruta sirve tal cual.
    pendientes = list(client.app.routes)
    planas = []
    while pendientes:
        r = pendientes.pop()
        if hasattr(r, "original_router"):
            pendientes.extend(r.original_router.routes)
        else:
            planas.append(r)
    rutas = sorted(
        {
            r.path
            for r in planas
            if "GET" in (getattr(r, "methods", None) or set())
            and "{" not in r.path
            and not r.path.startswith(("/api/", "/static", "/docs", "/redoc", "/openapi"))
        }
    )
    assert len(rutas) > 30
    renderizadas = 0
    for ruta in rutas:
        r = client.get(ruta, cookies=ck, follow_redirects=False)
        assert r.status_code < 500, (ruta, r.status_code)
        if r.status_code == 200 and 'class="app-lateral m-lateral"' in r.text:  # extiende base.html
            assert f'data-tema="{tema}"' in r.text, ruta
            renderizadas += 1
    assert renderizadas > 25
