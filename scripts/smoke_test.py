"""Smoke test manual — requiere .env con MP_TICKET y DATABASE_URL reales.

NO ejecutar en CI ni en tests automáticos. Solo para verificación manual.

Uso:
    python scripts/smoke_test.py            # smoke test completo (4 requests)
    python scripts/smoke_test.py --fechas   # solo el Paso 0 de F-fecha-cierre (2 requests)
    python scripts/smoke_test.py ventana-ca # sonda de F-ca-ventana (5 variantes, ~5–15 req)
    python scripts/smoke_test.py ventana-ca --solo G1,G2,B2,B3  # prueba de huso
    python scripts/smoke_test.py claves-ca  # ¿el LISTADO v2 trae descripción y productos? (2 req)
    python scripts/smoke_test.py claves-lic [código]  # F-datos-1: Comprador/Fechas en v1 (1 req)
    python scripts/smoke_test.py ca-sin-fechas        # F-datos-1: CA sin fechas + desierta (≤5 req)
    python scripts/smoke_test.py estados-ca           # F-datos-1: ¿v2 acepta desierta/cancelada? (2 req)
    python scripts/smoke_test.py --mercado            # F-merc-1: antigüedad (mar-2026, oct-2024) y horizonte 2025 (6 req)
    python scripts/smoke_test.py --detalle-ca COD [COD ...]  # sonda: ofertas repetidas del mismo RUT (1 req/código)
    python scripts/smoke_test.py --documentos [--ruta /v2/...{codigo}...{id}]  # F-plat-2: ¿descarga de anexos? (~3–5 req)
    python scripts/smoke_test.py --adjuntos 2467-760-COT26  # F-plat-anexos-auto: token + listar + 1 adjunto (3 req, sin ticket)
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

# Añadir la raíz del proyecto al path
sys.path.insert(0, str(Path(__file__).parent.parent))

import re  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402

from app.clients.mp_v1 import MercadoPublicoV1Client  # noqa: E402
from app.clients.mp_v2 import MercadoPublicoV2Client  # noqa: E402
from app.clients.types import CompraAgilBasica, RespuestaListadoV2  # noqa: E402
from app.core.db import normalizar_url_driver  # noqa: E402
from app.core.settings import Settings  # noqa: E402

# `load_dotenv()` se llama en `main()`, no al importar: así los tests del
# detector de formatos pueden importar este módulo sin que el .env real (con
# MP_TICKET y la connstring de Neon) se cuele en el entorno del proceso.

# ---------------------------------------------------------------------------
# Paso 0 de F-fecha-cierre — ¿qué manda de verdad la fuente en FechaCierre?
# ---------------------------------------------------------------------------
#
# Regla 20/23: no escribir como hecho lo que no se verificó en la fuente
# primaria. El proyecto interpreta las fechas SIN offset como hora de Chile
# (ver app/core/tiempo.py, marcado [I]); esta comprobación es lo que convierte
# esa suposición en dato verificado [V]. El resultado va a
# docs/archivo/00-estado-actual.md.
#
# Imprime SOLO valores de fecha: el ticket nunca aparece en la salida — no se
# imprime ninguna URL ni ningún parámetro de la request.


# Los tres formatos observados en respuestas reales:
#   '2026-09-22T18:00:00'       ISO con T y segundos
#   '2026-09-22T18:00:00.000Z'  ISO con T, milisegundos y Z
#   '2026-09-22 18:00'          separador ESPACIO y sin segundos (Compra Ágil)
# El detector viejo buscaba la hora por posición fija (`txt[11:19]`) y para el
# tercero informaba "SIN componente de hora" teniendo la hora a la vista. Una
# herramienta de verificación que afirma lo contrario de lo que muestra hace
# cerrar mal una pregunta: reglas 20 y 23.
_RE_FECHA = re.compile(
    r"^(?P<fecha>\d{4}-\d{2}-\d{2})"
    r"(?:[T ](?P<hora>\d{2}:\d{2})(?::(?P<segundos>\d{2}))?(?:\.(?P<fraccion>\d+))?)?"
    r"(?P<offset>[Zz]|[+-]\d{2}:?\d{2})?$"
)


def _describir_fecha(valor: object) -> str:
    """Describe un valor crudo de fecha: si trae hora, si trae segundos y si
    trae offset.

    Las tres por separado, porque son tres preguntas distintas: solo la primera
    decide si el cierre que guardamos es un instante real o uno derivado.
    """
    if not isinstance(valor, str) or not valor.strip():
        return f"{valor!r}  -> sin valor"
    txt = valor.strip()

    if len(txt) == 8 and txt.isdigit():
        return f"{txt!r}  -> formato ddmmaaaa: SIN hora, SIN segundos, SIN offset"

    m = _RE_FECHA.match(txt)
    if m is None:
        return f"{txt!r}  -> formato NO reconocido (mirarlo a mano antes de concluir nada)"

    hora = m.group("hora")
    segundos = m.group("segundos")
    fraccion = m.group("fraccion")
    offset = m.group("offset")

    if hora is None:
        hora_desc = "SIN componente de hora"
        seg_desc = "SIN segundos (no hay hora)"
    else:
        completa = hora if segundos is None else f"{hora}:{segundos}"
        if hora == "00:00" and segundos in (None, "00"):
            hora_desc = f"hora presente pero {completa} (indistinguible de 'solo fecha')"
        else:
            hora_desc = f"hora REAL: {completa}"
        if segundos is None:
            seg_desc = "SIN segundos (solo hh:mm)"
        elif fraccion is not None:
            seg_desc = f"con segundos y fracción: {segundos}.{fraccion}"
        else:
            seg_desc = f"con segundos: {segundos}"

    if offset is None:
        off_desc = "SIN offset  <- se interpreta como hora de Chile [I]"
    elif offset in ("Z", "z"):
        # F-ca-ventana [V]: en la v2 esa Z es falsa, el valor es hora de Chile.
        off_desc = "offset explícito: Z (UTC) — OJO: en la v2 es falsa, es hora de Chile"
    else:
        off_desc = f"offset explícito: {offset}"

    return f"{txt!r}\n        {hora_desc}\n        {seg_desc}\n        {off_desc}"


def verificar_formato_fechas(
    v1: MercadoPublicoV1Client, v2: MercadoPublicoV2Client, muestras: int = 3
) -> None:
    """Paso 0: imprime el formato crudo de las fechas de cierre. 2 requests."""
    from app.clients.mp_v1 import _LICITACIONES
    from app.clients.mp_v2 import _LISTADO, _validar_envelope

    print("=== Paso 0 — formato crudo de FechaCierre (F-fecha-cierre) ===")
    print()

    print("v1 · listado de licitaciones activas — campo 'FechaCierre':")
    data = v1._get(_LICITACIONES, {"estado": "activas"})
    listado = data.get("Listado") or []
    if not isinstance(listado, list) or not listado:
        print("  (el listado vino vacío — no hay nada que observar)")
    else:
        for item in listado[:muestras]:
            if not isinstance(item, dict):
                continue
            print(f"  {item.get('CodigoExterno')}:")
            print(f"    FechaCierre      = {_describir_fecha(item.get('FechaCierre'))}")
            print(f"    FechaPublicacion = {_describir_fecha(item.get('FechaPublicacion'))}")

    print()
    print("v2 · Compra Ágil (1 página) — campo 'fechas.fecha_cierre':")
    payload = _validar_envelope(
        v2._get(
            _LISTADO,
            {"estado": "publicada", "tamano_pagina": 10, "numero_pagina": 1},
        )
    )
    convocatorias = payload.get("convocatorias") or payload.get("items") or []
    if not isinstance(convocatorias, list) or not convocatorias:
        print("  (la página vino vacía — no hay nada que observar)")
        print(f"  claves del payload: {sorted(payload)}")
    else:
        for item in convocatorias[:muestras]:
            if not isinstance(item, dict):
                continue
            fechas = item.get("fechas") or {}
            if not isinstance(fechas, dict):
                fechas = {}
            print(f"  {item.get('codigo')}:")
            print(f"    fecha_cierre        = {_describir_fecha(fechas.get('fecha_cierre'))}")
            print(f"    fecha_publicacion   = {_describir_fecha(fechas.get('fecha_publicacion'))}")
            print(
                f"    fecha_ultimo_cambio = {_describir_fecha(fechas.get('fecha_ultimo_cambio'))}"
            )

    print()
    print("Qué hacer con esto:")
    print("  · Si hay hora real y SIN offset -> confirmar contra la hora de cierre que muestra")
    print("    la ficha del portal para ese mismo código. Si no coincide con Chile, el único")
    print("    cambio necesario es _TZ_SIN_OFFSET en app/core/tiempo.py.")
    print("  · Si viene con un offset (+hh:mm) -> el código lo respeta. Una Z en la v2 NO es UTC:")
    print("    es hora de Chile mal etiquetada (F-ca-ventana); parse_fecha_v2 la ignora.")
    print("  · Si NO hay hora -> la mitad de F-fecha-cierre no aplicaba; hay que replantearla.")
    print(
        "  · Anotar el resultado en docs/01-analisis-api-mercado-publico.md §10 marcando [V] lo observado."
    )
    print()
    print("=== Paso 0 completado ===")


# ---------------------------------------------------------------------------
# Sonda de F-ca-ventana — ¿el 504 del listado depende del ancho de la ventana?
# ---------------------------------------------------------------------------
#
# Hipótesis [I]: la ventana cambio_desde → ahora (~47 h de atraso) es demasiado
# grande para el backend y por eso cada `ca` muere con 504 en la página 1. La
# sonda pide SOLO la página 1 de cinco ventanas distintas y compara. Lo único
# que escribe en la base es el contador de cuota (quota_log), como cualquier
# request real; sync_state solo se LEE.
#
# Segunda pregunta (variantes G1, G2, B2, B3): ¿en qué huso interpreta la API
# cambio_desde/cambio_hasta? La guía oficial solo da ejemplos con `Z`, y la
# primera sonda mostró que los ítems quedan ~3 h antes de lo pedido. Con
# `enviar_utc=True` la sonda manda el instante UTC tal cual, SIN pasar por
# _iso_para_la_api; el cliente no se toca.

_SOLAPAMIENTO = timedelta(minutes=5)
_PAUSA_ENTRE_VARIANTES_S = 60.0
_PAUSA_TRAS_10500_S = 120.0


class VarianteSonda(NamedTuple):
    letra: str
    descripcion: str
    cambio_desde: datetime
    cambio_hasta: datetime | None
    # False: como el cliente (instante UTC → hora de Chile sin offset).
    # True: el instante UTC, sin convertir y sin offset.
    enviar_utc: bool = False
    # Tercera pregunta (H10, H20, A10): ¿la latencia depende de los ítems por
    # página y no del ancho de la ventana?
    tamano_pagina: int = 50


# Ventana fija de la prueba de huso: 21-sep, 15:00–18:00 UTC.
_G_DESDE = datetime(2026, 9, 21, 15, 0, 0)
_G_HASTA = datetime(2026, 9, 21, 18, 0, 0)
_REQUIEREN_CURSOR = frozenset({"A", "C", "D", "E", "A10"})


def variantes_sonda(cursor: datetime, ahora: datetime) -> list[VarianteSonda]:
    """Las cinco ventanas de la sonda, en el orden en que se piden.

    De menor a mayor ventana, con A (lo de hoy) al final: si A deja una consulta
    viva saturando el backend, no contamina a las que vienen después. Las letras
    se mantienen para que el reporte se lea igual que en el prompt.

    `cursor` y `ahora` en UTC naive, como los guarda la base; el cliente los
    pasa al huso de la API al serializar.
    """
    desde = cursor - _SOLAPAMIENTO
    return [
        VarianteSonda("B", "ahora − 2 h, sin hasta", ahora - timedelta(hours=2), None),
        VarianteSonda("D", "cursor − 5 min → cursor + 2 h", desde, cursor + timedelta(hours=2)),
        VarianteSonda("C", "cursor − 5 min → cursor + 6 h", desde, cursor + timedelta(hours=6)),
        VarianteSonda("E", "cursor − 5 min → cursor + 12 h", desde, cursor + timedelta(hours=12)),
        VarianteSonda("A", "cursor − 5 min, sin hasta (lo de hoy)", desde, None),
    ]


def variantes_huso(ahora: datetime) -> list[VarianteSonda]:
    """Las variantes de la prueba de huso. No dependen del cursor."""
    return [
        VarianteSonda("G1", "21-sep 15:00–18:00 UTC, enviado en UTC", _G_DESDE, _G_HASTA, True),
        VarianteSonda("G2", "21-sep 15:00–18:00 UTC, conversión actual", _G_DESDE, _G_HASTA),
        VarianteSonda(
            "B2",
            "ahora − 2 h → ahora − 10 min, enviado en UTC",
            ahora - timedelta(hours=2),
            ahora - timedelta(minutes=10),
            True,
        ),
        VarianteSonda(
            "B3", "ahora − 2 h, sin hasta, conversión actual", ahora - timedelta(hours=2), None
        ),
    ]


def variantes_tamano(cursor: datetime) -> list[VarianteSonda]:
    """Misma ventana, menos ítems por página: aísla la latencia por ítem."""
    g2 = "21-sep 15:00–18:00 UTC, conversión actual"
    return [
        VarianteSonda("H10", f"{g2}, tamano_pagina=10", _G_DESDE, _G_HASTA, tamano_pagina=10),
        VarianteSonda("H20", f"{g2}, tamano_pagina=20", _G_DESDE, _G_HASTA, tamano_pagina=20),
        VarianteSonda(
            "A10",
            "cursor − 5 min, sin hasta, tamano_pagina=10",
            cursor - _SOLAPAMIENTO,
            None,
            tamano_pagina=10,
        ),
    ]


def seleccionar_variantes(
    solo: list[str] | None, cursor: datetime | None, ahora: datetime
) -> list[VarianteSonda]:
    """Sin `solo`, las cinco originales. Con `solo`, esas letras en ese orden.

    ValueError (antes de pedir nada) si una letra no existe o necesita cursor y
    no lo hay.
    """
    if solo is None:
        if cursor is None:
            raise ValueError("sin cursor en sync_state: las variantes A–E no aplican")
        return variantes_sonda(cursor, ahora)
    faltan_cursor = [x for x in solo if x in _REQUIEREN_CURSOR]
    if cursor is None and faltan_cursor:
        raise ValueError(f"sin cursor en sync_state: {','.join(faltan_cursor)} no aplican")
    # Sin cursor, las que lo necesitan ya se rechazaron arriba: `ahora` es relleno.
    cursor_o_relleno = cursor or ahora
    base = (
        variantes_sonda(cursor_o_relleno, ahora)
        + variantes_huso(ahora)
        + variantes_tamano(cursor_o_relleno)
    )
    catalogo = {v.letra: v for v in base}
    desconocidas = [x for x in solo if x not in catalogo]
    if desconocidas:
        raise ValueError(
            f"variantes desconocidas: {','.join(desconocidas)} (hay: {','.join(catalogo)})"
        )
    return [catalogo[x] for x in solo]


def params_variante(var: VarianteSonda, estados: list[str]) -> dict[str, object]:
    """Los params de la página 1 de una variante, armados en la sonda.

    Con `enviar_utc=False` son exactamente los que arma listar_compra_agil (lo
    fija un test); con True, las fechas viajan en UTC sin convertir.
    """
    from app.clients.mp_v2 import _iso_para_la_api

    def formatear(dt: datetime) -> str:
        return dt.isoformat() if var.enviar_utc else _iso_para_la_api(dt)

    params: dict[str, object] = {
        "tamano_pagina": min(var.tamano_pagina, 50),
        "numero_pagina": 1,
        "cambio_desde": formatear(var.cambio_desde),
    }
    if var.cambio_hasta is not None:
        params["cambio_hasta"] = formatear(var.cambio_hasta)
    if estados:
        params["estado"] = ",".join(estados)
    return params


def describir_crudo(
    items_raw: list[dict[str, object]], desde: datetime, hasta: datetime | None
) -> list[str]:
    """fecha_ultimo_cambio tal como llega en el JSON (mínima y máxima) y cuántos
    ítems caen dentro de la ventana PEDIDA, [desde, hasta] en UTC.

    Se lee con parse_fecha_v2, igual que la ingesta: la `Z` es falsa y el valor
    es hora de Chile (verificado en F-ca-ventana). Se imprime además el string
    crudo, que es lo que hay que mirar para concluir.
    """
    from app.clients.types import parse_fecha_v2

    fechas: list[tuple[datetime, str]] = []
    for item in items_raw:
        bloque = item.get("fechas")
        crudo = bloque.get("fecha_ultimo_cambio") if isinstance(bloque, dict) else None
        leida = parse_fecha_v2(crudo)
        if leida is not None and isinstance(crudo, str):
            fechas.append((leida, crudo))
    if not fechas:
        return ["fecha_ultimo_cambio cruda: (ningún ítem la trae)"]
    fechas.sort()
    (min_dt, min_txt), (max_dt, max_txt) = fechas[0], fechas[-1]
    dentro = sum(1 for dt, _ in fechas if dt >= desde and (hasta is None or dt <= hasta))
    tope = f"{hasta.isoformat()}Z" if hasta is not None else "(sin hasta)"
    return [
        f"fecha_ultimo_cambio cruda: min={min_txt!r}  max={max_txt!r}",
        f"  en UTC (Chile → UTC):    min={min_dt.isoformat()}Z  max={max_dt.isoformat()}Z",
        f"dentro de la ventana pedida [{desde.isoformat()}Z, {tope}]: {dentro} de {len(fechas)}",
    ]


def describir_pagina(items: list[CompraAgilBasica], cambio_hasta: datetime | None) -> list[str]:
    """Líneas sobre las fechas de la página: el máximo fecha_ultimo_cambio y, si
    la variante tiene cambio_hasta, si la API lo respetó.

    Las fechas de los ítems ya vienen en UTC naive (parse_fecha_iso), igual que
    cambio_hasta: se comparan directo.
    """
    fechas = [i.fecha_ultimo_cambio for i in items if i.fecha_ultimo_cambio is not None]
    maximo = max(fechas).isoformat() + " UTC" if fechas else "(sin fechas)"
    lineas = [f"max_fecha_ultimo_cambio={maximo}"]
    if cambio_hasta is not None:
        if not items:
            lineas.append("cambio_hasta respetado: sin ítems para verificar")
        elif not fechas:
            lineas.append("cambio_hasta respetado: ningún ítem trae fecha_ultimo_cambio")
        else:
            fuera = sum(1 for f in fechas if f > cambio_hasta)
            if fuera:
                lineas.append(f"cambio_hasta respetado: no ({fuera} ítems posteriores)")
            else:
                lineas.append("cambio_hasta respetado: sí")
    return lineas


def _leer_cursor_ca(engine: Engine) -> datetime | None:
    """Cursor de sync_state 'compra_agil' en UTC naive. SOLO SELECT."""
    with engine.connect() as conn:
        fila = conn.execute(
            text("SELECT cursor FROM sync_state WHERE fuente = :f"), {"f": "compra_agil"}
        ).fetchone()
    if fila is None or not fila[0]:
        return None
    return datetime.fromisoformat(str(fila[0])).replace(tzinfo=None)


def _error_seguro(exc: BaseException) -> str:
    """Tipo y mensaje truncado. Los mensajes de MPError no llevan el ticket (va
    por header), pero igual se trunca por si una excepción de red trae ruido."""
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def correr_sonda(
    v2: MercadoPublicoV2Client,
    variantes: list[VarianteSonda],
    estados: list[str],
    dormir: Callable[[float], None] = time.sleep,
    reloj: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Pide la página 1 de cada variante e imprime lo observado.

    Devuelve las letras que sí se pidieron: la sonda se corta ante un 429 que
    no sea 10500 (regla 3: la API queda en pausa, no se sigue pidiendo), ante la
    cuota local agotada y ante un 401.
    """
    from app.clients.base import (
        MPAuthError,
        MPConcurrencyError,
        MPRateLimitError,
        QuotaExceededError,
    )
    from app.clients.mp_v2 import _LISTADO, _parse_ca_basica, _validar_envelope

    pedidas: list[str] = []
    for i, var in enumerate(variantes):
        if i > 0:
            print(f"  (pausa fija de {_PAUSA_ENTRE_VARIANTES_S:.0f} s)")
            dormir(_PAUSA_ENTRE_VARIANTES_S)
        hasta = var.cambio_hasta.isoformat() if var.cambio_hasta else "—"
        params = params_variante(var, estados)
        modo = "UTC sin convertir" if var.enviar_utc else "conversión actual (→ hora Chile)"
        print(f"\n[{var.letra}] {var.descripcion}")
        print(f"    pedido:  desde={var.cambio_desde.isoformat()}Z  hasta={hasta}Z")
        print(
            f"    viajó ({modo}): cambio_desde={params['cambio_desde']}"
            f"  cambio_hasta={params.get('cambio_hasta', '—')}"
        )
        pedidas.append(var.letra)
        t0 = reloj()
        try:
            payload = _validar_envelope(v2._get(_LISTADO, params))
        except MPConcurrencyError as exc:
            print(f"    -> 429/10500 tras {reloj() - t0:.1f} s  ({_error_seguro(exc)})")
            print(f"    (pausa extra de {_PAUSA_TRAS_10500_S:.0f} s y se sigue)")
            dormir(_PAUSA_TRAS_10500_S)
            continue
        except MPRateLimitError as exc:
            print(f"    -> 429 NO-10500 tras {reloj() - t0:.1f} s  ({_error_seguro(exc)})")
            print("    DETENIDA: API en pausa por 429 de tasa (regla 3). Esperar la pausa.")
            break
        except (QuotaExceededError, MPAuthError) as exc:
            print(f"    -> {_error_seguro(exc)}")
            print("    DETENIDA.")
            break
        except Exception as exc:
            print(f"    -> ERROR tras {reloj() - t0:.1f} s  ({_error_seguro(exc)})")
            continue

        segundos = reloj() - t0
        items_json = payload.get("convocatorias") or payload.get("items") or []
        items_raw = (
            [x for x in items_json if isinstance(x, dict)] if isinstance(items_json, list) else []
        )
        items = [_parse_ca_basica(x) for x in items_raw]
        # Crudo, para distinguir "total_resultados = 0" de "no informado".
        pag = payload.get("paginacion")
        pag = pag if isinstance(pag, dict) else {}
        total_crudo = pag.get("total_resultados")
        total_txt = "(no informado)" if total_crudo is None else str(total_crudo)
        print(f"    -> 200 en {segundos:.1f} s")
        print(
            f"       total_paginas={pag.get('total_paginas', '(no informado)')}"
            f"  (tamano_pagina={params['tamano_pagina']})"
        )
        print(f"       total_resultados={total_txt}")
        print(f"       items en la página={len(items)}")
        for linea in describir_crudo(items_raw, var.cambio_desde, var.cambio_hasta):
            print(f"       {linea}")
        for linea in describir_pagina(items, var.cambio_hasta):
            print(f"       {linea}")
        if items_raw:
            # Todas las fechas del primer ítem tal como llegan: en producción
            # fecha_publicacion está NULL en todas las CA y hay que ver por qué.
            print(f"       fechas crudas del 1er ítem: {items_raw[0].get('fechas')!r}")
    return pedidas


