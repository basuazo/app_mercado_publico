"""Cotización formal de una oferta (F-plat-seguimiento §1.3): número correlativo y documento DOCX.

- El número `COT-{año}-{n}` se asigna la primera vez que se genera el documento, con `FOR UPDATE`
  sobre la fila de la empresa (`empresas.ultimo_correlativo`): dos personas que lo piden a la vez
  no obtienen el mismo número. El correlativo es de la empresa y NO reinicia con el año.
- El DOCX se arma en memoria con `python-docx` (ya en el stack) y se devuelve; no se guarda
  (regla 11). Las mismas filas que el XLSX (`oferta.filas_cotizacion`).
- Generar el documento NO marca cumplido el requisito «Cotización formal adjunta»: adjuntarla en
  el portal lo hace una persona.
Fuente de los datos de la CA: Dirección ChileCompra.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tiempo import TZ_CHILE, ahora_utc
from app.mercado import oferta as of
from app.models.tables import CompraAgil, Empresa, OfertaPropia

IVA = 0.19
VALIDEZ_MAX_DIAS = 365
CONDICIONES_MAX = 4000
FUENTE = "Fuente de los datos de la Compra Ágil: Dirección ChileCompra"


class ErrorCotizacion(ValueError):
    """Entrada inválida (→ 422)."""


def configurar(
    oferta: OfertaPropia, validez: str | int | None, condiciones: str | None
) -> OfertaPropia:
    """Validez y condiciones de ESTA oferta (vacío = no cambia / vuelve al default de la empresa)."""
    if validez not in (None, ""):
        try:
            dias = int(str(validez).strip())
        except ValueError:
            raise ErrorCotizacion("La validez debe ser un número de días") from None
        if not 1 <= dias <= VALIDEZ_MAX_DIAS:
            raise ErrorCotizacion(f"La validez es de 1 a {VALIDEZ_MAX_DIAS} días")
        oferta.validez_dias = dias
    if condiciones is not None:
        c = condiciones.strip()
        if len(c) > CONDICIONES_MAX:
            raise ErrorCotizacion(f"Las condiciones admiten hasta {CONDICIONES_MAX} caracteres")
        oferta.condiciones = c or None
    return oferta


def asignar_numero(session: Session, empresa_id: int, oferta: OfertaPropia) -> str:
    """El número de la cotización; lo crea la primera vez. No hace commit.

    Orden de bloqueo: la fila de la empresa y luego la oferta (se relee tras el bloqueo: si otra
    sesión la numeró entre medio, se devuelve ese número y no se gasta otro correlativo)."""
    if oferta.numero_cotizacion:
        return str(oferta.numero_cotizacion)
    empresa = session.execute(
        select(Empresa).where(Empresa.id == empresa_id).with_for_update()
    ).scalar_one()
    session.refresh(oferta)
    if oferta.numero_cotizacion:
        return str(oferta.numero_cotizacion)
    empresa.ultimo_correlativo = (empresa.ultimo_correlativo or 0) + 1
    numero = f"COT-{datetime.now(TZ_CHILE).year}-{empresa.ultimo_correlativo}"
    oferta.numero_cotizacion = numero
    session.flush()
    return numero


def totales(filas: list[of.FilaCotizacion]) -> tuple[int, int, int]:
    """(neto, IVA 19 %, bruto) en pesos enteros: se redondea el IVA del total, no el de cada línea."""
    neto = round(sum(f.total or 0.0 for f in filas))
    iva = round(neto * IVA)
    return neto, iva, neto + iva


def _clp(valor: float | None) -> str:
    if valor is None:
        return "—"
    return "$" + f"{round(valor):,}".replace(",", ".")


def _cantidad(valor: float | None) -> str:
    if valor is None:
        return "—"
    return f"{valor:g}".replace(".", ",")


def condiciones_de(oferta: OfertaPropia, empresa: Empresa) -> str:
    return (oferta.condiciones or empresa.condiciones_cotizacion or "").strip()


def docx(
    empresa: Empresa,
    ca: CompraAgil,
    oferta: OfertaPropia,
    lineas: list[of.LineaOferta],
    *,
    fecha: datetime | None = None,
) -> bytes:
    """El documento formal: membrete de la empresa, número, fecha, validez, destinatario, referencia
    (código y nombre de la CA), tabla de líneas, totales neto / IVA / bruto, plazo, condiciones y
    la fuente. Requiere `oferta.numero_cotizacion` (ver `asignar_numero`)."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    filas = of.filas_cotizacion(lineas)
    neto, iva, bruto = totales(filas)
    hoy = (fecha or datetime.now(TZ_CHILE)).strftime("%d-%m-%Y")

    d: Any = Document()
    estilo = d.styles["Normal"]
    estilo.font.name = "Calibri"
    estilo.font.size = Pt(10.5)

    # Membrete.
    p = d.add_paragraph()
    r = p.add_run(empresa.nombre)
    r.bold = True
    r.font.size = Pt(16)
    for etiqueta, valor in (
        ("RUT", empresa.rut),
        ("Giro", empresa.giro),
        ("Dirección", empresa.direccion),
        ("Contacto", empresa.contacto),
    ):
        if valor:
            d.add_paragraph(f"{etiqueta}: {valor}").paragraph_format.space_after = Pt(0)

    t = d.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tr = t.add_run(f"COTIZACIÓN N° {oferta.numero_cotizacion}")
    tr.bold = True
    tr.font.size = Pt(14)

    d.add_paragraph(f"Fecha: {hoy}").paragraph_format.space_after = Pt(0)
    d.add_paragraph(f"Validez de la oferta: {oferta.validez_dias} días").paragraph_format.space_after = Pt(0)
    d.add_paragraph(f"Señores: {ca.organismo_nombre or '—'}").paragraph_format.space_after = Pt(0)
    if ca.unidad_compra:
        d.add_paragraph(f"Unidad de compra: {ca.unidad_compra}").paragraph_format.space_after = Pt(0)
    d.add_paragraph(f"Referencia: Compra Ágil {ca.codigo} — {ca.nombre or ''}")

    tabla = d.add_table(rows=1, cols=6)
    tabla.style = "Table Grid"
    for celda, texto in zip(
        tabla.rows[0].cells,
        ("N°", "Producto", "Formato", "Cantidad", "Precio neto unitario", "Total neto"),
        strict=True,
    ):
        celda.text = ""
        celda.paragraphs[0].add_run(texto).bold = True
    for f in filas:
        c = tabla.add_row().cells
        c[0].text = str(f.n)
        c[1].text = f.producto
        c[2].text = f.formato or ""
        c[3].text = _cantidad(f.cantidad)
        c[4].text = _clp(f.precio_unitario)
        c[5].text = _clp(f.total)

    d.add_paragraph()
    totales_doc: tuple[tuple[str, float], ...] = (
        ("Total neto", neto), ("IVA 19 %", iva), ("Total bruto", bruto),
    )
    for rotulo, importe in totales_doc:
        q = d.add_paragraph()
        q.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        q.paragraph_format.space_after = Pt(0)
        rr = q.add_run(f"{rotulo}: {_clp(importe)}")
        rr.bold = rotulo == "Total bruto"

    d.add_paragraph()
    plazo = oferta.plazo_entrega_dias
    d.add_paragraph(
        "Plazo de entrega: " + (f"{plazo} días corridos" if plazo is not None else "No indicado")
    )
    cond = condiciones_de(oferta, empresa)
    if cond:
        d.add_paragraph().add_run("Condiciones").bold = True
        for linea in cond.splitlines():
            if linea.strip():
                d.add_paragraph(linea.strip())

    pie = d.add_paragraph()
    pie.paragraph_format.space_before = Pt(18)
    pr = pie.add_run(FUENTE)
    pr.italic = True
    pr.font.size = Pt(8.5)

    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def marcar_generada(oferta: OfertaPropia) -> None:
    oferta.documento_generado_en = ahora_utc()
