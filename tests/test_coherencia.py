"""Tests F-coherencia — la ficha, los correos y el detector del smoke test
dicen lo mismo que la tarjeta del feed sobre la misma oportunidad.

Todo offline: SQLite en memoria y TestClient, sin red y sin Postgres. El
detector se carga por ruta (`scripts/` no es un paquete importable).
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.presentacion import fecha_cierre_legible, texto_cierre
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
# Bloque 2 — el detector de formatos del smoke test
# ---------------------------------------------------------------------------


def _cargar_smoke():
    """Carga `scripts/smoke_test.py` por ruta. No pega a la red ni lee el .env:
    `load_dotenv()` vive dentro de `main()`, no en el import."""
    ruta = Path(__file__).parent.parent / "scripts" / "smoke_test.py"
    spec = importlib.util.spec_from_file_location("smoke_test_mod", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.mark.parametrize(
    ("valor", "hora_esperada"),
    [
        # Los tres formatos observados en respuestas reales de la fuente.
        ("2026-09-22T18:00:00", "hora REAL: 18:00:00"),
        ("2026-09-22T18:00:00.000Z", "hora REAL: 18:00:00"),
        # El que el detector viejo reportaba como "SIN componente de hora".
        ("2026-09-22 18:00", "hora REAL: 18:00"),
    ],
)
def test_el_detector_ve_la_hora_en_los_tres_formatos_reales(valor: str, hora_esperada: str) -> None:
    salida = _cargar_smoke()._describir_fecha(valor)

    assert hora_esperada in salida
    assert "SIN componente de hora" not in salida


def test_el_detector_distingue_hora_segundos_y_offset() -> None:
    """Tres preguntas distintas, tres respuestas separadas."""
    smoke = _cargar_smoke()

    con_todo = smoke._describir_fecha("2026-09-22T18:00:00.000Z")
    assert "con segundos y fracción: 00.000" in con_todo
    assert "offset explícito: Z (UTC) — OJO: en la v2 es falsa, es hora de Chile" in con_todo

    sin_segundos = smoke._describir_fecha("2026-09-22 18:00")
    assert "SIN segundos (solo hh:mm)" in sin_segundos
    assert "SIN offset" in sin_segundos

    con_offset = smoke._describir_fecha("2026-09-22T18:00:00-03:00")
    assert "offset explícito: -03:00" in con_offset


def test_el_detector_sigue_reconociendo_ddmmaaaa_y_la_medianoche() -> None:
    smoke = _cargar_smoke()

    assert "formato ddmmaaaa" in smoke._describir_fecha("22092026")
    # La medianoche exacta no se afirma como hora real: es indistinguible de
    # "solo fecha" y esa distinción es justo la que el Paso 0 va a decidir.
    assert "indistinguible" in smoke._describir_fecha("2026-09-22T00:00:00")
    assert "indistinguible" in smoke._describir_fecha("2026-09-22 00:00")


def test_el_detector_no_inventa_con_basura() -> None:
    """Regla 6: lo desconocido se marca, no se interpreta."""
    salida = _cargar_smoke()._describir_fecha("no es una fecha")

    assert "NO reconocido" in salida


def test_el_smoke_test_normaliza_la_url_como_el_resto_del_proyecto() -> None:
    """Sin esto, una DATABASE_URL sin driver —como la entrega Neon— reventaba
    con ModuleNotFoundError: psycopg2."""
    fuente = (Path(__file__).parent.parent / "scripts" / "smoke_test.py").read_text(
        encoding="utf-8"
    )

    assert "create_engine(normalizar_url_driver(settings.database_url))" in fuente
    assert "create_engine(settings.database_url)" not in fuente


# ---------------------------------------------------------------------------
# Bloque 1 — un solo criterio para la hora de cierre
# ---------------------------------------------------------------------------


def test_la_licitacion_no_publica_una_hora_que_la_fuente_no_dio() -> None:
    cierre = datetime(2026, 12, 1, 0, 0)

    assert fecha_cierre_legible(cierre, "licitaciones") == "01/12/2026"
    assert "00:00" not in fecha_cierre_legible(cierre, "licitaciones")


def test_la_compra_agil_si_muestra_la_hora() -> None:
    assert fecha_cierre_legible(datetime(2026, 12, 1, 18, 30), "compras_agiles") == "01/12/2026 18:30"


def test_sin_fecha_de_cierre_el_texto_lo_dice() -> None:
    assert fecha_cierre_legible(None, "licitaciones") == "Sin fecha de cierre"


def test_texto_cierre_se_apoya_en_el_mismo_criterio() -> None:
    """Un solo lugar decide si la hora se muestra: si alguien cambia
    `fecha_cierre_legible`, el badge cambia con él."""
    cierre = datetime(2026, 12, 1, 18, 30)

    assert fecha_cierre_legible(cierre, "compras_agiles") in texto_cierre(cierre, 3, "compras_agiles")
    assert fecha_cierre_legible(cierre, "licitaciones") in texto_cierre(cierre, 3, "licitaciones")


# ---------------------------------------------------------------------------
# Fixtures (mismo patrón que tests/test_feed_ui.py)
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


def _crear(engine, owner_id: int, *, fuente: str, codigo: str, dias: float) -> datetime:
    """Crea la oportunidad + perfil + match y devuelve su fecha de cierre."""
    ahora = datetime.now(UTC).replace(tzinfo=None)
    # +1 h de margen: sin él el render calcula 4,9999 días e int() baja a 4.
    cierre = ahora + timedelta(days=dias, hours=1)
    with Session(engine) as s:
        if fuente == "licitaciones":
            s.add(
                Licitacion(
                    codigo=codigo,
                    nombre=f"Oportunidad {codigo}",
                    descripcion="",
                    estado="publicada",
                    monto_clp=1_000_000,
                    fecha_cierre=cierre,
                )
            )
        else:
            s.add(
                CompraAgil(
                    codigo=codigo,
                    nombre=f"Oportunidad {codigo}",
                    descripcion="",
                    estado="publicada",
                    monto_disponible_clp=1_000_000,
                    fecha_cierre=cierre,
                )
            )
        perfil = PerfilBusqueda(
            owner_id=owner_id,
            nombre="Perfil test",
            keywords=["test"],
            keywords_excluir=[],
            regiones=[],
            fuentes=[fuente],
            activo=True,
        )
        s.add(perfil)
        s.flush()
        s.add(
            OportunidadMatch(
                perfil_id=perfil.id,
                fuente=fuente,
                codigo_oportunidad=codigo,
                score=80,
                razones={},
            )
        )
        s.commit()
    return cierre


# ---------------------------------------------------------------------------
# Bloque 1 — la ficha dice lo mismo que la tarjeta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fuente", ["licitaciones", "compras_agiles"])
def test_la_ficha_y_la_tarjeta_dicen_lo_mismo_del_cierre(
    client, settings, usuario, engine, fuente: str
) -> None:
    """El bug que cerró esta fase: la misma licitación decía "cierra en 0d —
    21/09/2026 00:00" en su ficha y otra cosa en la tarjeta."""
    cierre = _crear(engine, usuario, fuente=fuente, codigo="OP-1", dias=5)
    esperado = texto_cierre(cierre, 5, fuente)

    feed = client.get("/", cookies=_cookie(settings, usuario)).text
    ficha = client.get(f"/oportunidad/{fuente}/OP-1", cookies=_cookie(settings, usuario)).text

    assert esperado in feed
    assert esperado in ficha