def _leer_solo(argv: list[str]) -> list[str] | None:
    """`--solo G1,G2` → ["G1", "G2"]; sin la opción → None."""
    if "--solo" not in argv:
        return None
    i = argv.index("--solo")
    if i + 1 >= len(argv):
        raise ValueError("--solo necesita una lista de variantes, p. ej. --solo G1,G2")
    return [x.strip().upper() for x in argv[i + 1].split(",") if x.strip()]


def sonda_ventana_ca(
    settings: Settings,
    engine: Engine,
    v2: MercadoPublicoV2Client,
    solo: list[str] | None = None,
) -> None:
    from app.clients.base import QuotaTracker
    from app.core.logging import setup_logging
    from app.core.tiempo import ahora_utc
    from app.ingest.compra_agil import _ESTADOS_VALIDOS

    # Los reintentos y enfriamientos del cliente se ven, pasando por el filtro
    # de secretos del logger raíz.
    setup_logging()

    print("=== Sonda F-ca-ventana — página 1 del listado de Compra Ágil ===")
    try:
        cursor = _leer_cursor_ca(engine)
    except Exception as exc:
        # Solo el tipo: el mensaje de un error de conexión puede traer el host.
        print(f"No se pudo leer sync_state: {type(exc).__name__}")
        return
    ahora = ahora_utc()
    try:
        variantes = seleccionar_variantes(solo, cursor, ahora)
    except ValueError as exc:
        print(f"No se pide nada: {exc}")
        return

    if cursor is None:
        print(f"cursor=(ninguno)   ahora={ahora.isoformat(timespec='seconds')} UTC")
    else:
        atraso_h = (ahora - cursor).total_seconds() / 3600
        print(f"cursor={cursor.isoformat()} UTC   ahora={ahora.isoformat(timespec='seconds')} UTC")
        print(f"atraso={atraso_h:.1f} h")
    print(f"variantes: {','.join(v.letra for v in variantes)}")

    qt = QuotaTracker(engine, settings.api_daily_budget)
    restante_antes = qt.remaining()
    print(f"cuota restante al empezar: {restante_antes} / {settings.api_daily_budget}")

    correr_sonda(v2, variantes, sorted(_ESTADOS_VALIDOS))

    restante_despues = qt.remaining()
    print(f"\nRequests contadas por el QuotaTracker: {restante_antes - restante_despues}")
    print("  (incluye reintentos del cliente; si un cron corrió en paralelo, también las suyas)")
    print(f"cuota restante al terminar: {restante_despues} / {settings.api_daily_budget}")
    print("=== Sonda completada ===")


