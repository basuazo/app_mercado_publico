"""Tests F-feed-ui-1: familias de estado, razones tipificadas y tarjeta nueva.

Todo offline: SQLite en memoria y TestClient, sin red y sin Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.presentacion import (
    banda_urgencia,
    presentacion_estado,
    razones_legibles,
    razones_tipificadas,
    texto_cierre,
)
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token
from app.core.settings import Settings
from app.models.base import Base
from app.models.enums import EstadoOportunidad, FamiliaEstado, familia_de_estado
from app.models.tables import (
    CompraAgil,
    Licitacion,
    OportunidadMatch,
    PerfilBusqueda,
    Usuario,
)

_PW = "contraseña-segura-test"
_PLANTILLAS = Path("app/api/templates")


# ---------------------------------------------------------------------------
# Bloque 2 — familias de estado
# ---------------------------------------------------------------------------


def test_familias_cubre_todos_los_estados() -> None:
    """Si ChileCompra agrega un estado, este test falla en vez de dejarlo caer
    en silencio a DESCONOCIDO."""
    from app.models.enums import _MAP_FAMILIA

    sin_mapear = [e for e in EstadoOportunidad if e not in _MAP_FAMILIA]
    assert sin_mapear == [], f"estados sin familia asignada: {sin_mapear}"


@pytest.mark.parametrize(
    ("estado", "familia"),
    [
        ("publicada", FamiliaEstado.ABIERTA),
        ("cerrada", FamiliaEstado.EN_EVALUACION),
        ("en_proceso", FamiliaEstado.EN_EVALUACION),
        ("enviada_proveedor", FamiliaEstado.EN_EVALUACION),
        ("pendiente_recepcion", FamiliaEstado.EN_EVALUACION),
        ("adjudicada", FamiliaEstado.ADJUDICADA),
        ("proveedor_seleccionado", FamiliaEstado.ADJUDICADA),
        ("aceptada", FamiliaEstado.ADJUDICADA),
        ("recepcion_conforme", FamiliaEstado.COMPLETADA),
        ("recepcion_parcial", FamiliaEstado.COMPLETADA),
        ("recepcion_conforme_incompleta", FamiliaEstado.COMPLETADA),
        ("desierta", FamiliaEstado.SIN_EFECTO),
        ("revocada", FamiliaEstado.SIN_EFECTO),
        ("cancelada", FamiliaEstado.SIN_EFECTO),
        ("suspendida", FamiliaEstado.SIN_EFECTO),
        ("desconocido", FamiliaEstado.DESCONOCIDO),
    ],
)
def test_familia_de_cada_estado(estado: str, familia: FamiliaEstado) -> None:
    assert familia_de_estado(estado) is familia


def test_familia_de_estado_no_rompe_con_basura() -> None:
    assert familia_de_estado("un_estado_que_no_existe") is FamiliaEstado.DESCONOCIDO
    assert familia_de_estado(None) is FamiliaEstado.DESCONOCIDO
    assert familia_de_estado(42) is FamiliaEstado.DESCONOCIDO


def test_familia_acepta_el_enum_directo() -> None:
    assert familia_de_estado(EstadoOportunidad.PUBLICADA) is FamiliaEstado.ABIERTA


def test_presentacion_estado_da_etiqueta_y_clase() -> None:
    p = presentacion_estado("publicada")
    assert p == {"familia": "abierta", "etiqueta": "Abierta", "clase": "familia_abierta"}


def test_presentacion_estado_desconocido_no_deja_badge_vacio() -> None:
    p = presentacion_estado("lo_que_sea")
    assert p["etiqueta"] == "Estado no informado"
    assert p["clase"] == "familia_desconocido"


def test_toda_familia_tiene_etiqueta_y_clase() -> None:
    """Igual que el mapa de estados: una familia nueva sin presentación falla acá."""
    from app.api.presentacion import _FAMILIAS

    assert set(_FAMILIAS) == set(FamiliaEstado)
    assert all(etiqueta and clase for etiqueta, clase in _FAMILIAS.values())


# ---------------------------------------------------------------------------
# Bloque 3 — razones tipificadas
# ---------------------------------------------------------------------------


def test_razones_keyword_y_rubro_son_match() -> None:
    chips = razones_tipificadas(
        {"keywords_hit": ["aseo"], "campo_hit": "nombre", "categorias_hit": ["1010"]}
    )
    assert [c["tipo"] for c in chips] == ["match", "match"]


def test_organismo_seguido_es_match() -> None:
    chips = razones_tipificadas({"organismo_seguido": True})
    assert chips == [{"texto": "De un organismo que sigues", "tipo": "match"}]


def test_poca_competencia_es_oportunidad() -> None:
    assert razones_tipificadas({"ofertas": 0})[0]["tipo"] == "oportunidad"
    assert razones_tipificadas({"ofertas": 2})[0]["tipo"] == "oportunidad"


def test_mucha_competencia_y_monto_ausente_son_advertencia() -> None:
    assert razones_tipificadas({"ofertas": 12})[0]["tipo"] == "advertencia"
    assert razones_tipificadas({"monto_no_informado": True})[0]["tipo"] == "advertencia"


def test_razones_legibles_deriva_de_las_tipificadas() -> None:
    razones = {"keywords_hit": ["aseo"], "campo_hit": "nombre", "ofertas": 0}
    assert razones_legibles(razones) == [c["texto"] for c in razones_tipificadas(razones)]


def test_razones_vacias_siguen_vacias() -> None:
    assert razones_tipificadas(None) == []
    assert razones_tipificadas({}) == []


def test_las_razones_siguen_sin_hablar_del_cierre() -> None:
    assert razones_tipificadas({"dias_al_cierre": 2.0}) == []


def test_todo_chip_tiene_un_tipo_conocido() -> None:
    chips = razones_tipificadas(
        {
            "keywords_hit": ["aseo"],
            "campo_hit": "nombre",
            "categorias_hit": ["1010"],
            "organismo_seguido": True,
            "ofertas": 0,
            "monto_no_informado": True,
        }
    )
    assert len(chips) == 5
    assert {c["tipo"] for c in chips} <= {"match", "oportunidad", "advertencia"}


# ---------------------------------------------------------------------------
# Urgencia y texto de cierre
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dias", "banda"),
    [
        (0.0, "critica"),
        (1.0, "critica"),
        (1.5, "alta"),
        (3.0, "alta"),
        (3.5, "media"),
        (7.0, "media"),
        (7.5, "baja"),
        (90.0, "baja"),
        (None, "baja"),
    ],
)
def test_banda_urgencia(dias: float | None, banda: str) -> None:
    assert banda_urgencia(dias) == banda


def test_licitacion_no_muestra_hora_de_cierre() -> None:
    """La hora de una licitación es medianoche fabricada por el parser: publicarla
    sería presentar como dato de la fuente algo que la fuente no entregó."""
    cierre = datetime(2026, 9, 24, 0, 0)
    texto = texto_cierre(cierre, 3.0, "licitaciones")
    assert "24/09/2026" in texto
    assert "00:00" not in texto
    assert ":" not in texto.split("·")[-1]


def test_compra_agil_si_muestra_hora() -> None:
    cierre = datetime(2026, 9, 24, 18, 30)
    texto = texto_cierre(cierre, 3.0, "compras_agiles")
    assert "24/09/2026 18:30" in texto


def test_textos_de_cierre_por_cercania() -> None:
    cierre = datetime(2026, 9, 24, 12, 0)
    assert texto_cierre(cierre, 0.5, "licitaciones").startswith("Cierra hoy")
    assert texto_cierre(cierre, 1.2, "licitaciones").startswith("Cierra mañana")
    assert texto_cierre(cierre, 5.0, "licitaciones").startswith("Cierra en 5 días")
    assert texto_cierre(cierre, 0.0, "licitaciones") == "Cerró el 24/09"


def test_sin_fecha_de_cierre_no_inventa_nada() -> None:
    assert texto_cierre(None, None, "licitaciones") == "Sin fecha de cierre"


# ---------------------------------------------------------------------------
# Render de la tarjeta
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
        u = Usuario(email="user@test.cl", password_hash=hash_password(_PW), activo=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _cookie(settings: Settings, user_id: int) -> dict[str, str]:
    return {COOKIE_NAME: create_session_token(settings.secret_key, user_id)}


def _perfil(session: Session, owner_id: int, fuente: str) -> int:
    p = PerfilBusqueda(
        owner_id=owner_id,
        nombre="Perfil test",
        keywords=["test"],
        keywords_excluir=[],
        regiones=[],
        fuentes=[fuente],
        activo=True,
    )
    session.add(p)
    session.flush()
    return int(p.id)


def _crear_lic(
    engine,
    owner_id: int,
    codigo: str = "LIC-1",
    *,
    estado: str = "publicada",
    score: float = 70,
    monto: float | None = 12500000,
    dias: float | None = 5,
    razones: dict | None = None,
) -> None:
    ahora = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as s:
        s.add(
            Licitacion(
                codigo=codigo,
                nombre=f"Licitación {codigo}",
                descripcion="",
                estado=estado,
                monto_clp=monto,
                # +1 h de margen: sin él el render calcula 4,9999 días e int() baja a 4.
                fecha_cierre=(
                    ahora + timedelta(days=dias, hours=1) if dias is not None else None
                ),
            )
        )
        perfil_id = _perfil(s, owner_id, "licitaciones")
        s.add(
            OportunidadMatch(
                perfil_id=perfil_id,
                fuente="licitaciones",
                codigo_oportunidad=codigo,
                score=score,
                razones=razones or {},
            )
        )
        s.commit()


def test_la_tarjeta_usa_la_banda_de_match_no_umbrales_propios(client, settings, usuario, engine):
    """65 pasa el preset "Alta relevancia" (60): el anillo va verde, no ámbar."""
    _crear_lic(engine, usuario, score=65)
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert "anillo-match anillo-match--alta" in html


def test_la_tarjeta_con_score_bajo_usa_banda_baja(client, settings, usuario, engine):
    _crear_lic(engine, usuario, score=10)
    html = client.get("/?min_score=0", cookies=_cookie(settings, usuario)).text
    assert "anillo-match anillo-match--baja" in html


@pytest.mark.parametrize(
    ("estado", "clase", "etiqueta"),
    [
        ("publicada", "familia_abierta", "Abierta"),
        ("cerrada", "familia_eval", "En evaluación"),
        ("adjudicada", "familia_adj", "Adjudicada"),
        ("recepcion_conforme", "familia_completada", "Completada"),
        ("desierta", "familia_sinefecto", "Sin efecto"),
        ("desconocido", "familia_desconocido", "Estado no informado"),
    ],
)
def test_la_tarjeta_pinta_las_seis_familias(
    client, settings, usuario, engine, estado, clase, etiqueta
):
    _crear_lic(engine, usuario, estado=estado)
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert f"badge-estado--{clase}" in html
    assert etiqueta in html
    # Nunca solo color: el badge siempre lleva icono y texto.
    assert "<svg" in html


@pytest.mark.parametrize(
    ("dias", "clase"),
    [(0.5, "critica"), (2, "alta"), (5, "media"), (30, "baja")],
)
def test_la_tarjeta_marca_las_cuatro_bandas_de_urgencia(
    client, settings, usuario, engine, dias, clase
):
    _crear_lic(engine, usuario, dias=dias)
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert f"tarjeta-op tarjeta-op--{clase}" in html


def test_monto_ausente_dice_no_informado(client, settings, usuario, engine):
    _crear_lic(engine, usuario, monto=None)
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert "No informado" in html
    assert "tarjeta-op__monto--ausente" in html


def test_monto_presente_va_en_formato_chileno(client, settings, usuario, engine):
    _crear_lic(engine, usuario, monto=12500000)
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert "$12.500.000" in html
    assert "$12,500,000" not in html


def test_licitacion_en_el_feed_no_muestra_hora_de_cierre(client, settings, usuario, engine):
    _crear_lic(engine, usuario, dias=5)
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert "Cierra en 5 días" in html
    assert "00:00" not in html


def test_compra_agil_en_el_feed_si_muestra_hora(client, settings, usuario, engine):
    ahora = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as s:
        s.add(
            CompraAgil(
                codigo="CA-1",
                nombre="Compra ágil test",
                descripcion="",
                estado="publicada",
                monto_disponible_clp=900000,
                fecha_cierre=ahora + timedelta(days=4, hours=1),
            )
        )
        perfil_id = _perfil(s, usuario, "compras_agiles")
        s.add(
            OportunidadMatch(
                perfil_id=perfil_id,
                fuente="compras_agiles",
                codigo_oportunidad="CA-1",
                score=70,
                razones={},
            )
        )
        s.commit()

    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert "Cierra en 4 días" in html
    assert "disponible" in html
    # La hora sí va en Compra Ágil: la fuente la entrega.
    hora = (ahora + timedelta(days=4, hours=1)).strftime("%H:%M")
    assert hora in html


def test_los_chips_se_cortan_en_tres_mas_el_boton_de_resto(client, settings, usuario, engine):
    _crear_lic(
        engine,
        usuario,
        razones={
            "keywords_hit": ["aseo"],
            "campo_hit": "nombre",
            "categorias_hit": ["1010"],
            "organismo_seguido": True,
            "ofertas": 0,
            "monto_no_informado": True,
        },
    )
    # Agrupado por fuente: con "motivo" la misma tarjeta aparece en varios
    # grupos y el conteo se multiplicaría por el número de apariciones.
    html = client.get("/?agrupar_por=fuente", cookies=_cookie(settings, usuario)).text
    assert html.count("chip-razon chip-razon--") == 4  # 3 visibles + el "+N"
    assert "chip-razon--mas" in html
    assert "+2" in html
    assert 'data-bs-toggle="popover"' in html


def test_los_toggles_de_la_tarjeta_exponen_su_estado(client, settings, usuario, engine):
    _crear_lic(engine, usuario)
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert 'data-accion="seguir"' in html
    assert 'data-accion="me-sirve"' in html
    assert 'data-accion="descartar"' in html
    assert 'aria-pressed="false"' in html
    assert "data-anuncio=" in html
    assert "data-nombre-oportunidad=" in html


def test_el_anuncio_nombra_la_oportunidad(client, settings, usuario, engine):
    _crear_lic(engine, usuario, codigo="LIC-NOMBRE")
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert "Sin marcas: Licitación LIC-NOMBRE" in html


def test_la_tarjeta_recargada_por_htmx_trae_los_mismos_datos(client, settings, usuario, engine):
    """Tras un swap la tarjeta no puede quedar sin banda, badge ni urgencia."""
    from app.auth.csrf import generate_csrf_token
    from app.auth.session import decode_session_token

    _crear_lic(engine, usuario, score=65, dias=2)
    token = create_session_token(settings.secret_key, usuario)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    _, nonce = decoded
    cookies = {COOKIE_NAME: token}
    headers = {
        "X-CSRF-Token": generate_csrf_token(settings.secret_key, nonce),
        "HX-Request": "true",
    }

    r = client.post(
        "/oportunidad/licitaciones/LIC-1/seguir", data={}, cookies=cookies, headers=headers
    )
    assert r.status_code == 200
    assert "anillo-match--alta" in r.text
    assert "badge-estado--familia_abierta" in r.text
    assert "tarjeta-op--alta" in r.text
    assert 'aria-pressed="true"' in r.text


# ---------------------------------------------------------------------------
# Criterios de la fase sobre las plantillas
# ---------------------------------------------------------------------------


def _plantillas() -> list[Path]:
    return sorted(_PLANTILLAS.glob("*.html"))


def test_no_quedan_umbrales_de_score_sueltos_en_plantillas() -> None:
    """Los cortes viven en un solo lugar: la ruta, vía banda_relevancia."""
    culpables = [
        p.name
        for p in _plantillas()
        if ">= 80" in p.read_text(encoding="utf-8") or ">= 50" in p.read_text(encoding="utf-8")
    ]
    assert culpables == []


def test_no_queda_formato_de_monto_a_mano_en_plantillas() -> None:
    culpables = [p.name for p in _plantillas() if "'{:,.0f}'" in p.read_text(encoding="utf-8")]
    assert culpables == []


def test_no_hay_fuentes_ni_iconos_nuevos_por_cdn() -> None:
    base = (_PLANTILLAS / "base.html").read_text(encoding="utf-8")
    assert "fonts.googleapis" not in base
    assert "font-awesome" not in base.lower()
    assert "bootstrap-icons" not in base.lower()


def test_los_colores_salen_de_variables_no_de_hexadecimales() -> None:
    import re

    for p in _plantillas():
        texto = p.read_text(encoding="utf-8")
        # Se excluye el atributo de color de los SVG, que usa currentColor.
        assert not re.search(r"#[0-9A-Fa-f]{6}\b", texto), f"hexadecimal suelto en {p.name}"
