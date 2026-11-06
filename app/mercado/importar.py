"""Importación de proveedores, precios, condiciones y logística desde XLSX o CSV (F-plat-3).

Formato = `data/mercado/proveedores_plantilla.xlsx`: hojas `precios`, `condiciones` y
`logistica` (opcionales cuando el libro trae `inventario`). CSV: solo `precios`. La hoja
`inventario` (F-plat-capas) tiene su propio par `parsear_inventario` / `aplicar_inventario`
y SOLO se lee de un .xlsx (decisión: un CSV no distingue la hoja y se prestaría a confundir
una lista de precios con inventario). Dos pasos sobre el MISMO
código: :func:`parsear` (puro, sin base) y :func:`aplicar` (escribe con flush, sin commit);
la vista previa es `aplicar` seguido de rollback, así los conteos nunca difieren de la
confirmación. Idempotente: la misma fila dos veces no duplica ni genera historial.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalogos.canonico import Catalogo, formato_de_texto, normalizar_texto, sugerir_vinculo
from app.catalogos.mercado import norm
from app.mercado import impuestos as imp
from app.mercado.catalogo import cargar_catalogo
from app.mercado.proveedores import (
    PROVINCIAS_NUBLE,
    buscar_producto,
    guardar_producto,
    hoy_chile,
    normalizar_rut,
    normalizar_unidad,
    vinculo_previsto,
)
from app.models.tables import (
    ProductoFormato,
    Proveedor,
    ProveedorProducto,
    TarifaLogistica,
)

# La confirmación reenvía el archivo en base64 en un campo de formulario y Starlette corta cada
# campo en 1 MB (`max_part_size`, defensa de memoria: no se sube). 700 KB * 4/3 < 1 MB.
MAX_BYTES = 700 * 1024
MAX_FILAS = 5000  # tope por hoja: evita planillas desproporcionadas

COLUMNAS_PRECIOS = [
    "proveedor", "rut", "comuna", "contacto", "telefono", "producto", "formato",
    "factor_base", "unidad_base", "precio_neto", "vigente_desde", "precio_bruto", "impuesto",
]  # fmt: skip
COLUMNAS_CONDICIONES = [
    "proveedor", "email", "direccion", "rubros (separados por coma)", "plazo_pago_dias",
    "minimo_compra_clp", "despacha (si/no)", "notas",
]  # fmt: skip
COLUMNAS_LOGISTICA = ["ambito", "nombre", "costo_fijo_clp", "costo_por_kg_clp", "dias", "notas"]
COLUMNAS_INVENTARIO = [
    "producto", "formato", "stock", "stock_minimo", "plazo_reposicion_dias", "marca",
    "proveedor_habitual", "notas",
]  # fmt: skip


class PlanillaInvalida(ValueError):
    """El archivo no se puede leer como planilla (formato, tamaño u hoja `precios` ausente)."""


@dataclass
class FilaPrecio:
    linea: int
    proveedor: str
    rut: str | None
    comuna: str | None
    contacto: str | None
    telefono: str | None
    producto: str
    formato: str
    factor_base: float | None
    unidad_base: str | None
    precio_neto: float | None
    vigente_desde: date
    # F-plat-impuestos: precio final con IVA e impuestos y el código de impuesto (una pista).
    precio_bruto: float | None = None
    impuesto: str | None = None


@dataclass
class FilaCondiciones:
    linea: int
    proveedor: str
    email: str | None
    direccion: str | None
    rubros: list[str] | None
    plazo_pago_dias: int | None
    minimo_compra_clp: int | None
    despacha: bool | None
    notas: str | None


@dataclass
class FilaLogistica:
    linea: int
    ambito: str
    nombre: str
    costo_fijo_clp: int | None
    costo_por_kg_clp: float | None
    dias: int | None
    notas: str | None


@dataclass
class Planilla:
    precios: list[FilaPrecio] = field(default_factory=list)
    condiciones: list[FilaCondiciones] = field(default_factory=list)
    logistica: list[FilaLogistica] = field(default_factory=list)
    ejemplos: list[str] = field(default_factory=list)  # proveedores "EJEMPLO…" ignorados
    errores: list[str] = field(default_factory=list)


@dataclass
class Resumen:
    proveedores_nuevos: int = 0
    proveedores_actualizados: int = 0
    productos_nuevos: int = 0
    productos_vinculados: int = 0  # al catálogo canónico (F-plat-capas §1.3)
    sin_vincular: int = 0
    precios_cambiados: int = 0
    precios_historicos: int = 0
    sin_cambios: int = 0
    # F-plat-impuestos: filas con precio_bruto cuyo neto se calculó solo sin IVA (familia
    # `clasificar` sin código) y avisos fila por fila (bruto y neto que no cuadran, pista que el
    # catálogo contradice). Los avisos no omiten la fila.
    por_clasificar: int = 0
    avisos: list[str] = field(default_factory=list)
    condiciones_actualizadas: int = 0
    tarifas_nuevas: int = 0
    tarifas_actualizadas: int = 0
    ejemplos: list[str] = field(default_factory=list)
    errores: list[str] = field(default_factory=list)

    @property
    def hay_cambios(self) -> bool:
        return any(
            (
                self.proveedores_nuevos, self.proveedores_actualizados, self.productos_nuevos,
                self.productos_vinculados, self.precios_cambiados, self.precios_historicos, self.condiciones_actualizadas, self.tarifas_nuevas,
                self.tarifas_actualizadas,
            )
        )  # fmt: skip


# ---------------------------------------------------------------------------
# Conversión de celdas
# ---------------------------------------------------------------------------


def _vacio(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _txt(v: Any) -> str | None:
    if _vacio(v):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # un teléfono o código numérico no debe quedar "123.0"
    return str(v).strip()


def _num(v: Any) -> float | None:
    """Número de una celda; acepta '27.500', '27.500,50', '$ 1.300' y '12,5'. Inválido → ValueError."""
    if _vacio(v):
        return None
    if isinstance(v, bool):
        raise ValueError(f"no es un número: {v!r}")
    if isinstance(v, int | float):
        return float(v)
    t = re.sub(r"[\s$]", "", str(v))
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        raise ValueError(f"no es un número: {v!r}") from None


def _entero(v: Any) -> int | None:
    n = _num(v)
    return None if n is None else int(round(n))


def _fecha(v: Any) -> date | None:
    if _vacio(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    t = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"fecha inválida: {v!r} (usa AAAA-MM-DD)")


def _si_no(v: Any) -> bool | None:
    if _vacio(v):
        return None
    t = norm(str(v))
    if t in {"si", "s", "1", "true", "yes", "x"}:
        return True
    if t in {"no", "n", "0", "false"}:
        return False
    raise ValueError(f"se esperaba si/no: {v!r}")


convertir_numero = _num
convertir_fecha = _fecha


def _clave_columna(encabezado: Any) -> str:
    """'rubros (separados por coma)' → 'rubros'; 'despacha (si/no)' → 'despacha'."""
    return norm(str(encabezado or "").split("(")[0]).replace(" ", "_")


# ---------------------------------------------------------------------------
# Lectura del archivo
# ---------------------------------------------------------------------------


def _filas_xlsx(data: bytes) -> dict[str, list[tuple[Any, ...]]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # zip corrupto, no es xlsx, etc.
        raise PlanillaInvalida("No pude leer el archivo como planilla Excel (.xlsx)") from exc
    hojas: dict[str, list[tuple[Any, ...]]] = {}
    try:
        for ws in wb.worksheets:
            filas: list[tuple[Any, ...]] = []
            for fila in ws.iter_rows(values_only=True):
                if len(filas) > MAX_FILAS:
                    raise PlanillaInvalida(f"La hoja {ws.title!r} supera {MAX_FILAS} filas")
                filas.append(tuple(fila))
            hojas[norm(ws.title)] = filas
    finally:
        wb.close()
    return hojas


def _filas_csv(data: bytes) -> list[tuple[Any, ...]]:
    for codificacion in ("utf-8-sig", "cp1252"):
        try:
            texto = data.decode(codificacion)
            break
        except UnicodeDecodeError:
            continue
    else:  # cp1252 casi no falla, pero por si acaso
        raise PlanillaInvalida("No pude leer el CSV (codificación)")
    primera = texto.splitlines()[0] if texto.strip() else ""
    delimitador = ";" if primera.count(";") > primera.count(",") else ","
    try:
        filas = [tuple(f) for f in csv.reader(io.StringIO(texto), delimiter=delimitador)]
    except csv.Error as exc:  # p. ej. un campo de más de 128 KB
        raise PlanillaInvalida(f"No pude leer el CSV: {exc}") from exc
    if len(filas) > MAX_FILAS + 1:
        raise PlanillaInvalida(f"El CSV supera {MAX_FILAS} filas")
    return filas


def _como_dicts(filas: list[tuple[Any, ...]]) -> list[tuple[int, dict[str, Any]]]:
    """(número de fila, {columna: valor}) sin las filas totalmente vacías."""
    if not filas:
        return []
    claves = [_clave_columna(c) for c in filas[0]]
    salida: list[tuple[int, dict[str, Any]]] = []
    for i, fila in enumerate(filas[1:], start=2):
        if all(_vacio(c) for c in fila):
            continue
        salida.append((i, {k: c for k, c in zip(claves, fila, strict=False) if k}))
    return salida


def parsear(data: bytes, nombre_archivo: str) -> Planilla:
    """Lee el archivo y valida fila a fila. Los errores se acumulan; no abortan el resto."""
    if len(data) > MAX_BYTES:
        raise PlanillaInvalida(
            f"El archivo supera {MAX_BYTES // 1024} KB; quita imágenes o formato, o divídelo"
        )
    es_csv = nombre_archivo.lower().endswith(".csv")
    if es_csv:
        hojas = {"precios": _filas_csv(data)}
    elif nombre_archivo.lower().endswith((".xlsx", ".xlsm")):
        hojas = _filas_xlsx(data)
    else:
        raise PlanillaInvalida("Sube un archivo .xlsx o .csv")
    if "precios" not in hojas and "inventario" not in hojas:
        raise PlanillaInvalida("Falta la hoja 'precios'")

    p = Planilla()
    ejemplos: set[str] = set()
    if "precios" not in hojas:
        # F-plat-capas: un libro con solo `inventario` no es de precios; sus filas se
        # importan desde «Inventario» (`parsear_inventario`).
        p.errores.append(
            "El archivo trae la hoja 'inventario' y no 'precios': impórtalo desde Inventario"
        )

    for linea, f in _como_dicts(hojas.get("precios", [])):
        nombre = _txt(f.get("proveedor"))
        if nombre is None:
            p.errores.append(f"precios, fila {linea}: falta el proveedor")
            continue
        if norm(nombre).startswith("ejemplo"):
            ejemplos.add(nombre)
            continue
        try:
            producto = _txt(f.get("producto"))
            if producto is None:
                raise ValueError("falta el producto")
            precio = _num(f.get("precio_neto"))
            precio_bruto = _num(f.get("precio_bruto"))
            if precio_bruto is not None and precio_bruto <= 0:
                raise ValueError("precio_bruto debe ser un número mayor que 0")
            if precio_bruto is None and (precio is None or precio <= 0):
                raise ValueError("precio_neto (o precio_bruto) debe ser un número mayor que 0")
            if precio is not None and precio <= 0:
                precio = None  # manda precio_bruto
            impuesto = _txt(f.get("impuesto"))
            factor = _num(f.get("factor_base"))
            unidad_txt = _txt(f.get("unidad_base"))
            unidad = normalizar_unidad(unidad_txt)
            if unidad_txt is not None and unidad is None:
                raise ValueError(f"unidad_base {unidad_txt!r} no es kg, L ni u")
            if factor is not None and factor <= 0:
                raise ValueError("factor_base debe ser mayor que 0")
            if (factor is None) != (unidad is None):
                raise ValueError("factor_base y unidad_base van juntos o ninguno")
            p.precios.append(
                FilaPrecio(
                    linea=linea,
                    proveedor=nombre,
                    rut=normalizar_rut(_txt(f.get("rut"))),
                    comuna=_txt(f.get("comuna")),
                    contacto=_txt(f.get("contacto")),
                    telefono=_txt(f.get("telefono")),
                    producto=producto,
                    formato=_txt(f.get("formato")) or "",
                    factor_base=factor,
                    unidad_base=unidad,
                    precio_neto=precio,
                    vigente_desde=_fecha(f.get("vigente_desde")) or hoy_chile(),
                    precio_bruto=precio_bruto,
                    impuesto=impuesto.strip().lower() if impuesto else None,
                )
            )
        except ValueError as exc:
            p.errores.append(f"precios, fila {linea}: {exc}")

    if "condiciones" in hojas and not es_csv:
        for linea, f in _como_dicts(hojas["condiciones"]):
            nombre = _txt(f.get("proveedor"))
            if nombre is None:
                p.errores.append(f"condiciones, fila {linea}: falta el proveedor")
                continue
            if norm(nombre).startswith("ejemplo"):
                ejemplos.add(nombre)
                continue
            try:
                rubros_txt = _txt(f.get("rubros"))
                p.condiciones.append(
                    FilaCondiciones(
                        linea=linea,
                        proveedor=nombre,
                        email=_txt(f.get("email")),
                        direccion=_txt(f.get("direccion")),
                        rubros=(
                            [r.strip() for r in rubros_txt.split(",") if r.strip()]
                            if rubros_txt
                            else None
                        ),
                        plazo_pago_dias=_entero(f.get("plazo_pago_dias")),
                        minimo_compra_clp=_entero(f.get("minimo_compra_clp")),
                        despacha=_si_no(f.get("despacha")),
                        notas=_txt(f.get("notas")),
                    )
                )
            except ValueError as exc:
                p.errores.append(f"condiciones, fila {linea}: {exc}")

    if "logistica" in hojas and not es_csv:
        for linea, f in _como_dicts(hojas["logistica"]):
            nombre = _txt(f.get("nombre"))
            ambito = norm(_txt(f.get("ambito")))
            sin_valores = all(
                _vacio(f.get(k)) for k in ("costo_fijo_clp", "costo_por_kg_clp", "dias")
            )
            if nombre is None or nombre.startswith("(") or sin_valores:
                continue  # fila de ejemplo o sin datos de la plantilla
            try:
                if ambito not in {"comuna", "provincia"}:
                    raise ValueError("ambito debe ser comuna o provincia")
                p.logistica.append(
                    FilaLogistica(
                        linea=linea,
                        ambito=ambito,
                        nombre=nombre,
                        costo_fijo_clp=_entero(f.get("costo_fijo_clp")),
                        costo_por_kg_clp=_num(f.get("costo_por_kg_clp")),
                        dias=_entero(f.get("dias")),
                        notas=_txt(f.get("notas")),
                    )
                )
            except ValueError as exc:
                p.errores.append(f"logistica, fila {linea}: {exc}")

    p.ejemplos = sorted(ejemplos)
    return p


def planilla_de_lista(
    proveedor: Proveedor, filas: list[tuple[str, str, Any]], vigente_desde: date | None = None
) -> Planilla:
    """Planilla de una lista de precios leída de una foto o PDF (F-plat-2 §1.4).

    `filas` = (producto, formato, precio neto). Mismas validaciones que la hoja `precios`; el
    factor y la unidad base salen del formato ("bolsa 1 kg" → 1 kg) cuando se pueden leer."""
    from app.catalogos.canonico import extraer_formato

    p = Planilla()
    vigente = vigente_desde or hoy_chile()
    for i, (producto, formato, precio) in enumerate(filas, start=1):
        prod = " ".join((producto or "").split())
        fmt = " ".join((formato or "").split())
        if not prod:
            continue  # fila borrada en la vista previa
        try:
            valor = _num(precio)
        except ValueError:
            valor = None
        if valor is None or valor <= 0:
            p.errores.append(f"fila {i} ({prod[:60]}): falta el precio neto")
            continue
        f = extraer_formato(fmt or prod)
        p.precios.append(
            FilaPrecio(
                linea=i,
                proveedor=proveedor.nombre,
                rut=proveedor.rut,
                comuna=None,
                contacto=None,
                telefono=None,
                producto=prod[:300],
                formato=fmt[:200],
                factor_base=f[2] if f else None,
                unidad_base=f[3] if f else None,
                precio_neto=valor,
                vigente_desde=vigente,
            )
        )
    return p


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------


class _Indice:
    """Proveedores de la empresa por RUT y por nombre normalizado (se actualiza al crear)."""

    def __init__(self, session: Session, empresa_id: int) -> None:
        self.session = session
        self.empresa_id = empresa_id
        self.por_rut: dict[str, Proveedor] = {}
        self.por_nombre: dict[str, list[Proveedor]] = {}
        for prov in session.execute(
            select(Proveedor).where(Proveedor.empresa_id == empresa_id)
        ).scalars():
            self._agregar(prov)

    def _agregar(self, prov: Proveedor) -> None:
        if prov.rut:
            self.por_rut[prov.rut] = prov
        self.por_nombre.setdefault(norm(prov.nombre), []).append(prov)

    def resolver(self, nombre: str, rut: str | None) -> tuple[Proveedor | None, bool]:
        """(proveedor, existía). Por RUT si viene; si no, por nombre normalizado. None = crear."""
        if rut and rut in self.por_rut:
            return self.por_rut[rut], True
        for prov in self.por_nombre.get(norm(nombre), []):
            if not rut or not prov.rut or prov.rut == rut:
                return prov, True
        return None, False

    def por_nombre_unico(self, nombre: str) -> Proveedor | None:
        candidatos = self.por_nombre.get(norm(nombre), [])
        return candidatos[0] if candidatos else None

    def crear(self, nombre: str, rut: str | None) -> Proveedor:
        prov = Proveedor(
            empresa_id=self.empresa_id,
            nombre=nombre[:300],
            rut=rut,
            rubros=[],
            despacha=False,
            activo=True,
        )
        self.session.add(prov)
        self.session.flush()
        self._agregar(prov)
        return prov


def _recortar(v: str | None, largo: int) -> str | None:
    return v[:largo] if v else None


def _set_si_cambia(obj: Any, campo: str, valor: Any) -> bool:
    if valor is None or getattr(obj, campo) == valor:
        return False
    setattr(obj, campo, valor)
    return True


def _catalogo_si_hay(session: Session, planilla: Planilla) -> Catalogo | None:
    """El catálogo canónico en memoria, una sola vez por importación (solo si hay precios)."""
    if not planilla.precios:
        return None
    return cargar_catalogo(session)


def _neto_de_fila(
    session: Session,
    fila: FilaPrecio,
    previo: ProveedorProducto | None,
    catalogo: Catalogo | None,
    tabla: imp.Tabla,
    r: Resumen,
) -> tuple[float | None, bool]:
    """(neto a guardar, ¿quedó «impuesto por clasificar»?). None = la fila es un error.

    Con `precio_bruto` el neto se calcula con el impuesto del catálogo (vía el vínculo que ya
    tiene el producto o el que tendrá al guardarlo) o, sin vínculo, con la pista `impuesto` de la
    fila. En una familia `clasificar`, un producto sin código usa la pista si la fila la trae; si
    no, el neto es solo sin IVA y la fila queda por clasificar. Si también viene `precio_neto` y no
    cuadra (±1 %), manda `precio_bruto` y se avisa."""
    donde = f"precios, fila {fila.linea}"
    if fila.impuesto is not None and not tabla.existe(fila.impuesto):
        r.errores.append(
            f"{donde}: impuesto {fila.impuesto!r} desconocido (usa uno de: "
            f"{', '.join([*tabla.codigos, imp.SIN_IMPUESTO])}, o déjalo vacío)"
        )
        return None, False
    producto_id: int | None = previo.producto_id if previo is not None else None
    if producto_id is None and catalogo is not None:
        previsto = vinculo_previsto(catalogo, fila.producto, fila.formato, fila.unidad_base)
        producto_id = previsto[0] if previsto else None
    pista = tabla.tasa(fila.impuesto, fila.vigente_desde)
    impuesto: imp.Resolucion
    if producto_id is None:
        impuesto = pista
    else:
        cod, fam = imp.datos_productos(session, [producto_id]).get(producto_id, (None, None))
        impuesto = tabla.de_producto(cod, fam, fila.vigente_desde)
        if impuesto is imp.POR_CLASIFICAR and fila.impuesto is not None:
            impuesto = pista
        elif imp.pista_contradice(tabla, fila.impuesto, cod, fam):
            r.avisos.append(
                f"{donde}: la planilla dice {fila.impuesto!r} pero el catálogo dice otra cosa "
                f"({tabla.codigo_resuelto(cod, fam)}): manda el catálogo"
            )
    if fila.precio_bruto is None:
        return fila.precio_neto, False
    neto = imp.neto_desde_bruto(fila.precio_bruto, impuesto)
    if fila.precio_neto is not None and not imp.cuadran(fila.precio_neto, fila.precio_bruto, impuesto):
        r.avisos.append(
            f"{donde}: precio_neto {fila.precio_neto:g} no cuadra con precio_bruto "
            f"{fila.precio_bruto:g} (neto calculado {neto:g}): manda precio_bruto"
        )
    return neto, impuesto is imp.POR_CLASIFICAR


def aplicar(
    session: Session,
    empresa_id: int,
    planilla: Planilla,
    usuario_id: int | None = None,
    *,
    proveedor_fijo: Proveedor | None = None,
    fuente: str = "planilla",
) -> Resumen:
    """Escribe la planilla (flush, sin commit). La vista previa es esto + rollback.

    `proveedor_fijo` (F-plat-2, lista de precios por foto/PDF): todas las filas de precios van a
    ese proveedor, que ya es de la empresa (lo valida la ruta), sin buscarlo por nombre."""
    if proveedor_fijo is not None and proveedor_fijo.empresa_id != empresa_id:
        raise ValueError("El proveedor no es de la empresa")
    r = Resumen(ejemplos=list(planilla.ejemplos), errores=list(planilla.errores))
    indice = _Indice(session, empresa_id)
    catalogo = _catalogo_si_hay(session, planilla)
    tabla = imp.cargar(session) if planilla.precios else imp.Tabla()

    for fila in planilla.precios:
        prov: Proveedor | None
        if proveedor_fijo is not None:
            prov = proveedor_fijo
        else:
            prov, _existia = indice.resolver(fila.proveedor, fila.rut)
        if prov is None:
            prov = indice.crear(fila.proveedor, fila.rut)
            prov.comuna = _recortar(fila.comuna, 100)
            prov.contacto_nombre = _recortar(fila.contacto, 200)
            prov.telefono = _recortar(fila.telefono, 50)
            r.proveedores_nuevos += 1
        elif proveedor_fijo is None:
            cambios = [
                _set_si_cambia(prov, "rut", None if prov.rut else fila.rut),
                _set_si_cambia(prov, "comuna", _recortar(fila.comuna, 100)),
                _set_si_cambia(prov, "contacto_nombre", _recortar(fila.contacto, 200)),
                _set_si_cambia(prov, "telefono", _recortar(fila.telefono, 50)),
            ]
            if any(cambios):
                r.proveedores_actualizados += 1
                if prov.rut:
                    indice.por_rut[prov.rut] = prov
        previo = buscar_producto(session, prov.id, fila.producto, fila.formato)
        tenia_vinculo = previo is not None and previo.producto_id is not None
        neto, sin_clasificar = _neto_de_fila(session, fila, previo, catalogo, tabla, r)
        if neto is None:
            continue
        try:
            pp, estado = guardar_producto(
                session,
                prov,
                producto_texto=fila.producto,
                formato_texto=fila.formato,
                precio_neto=neto,
                vigente_desde=fila.vigente_desde,
                factor_base=fila.factor_base,
                unidad_base=fila.unidad_base,
                fuente=fuente,
                usuario_id=usuario_id,
                catalogo=catalogo,
            )
        except ValueError as exc:  # no debería: parsear ya validó
            r.errores.append(f"precios, fila {fila.linea}: {exc}")
            continue
        if fila.impuesto is not None:
            pp.impuesto_codigo = fila.impuesto  # la pista es de la empresa (su proveedor)
        if sin_clasificar:
            r.por_clasificar += 1
        if pp.producto_id is None:
            r.sin_vincular += 1
        elif not tenia_vinculo:
            r.productos_vinculados += 1
        if estado == "nuevo":
            r.productos_nuevos += 1
        elif estado == "cambiado":
            r.precios_cambiados += 1
        elif estado == "historico":
            r.precios_historicos += 1
        else:
            r.sin_cambios += 1

    for c in planilla.condiciones:
        prov = indice.por_nombre_unico(c.proveedor)
        if prov is None:
            r.errores.append(
                f"condiciones, fila {c.linea}: el proveedor {c.proveedor!r} no está en "
                "'precios' ni en la empresa"
            )
            continue
        cambios = [
            _set_si_cambia(prov, "email", _recortar(c.email, 255)),
            _set_si_cambia(prov, "direccion", _recortar(c.direccion, 300)),
            _set_si_cambia(prov, "rubros", c.rubros),
            _set_si_cambia(prov, "plazo_pago_dias", c.plazo_pago_dias),
            _set_si_cambia(prov, "minimo_compra_clp", c.minimo_compra_clp),
            _set_si_cambia(prov, "despacha", c.despacha),
            _set_si_cambia(prov, "notas", c.notas),
        ]
        if any(cambios):
            r.condiciones_actualizadas += 1

    if planilla.logistica:
        existentes = {
            (t.ambito, norm(t.nombre)): t
            for t in session.execute(
                select(TarifaLogistica).where(TarifaLogistica.empresa_id == empresa_id)
            ).scalars()
        }
        for t in planilla.logistica:
            tarifa = existentes.get((t.ambito, norm(t.nombre)))
            if tarifa is None:
                tarifa = TarifaLogistica(
                    empresa_id=empresa_id,
                    ambito=t.ambito,
                    nombre=t.nombre[:100],
                    costo_fijo_clp=t.costo_fijo_clp or 0,
                    costo_por_kg_clp=t.costo_por_kg_clp or 0,
                    dias=t.dias,
                    notas=t.notas,
                )
                session.add(tarifa)
                existentes[(t.ambito, norm(t.nombre))] = tarifa
                r.tarifas_nuevas += 1
                continue
            cambios = [
                _set_si_cambia(tarifa, "costo_fijo_clp", t.costo_fijo_clp),
                _set_si_cambia(tarifa, "costo_por_kg_clp", t.costo_por_kg_clp),
                _set_si_cambia(tarifa, "dias", t.dias),
                _set_si_cambia(tarifa, "notas", t.notas),
            ]
            if any(cambios):
                r.tarifas_actualizadas += 1
    session.flush()
    return r


# ---------------------------------------------------------------------------
# Plantilla descargable (mismas 4 hojas y encabezados que la de Boris)
# ---------------------------------------------------------------------------


def generar_plantilla_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "precios"
    ws.append(COLUMNAS_PRECIOS)
    ejemplo = ["EJEMPLO Distribuidora X (borrar)", "76.123.456-7", "Chillán", "Juan Pérez",
               "+56 9 1234 5678"]  # fmt: skip
    ws.append([*ejemplo, "Leche entera", "caja 1 L", 1, "L", 890, "2026-11-03"])
    ws.append([*ejemplo, "Leche entera", "pack 12 cajas 1 L", 12, "L", 9900, "2026-11-03"])
    ws.append([*ejemplo, "Arroz grado 1", "saco 25 kg", 25, "kg", 27500, "2026-11-03"])
    ws.append([*ejemplo, "Bebida cola light", "botella 1,5 L", 1.5, "L", None, "2026-11-03",
               1290, "ila_analcoholica_baja"])  # fmt: skip

    ws = wb.create_sheet("condiciones")
    ws.append(COLUMNAS_CONDICIONES)
    ws.append(["EJEMPLO Distribuidora X (borrar)", "ventas@x.cl", "Av. Collín 123",
               "lácteos, abarrotes", 30, 50000, "si",
               "Despacha martes y jueves a Chillán y San Carlos"])  # fmt: skip

    ws = wb.create_sheet("logistica")
    ws.append(COLUMNAS_LOGISTICA)
    for prov in PROVINCIAS_NUBLE:
        ws.append(["provincia", prov, None, None, None,
                   "Costo de llevar UN pedido desde tu bodega a esa provincia"])  # fmt: skip
    ws.append(["comuna", "(opcional, ej. Cobquecura)", None, None, None,
               "Solo si una comuna se aleja mucho del promedio de su provincia"])  # fmt: skip

    ws = wb.create_sheet("Instrucciones")
    for linea in (
        "Hoja 'precios' (obligatoria): una fila por producto y formato; los datos del proveedor "
        "se repiten en cada fila.",
        "producto: nombre genérico ('Leche entera'). formato: como lo vende el proveedor "
        "('caja 1 L', 'saco 25 kg', 'unidad').",
        "factor_base y unidad_base (kg, L o u): cuántos kg, L o unidades trae UN formato. Con eso "
        "se compara el precio por kg/L/u entre proveedores. Sin ellos, el producto no compite.",
        "precio_neto: SIN IVA y SIN impuestos específicos (ILA), por FORMATO (no por kg). "
        "vigente_desde: AAAA-MM-DD (vacío = hoy).",
        "precio_bruto (opcional): el precio FINAL por formato, con IVA y con impuestos específicos "
        "(como cotiza el proveedor). Si viene, el neto se calcula con el impuesto del catálogo; "
        "si vienen los dos y no cuadran (±1 %), manda precio_bruto. Basta uno de los dos.",
        "impuesto (opcional): código del impuesto específico (p. ej. ila_analcoholica_baja, "
        "ila_analcoholica_alta, o ninguno). Es una pista: si el producto ya está en el catálogo, "
        "manda el catálogo. Sin código, una bebida de una familia «por clasificar» entra con el "
        "neto calculado solo sin IVA y queda marcada «impuesto por clasificar».",
        "Si el contador indica que un impuesto no se recupera, el catálogo lo marca así y ese "
        "impuesto queda dentro del neto (es costo).",
        "Hoja 'condiciones' (opcional): una fila por proveedor, identificado por su nombre. "
        "rubros separados por coma; despacha = si/no.",
        "Hoja 'logistica' (opcional): costo de llevar UN pedido a una provincia o comuna. "
        "Las filas sin valores o con el nombre entre paréntesis se ignoran.",
        "Las filas cuyo proveedor empieza con EJEMPLO se ignoran. Subir el mismo archivo dos "
        "veces no duplica nada.",
    ):
        ws.append([linea])
    ws.column_dimensions["A"].width = 120

    salida = io.BytesIO()
    wb.save(salida)
    return salida.getvalue()


# ---------------------------------------------------------------------------
# Inventario (F-plat-capas): hoja `inventario`, solo XLSX
# ---------------------------------------------------------------------------


@dataclass
class FilaInventario:
    linea: int
    producto: str
    formato: str
    stock: float | None
    stock_minimo: float | None
    plazo: int | None
    marca: str | None
    proveedor_habitual: str | None
    notas: str | None


@dataclass
class PlanillaInventario:
    filas: list[FilaInventario] = field(default_factory=list)
    ejemplos: int = 0
    errores: list[str] = field(default_factory=list)


@dataclass
class ResumenInventario:
    nuevos: int = 0
    actualizados: int = 0
    sin_cambios: int = 0
    ejemplos: int = 0
    errores: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def hay_cambios(self) -> bool:
        return bool(self.nuevos or self.actualizados)


def parsear_inventario(data: bytes, nombre_archivo: str) -> PlanillaInventario:
    """Lee la hoja `inventario` de un .xlsx y valida fila a fila (los errores se acumulan).
    Solo XLSX: un CSV no dice de qué hoja viene y se confundiría con una lista de precios."""
    if len(data) > MAX_BYTES:
        raise PlanillaInvalida(
            f"El archivo supera {MAX_BYTES // 1024} KB; quita imágenes o formato, o divídelo"
        )
    if not nombre_archivo.lower().endswith((".xlsx", ".xlsm")):
        raise PlanillaInvalida("El inventario se importa desde un .xlsx con la hoja 'inventario'")
    hojas = _filas_xlsx(data)
    if "inventario" not in hojas:
        raise PlanillaInvalida("Falta la hoja 'inventario'")
    p = PlanillaInventario()
    for linea, f in _como_dicts(hojas["inventario"]):
        producto = _txt(f.get("producto"))
        if producto is None:
            p.errores.append(f"inventario, fila {linea}: falta el producto")
            continue
        if norm(producto).startswith("ejemplo"):
            p.ejemplos += 1
            continue
        try:
            stock = _num(f.get("stock"))
            minimo = _num(f.get("stock_minimo"))
            plazo = _entero(f.get("plazo_reposicion_dias"))
            if stock is not None and not 0 <= stock <= 1e9:
                raise ValueError("stock debe ser un número entre 0 y 1.000.000.000")
            if minimo is not None and not 0 <= minimo <= 1e9:
                raise ValueError("stock_minimo debe ser un número entre 0 y 1.000.000.000")
            if plazo is not None and not 0 <= plazo <= 365:
                raise ValueError("plazo_reposicion_dias debe estar entre 0 y 365")
            p.filas.append(
                FilaInventario(
                    linea=linea,
                    producto=producto[:300],
                    formato=(_txt(f.get("formato")) or "")[:200],
                    stock=stock,
                    stock_minimo=minimo,
                    plazo=plazo,
                    marca=_recortar(_txt(f.get("marca")), 100),
                    proveedor_habitual=_txt(f.get("proveedor_habitual")),
                    notas=_recortar(_txt(f.get("notas")), 2000),
                )
            )
        except ValueError as exc:
            p.errores.append(f"inventario, fila {linea}: {exc}")
    return p


def _opciones_de_producto(catalogo: Catalogo, texto: str, maximo: int = 5) -> list[str]:
    """Nombres del catálogo que comparten una palabra (4+ letras) con el texto."""
    palabras = {w for w in normalizar_texto(texto).split() if len(w) >= 4}
    if not palabras:
        return []
    return sorted(
        p.nombre
        for p in catalogo.productos.values()
        if palabras & set(normalizar_texto(p.nombre).split())
    )[:maximo]


def _resolver_formato(
    catalogo: Catalogo,
    formatos: list[tuple[int, str, float]],
    producto_id: int,
    texto: str,
    del_alias: int | None,
) -> int | None:
    """Formato del producto que nombra `texto` (por nombre exacto o por su factor); sin texto,
    el que dio la sugerencia o, si no, el formato base. None = no existe."""
    if not texto:
        if del_alias is not None:
            return del_alias
        return next((i for i, _n, fac in formatos if abs(fac - 1.0) < 1e-9), None)
    clave = norm(texto)
    for i, nombre, _fac in formatos:
        if norm(nombre) == clave:
            return i
    return formato_de_texto(catalogo.productos[producto_id], texto)


def aplicar_inventario(
    session: Session,
    empresa_id: int,
    planilla: PlanillaInventario,
    usuario_id: int | None = None,
) -> ResumenInventario:
    """Escribe el inventario (flush, sin commit); la vista previa es esto + rollback.

    Cada producto se resuelve al canónico con las reglas de §1.3 (nombre exacto o sugerencia
    única y de confianza alta): si es ambiguo o no hay, la fila es un error CON las opciones y no
    rompe el lote. El stock de la planilla es el stock final: la diferencia queda como
    movimiento `ajuste` (referencia «planilla»). `proveedor_habitual` se busca por RUT o nombre
    entre los proveedores de la empresa; si no existe, aviso y queda sin proveedor habitual."""
    from app.mercado import oferta_propia as op  # lazy: oferta_propia no importa este módulo

    r = ResumenInventario(ejemplos=planilla.ejemplos, errores=list(planilla.errores))
    if not planilla.filas:
        return r
    catalogo = cargar_catalogo(session)
    formatos: dict[int, list[tuple[int, str, float]]] = {}
    for fid, pid, nombre, factor in session.execute(
        select(
            ProductoFormato.id,
            ProductoFormato.producto_id,
            ProductoFormato.nombre,
            ProductoFormato.factor_base,
        ).where(ProductoFormato.producto_id.in_(list(catalogo.productos)))
    ):
        formatos.setdefault(pid, []).append((fid, nombre, factor))
    indice = _Indice(session, empresa_id)

    for fila in planilla.filas:
        s = sugerir_vinculo(catalogo, fila.producto, fila.formato)
        if s is None:
            opciones = _opciones_de_producto(catalogo, fila.producto)
            sugerencia = (
                f"; opciones: {', '.join(opciones)}"
                if opciones
                else "; escríbelo como en el catálogo (Demanda MP) o créalo ahí"
            )
            r.errores.append(
                f"inventario, fila {fila.linea}: no pude identificar el producto "
                f"{fila.producto!r} en el catálogo{sugerencia}"
            )
            continue
        producto_id, del_alias = s
        formato_id = _resolver_formato(
            catalogo, formatos.get(producto_id, []), producto_id, fila.formato, del_alias
        )
        if formato_id is None:
            nombres = ", ".join(n for _i, n, _f in formatos.get(producto_id, []))
            r.errores.append(
                f"inventario, fila {fila.linea}: el formato {fila.formato!r} no existe para "
                f"{catalogo.productos[producto_id].nombre!r} (formatos: {nombres or 'ninguno'}); "
                "agrégalo en Demanda MP → Productos"
            )
            continue
        habitual: int | None | op._Sin = op.SIN
        if fila.proveedor_habitual:
            try:
                rut: str | None = normalizar_rut(fila.proveedor_habitual)
            except ValueError:
                rut = None
            prov, _ = indice.resolver(fila.proveedor_habitual, rut)
            if prov is None:
                r.avisos.append(
                    f"inventario, fila {fila.linea}: el proveedor habitual "
                    f"{fila.proveedor_habitual!r} no está en tu empresa; "
                    "quedó sin proveedor habitual"
                )
                habitual = None
            else:
                habitual = prov.id
        try:
            _, estado = op.guardar(
                session,
                empresa_id,
                usuario_id,
                producto_id=producto_id,
                formato_id=formato_id,
                stock=fila.stock,
                stock_minimo=op.SIN if fila.stock_minimo is None else fila.stock_minimo,
                plazo=op.SIN if fila.plazo is None else fila.plazo,
                proveedor_habitual_id=habitual,
                marca=op.SIN if fila.marca is None else fila.marca,
                notas=op.SIN if fila.notas is None else fila.notas,
                referencia="planilla",
            )
        except (op.ErrorInventario, op.InventarioAjeno) as exc:
            r.errores.append(f"inventario, fila {fila.linea}: {exc}")
            continue
        if estado == "nuevo":
            r.nuevos += 1
        elif estado == "actualizado":
            r.actualizados += 1
        else:
            r.sin_cambios += 1
    return r


def generar_plantilla_inventario_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "inventario"
    ws.append(COLUMNAS_INVENTARIO)
    ws.append(["EJEMPLO Arroz grado 1 (borrar)", "saco 25 kg", 4, 2, 7, "Tucapel",
               "EJEMPLO Distribuidora X", "Se pide los lunes"])  # fmt: skip
    ws = wb.create_sheet("Instrucciones")
    for linea in (
        "Hoja 'inventario': una fila por producto y formato que TIENES o vendes.",
        "producto: como se llama en el catálogo (Demanda MP). Si no lo reconoce, la fila "
        "queda con error y te propone opciones.",
        "formato: como se llama el formato en el catálogo ('saco 25 kg') o como se escribiría "
        "('25 kg'). En blanco = la unidad base del producto (kilo, litro o unidad).",
        "stock: cuántas UNIDADES DEL FORMATO tienes ahora (4 sacos de 25 kg = 4). Es el stock "
        "final: la diferencia con el actual queda anotada como ajuste. En blanco = no cambia.",
        "stock_minimo: bajo ese disponible el producto se marca en rojo. plazo_reposicion_dias: "
        "lo que tarda tu proveedor habitual en entregarte.",
        "proveedor_habitual: nombre o RUT de un proveedor ya cargado en Catálogo Proveedores.",
        "Las filas cuyo producto empieza con EJEMPLO se ignoran. Subir el mismo archivo dos "
        "veces no duplica nada.",
    ):
        ws.append([linea])
    ws.column_dimensions["A"].width = 120
    salida = io.BytesIO()
    wb.save(salida)
    return salida.getvalue()