# ---------------------------------------------------------------------------
# Sonda claves-ca — ¿el listado v2 ya trae descripción y productos?
# ---------------------------------------------------------------------------
#
# Si el listado los trae, bajar un detalle por CA sobra para el matching. Si no,
# es inevitable. Regla 20: esto se resuelve mirando la respuesta real, no el doc.
# Imprime SOLO nombres de claves, largos y conteos: ningún valor, URL ni ticket.

_PISTAS = ("descrip", "product", "item", "detalle", "rubro", "catalog", "unspsc")


def _claves_con_pista(obj: object, ruta: str = "", prof: int = 0) -> list[str]:
    """Rutas de claves cuyo nombre sugiere descripción o productos (hasta 3 niveles)."""
    encontradas: list[str] = []
    if prof > 3:
        return encontradas
    if isinstance(obj, dict):
        for k, v in obj.items():
            r = f"{ruta}.{k}" if ruta else str(k)
            if any(p in str(k).lower() for p in _PISTAS):
                encontradas.append(f"{r} ({_forma(v)})")
            encontradas += _claves_con_pista(v, r, prof + 1)
    elif isinstance(obj, list) and obj:
        encontradas += _claves_con_pista(obj[0], ruta + "[0]", prof + 1)
    return encontradas


def _forma(v: object) -> str:
    """Tipo y tamaño, sin el valor."""
    if v is None:
        return "null"
    if isinstance(v, str):
        return f"str, largo {len(v)}"
    if isinstance(v, list):
        return f"lista de {len(v)}"
    if isinstance(v, dict):
        return f"objeto con claves {sorted(v)}"
    return type(v).__name__