def test_la_ficha_usa_el_badge_de_urgencia_y_no_bandas_propias(
    client, settings, usuario, engine
) -> None:
    _crear(engine, usuario, fuente="licitaciones", codigo="OP-URGENTE", dias=0.5)
    html = client.get("/oportunidad/licitaciones/OP-URGENTE", cookies=_cookie(settings, usuario)).text

    assert "badge-cierre badge-cierre--critica" in html
    # Nunca solo color: el badge lleva icono y texto, igual que en la tarjeta.
    assert "<svg" in html


def test_la_ficha_de_una_licitacion_no_muestra_la_medianoche(
    client, settings, usuario, engine
) -> None:
    with Session(engine) as s:
        s.add(
            Licitacion(
                codigo="LIC-MEDIANOCHE",
                nombre="Licitación LIC-MEDIANOCHE",
                descripcion="",
                estado="publicada",
                # Instante derivado de un ddmmaaaa: la fuente nunca dio la hora.
                fecha_cierre=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=3),
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
                codigo_oportunidad="LIC-MEDIANOCHE",
                score=80,
                razones={},
            )
        )
        s.commit()

    html = client.get("/oportunidad/licitaciones/LIC-MEDIANOCHE", cookies=_cookie(settings, usuario)).text

    assert "00:00" not in html


def test_ninguna_plantilla_formatea_el_instante_de_cierre_por_su_cuenta() -> None:
    """El checklist de la fase: en las plantillas solo puede quedar `strftime`
    de FECHAS (publicación, PAC), nunca de un instante de cierre."""
    for plantilla in Path("app/api/templates").rglob("*.html"):
        texto = plantilla.read_text(encoding="utf-8")
        for linea in texto.splitlines():
            if "strftime" in linea:
                assert "fecha_cierre" not in linea, f"{plantilla}: {linea.strip()}"
