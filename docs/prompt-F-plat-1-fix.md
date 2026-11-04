# Prompt de implementación — F-plat-1-fix (semilla por clave y bandera "sin detalle" solo por anexo) · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet** (dos correcciones acotadas con tests claros). Un commit, **sin push**. Va sobre
> `f235686` (F-plat-1), que **todavía no está en Neon**. Por eso se corrige **la misma migración `a9d4c7e1f2b6`**, sin
> crear otra: en el Postgres local, `alembic downgrade d3b7a9e1c5f8` y luego `upgrade head`. Origen: auditoría de
> Cowork del 4-nov. Lo marcado [V] está verificado en el código.

## 1. La semilla resucita los productos renombrados [V]
**Qué pasa:**
- `sembrar_canasta` (`app/mercado/catalogo.py`) decide si un producto "existe" comparando su `nombre`.
- `/catalogo/productos/{id}` permite renombrar (`catalogo.py` de rutas, `p.nombre = ...`).
- `mercado-catalogo` corre cada 15 min y siembra en cada corrida.

**Consecuencia:** si Boris renombra "Hallulla", a los 15 min la semilla vuelve a crear "Hallulla" y quedan dos
productos. Peor aún: `sugerir_producto` resuelve la regla con `catalogo.por_nombre(regla.nombre)`
(`app/catalogos/canonico.py`), así que las sugerencias por palabra clave pasan a apuntar al duplicado y no al producto
corregido. El test `test_sembrar_no_renombra_ni_reactiva_lo_que_el_usuario_corrigio` da este comportamiento por
correcto (`productos_creados == 1`).

**Corrección:**
- `producto_canonico.clave_semilla` (String(100), nullable, UNIQUE): la clave estable de la regla de la canasta (un
  slug del nombre original o un id propio de `REGLAS`; elegir uno y documentarlo).
- La semilla inserta solo si **no hay ninguna fila con esa clave**, esté activa o no y se llame como se llame.
- Antes de insertar, si ya existe un producto **sin clave** con el mismo nombre ignorando mayúsculas (lo creó un
  usuario), se le asigna la clave y no se inserta otro.
- Las reglas se resuelven por `clave_semilla`, no por nombre: `Catalogo` indexa por clave.
- Tests:
  - renombrar y volver a sembrar → 0 creados, y la regla sugiere el producto renombrado;
  - desactivar y sembrar → no lo reactiva ni lo vuelve a crear;
  - producto creado a mano "hallulla" → la semilla lo adopta y no duplica.

  El test actual se invierte.

## 2. La bandera "sin detalle" mezcla anexo con "sin clasificar" [V]
**Qué pasa:** `recalcular_detalle_lineas` marca `sin_detalle` cuando hay alguna línea `pendiente`, y `pendiente` también
es una línea normal que las reglas no reconocieron. En agosto eso son 335 de 829 textos (informe de Claude Code). La
bandera que pidió Boris es otra: **la CA no trae el detalle (está en un anexo)**. Además, F-plat-2 §1.6 descarga anexos
de las CA con `detalle_lineas = sin_detalle`: con la definición actual gastaría cuota y presupuesto de LLM en CA que sí
traen sus líneas.

**Corrección** (los mismos tres valores, `String(12)`):
- `sin_detalle` = la CA no tiene líneas, o tiene alguna línea `origen = 'anexo'` todavía sin resolver (no `ok`);
- `incompleto` = alguna línea `pendiente` o `dudosa` (sin anexo);
- `completo` = todas las líneas `ok`.

Actualizar las etiquetas (`ETIQUETA_BANDERA`), el docstring, `00-estado.md` y los tests de la bandera. Agregar un caso:
una CA con una línea `api` sin sugerencia queda `incompleto`, no `sin_detalle`.

## 3. Observaciones sin cambio de código (anotar en `00-estado.md`)
- Cualquier usuario con empresa puede editar y **desactivar** un producto del catálogo común (fusionar es solo admin).
  Con 2 usuarios se acepta; revisar al sumar empresas.
- Las líneas se crean solo para alimentos (`G_ALIM`). F-plat-5 debe saberlo: la mesa no tendrá líneas de otros rubros.
- El cursor de `mercado-catalogo` lee `actualizado_en` y `cotizaciones_obtenidas_en`. Cuando entre F-merc-3, sumar
  `detalle_vigente_en` a la condición (reemplazar `ca_productos` no siempre mueve `actualizado_en` de la CA).

## 4. Cierre
`ruff check .`, `python -m mypy app`, `python -m pytest -rs` (0 fallos, 0 errores, 0 saltados) contra `mp-test-pg`.
Changelog: actualizar la entrada de F-plat-1 (no agregar otra). Commit:
`F-plat-1-fix: semilla por clave estable y bandera sin detalle solo por anexo`.