def _describir_item(item: dict[str, object]) -> list[str]:
    lineas = [f"claves de primer nivel: {sorted(item)}"]
    for k in sorted(item):
        if isinstance(item[k], dict):
            lineas.append(f"  {k}: {_forma(item[k])}")
    pistas = _claves_con_pista(item)
    lineas.append("claves con pista de descripción/productos:")
    lineas += [f"  {x}" for x in pistas] or ["  (ninguna)"]
    return lineas


def sonda_claves_ca(v2: MercadoPublicoV2Client) -> None:
    from app.clients.base import MPConcurrencyError, MPRateLimitError
    from app.clients.mp_v2 import _DETALLE, _LISTADO, _validar_envelope

    print("=== Sonda claves-ca: listado v2 vs detalle ===\n")
    params = {"tamano_pagina": 10, "numero_pagina": 1, "estado": "publicada"}
    try:
        payload = _validar_envelope(v2._get(_LISTADO, params))
    except (MPConcurrencyError, MPRateLimitError) as exc:
        print(f"LISTADO -> {_error_seguro(exc)}. DETENIDA.")
        return
    except Exception as exc:
        print(f"LISTADO -> ERROR {_error_seguro(exc)}. Reintenta más tarde.")
        return
    print(f"claves del payload del listado: {sorted(payload)}")
    items_json = payload.get("convocatorias") or payload.get("items") or []
    items = [x for x in items_json if isinstance(x, dict)] if isinstance(items_json, list) else []
    print(f"items en la página: {len(items)}\n")
    if not items:
        return
    print("[LISTADO] primer ítem:")
    for linea in _describir_item(items[0]):
        print(f"  {linea}")
    print("\n[LISTADO] por ítem: descripcion / productos_solicitados")
    for it in items:
        d = it.get("descripcion")
        p = it.get("productos_solicitados")
        print(
            f"  {str(it.get('codigo'))[:25]:25}  descripcion={_forma(d)}"
            f"  productos_solicitados={_forma(p)}"
        )

    codigo = str(items[0].get("codigo") or "")
    if not codigo:
        return
    print(f"\n(pausa de 5 s; luego el detalle de {codigo})")
    time.sleep(5)
    try:
        det = _validar_envelope(v2._get(_DETALLE.format(codigo=codigo)))
    except Exception as exc:
        print(f"DETALLE -> {_error_seguro(exc)}. El listado ya quedó arriba: con eso basta.")
        return
    print("\n[DETALLE] mismo código:")
    for linea in _describir_item(det):
        print(f"  {linea}")
    li, de = items[0], det
    for campo in ("descripcion", "productos_solicitados"):
        a, b = li.get(campo), de.get(campo)
        igual = "IGUALES" if a == b else "DISTINTOS"
        print(f"  {campo}: listado={_forma(a)}  detalle={_forma(b)}  -> {igual}")
    if isinstance(li.get("productos_solicitados"), list) and li["productos_solicitados"]:
        print(f"  claves de un producto (listado): {sorted(li['productos_solicitados'][0])}")
    if isinstance(de.get("productos_solicitados"), list) and de["productos_solicitados"]:
        print(f"  claves de un producto (detalle): {sorted(de['productos_solicitados'][0])}")


