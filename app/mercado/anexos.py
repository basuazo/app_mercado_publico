"""Anexos de Compra Ágil y listas de precios → líneas (F-plat-2).

Dos capas:

- Lectura pura (sin base ni red): :func:`detectar_mime` (firma del archivo, nunca la extensión)
  y :func:`extraer_texto` (pdfplumber con tablas, openpyxl, python-docx; un PDF sin capa de
  texto se renderiza a PNG para el modelo con visión). Nunca lanza por un archivo roto: lo
  informa en `ResultadoTexto.error` (regla 6).
- Servicio (:func:`procesar_anexo_ca`): guarda la fila `ca_anexos` (SIN el binario: reglas 10 y
  11), pide la extracción a OpenRouter y deja las líneas `dudosa` para que las confirme una
  persona. Flush, sin commit: confirma quien llama.

El flujo manual nunca depende del modelo: sin clave, sin modelo o sin presupuesto, el anexo
queda `sin_presupuesto` y la persona agrega las filas a mano.

F-plat-anexos-2: cada anexo registra su `fuente` y la empresa que lo subió; el mismo archivo en
la misma CA (sha256) se procesa una sola vez. Asociación por nombre para la carga masiva
(:func:`codigo_de_nombre`) y lectura de un ZIP miembro a miembro con guardas
(:func:`miembros_zip`, :func:`leer_miembro`).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import stat
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.catalogos.canonico import (
    LARGO_TEXTO_ORIGEN,
    Catalogo,
    formato_de_texto,
    normalizar_texto,
    sugerir_producto,
)
from app.clients.types import ExtraccionAnexo, LineaExtraida
from app.core.tiempo import ahora_utc
from app.models.tables import CaAnexo, CaLinea, CompraAgil

_log = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024  # 10 MB por anexo
MAX_TEXTO = 50_000  # ca_anexos.texto_extraido
MAX_PAGINAS_TEXTO = 40  # páginas de un PDF con texto que se leen (tiempo y RAM de la web)
MIN_CHARS_POR_PAGINA = 50  # bajo esto, el PDF se trata como escaneado
RESOLUCION_RENDER = 150  # dpi máximos de una página renderizada
MAX_LADO_PX = 2000  # lado mayor de la imagen de una página (RAM de Render y tokens de imagen)
# DOCX/XLSX son ZIP: un archivo de 10 MB puede expandirse a GB al abrirlo (Render: 512 MB).
MAX_ZIP_EXPANDIDO = 50 * 1024 * 1024
MAX_RAZON_COMPRESION = 100
_MAX_FILAS_HOJA = 5000

PDF = "application/pdf"
PNG = "image/png"
JPEG = "image/jpeg"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIMES_ACEPTADOS = (PDF, PNG, JPEG, DOCX, XLSX)
ETIQUETA_MIME = {PDF: "PDF", PNG: "imagen PNG", JPEG: "imagen JPG", DOCX: "Word", XLSX: "Excel"}


FUENTES = ("manual", "masiva", "api_externa", "api_mp", "descarga_directa")

# Carga masiva (F-plat-anexos-2 §2.2): un ZIP de hasta 50 MB; se rechaza entero si un miembro
# comprime más de 100:1 o el total expandido pasa de 200 MB (solo se lee el directorio).
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_ZIP_LOTE_EXPANDIDO = 200 * 1024 * 1024
MAX_MIEMBROS_ZIP = 200
LARGO_NOMBRE = 200  # `nombre_original` de los archivos de la carga masiva y del endpoint


class AnexoInvalido(ValueError):
    """El archivo no se acepta (vacío, muy grande o de un tipo no soportado). La ruta lo muestra."""


# ---------------------------------------------------------------------------
# Firma del archivo
# ---------------------------------------------------------------------------


def detectar_mime(data: bytes) -> str | None:
    """Tipo real por la firma (magic bytes). DOCX y XLSX son ZIP: se distinguen por su
    contenido. `.xls` (formato viejo), `.doc`, ejecutables y el resto → None."""
    if data.startswith(b"%PDF-"):
        return PDF
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                nombres = set(z.namelist())
        except (zipfile.BadZipFile, ValueError, OSError):
            return None
        if "[Content_Types].xml" not in nombres:
            return None
        if any(n.startswith("word/") for n in nombres):
            return DOCX
        if any(n.startswith("xl/") for n in nombres):
            return XLSX
    return None


def verificar_zip(data: bytes) -> None:
    """:class:`AnexoInvalido` si el ZIP (DOCX/XLSX) se expande más de `MAX_ZIP_EXPANDIDO` en
    total o algún miembro comprime más de `MAX_RAZON_COMPRESION` veces (bomba de compresión).
    Lee solo el directorio central: no descomprime nada."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise AnexoInvalido("El archivo está dañado") from exc
    if sum(i.file_size for i in infos) > MAX_ZIP_EXPANDIDO:
        raise AnexoInvalido(
            f"El archivo se expande a más de {MAX_ZIP_EXPANDIDO // (1024 * 1024)} MB al abrirlo"
        )
    for i in infos:
        if i.file_size and i.file_size > MAX_RAZON_COMPRESION * max(i.compress_size, 1):
            raise AnexoInvalido("El archivo tiene un contenido comprimido sospechoso")


