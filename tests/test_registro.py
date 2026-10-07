"""Tests F-registro — Mi registro (/registro): guardadas, cerradas, vencidas recientes,
descartadas y archivadas.

Corren sobre SQLite en memoria y, si hay `DATABASE_URL` de Postgres, también sobre
Postgres (misma suite, parametrizada). La ventana de vencimiento se calcula en
Python (sin aritmética de fechas en SQL), así que las dos bases dan lo mismo.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.query import (
    contar_descartadas,
    contar_vencidas_recientes,
    listar_registro_guardadas,
    listar_vencidas_recientes,
)
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.db import normalizar_url_driver
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.core.vigencia import condicion_vencida_en_ventana, fecha_vencimiento
from app.matching.feedback import descartar, listar_descartadas
from app.matching.seguimiento import guardar
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CompraAgil,
    Licitacion,
    OportunidadMatch,
    OportunidadSeguida,
    PerfilBusqueda,
    Usuario,
)

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_PG = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")
_PW = "contraseña-segura-test"
_EMAILS = ("reg-a@test.cl", "reg-b@test.cl")
_DIAS = 14
_PISO = 30


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "pg",
            marks=pytest.mark.skipif(not _TIENE_PG, reason="Requiere DATABASE_URL de Postgres"),
        ),
    ]
)
def engine(request):
    if request.param == "sqlite":
        import app.models.tables  # noqa: F401

        e = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(e)
        yield e
        return
    e = create_engine(normalizar_url_driver(_DB_URL))

    def limpiar() -> None:
        with Session(e) as s:
            s.execute(delete(Usuario).where(Usuario.email.in_(_EMAILS)))
            s.execute(delete(Licitacion).where(Licitacion.codigo.like("REG-%")))
            s.execute(delete(CompraAgil).where(CompraAgil.codigo.like("REG-%")))
            s.commit()

    limpiar()
    yield e
    limpiar()
    e.dispose()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url=_DB_URL if _TIENE_PG else "sqlite:///:memory:",
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
    )


def _usuario(s: Session, email: str = _EMAILS[0]) -> int:
    u = Usuario(email=email, password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
    s.add(u)
    s.flush()
    return u.id


def _perfil(s: Session, owner_id: int, nombre: str = "Perfil registro") -> int:
    p = PerfilBusqueda(
        owner_id=owner_id,
        nombre=nombre,
        keywords=["reg"],
        keywords_excluir=[],
        regiones=[],
        fuentes=["licitaciones", "compras_agiles"],
        activo=True,
    )
    s.add(p)
    s.flush()
    return p.id


def _ca(s: Session, codigo: str, *, cierre: datetime | None, **campos: Any) -> None:
    ahora = ahora_utc()
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": f"Compra {codigo}",
        "estado": "publicada",
        "region": 13,
        "organismo_nombre": "ORG REGISTRO",
        "monto_disponible_clp": 500_000.0,
        "fecha_publicacion": ahora - timedelta(days=20),
        "fecha_cierre": cierre,
        "actualizado_en": ahora,
    }
    base.update(campos)
    s.add(CompraAgil(**base))


def _lic(s: Session, codigo: str, *, cierre: datetime | None, **campos: Any) -> None:
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": f"Licitación {codigo}",
        "estado": "publicada",
        "fecha_cierre": cierre,
        "actualizado_en": ahora_utc(),
    }
    base.update(campos)
    s.add(Licitacion(**base))


def _match(s: Session, perfil_id: int, fuente: str, codigo: str, score: float) -> None:
    s.add(
        OportunidadMatch(
            perfil_id=perfil_id, fuente=fuente, codigo_oportunidad=codigo, score=score, razones={}
        )
    )


def _hace(dias: float) -> datetime:
    return ahora_utc() - timedelta(days=dias)


def _cliente(engine, settings: Settings, user_id: int) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(create_app(settings, engine))
    token = create_session_token(settings.secret_key, user_id)
    client.cookies.set(COOKIE_NAME, token)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    _, nonce = decoded
    return client, {"X-CSRF-Token": generate_csrf_token(settings.secret_key, nonce)}


def _codigos(html: str) -> list[str]:
    """Códigos de las tarjetas (o filas de descartadas) de la página, en orden."""
    return re.findall(r'data-oportunidad-key="[a-z_]+:([^"]+)"', html) or re.findall(
        r'id="descarte-[a-z_]+-([^"]+)"', html
    )


def _conteos(html: str) -> dict[str, int]:
    return {
        clave: int(n)
        for clave, n in re.findall(r'href="/registro\?tab=([a-z]+)"[^>]*>[^<(]*\((\d+)\)', html)
    }


def _tab(client: TestClient, tab: str, **params: str) -> str:
    r = client.get("/registro", params={"tab": tab, **params})
    assert r.status_code == 200
    return r.text


# ---------------------------------------------------------------------------
# Vencidas recientes
# ---------------------------------------------------------------------------


def _sembrar_vencidas(engine) -> int:
    with Session(engine) as s:
        uid = _usuario(s)
        p1 = _perfil(s, uid)
        p2 = _perfil(s, uid, "Otro perfil")
        _ca(s, "REG-V35", cierre=_hace(3))
        _match(s, p1, "compras_agiles", "REG-V35", 35)
        _ca(s, "REG-V25", cierre=_hace(3))
        _match(s, p1, "compras_agiles", "REG-V25", 25)
        _ca(s, "REG-V15D", cierre=_hace(15))
        _match(s, p1, "compras_agiles", "REG-V15D", 90)
        _ca(s, "REG-DESC", cierre=_hace(2))
        _match(s, p1, "compras_agiles", "REG-DESC", 80)
        _ca(s, "REG-GUARD", cierre=_hace(2))
        _match(s, p1, "compras_agiles", "REG-GUARD", 80)
        _ca(s, "REG-ARCH", cierre=_hace(2))
        _match(s, p1, "compras_agiles", "REG-ARCH", 80)
        _ca(s, "REG-DOS", cierre=_hace(1))
        _match(s, p1, "compras_agiles", "REG-DOS", 25)
        _match(s, p2, "compras_agiles", "REG-DOS", 35)
        _ca(s, "REG-VIGENTE", cierre=ahora_utc() + timedelta(days=3))
        _match(s, p1, "compras_agiles", "REG-VIGENTE", 90)
        _lic(s, "REG-LIC", cierre=_hace(5))
        _match(s, p1, "licitaciones", "REG-LIC", 60)
        s.flush()
        descartar(s, uid, "compras_agiles", "REG-DESC")
        guardar(s, uid, "compras_agiles", "REG-GUARD")
        guardar(s, uid, "compras_agiles", "REG-ARCH")
        s.execute(
            OportunidadSeguida.__table__.update()
            .where(OportunidadSeguida.codigo_oportunidad == "REG-ARCH")
            .values(archivada=True)
        )
        s.commit()
        return uid


def test_vencidas_recientes(engine, settings):
    uid = _sembrar_vencidas(engine)
    client, _ = _cliente(engine, settings, uid)
    html = _tab(client, "vencidas")
    codigos = _codigos(html)
    # Una vez cada una, la más reciente primero (REG-DOS cerró hace 1 día).
    assert codigos == ["REG-DOS", "REG-V35", "REG-LIC"]
    assert "Se oculta en 11 días" in html  # REG-V35: cerró hace 3 días, 14 - 3
    assert "Se oculta en 9 días" in html  # REG-LIC: hace 5 días
    # REG-DOS: perfiles con 25 y 35 -> una vez, con el máximo.
    bloque = html[html.index("REG-DOS") : html.index("REG-DOS") + 1200]
    assert ">35<" in bloque
    for fuera in ("REG-V25", "REG-V15D", "REG-DESC", "REG-GUARD", "REG-ARCH", "REG-VIGENTE"):
        assert fuera not in html
    # Ocultar no es borrar: el match de la vencida hace 15 días sigue existiendo.
    with Session(engine) as s:
        n = s.execute(
            select(func.count())
            .select_from(OportunidadMatch)
            .where(OportunidadMatch.codigo_oportunidad == "REG-V15D")
        ).scalar_one()
        assert n == 1


def test_descartada_solo_en_descartadas_y_guardada_o_archivada_nunca_en_vencidas(engine, settings):
    uid = _sembrar_vencidas(engine)
    client, _ = _cliente(engine, settings, uid)
    assert _codigos(_tab(client, "descartadas")) == ["REG-DESC"]
    assert _codigos(_tab(client, "cerradas")) == ["REG-GUARD"]
    assert _codigos(_tab(client, "archivadas")) == ["REG-ARCH"]
    assert "REG-GUARD" not in _tab(client, "vencidas")
    assert "REG-ARCH" not in _tab(client, "vencidas")


def test_se_oculta_hoy_el_ultimo_dia(engine, settings):
    with Session(engine) as s:
        uid = _usuario(s)
        _ca(s, "REG-ULT", cierre=_hace(13.5))
        _match(s, _perfil(s, uid), "compras_agiles", "REG-ULT", 50)
        s.commit()
    client, _ = _cliente(engine, settings, uid)
    assert "Se oculta hoy" in _tab(client, "vencidas")


def test_ca_sin_cierre_vence_a_los_siete_dias_de_publicada(engine, settings):
    with Session(engine) as s:
        uid = _usuario(s)
        p = _perfil(s, uid)
        # publicada hace 10 días -> venció hace 3; hace 22 -> venció hace 15 (fuera);
        # hace 2 -> vigente (no entra); sin publicación -> no se puede fechar: no entra
        # (actualizado_en no sirve, la ingesta lo reescribe).
        _ca(s, "REG-P10", cierre=None, fecha_publicacion=_hace(10))
        _ca(s, "REG-P22", cierre=None, fecha_publicacion=_hace(22))
        _ca(s, "REG-P2", cierre=None, fecha_publicacion=_hace(2))
        _ca(s, "REG-SINPUB", cierre=None, fecha_publicacion=None, actualizado_en=_hace(4))
        for codigo in ("REG-P10", "REG-P22", "REG-P2", "REG-SINPUB"):
            _match(s, p, "compras_agiles", codigo, 60)
        s.commit()
    client, _ = _cliente(engine, settings, uid)
    html = _tab(client, "vencidas")
    assert set(_codigos(html)) == {"REG-P10"}
    assert "Se oculta en 11 días" in html  # REG-P10: venció hace 3 días


def test_fecha_vencimiento_python_coincide_con_la_ventana_sql(engine):
    ahora = ahora_utc()
    desde = ahora - timedelta(days=_DIAS)
    cierres = [_hace(3), ahora + timedelta(days=2), None, _hace(20)]
    publicaciones = [None, _hace(10), _hace(22), _hace(2)]
    actualizaciones = [_hace(4), _hace(20), ahora]
    esperado: dict[str, bool] = {}
    n = 0
    with Session(engine) as s:
        for cierre in cierres:
            for actualizado in actualizaciones:
                n += 1
                codigo = f"REG-ML{n:03d}"
                _lic(s, codigo, cierre=cierre, actualizado_en=actualizado)
                op = s.get(Licitacion, codigo) or Licitacion(
                    codigo=codigo, nombre="x", fecha_cierre=cierre, actualizado_en=actualizado
                )
                venc = fecha_vencimiento(op, "licitaciones")
                esperado[codigo] = venc is not None and desde < venc <= ahora
        for cierre in cierres:
            for pub in publicaciones:
                for actualizado in actualizaciones:
                    n += 1
                    codigo = f"REG-MC{n:03d}"
                    _ca(s, codigo, cierre=cierre, fecha_publicacion=pub, actualizado_en=actualizado)
                    op_ca = CompraAgil(
                        codigo=codigo,
                        nombre="x",
                        fecha_cierre=cierre,
                        fecha_publicacion=pub,
                        actualizado_en=actualizado,
                    )
                    venc = fecha_vencimiento(op_ca, "compras_agiles")
                    esperado[codigo] = venc is not None and desde < venc <= ahora
        s.flush()
        en_sql = set(
            s.execute(
                select(Licitacion.codigo).where(
                    Licitacion.codigo.like("REG-ML%"),
                    condicion_vencida_en_ventana("licitaciones", desde, ahora),
                )
            ).scalars()
        ) | set(
            s.execute(
                select(CompraAgil.codigo).where(
                    CompraAgil.codigo.like("REG-MC%"),
                    condicion_vencida_en_ventana("compras_agiles", desde, ahora),
                )
            ).scalars()
        )
        s.rollback()
    assert en_sql == {c for c, v in esperado.items() if v}
    assert any(esperado.values()) and not all(esperado.values())


def test_sin_fecha_confiable_no_hay_vencimiento_ni_entra_a_la_ventana(engine):
    """Licitación sin cierre y CA sin cierre ni publicación, con actualizado_en = ahora:
    `fecha_vencimiento` es None y la ventana SQL las deja fuera."""
    ahora = ahora_utc()
    with Session(engine) as s:
        _lic(s, "REG-NF-LIC", cierre=None, actualizado_en=ahora)
        _ca(s, "REG-NF-CA", cierre=None, fecha_publicacion=None, actualizado_en=ahora)
        s.flush()
        lic = s.get(Licitacion, "REG-NF-LIC")
        ca = s.get(CompraAgil, "REG-NF-CA")
        assert lic is not None and ca is not None
        assert fecha_vencimiento(lic, "licitaciones") is None
        assert fecha_vencimiento(ca, "compras_agiles") is None
        desde = ahora - timedelta(days=_DIAS)
        assert (
            s.execute(
                select(Licitacion.codigo).where(
                    Licitacion.codigo == "REG-NF-LIC",
                    condicion_vencida_en_ventana("licitaciones", desde, ahora + timedelta(days=1)),
                )
            ).first()
            is None
        )
        assert (
            s.execute(
                select(CompraAgil.codigo).where(
                    CompraAgil.codigo == "REG-NF-CA",
                    condicion_vencida_en_ventana("compras_agiles", desde, ahora + timedelta(days=1)),
                )
            ).first()
            is None
        )
        s.rollback()


def test_vencidas_no_carga_matches_fuera_de_la_ventana(engine):
    with Session(engine) as s:
        uid = _usuario(s)
        p = _perfil(s, uid)
        for i in range(30):
            _ca(s, f"REG-OLD{i:02d}", cierre=_hace(40 + i))
            _match(s, p, "compras_agiles", f"REG-OLD{i:02d}", 90)
        for i in range(2):
            _ca(s, f"REG-NEW{i}", cierre=_hace(2 + i))
            _match(s, p, "compras_agiles", f"REG-NEW{i}", 90)
        s.commit()
        cargados: list[str] = []

        def al_cargar(target: Any, context: Any) -> None:  # noqa: ARG001
            cargados.append(target.codigo_oportunidad)

        event.listen(OportunidadMatch, "load", al_cargar)
        try:
            s.expire_all()
            items = listar_vencidas_recientes(s, uid, ahora_utc(), dias=_DIAS, min_score=_PISO)
        finally:
            event.remove(OportunidadMatch, "load", al_cargar)
    assert {i["codigo"] for i in items} == {"REG-NEW0", "REG-NEW1"}
    assert sorted(cargados) == ["REG-NEW0", "REG-NEW1"]


# ---------------------------------------------------------------------------
# Guardadas / cerradas / archivadas
# ---------------------------------------------------------------------------


def test_guardadas_cerradas_y_sin_match(engine, settings):
    with Session(engine) as s:
        uid = _usuario(s)
        p = _perfil(s, uid)
        futuro = ahora_utc() + timedelta(days=4)
        _lic(s, "REG-GV", cierre=futuro)
        _match(s, p, "licitaciones", "REG-GV", 80)
        _lic(s, "REG-GCERR", cierre=_hace(2))
        _match(s, p, "licitaciones", "REG-GCERR", 80)
        _ca(s, "REG-GCA", cierre=futuro)  # guardada desde Explorar CA: sin match
        _lic(s, "REG-GSIN", cierre=futuro)  # su match lo borró la limpieza
        s.flush()
        for fuente, codigo in [
            ("licitaciones", "REG-GV"),
            ("licitaciones", "REG-GCERR"),
            ("compras_agiles", "REG-GCA"),
            ("licitaciones", "REG-GSIN"),
        ]:
            guardar(s, uid, fuente, codigo)
        s.commit()
    client, _ = _cliente(engine, settings, uid)

    html = _tab(client, "guardadas")
    # Cierre más próximo primero; todas siguen guardadas.
    assert set(_codigos(html)) == {"REG-GV", "REG-GCA", "REG-GSIN"}
    assert "Guardada desde Explorar CA" in html
    assert "Ya no calza con tus perfiles" in html
    assert html.count("anillo-match anillo-match--") == 1  # solo REG-GV tiene score
    assert _codigos(_tab(client, "cerradas")) == ["REG-GCERR"]

    # Dashboard: la guardada vigente lleva su chip; la que venció ya no está.
    feed = client.get("/").text
    assert "REG-GV" in feed and "Guardada" in feed
    assert "REG-GCERR" not in feed


def test_acciones_desde_el_registro(engine, settings):
    uid = _sembrar_vencidas(engine)
    client, headers = _cliente(engine, settings, uid)
    hx = {**headers, "HX-Request": "true"}

    # Guardar una vencida -> sale de vencidas (vacío + anuncio) y entra a cerradas.
    r = client.post(
        "/oportunidad/compras_agiles/REG-V35/guardar",
        data={"origen": "registro", "next": "/registro?tab=vencidas"},
        headers=hx,
    )
    assert r.status_code == 200 and 'id="anuncios"' in r.text and "Guardada: Compra REG-V35" in r.text
    assert "REG-V35" in _codigos(_tab(client, "cerradas"))
    assert "REG-V35" not in _codigos(_tab(client, "vencidas"))

    # Descartar una vencida -> descartadas.
    r = client.post(
        "/oportunidad/licitaciones/REG-LIC/descartar", data={"origen": "registro"}, headers=hx
    )
    assert r.status_code == 200 and "Descartada:" in r.text
    assert "REG-LIC" in _codigos(_tab(client, "descartadas"))
    assert "REG-LIC" not in _codigos(_tab(client, "vencidas"))

    # Archivar / desarchivar / quitar vuelven a la pestaña indicada.
    base = "/oportunidad/compras_agiles/REG-V35"
    r = client.post(f"{base}/archivar", data={"next": "/registro?tab=cerradas"}, headers=headers, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/registro?tab=cerradas"
    assert "REG-V35" in _codigos(_tab(client, "archivadas"))
    r = client.post(f"{base}/desarchivar", data={"next": "/registro?tab=archivadas"}, headers=headers, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/registro?tab=archivadas"
    assert "REG-V35" in _codigos(_tab(client, "cerradas"))
    r = client.post(f"{base}/dejar-de-seguir", data={"next": "/registro?tab=cerradas"}, headers=headers, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/registro?tab=cerradas"
    assert "REG-V35" not in _codigos(_tab(client, "cerradas"))
    # Sin guardar ni descartar y dentro de la ventana, vuelve a Vencidas.
    assert "REG-V35" in _codigos(_tab(client, "vencidas"))

    # Restaurar una descartada vuelve a la pestaña descartadas.
    r = client.post(
        "/oportunidad/licitaciones/REG-LIC/deshacer-descarte",
        data={"next": "/registro?tab=descartadas"},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/registro?tab=descartadas"


def test_acciones_del_registro_exigen_csrf(engine, settings):
    uid = _sembrar_vencidas(engine)
    client, _ = _cliente(engine, settings, uid)
    for ruta in ("guardar", "descartar", "archivar", "desarchivar", "dejar-de-seguir"):
        r = client.post(
            f"/oportunidad/compras_agiles/REG-V35/{ruta}",
            data={"csrf_token": "invalido", "origen": "registro"},
            follow_redirects=False,
        )
        assert r.status_code == 403, ruta


# ---------------------------------------------------------------------------
# Ownership, conteos, paginación y filtros
# ---------------------------------------------------------------------------


def test_otro_usuario_no_ve_nada_ajeno(engine, settings):
    _sembrar_vencidas(engine)
    with Session(engine) as s:
        otro = _usuario(s, _EMAILS[1])
        s.commit()
    client, _ = _cliente(engine, settings, otro)
    for tab in ("guardadas", "cerradas", "vencidas", "descartadas", "archivadas"):
        html = _tab(client, tab)
        assert "REG-" not in html, tab
    assert set(_conteos(_tab(client, "guardadas")).values()) == {0}
    with Session(engine) as s:
        assert contar_vencidas_recientes(s, otro, ahora_utc(), dias=_DIAS, min_score=_PISO) == 0
        assert listar_registro_guardadas(s, otro, archivadas=False) == []


def test_conteos_igual_a_filas_y_dashboard_igual_a_vencidas(engine, settings):
    uid = _sembrar_vencidas(engine)
    client, _ = _cliente(engine, settings, uid)
    for tab in ("guardadas", "cerradas", "vencidas", "descartadas", "archivadas"):
        html = _tab(client, tab)
        assert _conteos(html)[tab] == len(_codigos(html)), tab
    n_vencidas = _conteos(_tab(client, "vencidas"))["vencidas"]
    assert n_vencidas == 3
    feed = client.get("/").text
    assert f"{n_vencidas} vencidas recientes en tu registro" in feed
    with Session(engine) as s:
        assert contar_vencidas_recientes(s, uid, ahora_utc(), dias=_DIAS, min_score=_PISO) == n_vencidas


def test_contar_descartadas_coincide_con_la_lista(engine):
    uid = _sembrar_vencidas(engine)
    with Session(engine) as s:
        otro = _usuario(s, _EMAILS[1])
        s.commit()
        assert contar_descartadas(s, uid) == len(listar_descartadas(s, uid)) == 1
        assert contar_descartadas(s, otro) == len(listar_descartadas(s, otro)) == 0


def test_paginacion_de_vencidas(engine, settings):
    with Session(engine) as s:
        uid = _usuario(s)
        p = _perfil(s, uid)
        for i in range(25):
            _ca(s, f"REG-PG{i:02d}", cierre=_hace(1 + i / 10))
            _match(s, p, "compras_agiles", f"REG-PG{i:02d}", 50)
        s.commit()
    client, _ = _cliente(engine, settings, uid)
    html = _tab(client, "vencidas")
    assert len(_codigos(html)) == 20 and "Ver 20 más" in html
    html = _tab(client, "vencidas", n="40")
    assert len(_codigos(html)) == 25 and "Ver 20 más" not in html


def test_filtros_fuente_y_texto(engine, settings):
    with Session(engine) as s:
        uid = _usuario(s)
        p = _perfil(s, uid)
        _ca(s, "REG-F1", cierre=_hace(2), nombre="Servicio de aseo", organismo_nombre="HOSPITAL X")
        _lic(s, "REG-F2", cierre=_hace(2), nombre="Servicio de jardinería")
        _match(s, p, "compras_agiles", "REG-F1", 50)
        _match(s, p, "licitaciones", "REG-F2", 50)
        s.commit()
    client, _ = _cliente(engine, settings, uid)
    assert set(_codigos(_tab(client, "vencidas"))) == {"REG-F1", "REG-F2"}
    assert _codigos(_tab(client, "vencidas", fuente="licitaciones")) == ["REG-F2"]
    assert _codigos(_tab(client, "vencidas", texto="hospital")) == ["REG-F1"]
    assert _codigos(_tab(client, "vencidas", texto="jardiner")) == ["REG-F2"]


# ---------------------------------------------------------------------------
# Navegación y rutas viejas
# ---------------------------------------------------------------------------


def test_rutas_viejas_redirigen_303(engine, settings):
    with Session(engine) as s:
        uid = _usuario(s)
        s.commit()
    client, _ = _cliente(engine, settings, uid)
    for ruta, destino in [
        ("/seguidas", "/registro?tab=guardadas"),
        ("/seguidas?archivadas=1", "/registro?tab=archivadas"),
        ("/descartadas", "/registro?tab=descartadas"),
    ]:
        r = client.get(ruta, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == destino, ruta


def test_navegacion_y_pestanas_accesibles(engine, settings):
    with Session(engine) as s:
        uid = _usuario(s)
        s.commit()
    client, _ = _cliente(engine, settings, uid)
    html = _tab(client, "cerradas")
    i = html.index('href="/registro"')
    enlace = html[html.rindex("<a", 0, i) : html.index("</a>", i)]
    assert 'aria-current="page"' in enlace
    assert html.count('aria-current="page"') == 2  # nav principal + pestaña activa
    assert "Cerradas (0)" in html and "Vencidas recientes (0)" in html
    # Una pestaña desconocida cae en guardadas.
    assert "Guardadas (0)" in _tab(client, "inventada")


def test_parametros_por_defecto():
    s = Settings(
        mp_ticket="T", database_url="sqlite:///:memory:", secret_key="x" * 32, jobs_token="j"
    )
    assert s.registro_dias_gracia == 14
    assert s.registro_min_score_vencidas == 30
