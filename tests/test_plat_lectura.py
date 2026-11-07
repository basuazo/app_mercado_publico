# ruff: noqa: F811
"""F-plat-lectura: juez corregido de la evaluación, OCR basura como imagen y volver a leer.

Textos sintéticos: los anexos reales viven en `data/` (fuera de git). Red mockeada (respx).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.types import LineaExtraida
from app.mercado import anexos
from app.models.tables import CaAnexo
from tests.pdf_minimo import pdf_texto as _pdf_texto
from tests.test_plat2 import _DOS_LINEAS, _URL_OR, _lineas, _respuesta
from tests.test_plat_anexos2 import (  # noqa: F401  (fixtures de pytest)
    _caso_d,
    _xlsx_con,
    client,
    cliente,
    engine,
    mundo,
    session,
    settings,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "mercado"))
import evaluar_extraccion as ev  # noqa: E402


def _esp(producto: str, cantidad: float | None, unidad: str) -> ev.Esperada:
    return ev.Esperada(
        producto=producto, cantidad=cantidad, unidad=ev.unidad_norm(unidad), no_alimento=False
    )


def _par(esp: ev.Esperada, texto: str, cantidad: float | None, unidad: str) -> tuple[bool, bool]:
    """(estricto, corregido) para una esperada y una extraída."""
    ext = [(texto, cantidad, unidad)]
    return ev.emparejar([esp], ext, corregido=False)[0], ev.emparejar([esp], ext)[0]


# ---------------------------------------------------------------------------
# §1 Juez corregido: una prueba por clase de falla de la medición del 6-nov
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unidad", ["botella", "caja", "tarro", "bolsa", "paquete", "frasco"])
def test_juez_unidad_de_envase_equivale_a_unidad(unidad):
    e = _esp("Aceite vegetal 1 L", 6, "unidad")
    estricto, corregido = _par(e, "Aceite vegetal 1 L", 6, unidad)
    assert not estricto and corregido


def test_juez_envase_distinto_de_medida_sigue_siendo_error():
    e = _esp("Aceite vegetal", 6, "unidad")
    assert _par(e, "Aceite vegetal", 6, "kg") == (False, False)


def test_juez_cruce_medida_envase_exige_que_la_cantidad_cuadre_con_el_formato():
    # Formato 1: «Azúcar 1 kg», 10 kg vs 10 unidades (y al revés) → acierto.
    assert _par(_esp("Azúcar 1 kg", 10, "unidad"), "Azúcar 1 kg", 10, "kg") == (False, True)
    assert _par(_esp("Azúcar 1 kg", 10, "kg"), "Azúcar 1 kg", 10, "unidad") == (False, True)
    # Cantidad × formato: «Arroz 5 kg», 50 kg vs 10 unidades → acierto.
    assert _par(_esp("Arroz 5 kg", 50, "kg"), "Arroz 5 kg", 10, "unidad") == (False, True)
    # Errores reales que el juez anterior daba por buenos.
    assert _par(_esp("Arroz 5 kg", 10, "kg"), "Arroz 5 kg", 10, "unidad") == (False, False)
    assert _par(_esp("Jugo 200 ml", 48, "unidad"), "Jugo 200 ml", 48, "L") == (False, False)
    # «Jugo 200 ml» 48 unidades = 9,6 L.
    assert _par(_esp("Jugo 200 ml", 48, "unidad"), "Jugo 200 ml", 9.6, "L") == (False, True)
    # Sin formato en ninguno de los dos textos, no hay de dónde sacar que «unidad» era kg.
    assert _par(_esp("Azúcar", 10, "kg"), "Azúcar", 10, "unidad") == (False, False)


@pytest.mark.parametrize(
    ("esperado", "extraido"),
    [
        ("Manjar 1 kg", "Manjar 1k"),
        ("Leche 300 ml", "Leche 300cc"),
        ("Azúcar 1 kg", "Azúcar 1kg"),
        ("Aceite 1 L", "Aceite 1lt"),
        ("Galletas surtidas", "Galleta surtida"),
        ("Pan c/u", "Pan"),
    ],
)
def test_juez_normaliza_el_texto(esperado, extraido):
    e = _esp(esperado, 5, "unidad")
    estricto, corregido = _par(e, extraido, 5, "unidad")
    assert corregido
    if esperado != "Pan c/u":  # «c/u» ya pasaba por la normalización del estricto
        assert not estricto


def test_juez_normaliza_el_texto_pegado():
    assert ev.normalizar_texto_juez("Manjar 1k") == ev.normalizar_texto_juez("Manjar 1 kg")
    assert ev.normalizar_texto_juez("300cc") == ev.normalizar_texto_juez("300 ml")
    assert ev.normalizar_texto_juez("Aceite 1lt") == ev.normalizar_texto_juez("Aceite 1 L")
    assert "c/u" not in ev.normalizar_texto_juez("Pan c/u")


def test_juez_cantidad_convertida_g_kg_y_ml_l():
    e = _esp("Harina", 0.75, "kg")
    assert _par(e, "Harina", 750, "g") == (False, True)
    e2 = _esp("Leche", 750, "g")
    assert _par(e2, "Leche", 0.75, "kg") == (False, True)
    e3 = _esp("Jugo", 1.5, "L")
    assert _par(e3, "Jugo", 1500, "ml") == (False, True)
    # Otra cantidad, aunque la unidad sea de la misma familia, sigue mal.
    assert _par(_esp("Harina", 0.75, "kg"), "Harina", 700, "g") == (False, False)


def test_juez_no_perdona_un_error_real():
    e = _esp("Arroz grado 1", 10, "kg")
    assert _par(e, "Fideos espagueti", 10, "kg") == (False, False)  # otro producto
    assert _par(e, "Arroz grado 1", 12, "kg") == (False, False)  # otra cantidad


def test_juez_corregido_es_el_default_de_emparejar_indices():
    e = _esp("Aceite 1 L", 6, "unidad")
    assert ev.emparejar_indices([e], [("Aceite 1 L", 6, "botella")]) == [0]
    assert ev.emparejar_indices([e], [("Aceite 1 L", 6, "botella")], corregido=False) == [None]


def test_informe_muestra_estricto_corregido_y_referencia():
    r = ev.ResumenModelo(modelo="m/a")
    r.anexos.append(
        ev.ResultadoAnexo(
            archivo="a.pdf",
            tipo="escaneado",
            esperadas=10,
            correctas=10,
            esperadas_alim=10,
            correctas_alim=10,
            costo=0.01,
            latencia=1.0,
            est_correctas=6,
            est_correctas_alim=6,
        )  # fmt: skip
    )
    assert r.pct() == 100.0 and r.pct(estricto=True) == 60.0
    out = ev.informe([r])
    assert "claude-cowork (6-nov, manual)" in out and "98,0 % sin unidad" in out
    assert "no es el juez corregido" in out
    assert "GANADOR: m/a" in out  # el criterio usa el corregido (100 %), no el estricto (60 %)


# ---------------------------------------------------------------------------
# §2 OCR basura → imagen
# ---------------------------------------------------------------------------

_BUENO = (
    "[página 1]\nREQUERIMIENTO DE ALIMENTOS PARA EL PROGRAMA DE ADULTO MAYOR\n"
    "Se requieren los siguientes productos para la actividad del mes de septiembre\n"
    "10 kg de azúcar granulada, 5 litros de aceite vegetal, 20 paquetes de galletas surtidas, "
    "12 unidades de té en cajas y 8 bolsas de pan de molde para la entrega en la sede comunal. "
    "Los productos deben venir sellados y con fecha de vencimiento vigente a la entrega."
)
_BASURA_LETRAS = "[página 1]\n" + " ".join(
    [
        "5",
        "1",
        "0",
        "5",
        "U",
        "U",
        "n",
        "n",
        "i",
        "i",
        "d",
        "d",
        "a",
        "a",
        "d",
        "d",
        "e",
        "e",
        "s",
        "s",
    ]
    * 4
)
_BASURA_SIMBOLOS = (
    "[página 1]\n'\nI\nI\nA\nr\nT\nREPUBLICA DE CI]ILE\nI\nB\nl\nnseroN os ]uBr,s\n"
    + "03 Ki]-os gueso nantecoso en tlozo 45.000 o4/09/2026\n"
    + "o2 Kilos Sa].ane ]-aminado ahumado 24.OOO o4/o9/2026\n"
    + "gueso urrtab].e igua]- o simi].ar a ]a mue]-tra solicit!da en ]os !tems !ndicados\n"
    + "FORMU],ARIO UNICO ORDEN DE PEDIDO INTERNO NO s]n va]or comerc]a] ]ncluido en e] pedido\n"
)


def test_basura_detecta_ocr_malo():
    assert anexos.texto_es_basura(_BASURA_LETRAS)
    assert anexos.texto_es_basura(_BASURA_SIMBOLOS)


@pytest.mark.parametrize(
    "fila",
    [
        "Leche entera 1 L | {n} | unidades",
        "Detergente líquido 3 L | {n} | UN",
        "Servilletas paquete x 100 | {n} | PQT",
        "Arroz grado 1 | {n} | KGS",
        "Aceite vegetal | {n} | LTS",
        "Proveedor RUT {n} SKU {n} | {n} | UND",
    ],
)
def test_basura_no_marca_tablas_normales_con_unidades_y_siglas(fila):
    filas = [fila.format(n=i) for i in range(1, 26)]
    tabla = "\n".join(["Descripción | Cantidad | Unidad", *filas])
    assert not anexos.texto_es_basura(tabla)


def test_basura_marca_pares_repetidos_del_ocr_malo():
    assert anexos.texto_es_basura("5 1 0 5 " + "U U n n i i d d a a d d e e s s " * 6)


def test_basura_no_marca_texto_bueno_ni_corto():
    assert not anexos.texto_es_basura(_BUENO)
    assert not anexos.texto_es_basura("")
    assert not anexos.texto_es_basura("5 1 0 U U n")  # muy corto para juzgarlo
    # Una tabla de cotización normal: muchos números, pocas letras sueltas.
    tabla = "\n".join(
        f"{i} Arroz grado 1 saco 25 kg | {i * 10} | $ {i * 1000}" for i in range(1, 12)
    )
    assert not anexos.texto_es_basura(tabla)


def test_pdf_con_capa_basura_va_como_imagen():
    pdf = _pdf_texto(_BASURA_SIMBOLOS * 3)
    res = anexos.extraer_texto(pdf, anexos.PDF, render=lambda _p: b"PNG-FALSO")
    assert res.escaneado and res.imagenes == [(b"PNG-FALSO", anexos.PNG)]
    assert res.texto == ""  # la capa mala no se le pasa al modelo


def test_pdf_con_texto_bueno_sigue_como_texto():
    pdf = _pdf_texto(_BUENO * 3)
    res = anexos.extraer_texto(pdf, anexos.PDF, render=lambda _p: b"PNG-FALSO")
    assert (
        not res.escaneado and not res.imagenes and "azucar" in res.texto.lower().replace("ú", "u")
    )


def test_pdf_basura_sin_poder_renderizar_conserva_la_capa():
    pdf = _pdf_texto(_BASURA_SIMBOLOS * 3)

    def falla(_p):
        raise RuntimeError("sin render")

    res = anexos.extraer_texto(pdf, anexos.PDF, render=falla)
    assert res.texto and not res.imagenes and res.error is None


# ---------------------------------------------------------------------------
# §3 Volver a leer
# ---------------------------------------------------------------------------


@respx.mock
def test_relectura_de_sin_presupuesto_se_procesa_en_la_misma_fila(session, cliente):
    ca = _caso_d(session)
    data = _xlsx_con("arroz")
    primero = anexos.procesar_anexo_ca(session, ca, data, "a.xlsx", usuario_id=1, extractor=None)
    session.commit()
    assert primero.anexo.extraccion_estado == "sin_presupuesto" and not primero.lineas
    assert anexos.anexo_ya_leido(session, ca.codigo, primero.anexo.sha256) is None
    ruta = respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    segundo = anexos.procesar_anexo_ca(session, ca, data, "a.xlsx", usuario_id=2, extractor=cliente)
    session.commit()
    assert not segundo.duplicado and ruta.call_count == 1
    assert segundo.anexo.id == primero.anexo.id
    assert len(list(session.execute(select(CaAnexo)).scalars())) == 1
    (fila,) = session.execute(select(CaAnexo)).scalars()
    assert fila.extraccion_estado == "hecha" and fila.extraccion_modelo
    assert fila.subido_por == 2
    nuevas = [ln for ln in _lineas(session) if ln.anexo_id == fila.id]
    assert len(nuevas) == 2 and {ln.estado for ln in nuevas} == {"dudosa"}


@respx.mock
def test_relectura_de_error_se_procesa(session, cliente):
    ca = _caso_d(session)
    data = _xlsx_con("arroz")
    respx.post(_URL_OR).mock(return_value=_respuesta("esto no es json"))
    primero = anexos.procesar_anexo_ca(
        session, ca, data, "a.xlsx", usuario_id=None, extractor=cliente
    )
    session.commit()
    assert primero.anexo.extraccion_estado == "error"
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    segundo = anexos.procesar_anexo_ca(
        session, ca, data, "a.xlsx", usuario_id=None, extractor=cliente
    )
    session.commit()
    assert segundo.anexo.id == primero.anexo.id and segundo.anexo.extraccion_estado == "hecha"
    assert len(segundo.lineas) == 2


@respx.mock
def test_duplicado_hecha_sigue_saliendo_ya_existia(session, cliente):
    ca = _caso_d(session)
    data = _xlsx_con("arroz")
    ruta = respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    anexos.procesar_anexo_ca(session, ca, data, "a.xlsx", usuario_id=None, extractor=cliente)
    session.commit()
    otra = anexos.procesar_anexo_ca(session, ca, data, "a.xlsx", usuario_id=None, extractor=cliente)
    assert otra.duplicado and ruta.call_count == 1
    assert anexos.anexo_ya_leido(session, ca.codigo, otra.anexo.sha256) is not None


@respx.mock
def test_relectura_sin_presupuesto_otra_vez_no_duplica(session):
    ca = _caso_d(session)
    data = _xlsx_con("arroz")
    for _ in range(2):
        r = anexos.procesar_anexo_ca(session, ca, data, "a.xlsx", usuario_id=None, extractor=None)
        session.commit()
        assert r.anexo.extraccion_estado == "sin_presupuesto" and not r.duplicado
    assert len(list(session.execute(select(CaAnexo)).scalars())) == 1


@respx.mock
def test_relectura_concurrente_no_duplica_lineas_y_conserva_las_ok(session, cliente):
    ca = _caso_d(session)
    data = _xlsx_con("arroz")
    primero = anexos.procesar_anexo_ca(session, ca, data, "a.xlsx", usuario_id=7, extractor=None)
    session.commit()
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    segundo = anexos.procesar_anexo_ca(session, ca, data, "a.xlsx", usuario_id=None, extractor=cliente)
    session.commit()
    assert segundo.anexo.id == primero.anexo.id and len(segundo.lineas) == 2
    assert segundo.anexo.subido_por == 7  # sin usuario en la relectura se conserva el original
    # Carrera: otra lectura del mismo anexo dejó sus líneas `dudosa` antes de que esta termine.
    ok = segundo.lineas[0]
    ok.estado = "ok"
    session.commit()
    anexos.crear_lineas_de_anexo(
        session, ca, segundo.anexo, [LineaExtraida(texto="Fideos", cantidad=2, unidad="unidad")]
    )
    session.commit()
    del_anexo = [ln for ln in _lineas(session) if ln.anexo_id == segundo.anexo.id]
    assert len(del_anexo) == 2  # la `ok` + la nueva; la `dudosa` anterior se reemplazó
    assert ok.id in {ln.id for ln in del_anexo}
    assert sum(1 for ln in del_anexo if ln.estado == "dudosa") == 1


def _ficha(engine, settings, mundo, anexo: CaAnexo) -> str:
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from tests.test_plat2 import _sesion

    with Session(engine) as s:
        anexo.ca_codigo = "1-1-COT26"
        s.add(anexo)
        s.commit()
    cliente_web = TestClient(create_app(settings, engine))
    c, _h = _sesion(settings, mundo["con"])
    return cliente_web.get("/catalogo/ca/1-1-COT26", cookies=c).text


def _anexo(**kw) -> CaAnexo:
    base = dict(
        nombre_original="x.pdf", mime="application/pdf", tamano=10, sha256="a" * 64,
        escaneado=False, extraccion_estado="sin_presupuesto", fuente="manual",
    )  # fmt: skip
    base.update(kw)
    return CaAnexo(**base)


def test_ficha_invita_a_releer_solo_si_tiene_sentido(client, settings, mundo, engine):
    aviso = "Vuelve a subir el archivo para leerlo"
    leido = _anexo(paginas=2, texto_extraido="texto")
    assert aviso in _ficha(engine, settings, mundo, leido)


def test_ficha_no_invita_a_releer_un_archivo_rechazado(client, settings, mundo, engine):
    rechazado = _anexo(mime="desconocido", extraccion_estado="error", sha256="b" * 64)
    assert "Vuelve a subir el archivo" not in _ficha(engine, settings, mundo, rechazado)
    vacio = _anexo(extraccion_estado="error", sha256="c" * 64)  # sin páginas ni texto
    assert "Vuelve a subir el archivo" not in _ficha(engine, settings, mundo, vacio)