def validar_archivo(data: bytes) -> str:
    """MIME del archivo o :class:`AnexoInvalido` con el motivo."""
    if not data:
        raise AnexoInvalido("El archivo está vacío")
    if len(data) > MAX_BYTES:
        raise AnexoInvalido(f"El archivo supera {MAX_BYTES // (1024 * 1024)} MB")
    mime = detectar_mime(data)
    if mime is None:
        raise AnexoInvalido(
            "Tipo de archivo no aceptado: sube PDF, Excel (.xlsx), Word (.docx), PNG o JPG. "
            "Un .xls antiguo se puede guardar como .xlsx."
        )
    if mime in (DOCX, XLSX):
        verificar_zip(data)
    return mime


# ---------------------------------------------------------------------------
# Carga masiva: asociación por nombre y ZIP miembro a miembro (F-plat-anexos-2 §2.2)
# ---------------------------------------------------------------------------

_PATRON_CODIGO = re.compile(r"\s*(\d+-\d+-COT\d+)", re.IGNORECASE)
_PATRON_CODIGO_LIBRE = re.compile(r"^[A-Za-z0-9-]{3,50}$")


def nombre_base(nombre: str) -> str:
    """Último componente de la ruta (un miembro de ZIP trae carpetas), recortado a 200."""
    base = (nombre or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return base[:LARGO_NOMBRE]


def codigo_de_nombre(nombre: str) -> str | None:
    """Código de la CA a la que va el archivo, por su nombre: lo que va antes de `__`
    (`1234-56-COT26__Anexo 1.pdf`) o, sin `__`, el patrón de código de CA al inicio. None si no
    hay uno reconocible (la vista lo muestra «no asociado»; nunca rompe el lote, regla 6)."""
    base = nombre_base(nombre)
    if "__" in base:
        cand = base.split("__", 1)[0].strip()
        return cand.upper() if _PATRON_CODIGO_LIBRE.fullmatch(cand) else None
    m = _PATRON_CODIGO.match(base)
    return m.group(1).upper() if m else None


@dataclass(frozen=True)
class MiembroZip:
    indice: int  # posición en el directorio del ZIP
    nombre: str  # nombre base (sin carpetas), recortado
    tamano: int  # expandido
    motivo: str | None = None  # por qué no se procesa (muy grande, enlace, ruta rara)


def _es_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def miembros_zip(z: zipfile.ZipFile) -> list[MiembroZip]:
    """Miembros procesables de un ZIP ya abierto. Lee SOLO el directorio central (no
    descomprime): :class:`AnexoInvalido` si algún miembro comprime más de
    `MAX_RAZON_COMPRESION` veces, si el total expandido pasa de `MAX_ZIP_LOTE_EXPANDIDO` o si
    trae más de `MAX_MIEMBROS_ZIP` archivos. Las carpetas y los metadatos de macOS no cuentan;
    los enlaces simbólicos, las rutas con `..` o absolutas y los de más de 10 MB quedan con
    `motivo` (no se leen)."""
    infos = z.infolist()
    archivos = [(i, info) for i, info in enumerate(infos) if not info.is_dir()]
    if len(archivos) > MAX_MIEMBROS_ZIP:
        raise AnexoInvalido(f"El ZIP trae más de {MAX_MIEMBROS_ZIP} archivos")
    if sum(info.file_size for _, info in archivos) > MAX_ZIP_LOTE_EXPANDIDO:
        raise AnexoInvalido(
            f"El ZIP se expande a más de {MAX_ZIP_LOTE_EXPANDIDO // (1024 * 1024)} MB al abrirlo"
        )
    for _, info in archivos:
        if info.file_size and info.file_size > MAX_RAZON_COMPRESION * max(info.compress_size, 1):
            raise AnexoInvalido("El ZIP tiene un contenido comprimido sospechoso")
    out: list[MiembroZip] = []
    for i, info in archivos:
        ruta = info.filename.replace("\\", "/")
        nombre = nombre_base(ruta)
        if nombre.startswith(".") or ruta.startswith("__MACOSX/"):
            continue  # metadatos del sistema (macOS), no anexos
        motivo = None
        if _es_symlink(info):
            motivo = "Es un enlace, no un archivo"
        elif ruta.startswith("/") or ".." in ruta.split("/") or re.match(r"^[A-Za-z]:", ruta):
            motivo = "Ruta no permitida"
        elif info.file_size > MAX_BYTES:
            motivo = f"Supera {MAX_BYTES // (1024 * 1024)} MB"
        out.append(MiembroZip(indice=i, nombre=nombre, tamano=info.file_size, motivo=motivo))
    return out


def leer_miembro(z: zipfile.ZipFile, miembro: MiembroZip) -> bytes:
    """Bytes de UN miembro, con corte en `MAX_BYTES` + 1 (el directorio puede mentir sobre el
    tamaño: se lee con tope igual). :class:`AnexoInvalido` si está dañado o protegido."""
    info = z.infolist()[miembro.indice]
    try:
        with z.open(info) as f:
            return f.read(MAX_BYTES + 1)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as exc:
        raise AnexoInvalido(f"No pude leer «{miembro.nombre}» del ZIP") from exc


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------


@dataclass
class ResultadoTexto:
    texto: str = ""
    # (bytes, mime) que van al modelo como imagen: páginas de un escaneado o la imagen suelta.
    imagenes: list[tuple[bytes, str]] = field(default_factory=list)
    paginas: int | None = None
    escaneado: bool = False
    # Páginas que no se leyeron por un tope: de un escaneado, las que no van como imagen; de un
    # PDF con texto, las que pasan de `MAX_PAGINAS_TEXTO` o de `MAX_TEXTO`.
    paginas_omitidas: int = 0
    paginas_leidas: int | None = None  # con tope: cuántas se leyeron (el aviso lo dice)
    error: str | None = None

    @property
    def hay_contenido(self) -> bool:
        return bool(self.texto.strip()) or bool(self.imagenes)


def _celda(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return " ".join(str(v).split())


def _fila(celdas: list[Any] | tuple[Any, ...]) -> str:
    """Celdas de una fila de tabla separadas por `|`; vacía si todas lo están."""
    textos = [_celda(c) for c in celdas]
    return " | ".join(textos) if any(textos) else ""


def dpi_render(ancho_pt: float, alto_pt: float) -> float:
    """dpi para que el lado mayor de la página no pase de `MAX_LADO_PX` (máx. 150 dpi).

    Un MediaBox enorme (un plano, una página mal escaneada) a 150 dpi da imágenes de cientos de
    MB en RAM; además, menos píxeles = menos tokens de imagen."""
    lado = max(float(ancho_pt or 0), float(alto_pt or 0))
    if lado <= 0:
        return float(RESOLUCION_RENDER)
    return min(float(RESOLUCION_RENDER), MAX_LADO_PX * 72.0 / lado)


def _renderizar_pagina(pagina: Any) -> bytes:
    """PNG de una página (pypdfium2 vía pdfplumber). Aparte para poder mockearlo en los tests."""
    img = pagina.to_image(resolution=dpi_render(pagina.width, pagina.height)).original
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _pdf_con_pdfium(data: bytes, max_paginas_imagen: int, motivo: str) -> ResultadoTexto:
    """Respaldo cuando pdfplumber no lee el PDF: pdfium (ya viene con pdfplumber) suele abrir
    lo que pdfminer no (en la muestra, un PDF con 0 páginas para pdfplumber trae 3 escaneadas
    [V, 4-nov]). Se trata como escaneado: las primeras páginas van como imagen."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
    except Exception:
        return ResultadoTexto(error=motivo)
    try:
        n = len(pdf)
        if n == 0:
            return ResultadoTexto(paginas=0, error=motivo)
        imagenes: list[tuple[bytes, str]] = []
        for i in range(min(n, max(0, max_paginas_imagen))):
            ancho, alto = pdf[i].get_size()
            img = pdf[i].render(scale=dpi_render(ancho, alto) / 72).to_pil()
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            imagenes.append((buf.getvalue(), PNG))
    except Exception:
        return ResultadoTexto(error=motivo)
    finally:
        pdf.close()
    if not imagenes:
        return ResultadoTexto(paginas=n, error=motivo)
    return ResultadoTexto(
        imagenes=imagenes, paginas=n, escaneado=True, paginas_omitidas=max(0, n - len(imagenes))
    )


def _texto_pdf(
    data: bytes, max_paginas_imagen: int, render: Callable[[Any], bytes]
) -> ResultadoTexto:
    import pdfplumber

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # malformado, protegido con clave, truncado…
        return _pdf_con_pdfium(
            data, max_paginas_imagen, f"No pude abrir el PDF ({type(exc).__name__})"
        )
    with pdf:
        try:
            n = len(pdf.pages)  # objetos livianos: el layout de cada página se calcula al leerla
        except Exception as exc:
            return _pdf_con_pdfium(
                data, max_paginas_imagen, f"No pude leer las páginas del PDF ({type(exc).__name__})"
            )
        if n == 0:
            return _pdf_con_pdfium(data, max_paginas_imagen, "El PDF no tiene páginas legibles")
        partes: list[str] = []
        chars = largo = leidas = 0
        # De a una página, liberando su caché apenas se lee; corta en `MAX_PAGINAS_TEXTO` o en
        # cuanto el texto alcanza `MAX_TEXTO` (lo demás se recortaría igual). Render: 512 MB.
        for i in range(min(n, MAX_PAGINAS_TEXTO)):
            if largo >= MAX_TEXTO:
                break
            pag = pdf.pages[i]
            leidas += 1
            try:
                tablas = pag.find_tables()
                resto = pag
                for t in tablas:
                    resto = resto.outside_bbox(t.bbox)
                texto = resto.extract_text() or ""
                filas = [_fila(f) for t in tablas for f in t.extract()]
                chars += len(pag.chars)
            except Exception as exc:
                _log.warning("Anexo PDF: página %s ilegible (%s)", i + 1, type(exc).__name__)
                continue
            finally:
                pag.close()
            bloque = "\n".join(x for x in (texto.strip(), *[f for f in filas if f]) if x)
            if bloque:
                partes.append(f"[página {i + 1}]\n{bloque}")
                largo += len(partes[-1]) + 2
        if chars / leidas >= MIN_CHARS_POR_PAGINA:
            return ResultadoTexto(
                texto="\n\n".join(partes)[:MAX_TEXTO],
                paginas=n,
                paginas_leidas=leidas,
                paginas_omitidas=n - leidas,
            )
        # Sin capa de texto (escaneado): las primeras páginas van como imagen.
        imagenes: list[tuple[bytes, str]] = []
        for i in range(min(n, max(0, max_paginas_imagen))):
            pag = pdf.pages[i]
            try:
                imagenes.append((render(pag), PNG))
            except Exception as exc:
                _log.warning("Anexo PDF: no pude renderizar una página (%s)", type(exc).__name__)
            finally:
                pag.close()
        if not imagenes:
            return ResultadoTexto(
                paginas=n, escaneado=True, error="PDF escaneado que no se pudo convertir a imagen"
            )
        return ResultadoTexto(
            texto="\n\n".join(partes)[:MAX_TEXTO],
            imagenes=imagenes,
            paginas=n,
            escaneado=True,
            paginas_leidas=len(imagenes),
            paginas_omitidas=max(0, n - len(imagenes)),
        )


def _texto_xlsx(data: bytes) -> ResultadoTexto:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        return ResultadoTexto(error=f"No pude abrir el Excel ({type(exc).__name__})")
    partes: list[str] = []
    try:
        for ws in wb.worksheets:
            filas = []
            for i, fila in enumerate(ws.iter_rows(values_only=True)):
                if i >= _MAX_FILAS_HOJA:
                    break
                f = _fila(fila)
                if f:
                    filas.append(f)
            if filas:
                partes.append(f"[hoja {ws.title}]\n" + "\n".join(filas))
    except Exception as exc:
        return ResultadoTexto(error=f"No pude leer el Excel ({type(exc).__name__})")
    finally:
        wb.close()
    return ResultadoTexto(texto="\n\n".join(partes)[:MAX_TEXTO], paginas=len(partes) or None)


def _texto_docx(data: bytes) -> ResultadoTexto:
    import docx
    from docx.table import Table

    try:
        documento = docx.Document(io.BytesIO(data))
    except Exception as exc:
        return ResultadoTexto(error=f"No pude abrir el Word ({type(exc).__name__})")
    partes: list[str] = []
    try:
        # Párrafos y tablas en el orden del documento.
        for bloque in documento.iter_inner_content():
            if isinstance(bloque, Table):
                for fila in bloque.rows:
                    f = _fila([c.text for c in fila.cells])
                    if f:
                        partes.append(f)
            else:
                t = " ".join(bloque.text.split())
                if t:
                    partes.append(t)
    except Exception as exc:
        return ResultadoTexto(error=f"No pude leer el Word ({type(exc).__name__})")
    return ResultadoTexto(texto="\n".join(partes)[:MAX_TEXTO])


def extraer_texto(
    data: bytes,
    mime: str,
    *,
    max_paginas_imagen: int = 5,
    render: Callable[[Any], bytes] = _renderizar_pagina,
) -> ResultadoTexto:
    """Texto (y, si hace falta, imágenes) de un anexo ya validado. Nunca lanza (regla 6)."""
    if mime in (DOCX, XLSX):
        try:
            verificar_zip(data)  # también para quien llama sin `validar_archivo` (scripts)
        except AnexoInvalido as exc:
            return ResultadoTexto(error=str(exc))
    try:
        if mime == PDF:
            return _texto_pdf(data, max_paginas_imagen, render)
        if mime == XLSX:
            return _texto_xlsx(data)
        if mime == DOCX:
            return _texto_docx(data)
        if mime in (PNG, JPEG):
            return ResultadoTexto(imagenes=[(data, mime)], paginas=1, escaneado=True)
    except Exception as exc:  # defensa final: un archivo raro no rompe la subida
        _log.warning("Anexo: extracción de texto falló (%s)", type(exc).__name__, exc_info=True)
        return ResultadoTexto(error=f"No pude leer el archivo ({type(exc).__name__})")
    return ResultadoTexto(error="Tipo de archivo no soportado")


# ---------------------------------------------------------------------------
# Servicio: anexo de CA → líneas `dudosa`
# ---------------------------------------------------------------------------


class Extractor(Protocol):
    """Lo que el servicio necesita del cliente de OpenRouter (`OpenRouterClient`)."""

    @property
    def habilitado(self) -> bool: ...

    def extraer_lineas(
        self,
        texto: str | None,
        imagenes: Sequence[tuple[bytes, str]] = (),
        *,
        esquema: str = "lineas",
    ) -> ExtraccionAnexo: ...


@dataclass
class ResultadoCarga:
    anexo: CaAnexo
    lineas: list[CaLinea]
    aviso: str
    duplicado: bool = False  # el archivo ya estaba cargado en esa CA: no se volvió a procesar


def texto_de_linea(ln: LineaExtraida) -> str:
    """`producto_texto` de la línea: el texto con su formato (si no lo trae ya)."""
    texto = " ".join((ln.texto or "").split())
    formato = " ".join((ln.formato or "").split())
    if formato and normalizar_texto(formato) not in normalizar_texto(texto):
        texto = f"{texto} {formato}"
    return texto[:LARGO_TEXTO_ORIGEN]


def _sugerir(ln: LineaExtraida, catalogo: Catalogo) -> tuple[int | None, int | None]:
    from app.mercado.catalogo import _formato_por_unidad

    texto = texto_de_linea(ln)
    sug = sugerir_producto(texto, None, catalogo)
    if sug is None:
        return None, None
    producto = catalogo.productos[sug.producto_id]
    formato = sug.formato_id or formato_de_texto(producto, ln.formato)
    if formato is None:
        formato = _formato_por_unidad(producto, ln.unidad)
    return sug.producto_id, formato


def _es_placeholder(ln: CaLinea) -> bool:
    return ln.origen == "anexo" and ln.estado == "pendiente" and ln.anexo_id is None


def resolver_anexo(session: Session, ca: CompraAgil) -> int:
    """«Anexo resuelto»: borra los placeholders «sin detalle (anexo)» de la CA y la marca para
    que `mercado-catalogo` no los vuelva a crear; las líneas de la API pasan a `reemplazada`
    (F-plat-anexos-2: el anexo manda). Devuelve cuántos placeholders borró. Sin commit."""
    from app.mercado.catalogo import recalcular_detalle_lineas, reemplazar_lineas_api

    borradas = 0
    for ln in session.execute(select(CaLinea).where(CaLinea.ca_codigo == ca.codigo)).scalars():
        if _es_placeholder(ln):
            session.delete(ln)
            borradas += 1
    session.flush()
    reemplazar_lineas_api(session, ca.codigo)
    _marcar_resuelto(session, ca)
    recalcular_detalle_lineas(session, [ca.codigo])
    return borradas


def _marcar_resuelto(session: Session, ca: CompraAgil) -> None:
    """`anexo_resuelto_en` con un UPDATE de Core que conserva `actualizado_en`: como la bandera,
    resolver el anexo no cuenta como «la CA cambió» (lo leen el cursor y la plataforma)."""
    ahora = ahora_utc()
    session.execute(
        update(CompraAgil)
        .where(CompraAgil.codigo == ca.codigo)
        .values(anexo_resuelto_en=ahora, actualizado_en=CompraAgil.actualizado_en)
        .execution_options(synchronize_session=False)
    )
    set_committed_value(ca, "anexo_resuelto_en", ahora)


def crear_lineas_de_anexo(
    session: Session, ca: CompraAgil, anexo: CaAnexo, extraidas: list[LineaExtraida]
) -> list[CaLinea]:
    """Las líneas extraídas, todas `dudosa` (toda línea de anexo la confirma una persona).

    Reemplaza los placeholders de la CA y las líneas aún sin confirmar de un anexo anterior con
    el MISMO nombre (una relectura del mismo documento); las de otros documentos de la CA
    conviven (Anexo 1 y Anexo 2 de una carga masiva, F-plat-anexos-2). No toca las `ok`, las de
    la API ni las manuales. Sin líneas extraídas no borra nada."""
    from app.mercado.catalogo import (
        N_MANUAL_DESDE,
        _cantidad_base,
        cargar_catalogo,
        recalcular_detalle_lineas,
    )

    utiles = [ln for ln in extraidas if (ln.texto or "").strip()]
    if not utiles:
        return []
    mismo_documento = set(
        session.execute(
            select(CaAnexo.id).where(
                CaAnexo.ca_codigo == ca.codigo,
                CaAnexo.nombre_original == anexo.nombre_original,
                CaAnexo.id != anexo.id,
            )
        ).scalars()
    )
    reemplazables = [
        ln
        for ln in session.execute(select(CaLinea).where(CaLinea.ca_codigo == ca.codigo)).scalars()
        if _es_placeholder(ln) or (ln.anexo_id in mismo_documento and ln.estado != "ok")
    ]
    habia_placeholder = any(_es_placeholder(ln) for ln in reemplazables)
    if reemplazables:
        session.execute(delete(CaLinea).where(CaLinea.id.in_([ln.id for ln in reemplazables])))
        session.expire_all()
    maximo = session.execute(
        select(func.max(CaLinea.n)).where(CaLinea.ca_codigo == ca.codigo)
    ).scalar_one_or_none()
    n = max(N_MANUAL_DESDE, (maximo or 0) + 1)
    catalogo = cargar_catalogo(session)
    from app.models.tables import ProductoFormato

    nuevas: list[CaLinea] = []
    for ext in utiles:
        texto = texto_de_linea(ext)
        producto_id, formato_id = _sugerir(ext, catalogo)
        fmt = session.get(ProductoFormato, formato_id) if formato_id else None
        factor = fmt.factor_base if fmt is not None else None
        cantidad = ext.cantidad if ext.cantidad is not None and ext.cantidad > 0 else None
        linea = CaLinea(
            ca_codigo=ca.codigo,
            n=n,
            producto_id=producto_id,
            formato_id=formato_id,
            cantidad=cantidad,
            cantidad_base=_cantidad_base(cantidad, factor),
            texto_origen=texto,
            texto_norm=normalizar_texto(texto),
            origen="anexo",
            estado="dudosa",
            anexo_id=anexo.id,
        )
        session.add(linea)
        nuevas.append(linea)
        n += 1
    session.flush()
    if habia_placeholder:
        _marcar_resuelto(session, ca)
    recalcular_detalle_lineas(session, [ca.codigo])
    return nuevas


def anexo_rechazado(
    ca: CompraAgil,
    data: bytes,
    nombre: str,
    motivo: str,
    *,
    fuente: str = "api_mp",
    empresa_id: int | None = None,
) -> CaAnexo:
    """Fila `error` de un archivo que no se aceptó (§1.6): deja constancia y evita bajarlo de
    nuevo en cada corrida. Sin el binario. Quien la agrega revisa antes el duplicado
    (:func:`anexo_existente`): UNIQUE (ca_codigo, sha256)."""
    return CaAnexo(
        ca_codigo=ca.codigo,
        nombre_original=(nombre or "anexo")[:255],
        mime=(detectar_mime(data) or "desconocido")[:100],
        tamano=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        escaneado=False,
        extraccion_estado="error",
        extraccion_detalle=motivo[:500],
        fuente=fuente,
        empresa_id=empresa_id,
    )


def anexo_existente(session: Session, ca_codigo: str, sha256: str) -> CaAnexo | None:
    """El anexo con ese contenido ya cargado en esa CA, si lo hay (idempotencia, §1)."""
    return session.execute(
        select(CaAnexo).where(CaAnexo.ca_codigo == ca_codigo, CaAnexo.sha256 == sha256)
    ).scalar_one_or_none()


def _aviso_paginas(res: ResultadoTexto) -> str:
    if res.paginas_omitidas:
        leidas = res.paginas_leidas if res.paginas_leidas is not None else len(res.imagenes)
        return (
            f" Solo se leyeron las primeras {leidas} páginas de {res.paginas}: "
            "agrega a mano las filas de las demás."
        )
    return ""


def _duplicado(previo: CaAnexo) -> ResultadoCarga:
    return ResultadoCarga(
        anexo=previo,
        lineas=[],
        aviso="Ese archivo ya estaba cargado en esta Compra Ágil: no se volvió a leer.",
        duplicado=True,
    )


def procesar_anexo_ca(
    session: Session,
    ca: CompraAgil,
    data: bytes,
    nombre: str,
    *,
    usuario_id: int | None,
    extractor: Extractor | None,
    max_paginas_imagen: int = 5,
    render: Callable[[Any], bytes] = _renderizar_pagina,
    fuente: str = "manual",
    empresa_id: int | None = None,
) -> ResultadoCarga:
    """Sube un anexo a una CA. :class:`AnexoInvalido` si el archivo no se acepta; cualquier otro
    problema (PDF roto, sin presupuesto, modelo caído) deja el anexo con su estado y la vista en
    manual: nunca lanza por eso (regla 6). Flush, sin commit.

    La sesión llega SIN escrituras pendientes (quien llama solo leyó la CA, la región, el
    usuario): antes de leer el archivo y llamar al modelo se hace `rollback()`, que cierra la
    transacción de lectura y devuelve la conexión al pool (regla 15: la extracción puede tardar
    más de un minuto y `OpenRouterClient` usa sus propias sesiones). Después se recarga la CA y
    recién ahí se escribe; si la CA ya no existe → :class:`AnexoInvalido`.

    Idempotente (F-plat-anexos-2): si el mismo archivo (sha256) ya está en la CA, no se lee ni se
    llama al modelo: devuelve ese anexo con `duplicado=True`."""
    from app.clients.openrouter import LLMError, LLMPresupuestoAgotado

    if fuente not in FUENTES:
        raise ValueError(f"fuente desconocida: {fuente}")
    codigo = ca.codigo
    mime = validar_archivo(data)
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("procesar_anexo_ca: la sesión trae cambios sin confirmar")
    sha = hashlib.sha256(data).hexdigest()
    previo = anexo_existente(session, codigo, sha)
    if previo is not None:
        return _duplicado(previo)
    session.rollback()  # suelta la conexión mientras se lee el archivo y responde el modelo
    res = extraer_texto(data, mime, max_paginas_imagen=max_paginas_imagen, render=render)

    ext: ExtraccionAnexo | None = None
    estado, aviso = "hecha", ""
    if res.error:
        estado, aviso = "error", f"{res.error}. Agrega las filas a mano."
    elif not res.hay_contenido:
        aviso = "El archivo no trae contenido legible: no hay filas que extraer."
    elif extractor is None or not extractor.habilitado:
        estado = "sin_presupuesto"
        aviso = "La lectura automática no está configurada: agrega las filas a mano."
    else:
        try:
            ext = extractor.extraer_lineas(res.texto or None, res.imagenes)
        except LLMPresupuestoAgotado:
            estado = "sin_presupuesto"
            aviso = "Se agotó el presupuesto del mes para la lectura automática: agrega las filas a mano."
        except LLMError as exc:
            _log.warning("Anexo %s: la extracción falló (%s)", codigo, exc)
            estado, aviso = "error", "La lectura automática falló: agrega las filas a mano."
        else:
            if ext.invalida:
                estado = "error"
                aviso = "El modelo no devolvió filas legibles: agrega las filas a mano."

    # Recarga tras el rollback (el objeto quedó expirado): un SELECT en una transacción nueva.
    recargada = session.get(CompraAgil, codigo)
    if recargada is None:
        raise AnexoInvalido("La compra ágil ya no está disponible")
    ca = recargada
    anexo = CaAnexo(
        ca_codigo=ca.codigo,
        nombre_original=(nombre or "anexo")[:255],
        mime=mime,
        tamano=len(data),
        sha256=sha,
        paginas=res.paginas,
        escaneado=res.escaneado,
        texto_extraido=res.texto or None,
        subido_por=usuario_id,
        extraccion_estado="pendiente",
        fuente=fuente,
        empresa_id=empresa_id,
    )
    if ext is not None:
        anexo.extraccion_modelo = ext.modelo[:200] or None
        anexo.extraccion_costo_usd = 0.0 if ext.desde_cache else ext.costo_usd
        anexo.extraccion_respaldo = ext.respaldo
    session.add(anexo)
    try:
        session.flush()
    except IntegrityError:
        # Otra request subió el mismo archivo mientras este se leía: gana la primera.
        session.rollback()
        previo = anexo_existente(session, codigo, sha)
        if previo is None:
            raise
        return _duplicado(previo)

    def _cerrar(estado: str, aviso: str, lineas: list[CaLinea] | None = None) -> ResultadoCarga:
        anexo.extraccion_estado = estado
        anexo.extraccion_detalle = aviso[:500] or None
        session.flush()
        return ResultadoCarga(anexo=anexo, lineas=lineas or [], aviso=aviso)

    if ext is None or ext.invalida:
        return _cerrar(estado, aviso)
    lineas = crear_lineas_de_anexo(session, ca, anexo, ext.lineas)
    if not lineas:
        return _cerrar(
            "hecha", "No se encontraron filas de productos en el anexo." + _aviso_paginas(res)
        )
    aviso = f"Se leyeron {len(lineas)} fila(s): revísalas y confírmalas." + _aviso_paginas(res)
    return _cerrar("hecha", aviso, lineas)