# ---------------------------------------------------------------------------
# Sondas de F-datos-1 — ¿dónde viven organismo, región y fechas?
# ---------------------------------------------------------------------------
#
# raw_json guarda el dataclass ya parseado, no la respuesta: un campo que el
# parser no lee no deja rastro en la base. Regla 23: los nombres se confirman
# en la respuesta real antes de escribir el parseo. Ninguna sonda imprime URL,
# parámetros de la request ni el ticket.


def _hojas(obj: object, ruta: str = "") -> list[str]:
    """Rutas de todas las claves hoja (recursivo; de las listas, solo el [0])."""
    if isinstance(obj, dict):
        if not obj:
            return [f"{ruta} (objeto vacío)"]
        salida: list[str] = []
        for k in obj:
            salida += _hojas(obj[k], f"{ruta}.{k}" if ruta else str(k))
        return salida
    if isinstance(obj, list):
        if not obj:
            return [f"{ruta} (lista vacía)"]
        return _hojas(obj[0], f"{ruta}[0]") + ([f"{ruta} (lista de {len(obj)})"])
    return [f"{ruta} ({_forma(obj)})"]


def _codigo_lic_vigente(engine: Engine) -> str | None:
    """Licitación vigente creada más recientemente. SOLO SELECT."""
    from sqlalchemy import select

    from app.core.tiempo import ahora_utc
    from app.core.vigencia import condicion_lic_vigente
    from app.models.tables import Licitacion

    with engine.connect() as conn:
        return conn.execute(
            select(Licitacion.codigo)
            .where(condicion_lic_vigente(ahora_utc()))
            .order_by(Licitacion.creado_en.desc())
            .limit(1)
        ).scalar()


def sonda_claves_lic(engine: Engine, v1: MercadoPublicoV1Client, codigo: str | None) -> None:
    """1 request: detalle v1 de una licitación vigente; claves hoja y valores de
    Comprador.*, Fechas.*, Tipo y CodigoTipo (organismo y fechas: no sensibles)."""
    from app.clients.mp_v1 import _LICITACIONES

    print("=== Sonda claves-lic: detalle v1 de 1 licitación ===\n")
    codigo = codigo or _codigo_lic_vigente(engine)
    if not codigo:
        print("No hay licitación vigente en la base; pasa el código como argumento.")
        return
    print(f"código: {codigo}")
    try:
        data = v1._get(_LICITACIONES, {"codigo": codigo}, reintentar_transitorios=False)
    except Exception as exc:
        print(f"DETALLE -> {_error_seguro(exc)}. DETENIDA.")
        return
    listado = data.get("Listado")
    if not isinstance(listado, list) or not listado or not isinstance(listado[0], dict):
        print(f"Listado vacío o con otra forma. Claves de la respuesta: {sorted(data)}")
        return
    lic = listado[0]
    print(f"\nclaves de primer nivel: {sorted(lic)}\n")
    print("claves hoja de Listado[0]:")
    for h in _hojas(lic):
        print(f"  {h}")
    for bloque in ("Comprador", "Fechas"):
        valor = lic.get(bloque)
        print(f"\n{bloque}:")
        if isinstance(valor, dict):
            for k, v in valor.items():
                print(f"  {k} = {v!r}")
        else:
            print(f"  ({_forma(valor)})")
    for campo in ("Tipo", "CodigoTipo", "FechaPublicacion", "FechaCierre", "CodigoOrganismo"):
        print(f"{campo} (primer nivel) = {lic.get(campo)!r}")


def _describir_estado(v: object) -> object:
    return v.get("codigo") if isinstance(v, dict) else v


def sonda_ca_sin_fechas(engine: Engine, v2: MercadoPublicoV2Client) -> None:
    """≤ 5 requests: detalle de 3 CA publicadas sin fecha_publicacion, 1 página
    del listado en la hora de su fecha_ultimo_cambio y 1 listado con
    estado=desierta (¿la API acepta el slug?)."""
    from app.clients.mp_v2 import _DETALLE, _LISTADO, _iso_para_la_api, _validar_envelope

    print("=== Sonda ca-sin-fechas ===\n")
    with engine.connect() as conn:
        filas = conn.execute(
            text(
                "SELECT codigo, fecha_ultimo_cambio, creado_en FROM compras_agiles"
                " WHERE estado = 'publicada' AND fecha_publicacion IS NULL"
                " ORDER BY creado_en DESC LIMIT 3"
            )
        ).fetchall()
        total = conn.execute(
            text(
                "SELECT count(*) FROM compras_agiles"
                " WHERE estado = 'publicada' AND fecha_publicacion IS NULL"
            )
        ).scalar()
    print(f"CA publicadas sin fecha_publicacion en la base: {total}")
    if not filas:
        print("Ninguna: nada que sondear.")
        return

    ultimo_cambio: datetime | None = None
    for codigo, fuc, creado in filas:
        print(f"\n[DETALLE] {codigo}  (fecha_ultimo_cambio base={fuc}, creado_en={creado})")
        if fuc is not None and ultimo_cambio is None:
            ultimo_cambio = fuc
        try:
            det = _validar_envelope(
                v2._get(_DETALLE.format(codigo=codigo), reintentar_transitorios=False)
            )
        except Exception as exc:
            print(f"  -> {_error_seguro(exc)}")
            continue
        fechas = det.get("fechas")
        print(f"  claves de primer nivel: {sorted(det)}")
        if isinstance(fechas, dict):
            for k, v in fechas.items():
                print(f"  fechas.{k} = {v!r}")
        else:
            print(f"  fechas: {_forma(fechas)}")
        otras = [h for h in _hojas(det) if "fecha" in h.lower() and not h.startswith("fechas.")]
        print(f"  otras claves con 'fecha': {otras or '(ninguna)'}")

    if ultimo_cambio is None:
        print("\nNinguna tiene fecha_ultimo_cambio: se omite la página del listado.")
    else:
        desde = ultimo_cambio.replace(minute=0, second=0, microsecond=0)
        hasta = desde + timedelta(hours=1)
        print(f"\n[LISTADO] ventana UTC {desde} → {hasta}, estado=publicada, tamano_pagina=20")
        params: dict[str, object] = {
            "cambio_desde": _iso_para_la_api(desde),
            "cambio_hasta": _iso_para_la_api(hasta),
            "estado": "publicada",
            "tamano_pagina": 20,
            "numero_pagina": 1,
        }
        try:
            payload = _validar_envelope(v2._get(_LISTADO, params, reintentar_transitorios=False))
            items_json = payload.get("convocatorias") or payload.get("items") or []
            lista = items_json if isinstance(items_json, list) else []
            items = [x for x in lista if isinstance(x, dict)]
            print(f"  items: {len(items)}")
            buscados = {str(f[0]) for f in filas}
            for it in items:
                f = it.get("fechas")
                desc = f"claves {sorted(f)}" if isinstance(f, dict) else _forma(f)
                marca = "  <- de la base" if str(it.get("codigo")) in buscados else ""
                print(f"  {str(it.get('codigo'))[:25]:25}  fechas: {desc}{marca}")
                if not isinstance(f, dict) or not f.get("fecha_publicacion"):
                    otras = [h for h in _hojas(it) if "fecha" in h.lower()]
                    print(f"    sin fecha_publicacion; claves con 'fecha': {otras}")
        except Exception as exc:
            print(f"  -> {_error_seguro(exc)}")

    sonda_estados_ca(v2, ["desierta"])


