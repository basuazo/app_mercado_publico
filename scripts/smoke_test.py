"""Smoke test manual — requiere .env con MP_TICKET y DATABASE_URL reales.

NO ejecutar en CI ni en tests automáticos. Solo para verificación manual.

Uso:
    python scripts/smoke_test.py            # smoke test completo (4 requests)
    python scripts/smoke_test.py --fechas   # solo el Paso 0 de F-fecha-cierre (2 requests)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Añadir la raíz del proyecto al path
sys.path.insert(0, str(Path(__file__).parent.parent))

import re  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from app.clients.mp_v1 import MercadoPublicoV1Client  # noqa: E402
from app.clients.mp_v2 import MercadoPublicoV2Client  # noqa: E402
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
        off_desc = "offset explícito: Z (UTC)"
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
    print("  · Si viene con Z o con un offset -> no hay suposición: el código ya lo respeta.")
    print("  · Si NO hay hora -> la mitad de F-fecha-cierre no aplicaba; hay que replantearla.")
    print("  · Anotar el resultado en docs/00-estado-actual.md marcando [V] lo observado.")
    print()
    print("=== Paso 0 completado ===")


def main() -> None:
    load_dotenv()
    settings = Settings()  # type: ignore[call-arg]
    # Mismo helper que `make_engine` y `alembic/env.py`: sin él, una
    # DATABASE_URL sin driver explícito —como la entrega Neon— reventaba con
    # ModuleNotFoundError: psycopg2, que no dice nada de la causa real.
    engine = create_engine(normalizar_url_driver(settings.database_url))

    v1 = MercadoPublicoV1Client(settings, engine)
    v2 = MercadoPublicoV2Client(settings, engine)

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
