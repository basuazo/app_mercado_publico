"""F-plat-1: catálogo canónico, líneas 1:1 por Compra Ágil y bandera de CA sin detalle.

Reglas puras con textos reales de `cot_api_2026-08` (sin RUT ni montos), backfill idempotente
que no pisa revisiones humanas, estados de `detalle_lineas`, re-aplicación de alias y permisos.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.catalogos.canasta_alimentos import KITS, REGLAS
from app.catalogos.canonico import (
    Catalogo,
    detectar_sin_detalle,
    estado_por_confianza,
    extraer_formato,
    normalizar_texto,
    sugerir_producto,
)
from app.core.settings import Settings
from app.mercado import catalogo as cat
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaCotizacion,
    CaCotizacionProducto,
    CaLinea,
    CaProducto,
    CompraAgil,
    Empresa,
    ProductoAlias,
    ProductoCanonico,
    ProveedorProducto,
    SyncState,
    Usuario,
)

_PW = "contraseña-segura-test"


# ---------------------------------------------------------------------------
# Reglas puras
# ---------------------------------------------------------------------------

# 60 textos reales de agosto 2026 (alimentos, UNSPSC 50…) con el formato esperado.
# (texto, (cantidad, unidad, factor_base, unidad_base) | None)
FORMATOS_REALES = [
    ("galletas de chocolate crema 100g", (100, "g", 0.1, "kg")),
    ("GALLETAS COSTA COCO 140 GRAMOS", (140, "g", 0.14, "kg")),
    (
        "Té Negro premium. Caja de 100 unidades. Marca similar o equivalente a Lipton.",
        (100, "u", 100, "u"),
    ),
    ("QUEQUE SABORES 400 GRS (VAINILLA,  CHOCOLATE)", (400, "g", 0.4, "kg")),
    ("chocolate trencito o similar 14 grs", (14, "g", 0.014, "kg")),
    ("JUGO EN POLVO 90GRS SIN AZUCAR", (90, "g", 0.09, "kg")),
    ("Galleta de mantequilla 140 grs.", (140, "g", 0.14, "kg")),
    ("Tarro de café 400 grs.", (400, "g", 0.4, "kg")),
    ("OREGENO 20 GRS", (20, "g", 0.02, "kg")),
    ("Galletas surtidas de 120 gr", (120, "g", 0.12, "kg")),
    ("MARGARINA POTE DE 500GRS", (500, "g", 0.5, "kg")),
    ("Kilos de palta", (1, "kg", 1, "kg")),
    ("ENDULZANTE LIQUIDO IANSA CERO K STEVIA SUCRALOSA 350ML", (350, "ml", 0.35, "L")),
    ("CHIP CHOCOLATE KILO", (1, "kg", 1, "kg")),
    ("salsa de tomate 200gr", (200, "g", 0.2, "kg")),
    ("queso laminado 500 gr", (500, "g", 0.5, "kg")),
    ("FLANES EN POLVO SIN AZUCAR 90GRS APROX", (90, "g", 0.09, "kg")),
    ("JUGO WATTS 1.5LITROS SABORES", (1.5, "L", 1.5, "L")),
    ("ZANAHORIAS POR KILO ./ SEGUN ESPECIFICACIONES TECNICAS", (1, "kg", 1, "kg")),
    ("aceite de oliva 500 ml", (500, "ml", 0.5, "L")),
    ("QUESO CREMA 200GRS APROX", (200, "g", 0.2, "kg")),
    ("Maní salado 380 grs. Marco Polo o equivalente", (380, "g", 0.38, "kg")),
    ("EMPANADAS DE PINO FRESCAS DE HORNO, 240 GRAMOS CADA UNA", (240, "g", 0.24, "kg")),
    ("Jugo sin azucar, en botella 1.5 litros diferentes sabores", (1.5, "L", 1.5, "L")),
    ("BIDONES DE AGUA BIDESTILADA DE 5 LITROS", (5, "L", 5, "L")),
    ("ACEITE 900ML", (900, "ml", 0.9, "L")),
    ("JUGO INDIVIDUAL SABORES DE 200CC", (200, "ml", 0.2, "L")),
    ("Queques de 160 gr o mas", (160, "g", 0.16, "kg")),
    ("palta hass madura - kilo", (1, "kg", 1, "kg")),
    ("brownie Individual 62g", (62, "g", 0.062, "kg")),
    ("JUGO NECTAR WATTS 1.5LT DIFERENTES SABORES", (1.5, "L", 1.5, "L")),
    ("AZUCAR KILO", (1, "kg", 1, "kg")),
    ("Galleta Tipo Chokita 30 gr o superior", (30, "g", 0.03, "kg")),
    ("POSTA ROSADA SIN MARINAR BOLSA 1 KILO", (1, "kg", 1, "kg")),
    ("Agua mineral desechable saborizada 600cc", (600, "ml", 0.6, "L")),
    (
        "Café descafeinado soluble 170 grs, fecha de vencimiento no menor a 12 meses",
        (170, "g", 0.17, "kg"),
    ),
    ("MERMELADAS   SABOR DURAZNO BOLSA  DE 250 GRS  SIMILAR A TIA LIA", (250, "g", 0.25, "kg")),
    ('Azucar de 1 kg similar a "IANZA"', (1, "kg", 1, "kg")),
    (
        "AGUA MINERAL CON GAS INDIVIDUAL 500CC, DEBEN SER ENTREGADAS EL DÍA 04 DE SEPTIEMBRE",
        (500, "ml", 0.5, "L"),
    ),
    ("PECHUGA DE POLLO DESHUESADA 850 GRS", (850, "g", 0.85, "kg")),
    ("Bebida Isotonica Gatorade botella, 1L", (1, "L", 1, "L")),
    ("Agua saborizada diferentes sabores, 1.5 lts.", (1.5, "L", 1.5, "L")),
    ("BEBIDA SABOR NARANJA DE 2LTS CCU O COCA-COLA.", (2, "L", 2, "L")),
    ("MANJAR CON ALULOSA 800GRS", (800, "g", 0.8, "kg")),
    ("AGUA SIN GAS, BOTELLA DE 500 CC", (500, "ml", 0.5, "L")),
    ("PIÑA EN CONSERVA TARRO 560 GRS", (560, "g", 0.56, "kg")),
    ("coco rallado 200gr", (200, "g", 0.2, "kg")),
    ("endulzante 250 ml", (250, "ml", 0.25, "L")),
    ("FRAMBUESA CONGELADA 500 GRS", (500, "g", 0.5, "kg")),
    # packs y formatos compuestos
    ("Sachet 1 kg de salsa", (1, "kg", 1, "kg")),
    ("Leche pack 6 unidades 1 L", (6, "L", 6, "L")),
    ("jugo 6 x 200 ml", (1200, "ml", 1.2, "L")),
    ("Yogurt x 24", (24, "u", 24, "u")),
    ("Huevos docena", (12, "u", 12, "u")),
    ("Sal bolsa 1.000 g", (1000, "g", 1.0, "kg")),
    # sin formato: un número solo no es un formato
    ("50 tortas de diferentes sabores para 50 personas", None),
    ("800 empanadas de pino", None),
    ("Marraqueta fresca o similar", None),
    ("ALIMENTOS SEGUN ANEXO ADJUNTO", None),
    ("", None),
]


@pytest.mark.parametrize(("texto", "esperado"), FORMATOS_REALES)
def test_extraer_formato_textos_reales(texto, esperado):
    r = extraer_formato(texto)
    if esperado is None:
        assert r is None
    else:
        assert r is not None
        cant, unidad, factor, base = r
        assert (unidad, base) == (esperado[1], esperado[3])
        assert cant == pytest.approx(esperado[0])
        assert factor == pytest.approx(esperado[2])


def test_hay_al_menos_60_textos_en_la_tabla():
    assert len(FORMATOS_REALES) >= 60


def test_normalizar_texto():
    assert (
        normalizar_texto("50 Tortas de CHOCOLATE, para 50 personas")
        == "tortas de chocolate para personas"
    )
    assert normalizar_texto("Café  Nescafé 170 g.") == "cafe nescafe 170 g"
    assert normalizar_texto("Jugo 1,5 L") == "jugo 1,5 l"
    assert normalizar_texto(None) == ""
    assert len(normalizar_texto("palabra " * 100)) <= 300


# (texto, cantidad, monto, esperado). Positivos y falsos positivos medidos sobre textos reales.
SIN_DETALLE = [
    (
        "ALIMENTOS SEGUN ANEXO ADJUNTO N°1 , ESTOS ALIMENTOS DEBERAN SER ENTREGADOS EN LAS OFICINAS",
        1,
        500000,
        True,
    ),
    ("LISTADO DE ABARROTES REQUERIDOS EN DOCUMENTO ADJUNTO.", 1, 300000, True),
    ('Alimentos diferentes convenios del CESFAM, detalle en Excel "Valores".', 1, 1289800, True),
    ("INSUMOS SEGUN ORDEN DE PEDIDO N°123, ADJUNTA (REVISAR DOCUMENTO)", 1, 2020000, True),
    ("CAJA DE ALIMENTOS. DETALLE DE PRODUCTOS EN DOCUMENTO ADJUNTO", 1, 140000, True),
    ("Revisar archivo adjunto.", 1, 2301000, True),
    ("SEGUN ANEXO ESCUELA VILLA JESUS RESPETAR FECHA Y HORA DE ENTREGA", 1, 300000, True),
    ("ALIMENTOS PARA EL PROGRAMA SENDA, SEGUN ESPECIFICACIONES ADJUNTAS", 1, 200000, True),
    # la heurística exige cantidad 1 y monto > $100.000
    ("ZANAHORIAS POR KILO ./ SEGUN ESPECIFICACIONES TECNICAS", 100, 610382, False),
    ("PAN FRANCES O HALLULLAS EN KILOS. / SEGUN ESPECIFICACIONES TECNICAS", 60, 150000, False),
    ("ADJUNTAR ESPECIFICACIONES DE LOS PRODUCTOS", 400, 250000, False),
    ("ALIMENTOS SEGUN ANEXO ADJUNTO", 1, 90000, False),
    ("ALIMENTOS SEGUN ANEXO ADJUNTO", 1, 100000, False),
    ("ALIMENTOS SEGUN ANEXO ADJUNTO", None, None, True),
]


@pytest.mark.parametrize(("texto", "cantidad", "monto", "esperado"), SIN_DETALLE)
def test_detectar_sin_detalle(texto, cantidad, monto, esperado):
    assert detectar_sin_detalle(texto, cantidad, monto) is esperado


def test_sin_detalle_no_marca_productos_con_nombre_ni_cantidad():
    """Falsos positivos: los textos de producto reales de la tabla de formatos jamás son anexo."""
    marcados = [t for t, _ in FORMATOS_REALES if detectar_sin_detalle(t, 1, 500000)]
    # Solo el texto que dice "ANEXO ADJUNTO" en la tabla de formatos (0 falsos positivos en 62).
    assert marcados == ["ALIMENTOS SEGUN ANEXO ADJUNTO"]


def test_estado_por_confianza():
    assert estado_por_confianza(1.0) == "ok"
    assert estado_por_confianza(0.8) == "ok"
    assert estado_por_confianza(0.79) == "dudosa"
    assert estado_por_confianza(0.5) == "dudosa"
    assert estado_por_confianza(0.49) == "pendiente"
    assert estado_por_confianza(None) == "pendiente"


def test_reglas_compilan_y_los_nombres_son_unicos():
    nombres = [r.nombre for r in REGLAS]
    assert len(nombres) == len(set(nombres))
    for r in REGLAS:
        re.compile(r.patron)
    for k in KITS:
        re.compile(k.patron)
    assert sum(r.sembrar for r in REGLAS) == 80


# ---------------------------------------------------------------------------
# Base de datos (SQLite en memoria)
# ---------------------------------------------------------------------------


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
def session(engine):
    with Session(engine) as s:
        yield s


def _ca(session, codigo, productos, region=16, monto=300000.0, nombre="CA de prueba"):
    """CA con sus productos solicitados: (UNSPSC, nombre, descripcion, cantidad, unidad)."""
    session.add(CompraAgil(codigo=codigo, nombre=nombre, region=region, monto_disponible_clp=monto))
    session.flush()
    for cod, nom, desc, cant, uni in productos:
        session.add(
            CaProducto(
                ca_codigo=codigo, codigo_producto=cod, nombre=nom, descripcion=desc,
                cantidad=cant, unidad=uni,
            )
        )  # fmt: skip
    session.flush()


def _lineas(session, codigo):
    return list(
        session.execute(
            select(CaLinea).where(CaLinea.ca_codigo == codigo).order_by(CaLinea.n)
        ).scalars()
    )


def _producto(session, nombre):
    return session.execute(
        select(ProductoCanonico).where(ProductoCanonico.nombre == nombre)
    ).scalar_one()


def test_sembrar_crea_80_productos_con_formato_base_y_es_idempotente(session):
    r = cat.sembrar_canasta(session)
    session.commit()
    assert r["productos_creados"] == 80
    assert session.scalar(select(func.count()).select_from(ProductoCanonico)) == 80
    # Todo producto nace con su formato base (factor 1).
    sin_base = [
        p.nombre for p in session.execute(select(ProductoCanonico)).scalars()
        if not any(f.factor_base == 1.0 for f in p.formatos)
    ]  # fmt: skip
    assert sin_base == []
    assert cat.sembrar_canasta(session)["productos_creados"] == 0


def test_renombrar_y_volver_a_sembrar_no_duplica_y_la_regla_sigue_al_producto(session):
    cat.sembrar_canasta(session)
    q = _producto(session, "Hallulla")
    q.nombre = "Hallulla corregida por Boris"
    session.commit()
    r = cat.sembrar_canasta(session)
    session.commit()
    assert r["productos_creados"] == 0
    assert session.scalar(select(func.count()).select_from(ProductoCanonico)) == 80
    # La regla sugiere el producto renombrado, no un duplicado.
    c = cat.cargar_catalogo(session)
    s = sugerir_producto("hallulla fresca", "50181901", c)
    assert s is not None and s.producto_id == q.id


def test_desactivar_no_reactiva_ni_vuelve_a_crear(session):
    cat.sembrar_canasta(session)
    p = _producto(session, "Marraqueta")
    p.activo = False
    session.commit()
    assert cat.sembrar_canasta(session)["productos_creados"] == 0
    session.commit()
    assert _producto(session, "Marraqueta").activo is False
    assert session.scalar(select(func.count()).select_from(ProductoCanonico)) == 80


def test_producto_creado_a_mano_es_adoptado_por_la_semilla(session):
    propio = cat.crear_producto(session, nombre="hallulla", unidad_base="kg", usuario_id=1)
    session.commit()
    r = cat.sembrar_canasta(session)
    session.commit()
    assert r["productos_adoptados"] == 1 and r["productos_creados"] == 79
    session.refresh(propio)
    assert propio.clave_semilla == "hallulla"
    assert session.scalar(select(func.count()).select_from(ProductoCanonico)) == 80
    assert cat.sembrar_canasta(session)["productos_creados"] == 0


def _catalogo(session) -> Catalogo:
    cat.sembrar_canasta(session)
    session.commit()
    return cat.cargar_catalogo(session)


def test_sugerir_regla_alias_y_exclusiones(session):
    c = _catalogo(session)
    s = sugerir_producto("Marraqueta fresca 1 kg", "50181901", c)
    assert s is not None and s.via == "regla" and s.confianza == 0.5
    assert estado_por_confianza(s.confianza) == "dudosa"
    assert c.productos[s.producto_id].nombre == "Marraqueta"
    # Fuera de la familia de alimentos por UNSPSC: no se sugiere.
    assert sugerir_producto("Marraqueta", "44121500", c) is None
    # Kit/servicio/anexo y texto sin producto: nada.
    assert sugerir_producto("Servicio de coffee break para 50 personas", "50192100", c) is None
    assert sugerir_producto("ALIMENTOS SEGUN ANEXO ADJUNTO", "50192100", c) is None
    assert sugerir_producto("tornillos de acero", "50192100", c) is None
    # Una regla de un producto que NO entra a la canasta (reserva) no cae en otro.
    assert sugerir_producto("Pizza familiar", "50192800", c) is None


def test_sugerir_alias_exacto_y_contenido(session):
    c = _catalogo(session)
    marraqueta = _producto(session, "Marraqueta")
    kilo = next(f for f in marraqueta.formatos if f.factor_base == 1.0)
    session.add(
        ProductoAlias(
            texto_normalizado="pan batido de campo", producto_id=marraqueta.id,
            formato_id=kilo.id, fuente="humano", confianza=1.0, veces_visto=1,
        )
    )  # fmt: skip
    session.commit()
    c = cat.cargar_catalogo(session)
    exacto = sugerir_producto("Pan batido de CAMPO", "50181901", c)
    assert exacto and exacto.via == "alias" and exacto.confianza == 1.0
    assert exacto.formato_id == kilo.id
    contenido = sugerir_producto("20 kilos de pan batido de campo para el liceo", "50181901", c)
    assert contenido and contenido.via == "contenido" and contenido.confianza == 0.8
    assert estado_por_confianza(contenido.confianza) == "ok"


def test_backfill_crea_lineas_estados_y_bandera(session):
    cat.sembrar_canasta(session)
    _ca(session, "1-1-COT26", [
        ("50181901", "Marraqueta", "marraqueta fresca", 40, "Kilogramo"),
        ("50181901", "Pan", "tortillas del sur", 5, "Unidad"),
        ("44121500", "Lápices", "lápices grafito", 10, "Unidad"),  # no es alimento: sin línea
    ])  # fmt: skip
    _ca(
        session,
        "2-1-COT26",
        [("50192100", "Alimentos", "ALIMENTOS SEGUN ANEXO ADJUNTO", 1, "Unidad")],
        monto=500000,
    )
    _ca(
        session, "3-1-COT26", [("50181901", "Marraqueta", "marraqueta", 3, "Kilogramo")], region=5
    )  # otra región
    session.commit()

    r = cat.procesar_catalogo(session, [16], inicial=True)
    assert r["lineas_nuevas"] == 3 and r["cas"] == 2

    a, b = _lineas(session, "1-1-COT26")
    assert (a.n, a.estado, a.origen) == (1, "dudosa", "api")  # regla = 0,5
    assert a.producto_id == _producto(session, "Marraqueta").id
    assert a.cantidad == 40 and a.cantidad_base == 40  # "Kilogramo" = base kg → formato base
    assert (b.n, b.estado, b.producto_id) == (2, "pendiente", None)
    (anexo,) = _lineas(session, "2-1-COT26")
    assert (anexo.origen, anexo.estado) == ("anexo", "pendiente")
    assert _lineas(session, "3-1-COT26") == []

    ca1 = session.get(CompraAgil, "1-1-COT26")
    session.refresh(ca1)
    # Una línea de la API sin sugerencia es trabajo de curación, no «la CA no trae el detalle».
    assert ca1.detalle_lineas == "incompleto"
    assert session.get(CompraAgil, "2-1-COT26").detalle_lineas == "sin_detalle"


def test_backfill_idempotente(session):
    _ca(session, "1-1-COT26", [("50181901", "Marraqueta", "marraqueta", 40, "Kilogramo")])
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    n = session.scalar(select(func.count()).select_from(CaLinea))
    r2 = cat.procesar_catalogo(session, [16], inicial=True)
    assert session.scalar(select(func.count()).select_from(CaLinea)) == n == 1
    assert (r2["lineas_nuevas"], r2["lineas_actualizadas"], r2["lineas_quitadas"]) == (0, 0, 0)
    assert r2["productos_creados"] == 0


def test_backfill_no_pisa_una_revision_humana(session):
    _ca(session, "1-1-COT26", [("50181901", "Pan", "pan batido de campo", 10, "Unidad")])
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    (ln,) = _lineas(session, "1-1-COT26")
    assert ln.estado == "pendiente"
    hallulla = _producto(session, "Hallulla")
    cat.confirmar_linea(session, ln, producto_id=hallulla.id, formato_id=None, usuario_id=1)
    session.commit()

    # Mismo texto: la revisión sigue intacta.
    cat.procesar_catalogo(session, [16], inicial=True)
    (ln,) = _lineas(session, "1-1-COT26")
    assert (ln.estado, ln.producto_id, ln.revisado_por) == ("ok", hallulla.id, 1)

    # La CA cambia el texto de esa posición: queda dudosa, con su producto y su revisión.
    p = session.execute(select(CaProducto).where(CaProducto.ca_codigo == "1-1-COT26")).scalar_one()
    p.descripcion = "pan de otro tipo, totalmente distinto"
    p.cantidad = 12
    session.commit()
    r = cat.procesar_catalogo(session, [16], inicial=True)
    (ln,) = _lineas(session, "1-1-COT26")
    assert r["lineas_a_revisar_de_nuevo"] == 1
    assert (ln.estado, ln.producto_id, ln.revisado_por) == ("dudosa", hallulla.id, 1)
    assert ln.cantidad == 12
    assert "otro tipo" in ln.texto_origen


def test_backfill_recalcula_si_no_hubo_revision(session):
    _ca(session, "1-1-COT26", [("50181901", "Pan", "marraqueta", 10, "Kilogramo")])
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    p = session.execute(select(CaProducto).where(CaProducto.ca_codigo == "1-1-COT26")).scalar_one()
    p.descripcion = "hallulla"
    session.commit()
    r = cat.procesar_catalogo(session, [16], inicial=True)
    (ln,) = _lineas(session, "1-1-COT26")
    assert r["lineas_actualizadas"] == 1
    assert ln.producto_id == _producto(session, "Hallulla").id


def test_texto_de_la_oferta_es_segunda_fuente(session):
    """El texto pedido no dice nada; el de la oferta ("Nescafé 170 g") sí."""
    _ca(session, "1-1-COT26", [("50201706", "Producto", "según lo indicado", 5, "Unidad")])
    session.add(
        CaCotizacion(ca_codigo="1-1-COT26", llamado=1, id_cotizacion="x", rut_proveedor="1-9")
    )
    session.flush()
    cot = session.execute(select(CaCotizacion)).scalar_one()
    session.add(
        CaCotizacionProducto(
            cotizacion_id=cot.id,
            codigo_producto="50201706",
            nombre_producto="Nescafé tradicional 170 g",
            precio_unitario=4000,
        )
    )
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    (ln,) = _lineas(session, "1-1-COT26")
    assert ln.producto_id == _producto(session, "Café instantáneo").id
    assert ln.estado == "dudosa"


def test_incremental_usa_el_cursor(session):
    _ca(session, "1-1-COT26", [("50181901", "Marraqueta", "marraqueta", 40, "Kilogramo")])
    session.commit()
    assert cat.procesar_catalogo(session, [16])["cas"] == 1  # sin cursor: todas
    assert session.get(SyncState, "mercado_catalogo").cursor
    assert cat.procesar_catalogo(session, [16])["cas"] == 0  # nada cambió
    ca = session.get(CompraAgil, "1-1-COT26")
    ca.actualizado_en = datetime(2099, 1, 1)
    session.commit()
    assert cat.procesar_catalogo(session, [16])["cas"] == 1


def test_incremental_reprocesa_una_vigente_cuyo_detalle_cambio(session):
    # F-merc-3: el cursor también mira `detalle_vigente_en` (reemplazar `ca_productos` no
    # siempre mueve `actualizado_en`).
    _ca(session, "1-1-COT26", [("50181901", "Marraqueta", "marraqueta", 40, "Kilogramo")])
    session.commit()
    assert cat.procesar_catalogo(session, [16])["cas"] == 1
    assert cat.procesar_catalogo(session, [16])["cas"] == 0
    ca = session.get(CompraAgil, "1-1-COT26")
    ca.detalle_vigente_en = datetime(2099, 1, 1)
    session.commit()
    assert cat.procesar_catalogo(session, [16])["cas"] == 1


def test_confirmar_crea_alias_y_reaplica_a_las_iguales(session):
    for i in range(3):
        _ca(
            session,
            f"{i}-1-COT26",
            [("50181901", "Pan", "pan batido de campo", 10 + i, "Kilogramo")],
        )
    _ca(session, "9-1-COT26", [("50181901", "Pan", "pan batido de campo del sur", 1, "Kilogramo")])
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    assert all(
        ln.estado == "pendiente" for c in range(3) for ln in _lineas(session, f"{c}-1-COT26")
    )

    hallulla = _producto(session, "Hallulla")
    base = next(f for f in hallulla.formatos if f.factor_base == 1.0)
    (primera,) = _lineas(session, "0-1-COT26")
    otras = cat.confirmar_linea(
        session, primera, producto_id=hallulla.id, formato_id=base.id, usuario_id=7
    )
    session.commit()
    assert otras == 2  # las dos con el mismo texto; la del "del sur" es otro texto

    alias = session.execute(select(ProductoAlias)).scalar_one()
    assert (alias.texto_normalizado, alias.fuente, alias.confianza, alias.revisado_por) == (
        "pan batido de campo", "humano", 1.0, 7,
    )  # fmt: skip
    for c in (1, 2):
        (ln,) = _lineas(session, f"{c}-1-COT26")
        assert (ln.estado, ln.producto_id, ln.revisado_por) == ("ok", hallulla.id, None)
        assert ln.cantidad_base == ln.cantidad
    assert _lineas(session, "9-1-COT26")[0].estado == "pendiente"
    for c in range(3):
        assert session.get(CompraAgil, f"{c}-1-COT26").detalle_lineas == "completo"

    # Reforzar el alias suma veces_visto, no duplica.
    (otra,) = _lineas(session, "9-1-COT26")
    cat.confirmar_linea(session, otra, producto_id=hallulla.id, formato_id=None, usuario_id=7)
    session.commit()
    assert session.scalar(select(func.count()).select_from(ProductoAlias)) == 2


def test_estados_de_detalle_lineas(session):
    _ca(session, "1-1-COT26", [
        ("50181901", "A", "marraqueta", 1, "Kilogramo"),
        ("50181901", "B", "hallulla", 1, "Kilogramo"),
    ])  # fmt: skip
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    ca = session.get(CompraAgil, "1-1-COT26")
    lineas = _lineas(session, "1-1-COT26")
    assert {ln.estado for ln in lineas} == {"dudosa"}
    assert ca.detalle_lineas == "incompleto"
    for ln in lineas:
        cat.confirmar_linea(
            session,
            ln,
            producto_id=_producto(session, "Marraqueta").id,
            formato_id=None,
            usuario_id=1,
        )
    session.commit()
    session.refresh(ca)
    assert ca.detalle_lineas == "completo"
    cat.marcar_sin_detalle(session, lineas[0], 1)
    session.commit()
    session.refresh(ca)
    assert ca.detalle_lineas == "sin_detalle"
    assert (lineas[0].origen, lineas[0].estado, lineas[0].producto_id) == (
        "anexo",
        "pendiente",
        None,
    )
    # El reproceso respeta la marca humana de «sin detalle».
    cat.procesar_catalogo(session, [16], inicial=True)
    session.refresh(lineas[0])
    assert (lineas[0].origen, lineas[0].estado) == ("anexo", "pendiente")


def test_linea_manual(session):
    _ca(
        session,
        "1-1-COT26",
        [("50192100", "Alimentos", "SEGUN ANEXO ADJUNTO", 1, "Unidad")],
        monto=400000,
    )
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    ca = session.get(CompraAgil, "1-1-COT26")
    p = _producto(session, "Café instantáneo")
    f = next(f for f in p.formatos if f.nombre == "tarro 170 g")  # lo trae la semilla
    ln = cat.agregar_linea_manual(
        session, ca, producto_id=p.id, formato_id=f.id, cantidad=10, usuario_id=3
    )
    session.commit()
    assert (ln.origen, ln.estado, ln.n >= 1000) == ("manual", "ok", True)
    assert ln.cantidad_base == pytest.approx(1.7)
    # El reproceso no toca las manuales ni las revisa de nuevo.
    cat.procesar_catalogo(session, [16], inicial=True)
    session.refresh(ln)
    assert (ln.origen, ln.estado, ln.revisado_por) == ("manual", "ok", 3)
    with pytest.raises(cat.CatalogoError):
        cat.agregar_linea_manual(
            session, ca, producto_id=p.id, formato_id=None, cantidad=-1, usuario_id=3
        )
    cat.quitar_linea_manual(session, ln)
    with pytest.raises(cat.CatalogoError):
        cat.quitar_linea_manual(
            session, _lineas(session, "1-1-COT26")[0]
        )  # la de la API no se quita


def test_crear_producto_y_formato_validan(session):
    p = cat.crear_producto(session, nombre="  Quinoa  ", unidad_base="kg", usuario_id=1)
    assert p.nombre == "Quinoa" and [f.factor_base for f in p.formatos] == [1.0]
    with pytest.raises(cat.CatalogoError):
        cat.crear_producto(session, nombre="quinoa", unidad_base="kg")
    with pytest.raises(cat.CatalogoError):
        cat.crear_producto(session, nombre="Otro", unidad_base="m")
    with pytest.raises(cat.CatalogoError):
        cat.agregar_formato(session, p, "bolsa", 0)
    cat.agregar_formato(session, p, "bolsa 500 g", 0.5)
    with pytest.raises(cat.CatalogoError):
        cat.agregar_formato(session, p, "bolsa 500 g", 0.5)


def test_fusionar_mueve_lineas_alias_y_costos(session):
    cat.sembrar_canasta(session)
    origen = cat.crear_producto(session, nombre="Pan corriente 2", unidad_base="kg")
    destino = _producto(session, "Hallulla")
    cat.agregar_formato(session, origen, "bolsa 2 kg", 2.0)
    _ca(session, "1-1-COT26", [("50181901", "Pan", "pan corriente 2", 5, "Kilogramo")])
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    (ln,) = _lineas(session, "1-1-COT26")
    bolsa = next(f for f in origen.formatos if f.nombre == "bolsa 2 kg")
    cat.confirmar_linea(session, ln, producto_id=origen.id, formato_id=bolsa.id, usuario_id=1)
    session.commit()
    r = cat.fusionar_productos(session, origen.id, destino.id)
    session.commit()
    session.refresh(ln)
    assert r["lineas"] == 1 and ln.producto_id == destino.id
    assert ln.cantidad_base == 10  # 5 × 2 kg: el formato viajó al destino con su factor
    assert session.execute(select(ProductoAlias)).scalar_one().producto_id == destino.id
    assert session.get(ProductoCanonico, origen.id).activo is False
    with pytest.raises(cat.CatalogoError):
        cat.fusionar_productos(session, destino.id, destino.id)
    otro_base = cat.crear_producto(session, nombre="Bebida X", unidad_base="L")
    with pytest.raises(cat.CatalogoError):
        cat.fusionar_productos(session, otro_base.id, destino.id)


def test_proveedor_producto_acepta_fk_al_catalogo(session):
    from app.models.tables import Proveedor

    e = Empresa(nombre="E", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True)
    session.add(e)
    session.flush()
    prov = Proveedor(empresa_id=e.id, nombre="P", rubros=[], despacha=False, activo=True)
    session.add(prov)
    session.flush()
    p = cat.crear_producto(session, nombre="Quinoa", unidad_base="kg")
    from datetime import date

    pp = ProveedorProducto(
        proveedor_id=prov.id, producto_id=p.id, formato_id=p.formatos[0].id,
        producto_texto="Quinoa", precio_neto_actual=3000, vigente_desde=date(2026, 11, 1),
    )  # fmt: skip
    session.add(pp)
    session.commit()
    assert session.get(ProveedorProducto, pp.id).producto_id == p.id


# ---------------------------------------------------------------------------
# Rutas y permisos
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(engine, settings):
    return TestClient(create_app(settings, engine), raise_server_exceptions=True)


def _usuario(session, email, empresa_id=None, rol=RolUsuario.USUARIO) -> int:
    u = Usuario(
        email=email, password_hash=hash_password(_PW), rol=rol, activo=True, empresa_id=empresa_id
    )
    session.add(u)
    session.flush()
    return u.id


def _sesion(settings, user_id):
    token = create_session_token(settings.secret_key, user_id)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    return {COOKIE_NAME: token}, {
        "X-CSRF-Token": generate_csrf_token(settings.secret_key, decoded[1])
    }


@pytest.fixture()
def mundo(engine, settings):
    """Empresa activa (región 16) con un usuario, un admin sin empresa y un usuario sin empresa."""
    with Session(engine) as s:
        e = Empresa(nombre="E1", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True)
        s.add(e)
        s.flush()
        ids = {
            "con": _usuario(s, "con@t.cl", e.id),
            "admin_con": _usuario(s, "admin-con@t.cl", e.id, RolUsuario.ADMIN),
            "sin": _usuario(s, "sin@t.cl"),
            "admin_sin": _usuario(s, "admin-sin@t.cl", None, RolUsuario.ADMIN),
        }
        cat.sembrar_canasta(s)
        _ca(
            s,
            "1-1-COT26",
            [("50181901", "Pan", "pan batido de campo", 10, "Kilogramo")],
            monto=800000,
        )
        _ca(s, "5-1-COT26", [("50181901", "Pan", "pan batido", 1, "Kilogramo")], region=5)
        s.commit()
        cat.procesar_catalogo(s, [16], inicial=True)
        # CA de otra región con una línea creada a mano (no debe ser visible).
        s.add(
            CaLinea(
                ca_codigo="5-1-COT26",
                n=1,
                texto_origen="x",
                texto_norm="x",
                origen="api",
                estado="pendiente",
            )
        )
        s.commit()
        ids["linea"] = _lineas(s, "1-1-COT26")[0].id
        ids["linea_otra_region"] = _lineas(s, "5-1-COT26")[0].id
        ids["hallulla"] = _producto(s, "Hallulla").id
    return ids


def test_catalogo_exige_empresa_activa(client, settings, mundo):
    for quien in ("sin", "admin_sin"):
        c, _ = _sesion(settings, mundo[quien])
        assert client.get("/catalogo", cookies=c).status_code == 403
    assert client.get("/catalogo", follow_redirects=False).status_code in (302, 303, 307)
    c, _ = _sesion(settings, mundo["con"])
    r = client.get("/catalogo", cookies=c)
    assert r.status_code == 200 and "pan batido de campo" in r.text.lower()


@pytest.mark.parametrize("tab", ["productos", "alias"])
def test_pestanas_responden(client, settings, mundo, tab):
    c, _ = _sesion(settings, mundo["con"])
    assert client.get(f"/catalogo?tab={tab}", cookies=c).status_code == 200


def test_confirmar_por_htmx_responde_200_vacio_con_aviso(client, settings, mundo, engine):
    c, h = _sesion(settings, mundo["con"])
    r = client.post(
        f"/catalogo/lineas/{mundo['linea']}/confirmar",
        data={"producto_id": mundo["hallulla"], "formato_id": "", "vista": "pendientes"},
        cookies=c, headers={**h, "HX-Request": "true"},
    )  # fmt: skip
    assert r.status_code == 200  # nunca 204: HTMX no quitaría la fila
    assert 'id="catalogo-aviso"' in r.text and "Línea confirmada" in r.text
    assert "<tr" not in r.text
    with Session(engine) as s:
        ln = s.get(CaLinea, mundo["linea"])
        assert (ln.estado, ln.revisado_por) == ("ok", mundo["con"])


def test_confirmar_sin_csrf_o_con_producto_inexistente(client, settings, mundo, engine):
    c, h = _sesion(settings, mundo["con"])
    r = client.post(
        f"/catalogo/lineas/{mundo['linea']}/confirmar", data={"producto_id": 1}, cookies=c
    )
    assert r.status_code == 403
    r = client.post(
        f"/catalogo/lineas/{mundo['linea']}/confirmar", data={"producto_id": 99999},
        cookies=c, headers={**h, "HX-Request": "true"},
    )  # fmt: skip
    assert (
        r.status_code == 200
        and r.headers["HX-Reswap"] == "none"
        and "no encontrado" in r.text.lower()
    )
    with Session(engine) as s:
        assert s.get(CaLinea, mundo["linea"]).estado == "pendiente"


def test_linea_de_region_no_activa_es_404(client, settings, mundo):
    c, h = _sesion(settings, mundo["con"])
    r = client.post(
        f"/catalogo/lineas/{mundo['linea_otra_region']}/confirmar",
        data={"producto_id": mundo["hallulla"]}, cookies=c, headers=h,
    )  # fmt: skip
    assert r.status_code == 404
    assert client.get("/catalogo/ca/5-1-COT26", cookies=c).status_code == 404
    assert client.get("/catalogo/ca/1-1-COT26", cookies=c).status_code == 200


def test_usuario_sin_empresa_no_edita(client, settings, mundo):
    c, h = _sesion(settings, mundo["sin"])
    for url, data in (
        (f"/catalogo/lineas/{mundo['linea']}/confirmar", {"producto_id": mundo["hallulla"]}),
        (f"/catalogo/lineas/{mundo['linea']}/anexo", {}),
        ("/catalogo/productos", {"nombre": "X", "unidad_base": "kg"}),
        (f"/catalogo/productos/{mundo['hallulla']}", {"nombre": "X"}),
        ("/catalogo/ca/1-1-COT26/lineas", {"producto_id": mundo["hallulla"]}),
    ):
        assert client.post(url, data=data, cookies=c, headers=h).status_code == 403


def test_crear_producto_cualquier_usuario_con_empresa_y_fusionar_solo_admin(
    client, settings, mundo, engine
):
    c, h = _sesion(settings, mundo["con"])
    r = client.post(
        "/catalogo/productos", data={"nombre": "Quinoa", "unidad_base": "kg"},
        cookies=c, headers=h, follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303 and "/catalogo/productos/" in r.headers["location"]
    r = client.post(
        f"/catalogo/productos/{mundo['hallulla']}/fusionar", data={"destino_id": mundo["hallulla"]},
        cookies=c, headers=h,
    )  # fmt: skip
    assert r.status_code == 403
    with Session(engine) as s:
        quinoa = _producto(s, "Quinoa").id
        s.add(ProductoCanonico(nombre="Quinoa 2", unidad_base="kg", activo=True))
        s.commit()
        otro = _producto(s, "Quinoa 2").id
    c, h = _sesion(settings, mundo["admin_con"])
    r = client.post(
        f"/catalogo/productos/{otro}/fusionar", data={"destino_id": quinoa},
        cookies=c, headers=h, follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303 and "mensaje" in r.headers["location"]
    page = client.get(f"/catalogo/productos/{quinoa}", cookies=c)
    assert "Fusionar" in page.text
    c2, _ = _sesion(settings, mundo["con"])
    assert "Fusionar en" not in client.get(f"/catalogo/productos/{quinoa}", cookies=c2).text


def test_anexo_y_linea_manual_por_ruta(client, settings, mundo, engine):
    c, h = _sesion(settings, mundo["con"])
    r = client.post(
        f"/catalogo/lineas/{mundo['linea']}/anexo", data={"vista": "pendientes"},
        cookies=c, headers={**h, "HX-Request": "true"},
    )  # fmt: skip
    assert r.status_code == 200 and "<tr" not in r.text
    r = client.post(
        "/catalogo/ca/1-1-COT26/lineas",
        data={"producto_id": mundo["hallulla"], "formato_id": "", "cantidad": "12,5"},
        cookies=c, headers=h, follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303
    with Session(engine) as s:
        manual = [ln for ln in _lineas(s, "1-1-COT26") if ln.origen == "manual"]
        assert len(manual) == 1 and manual[0].cantidad == 12.5


def test_ficha_de_ca_muestra_lineas_solo_a_usuarios_con_empresa(client, settings, mundo):
    c, _ = _sesion(settings, mundo["con"])
    r = client.get("/oportunidad/compras_agiles/1-1-COT26", cookies=c)
    assert r.status_code == 200 and "ficha-panel-lineas" in r.text and "Corregir líneas" in r.text
    # Región no activa: la ficha no cambia.
    r = client.get("/oportunidad/compras_agiles/5-1-COT26", cookies=c)
    assert "ficha-panel-lineas" not in r.text
    c, _ = _sesion(settings, mundo["sin"])
    r = client.get("/oportunidad/compras_agiles/1-1-COT26", cookies=c)
    assert "ficha-panel-lineas" not in r.text


def test_buscar_producto_y_formatos_htmx(client, settings, mundo):
    c, _ = _sesion(settings, mundo["con"])
    r = client.get("/catalogo/buscar-producto?linea_id=1&q=hallulla", cookies=c)
    assert r.status_code == 200 and "Hallulla" in r.text and 'name="formato_id"' in r.text
    r = client.get(f"/catalogo/formatos?linea_id=1&producto_id={mundo['hallulla']}", cookies=c)
    assert r.status_code == 200 and "kilo" in r.text


# ---------------------------------------------------------------------------
# Job y juez
# ---------------------------------------------------------------------------


def test_job_mercado_catalogo_en_el_cli():
    from app.ingest.__main__ import _JOBS

    assert "mercado-catalogo" in _JOBS


def test_run_mercado_catalogo_usa_las_regiones_activas(engine, settings):
    from app.ingest.orchestrator import run_mercado_catalogo

    with Session(engine) as s:
        s.add(Empresa(nombre="E", regiones_prioritarias=[8], comunas_prioritarias=[], activa=True))
        _ca(s, "1-1-COT26", [("50181901", "Marraqueta", "marraqueta", 4, "Kilogramo")], region=8)
        _ca(s, "2-1-COT26", [("50181901", "Marraqueta", "marraqueta", 4, "Kilogramo")], region=16)
        s.commit()
    r = run_mercado_catalogo(settings, engine, inicial=True)
    assert r["cas"] == 1 and r["lineas_nuevas"] == 1


def _cargar_juez():
    ruta = (
        Path(__file__).resolve().parent.parent / "scripts" / "mercado" / "walk_forward_canonico.py"
    )
    spec = importlib.util.spec_from_file_location("walk_forward_canonico", ruta)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # los @dataclass del script lo necesitan
    spec.loader.exec_module(mod)
    return mod


def test_juez_walk_forward_error_y_coberturas(session):
    from datetime import datetime as dt

    juez = _cargar_juez()
    cat.sembrar_canasta(session)
    azucar = _producto(session, "Azúcar")
    base = next(f for f in azucar.formatos if f.factor_base == 1.0)
    # 3 meses previos a $1.000/kg y el mes evaluado a $1.100/kg (error 100/1.100 = 9,1 %).
    for i, (mes, precio) in enumerate([(7, 1000), (8, 1000), (9, 1000), (10, 1100)]):
        codigo = f"{i}-9-COT26"
        session.add(
            CompraAgil(codigo=codigo, nombre="x", region=16, monto_disponible_clp=100000,
                       fecha_publicacion=dt(2026, mes, 10), unidad_compra="Municipalidad de Chillán")
        )  # fmt: skip
        session.add(CaLinea(ca_codigo=codigo, n=1, producto_id=azucar.id, formato_id=base.id, cantidad=10,
                            cantidad_base=10, texto_origen="azucar", texto_norm="azucar", codigo_unspsc="50161509",
                            origen="api", estado="ok"))  # fmt: skip
        session.flush()
        cot = CaCotizacion(
            ca_codigo=codigo, llamado=1, id_cotizacion="c", rut_proveedor="1-9", seleccionado=True
        )
        session.add(cot)
        session.flush()
        session.add(
            CaCotizacionProducto(
                cotizacion_id=cot.id,
                codigo_producto="50161509",
                nombre_producto="azucar",
                precio_unitario=precio,
            )
        )
    # Una CA con anexo y otra con línea pendiente: entran a (b) y (a) solo la pendiente.
    session.add(
        CompraAgil(
            codigo="A-1",
            nombre="x",
            region=16,
            monto_disponible_clp=100000,
            fecha_publicacion=dt(2026, 10, 5),
        )
    )
    session.add(
        CaLinea(
            ca_codigo="A-1",
            n=1,
            texto_origen="segun anexo",
            texto_norm="segun anexo",
            origen="anexo",
            estado="pendiente",
        )
    )
    session.commit()

    texto = juez.informe(session, [16], None, hoy=datetime(2026, 11, 4).date())
    assert "Mes evaluado: 2026-10" in texto
    assert "Error mediano: 9.1 %" in texto
    assert "Dentro de ±20 %: 100.0 %" in texto
    # (a): 4 líneas ok de 4 elegibles = 100 %; (b): 4 ok de 5 líneas (200.000 + ... ) → 80 %.
    assert "(a) Cobertura con detalle propio: 100.0 %" in texto
    assert "(b) Cobertura total:              80.0 %" in texto
    assert "CUMPLE" in texto


def test_bandera_sin_detalle_es_solo_por_anexo_o_sin_lineas(session):
    _ca(session, "1-1-COT26", [("50181901", "Pan", "pan batido de campo", 10, "Kilogramo")])
    _ca(session, "2-1-COT26", [("44121500", "Lápices", "lápices", 10, "Unidad")])  # sin alimentos
    _ca(
        session,
        "3-1-COT26",
        [("50192100", "Alimentos", "SEGUN ANEXO ADJUNTO", 1, "Unidad")],
        monto=400000,
    )
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    (ln,) = _lineas(session, "1-1-COT26")
    assert (ln.origen, ln.estado, ln.producto_id) == ("api", "pendiente", None)
    por_ca = {c.codigo: c.detalle_lineas for c in session.execute(select(CompraAgil)).scalars()}
    assert por_ca["1-1-COT26"] == "incompleto"  # línea api sin sugerencia
    assert por_ca["2-1-COT26"] == "sin_detalle"  # ninguna línea
    assert por_ca["3-1-COT26"] == "sin_detalle"  # anexo sin resolver
    # Resolver el anexo (confirmarlo) saca a la CA de «sin detalle».
    (anexo,) = _lineas(session, "3-1-COT26")
    cat.confirmar_linea(
        session, anexo, producto_id=_producto(session, "Hallulla").id, formato_id=None, usuario_id=1
    )
    session.commit()
    session.expire_all()
    assert session.get(CompraAgil, "3-1-COT26").detalle_lineas == "completo"