def sonda_estados_ca(v2: MercadoPublicoV2Client, slugs: list[str]) -> None:
    """1 request por slug: ¿el parámetro `estado` del listado v2 lo acepta?
    tamano_pagina=10 es el mínimo que admite la API [V, sonda ca-sin-fechas]."""
    from app.clients.mp_v2 import _LISTADO, _validar_envelope

    for i, slug in enumerate(slugs):
        if i:
            time.sleep(5)
        print(f"\n[LISTADO] estado={slug}, tamano_pagina=10 (¿acepta el slug?)")
        try:
            payload = _validar_envelope(
                v2._get(
                    _LISTADO,
                    {"estado": slug, "tamano_pagina": 10, "numero_pagina": 1},
                    reintentar_transitorios=False,
                )
            )
        except Exception as exc:
            print(f"  -> {_error_seguro(exc)}")
            continue
        items_json = payload.get("convocatorias") or payload.get("items") or []
        lista = items_json if isinstance(items_json, list) else []
        estados = [_describir_estado(x.get("estado")) for x in lista if isinstance(x, dict)]
        print(f"  -> 200 OK; paginacion={payload.get('paginacion')}; estados={estados}")


# F-merc-1 §1.1, punto 1 (antigüedad): ¿hasta dónde llega `publicado_desde` hacia atrás?
# Agosto 2026 ya es [V] (sonda 31-oct); faltan marzo 2026 (mes sin Ñuble en COT_) y el
# piso del backfill (oct 2024). Las claves del detalle (puntos 2 y 3) ya son [V].
_VENTANAS_ANTIGUEDAD = (("2026-03-01", "2026-03-07"), ("2024-10-01", "2024-10-07"))
# Barrido del horizonte (tras la sonda del 3-nov: mar-2026 = 600, oct-2024 = 0 [V]):
# primera semana de cada trimestre de 2025. Con eso se fija MERCADO_BACKFILL_PRIMER_MES.
_MESES_HORIZONTE = ("2025-01", "2025-04", "2025-07", "2025-10")


def _listar_semana(v2: MercadoPublicoV2Client, desde: str, hasta: str) -> RespuestaListadoV2:
    """Listado de región 16, publicado desde → hasta (días en hora de Chile), sin `estado`."""
    from datetime import date

    from app.core.tiempo import borde_del_dia_utc_naive

    return v2.listar_compra_agil(
        publicado_desde=borde_del_dia_utc_naive(date.fromisoformat(desde), fin_de_dia=False),
        publicado_hasta=borde_del_dia_utc_naive(date.fromisoformat(hasta), fin_de_dia=True),
        regiones=[16],
        tamano_pagina=10,
        ordenar_por="FechaPublicacion",
    )


def sonda_mercado(v2: MercadoPublicoV2Client, pausa_s: float = 5.0) -> None:
    """6 requests: listado de región 16 por semana de publicación, SIN `estado`.

    Sin `estado` porque `estado=proveedor_seleccionado` devuelve 0 [V, 31-oct]. Las dos
    primeras imprimen total_resultados, 3 códigos y los estados de la página; el barrido
    del horizonte, solo mes y total_resultados. Ni RUT ni montos. Entre requests, `pausa_s`
    además del rate limit del cliente.
    """
    from collections import Counter

    for i, (desde, hasta) in enumerate(_VENTANAS_ANTIGUEDAD):
        if i:
            time.sleep(pausa_s)
        print(f"\n[LISTADO] region=16, publicado {desde} → {hasta}, sin estado, tamano_pagina=10")
        try:
            resp = _listar_semana(v2, desde, hasta)
        except Exception as exc:
            print(f"  -> {_error_seguro(exc)}")
            continue
        estados = Counter(ca.estado for ca in resp.items)
        print(f"  -> total_resultados={resp.paginacion.total_resultados}")
        print(f"  -> códigos: {[ca.codigo for ca in resp.items[:3]]}")
        print(f"  -> estados en la página: {dict(estados)}")

    print("\n[HORIZONTE] region=16, días 1–7 de cada mes, sin estado")
    for mes in _MESES_HORIZONTE:
        time.sleep(pausa_s)
        try:
            resp = _listar_semana(v2, f"{mes}-01", f"{mes}-07")
        except Exception as exc:
            print(f"  {mes}: {_error_seguro(exc)}")
            continue
        print(f"  {mes}: total_resultados={resp.paginacion.total_resultados}")
    print(
        "\nAnotar en docs/01-analisis-api-mercado-publico.md §10 con [V]. Fijar "
        "MERCADO_BACKFILL_PRIMER_MES en el primer mes con total > 0 (regla 23: un 0 no prueba "
        "que la API no tenga datos de antes; solo acota el horizonte)."
    )


# ---------------------------------------------------------------------------
# Sonda --detalle-ca — ¿qué distingue a las ofertas repetidas del mismo RUT?
# ---------------------------------------------------------------------------
#
# Pide el JSON CRUDO del detalle (sin `_parse_cotizacion`, que es lo que se evalúa).
# El cliente no expone el crudo: se usa `_get` (devuelve el envelope) y
# `_validar_envelope` (devuelve el payload), los mismos pasos que `detalle_compra_agil`.
# Sin reintentos transitorios, sin escritura en Neon, 1 req por código (el rate
# limit de 1 req/s lo pone el cliente). Ante un 429 se imprime y se sale (regla 3).


def _estado_txt(v: object) -> str:
    if isinstance(v, dict):
        return f"{v.get('id')}/{v.get('glosa') or v.get('codigo')}"
    return str(v)


def _suma_productos(prods: object) -> tuple[int, float]:
    if not isinstance(prods, list):
        return 0, 0.0
    total = 0.0
    for p in prods:
        if isinstance(p, dict):
            with contextlib.suppress(TypeError, ValueError):
                total += float(p.get("monto_total_producto") or 0)
    return len(prods), total


def _sin_id(oferta: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in oferta.items() if k != "id_cotizacion"}


