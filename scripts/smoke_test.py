"""Smoke test manual — requiere .env con MP_TICKET y DATABASE_URL reales.

NO ejecutar en CI ni en tests automáticos. Solo para verificación manual.

Uso:
    python scripts/smoke_test.py            # smoke test completo (4 requests)
    python scripts/smoke_test.py --fechas   # solo el Paso 0 de F-fecha-cierre (2 requests)
    python scripts/smoke_test.py ventana-ca # sonda de F-ca-ventana (5 variantes, ~5–15 req)
    python scripts/smoke_test.py ventana-ca --solo G1,G2,B2,B3  # prueba de huso
"""

from __future__ import annotations

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
from app.clients.types import CompraAgilBasica  # noqa: E402
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
# docs/00-estado-actual.md.
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
    print("  · Anotar el resultado en docs/00-estado-actual.md marcando [V] lo observado.")
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
    no sea 10500 (regla 3: tope diario, no se sigue pidiendo ese día), ante la
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
            print("    DETENIDA: tope diario (regla 3). No pedir más hoy.")
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


def main() -> None:
    load_dotenv()
    settings = Settings()  # type: ignore[call-arg]
    # Mismo helper que `make_engine` y `alembic/env.py`: sin él, una
    # DATABASE_URL sin driver explícito —como la entrega Neon— reventaba con
    # ModuleNotFoundError: psycopg2, que no dice nada de la causa real.
    engine = create_engine(normalizar_url_driver(settings.database_url))

    v1 = MercadoPublicoV1Client(settings, engine)
    v2 = MercadoPublicoV2Client(settings, engine)

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