def analizar_detalle_ca(codigo: str, payload: dict[str, object]) -> list[str]:
    """Líneas de salida para un detalle crudo. Pura: no toca red ni disco."""
    out = [f"\n=== {codigo} ==="]
    raw = payload.get("proveedores_cotizando")
    ofertas = [o for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []
    por_rut: dict[str, list[dict[str, object]]] = {}
    for o in ofertas:
        por_rut.setdefault(str(o.get("rut_proveedor")), []).append(o)
    out.append(f"ofertas={len(ofertas)}  RUT distintos={len(por_rut)}")
    if ofertas:
        out.append(f"claves de la 1.ª oferta: {sorted(ofertas[0].keys())}")
    repetidos = {r: os_ for r, os_ in por_rut.items() if len(os_) > 1}
    if not repetidos:
        out.append("(sin RUT repetidos)")
        return out
    for rut, os_ in repetidos.items():
        out.append(f"\nRUT {rut}: {len(os_)} ofertas")
        for o in os_:
            n, suma = _suma_productos(o.get("productos_cotizados"))
            fechas = {k: v for k, v in o.items() if "fecha" in k.lower()}
            out.append(
                f"  - {str(o.get('razon_social'))[:30]!r} id_cot={o.get('id_cotizacion')} "
                f"estado={_estado_txt(o.get('estado', o.get('estado_cotizacion')))} "
                f"sel={o.get('proveedor_seleccionado')} suc={o.get('codigo_sucursal_empresa')} "
                f"monto_total={o.get('monto_total')} prods={n} suma_prod={suma:g}"
            )
            extra = {
                k: o.get(k)
                for k in ("activo", "estado_por_comprador", "id_oc", "valor_neto", "monto_despacho")
                if k in o
            }
            out.append(f"      fechas={fechas} otros={extra}")
        for i in range(len(os_)):
            for j in range(i + 1, len(os_)):
                if _sin_id(os_[i]) == _sin_id(os_[j]):
                    out.append(f"  >> IDÉNTICAS (salvo id_cotizacion): #{i + 1} y #{j + 1}")
    return out


def sonda_detalle_ca(
    engine: Engine, v2: MercadoPublicoV2Client, codigos: list[str], ticket: str
) -> None:
    from app.clients.base import MPRateLimitError, leer_pausa
    from app.clients.mp_v2 import _DETALLE, _validar_envelope
    from app.core.tiempo import ahora_utc

    pausa = leer_pausa(engine)
    # leer_pausa devuelve UTC con zona; ahora_utc() es UTC naive (como la base).
    if pausa is not None and pausa.replace(tzinfo=None) > ahora_utc():
        print(f"Pausa vigente hasta {pausa.isoformat()} UTC. No se pide nada.")
        return

    lineas: list[str] = []
    for i, codigo in enumerate(codigos):
        if i:
            time.sleep(1.0)
        try:
            data = v2._get(_DETALLE.format(codigo=codigo), reintentar_transitorios=False)
            payload = _validar_envelope(data)
        except MPRateLimitError as exc:
            lineas.append(f"\n{codigo}: 429 -> {_error_seguro(exc)}. Se corta (regla 3).")
            break
        except Exception as exc:
            lineas.append(f"\n{codigo}: {_error_seguro(exc)}")
            continue
        lineas.extend(analizar_detalle_ca(codigo, payload))

    texto = "\n".join(lineas)
    if ticket and ticket in texto:
        print("ERROR: el valor del ticket aparece en la salida; no se escribe ni se imprime.")
        sys.exit(1)
    print(texto)
    from zoneinfo import ZoneInfo

    destino = Path(__file__).parent.parent / "data" / "logs"
    destino.mkdir(parents=True, exist_ok=True)
    archivo = (
        destino
        / f"sonda_ofertas_{ahora_utc().astimezone(ZoneInfo('America/Santiago')).date().isoformat()}.txt"
    )
    archivo.write_text(texto + "\n", encoding="utf-8")
    print(f"\nSalida guardada en {archivo}")


# ---------------------------------------------------------------------------
# Sonda de F-plat-2 — ¿la API v2 entrega los anexos de una Compra Ágil?
# ---------------------------------------------------------------------------
#
# La guía v3.0 documenta `documentos[].{id, nombre}` en el detalle y NO documenta la descarga:
# que no exista es [I] hasta probarlo (regla 20). Imprime solo claves, status, content-type y
# largo: ningún valor del documento, URL con parámetros ni el ticket. ~3–5 requests. El bundle
# JS del buscador (docs/04 §0) responde 403 de CloudFront a clientes sin navegador [V, 4-nov]:
# una ruta que aparezca ahí (DevTools → Network al bajar un adjunto) se prueba con --ruta.


def _ca_con_documentos(engine: Engine) -> list[str]:
    """Códigos candidatos: primero con `n_documentos > 0` (F-merc-3), luego de agosto 2026."""
    with engine.connect() as c:
        filas = c.execute(
            text(
                "SELECT codigo FROM compras_agiles WHERE region = 16 AND ("
                " n_documentos > 0 OR (fecha_publicacion >= '2026-08-01'"
                " AND fecha_publicacion < '2026-09-01'))"
                " ORDER BY (n_documentos > 0) DESC NULLS LAST, fecha_publicacion DESC LIMIT 3"
            )
        ).fetchall()
    return [str(f[0]) for f in filas]


def sonda_documentos(engine: Engine, v2: MercadoPublicoV2Client, rutas_extra: list[str]) -> None:
    from app.clients.base import MPRateLimitError, leer_pausa
    from app.clients.mp_v2 import _BASE, _DETALLE, _DOCUMENTO_CANDIDATAS, _validar_envelope
    from app.core.tiempo import ahora_utc

    print("=== Sonda documentos (F-plat-2 §0.a) ===\n")
    pausa = leer_pausa(engine)
    if pausa is not None and pausa.replace(tzinfo=None) > ahora_utc():
        print(f"Pausa vigente hasta {pausa.isoformat()} UTC. No se pide nada.")
        return
    codigo, doc_id = "", ""
    for cand in _ca_con_documentos(engine):
        try:
            det = _validar_envelope(
                v2._get(_DETALLE.format(codigo=cand), reintentar_transitorios=False)
            )
        except MPRateLimitError as exc:
            print(f"DETALLE {cand}: 429 -> {_error_seguro(exc)}. Se corta (regla 3).")
            return
        except Exception as exc:
            print(f"DETALLE {cand}: {_error_seguro(exc)}")
            continue
        docs = det.get("documentos")
        print(f"1. {cand}: documentos = {_forma(docs)}")
        if isinstance(docs, list) and docs and isinstance(docs[0], dict):
            print(f"   claves de documentos[0]: {sorted(docs[0])}")
            codigo, doc_id = cand, str(docs[0].get("id") or "")
            break
        time.sleep(1.0)
    if not codigo or not doc_id:
        print("\nNinguna CA candidata trae documentos con id. Prueba con otra CA (--detalle-ca).")
        return

    rutas = [r.format(codigo=codigo, id=doc_id) for r in _DOCUMENTO_CANDIDATAS]
    rutas += [_BASE + r.format(codigo=codigo, id=doc_id) for r in rutas_extra]
    http = v2._client._http
    quota = v2._client._quota
    print(f"\n2. Descarga del documento {doc_id[:6]}… de {codigo} ({len(rutas)} rutas, 1 req c/u):")
    for i, url in enumerate(rutas, start=1):
        time.sleep(1.5)
        quota.check_budget()
        v2._client._rate_limiter.acquire()
        try:
            r = http.get(url)
        except Exception as exc:
            print(f"   ruta {i}: {type(exc).__name__}")
            continue
        finally:
            quota.consume()
        ruta_txt = url.replace(_BASE, "").replace(codigo, "{codigo}").replace(doc_id, "{id}")
        print(
            f"   ruta {i} {ruta_txt}: HTTP {r.status_code}  content-type="
            f"{r.headers.get('content-type', '(ninguno)')}  largo={len(r.content)}"
        )
        if r.status_code == 429:
            v2._client._handle_response(r)  # persiste la pausa (regla 3); lanza
    print("\n3. Anotar el resultado en docs/01-analisis-api-mercado-publico.md §8 con [V].")


# ---------------------------------------------------------------------------
# Sonda de F-plat-anexos-auto — servicio de adjuntos de la ficha pública
# ---------------------------------------------------------------------------
#
# 1 request de token + 1 de listar + 1 de descarga (máx. 3 si hay redirects), 1 req/s. Imprime solo status,
# claves de la respuesta del token (sin valores), vencimiento, content-type y tamaño del archivo.
# Ningún token, clave ni header sale por pantalla. Un 403/429 se informa y NO se reintenta
# (regla 9: si con los headers da 403, parar y reportar; no probar otras formas de entrar).


def sonda_adjuntos(engine: Engine, settings: Settings, codigo: str) -> None:
    import httpx

    from app.clients import mp_documentos as md

    print(f"=== Sonda adjuntos (F-plat-anexos-auto §1) — {codigo} ===\n")
    print("Requests: 1 token + 1 listar + 1 descarga (1 req/s). Sin ticket ni cuota de la API.\n")
    if not settings.mp_adjuntos_user_key:
        print("Falta MP_ADJUNTOS_USER_KEY en el .env.")
        return
    guardados: list[str] = []
    try:
        with engine.connect() as c:
            fila = c.execute(
                text("SELECT documentos FROM compras_agiles WHERE codigo = :c"), {"c": codigo}
            ).first()
        if fila is not None:
            guardados = [
                str(d.get("id")) for d in (fila[0] or []) if isinstance(d, dict) and d.get("id")
            ]
    except Exception as exc:
        print(f"(no se pudo leer la base: {type(exc).__name__}; sigo sin comparar ids)")

    class _Espia(httpx.Client):
        """Anota status, content-type, tamaño y nombres de headers enviados, sin valores."""

        def send(self, request: httpx.Request, **kw: object) -> httpx.Response:  # type: ignore[override]
            r = super().send(request, **kw)  # type: ignore[arg-type]
            es_token = str(request.url) == md.URL_TOKEN
            ruta = request.url.path if es_token else request.url.path.rsplit("/", 1)[0] + "/…"
            largo = r.headers.get("content-length", "?")
            print(f"  {request.method} {request.url.host}{ruta}: HTTP {r.status_code}"
                  f"  content-type={r.headers.get('content-type', '(ninguno)')}  largo={largo}")  # fmt: skip
            print(f"    headers enviados: {sorted(k.lower() for k in request.headers)}")
            if r.status_code in (400, 401, 403):
                print("    rechazado: anotar el status; NO agregar origin/referer por cuenta propia")
            return r

    cliente = md.DocumentosClient.desde_settings(
        settings, http=_Espia(timeout=md.TIMEOUT_S, follow_redirects=False)
    )
    try:
        print("1. listar (pide antes el token):")
        lst = cliente.listar(codigo)
        if lst.archivos is None:
            print(f"\nSin listado: {lst.motivo}.")
            return
        print(f"   {len(lst.archivos)} archivo(s): "
              + "; ".join(f"{i[:8]}… «{n}»" for i, n in lst.archivos))  # fmt: skip
        if guardados:
            iguales = {g.upper() for g in guardados} == {i.upper() for i, _ in lst.archivos}
            print(f"   ids guardados en la base: {len(guardados)}; ¿iguales a listar? {iguales}")
        else:
            print("   la base no tiene documentos[].id de esta CA (se usa listar, como el job)")
        if not lst.archivos:
            return
        doc_id, nombre = lst.archivos[0]
        print(f"\n2. descargar «{nombre}»:")
        d = cliente.descargar_adjunto(codigo, doc_id)
    except md.DescargaBloqueada as exc:
        print(f"\nBLOQUEADO (HTTP {exc.status}). Parar y reportar: no probar otras formas (regla 9).")
        return
    except (md.TokenNoDisponible, md.DescargaTransitoria) as exc:
        print(f"\nSin archivo: {type(exc).__name__} ({exc}).")
        return
    if d.data is None:
        print(f"\nSin archivo: {d.motivo}.")
        return
    from app.mercado.anexos import detectar_mime

    print(f"\nOK: {len(d.data):,} bytes, tipo detectado {detectar_mime(d.data) or 'desconocido'}.")
    print("Anotar el resultado en docs/01-analisis-api-mercado-publico.md R8 con [V].")


def main() -> None:
    load_dotenv()
    settings = Settings()  # type: ignore[call-arg]
    # Mismo helper que `make_engine` y `alembic/env.py`: sin él, una
    # DATABASE_URL sin driver explícito —como la entrega Neon— reventaba con
    # ModuleNotFoundError: psycopg2, que no dice nada de la causa real.
    engine = create_engine(normalizar_url_driver(settings.database_url))

    v1 = MercadoPublicoV1Client(settings, engine)
    v2 = MercadoPublicoV2Client(settings, engine)

    args = sys.argv[1:]
    if args[:1] == ["--adjuntos"]:
        if len(args) < 2:
            print("Uso: --adjuntos CODIGO")
            return
        try:
            sonda_adjuntos(engine, settings, args[1])
        except Exception as exc:
            print(f"Sonda cortada: {type(exc).__name__}")
        return

    if args[:1] == ["--documentos"]:
        extra = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--ruta"]
        try:
            sonda_documentos(engine, v2, extra)
        except Exception as exc:
            print(f"Sonda cortada: {_error_seguro(exc)}")
        return

    if args[:1] == ["--detalle-ca"]:
        if len(args) < 2:
            print("Uso: --detalle-ca CODIGO [CODIGO ...]")
            return
        sonda_detalle_ca(engine, v2, args[1:], settings.mp_ticket)
        return

    if "--mercado" in args:
        sonda_mercado(v2)
        return

    if args[:1] == ["claves-lic"]:
        sonda_claves_lic(engine, v1, args[1] if len(args) > 1 else None)
        return

    if args[:1] == ["estados-ca"]:
        sonda_estados_ca(v2, ["desierta", "cancelada"])
        return

    if args[:1] == ["ca-sin-fechas"]:
        sonda_ca_sin_fechas(engine, v2)
        return

    if "claves-ca" in sys.argv[1:]:
        sonda_claves_ca(v2)
        return

    if "ventana-ca" in sys.argv[1:]:
        sonda_ventana_ca(settings, engine, v2, solo=_leer_solo(sys.argv[1:]))
        return

    if "--fechas" in sys.argv:
        verificar_formato_fechas(v1, v2)
        return

    print("=== Smoke Test F1 ===\n")

    # 1. Licitaciones activas
    activas = v1.licitaciones_activas()
    print(f"Licitaciones activas: {len(activas)}")
    if activas:
        print(f"  Primera: {activas[0].codigo} — {activas[0].nombre[:60]}")

        # 2. Detalle de la primera licitación
        detalle = v1.licitacion_detalle(activas[0].codigo)
        print(f"\nDetalle licitación {detalle.codigo}:")
        print(f"  Nombre: {detalle.nombre[:60]}")
        print(f"  Estado: {detalle.estado}")
        print(f"  Ítems: {len(detalle.items)}")

    # 3. Primera página de Compras Ágiles publicadas
    print("\nCompras Ágiles publicadas (1 página):")
    resp = v2.listar_compra_agil(estados=["publicada"], tamano_pagina=10)
    print(f"  Total resultados: {resp.paginacion.total_resultados}")
    print(f"  Total páginas: {resp.paginacion.total_paginas}")
    if resp.items:
        ca = resp.items[0]
        print(f"  Primera: {ca.codigo} — {ca.nombre[:60]}")

    # 4. Cuota restante
    from app.clients.base import QuotaTracker

    qt = QuotaTracker(engine, settings.api_daily_budget)
    print(f"\nCuota restante hoy: {qt.remaining()} / {settings.api_daily_budget}")
    print("\n=== Smoke test completado ===")


if __name__ == "__main__":
    main()
