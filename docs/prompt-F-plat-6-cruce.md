# Prompt — F-plat-6-cruce (cruce CA ↔ OC en demanda) · 5-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit, sin migración (salvo que §2 decida poblar una columna existente;
> sin columnas nuevas), sin push. [V] verificado, [I] inferido.

## Hallazgo (log de Actions `mercado-indices`, 5-nov 20:06, producción)
- `mercado-demanda`: `medicion.c_cas_con_oc = 0`, `series.cas_con_oc = 0`, `cas_sin_oc = 4231` [V]. Ninguna CA cruzó
  con su OC, así que (c) no se puede medir (la clave queda en RUT por defecto) y la regla de doble conteo trata todas
  las CA como «sin OC». Hoy no infla nada porque `lineas_ca_ok_sumadas = 0`, pero en cuanto haya líneas `ok` sumaría
  lo pedido y lo adjudicado dos veces.
- Causa probable [V en el código, I en los datos]: `demanda.py` cruza por `compras_agiles.id_orden_compra`
  (líneas ~286 y ~556), pero esa columna solo la llena el job viejo `ca` (`app/ingest/compra_agil.py:161`), apagado
  desde el 31-oct. La ingesta actual (`app/ingest/mercado.py`) guarda el dato en **`ca_cotizaciones.id_oc`**
  (línea ~235) y nunca en `compras_agiles.id_orden_compra`.
- No verificado [I]: que `ca_cotizaciones.id_oc` tenga el mismo formato que `oc_lineas_ca.codigo_oc` (p. ej.
  `1234-56-SE26`); `docs/01-analisis-api-mercado-publico.md` dice que en v2 `codigo_orden_compra` viene null y que
  `id_orden_compra` es lo confiable, pero no que sea el código con guiones.

## 1. Verificar antes de cambiar (anotar en el commit)
Script de solo lectura `scripts/mercado/sonda_cruce_ca_oc.py` (lo corre Boris contra prod con `DATABASE_URL_PROD`, o
Claude Code contra Neon dev si responde): 20 ejemplos de `ca_cotizaciones.id_oc` no nulos (con `seleccionado`),
20 de `compras_agiles.id_orden_compra` no nulos, 20 de `oc_lineas_ca.codigo_oc`; cuántos `id_oc` distintos hay; cuántos
calzan tal cual con `codigo_oc`, y cuántos tras normalizar (mayúsculas, espacios). Sin escribir nada.

## 2. Arreglo según lo que muestre §1
- Si `id_oc` calza con `codigo_oc`: en `demanda.py`, el código de OC de una CA = `compras_agiles.id_orden_compra` y,
  si es nulo, el `id_oc` de su oferta seleccionada (`ca_cotizaciones`, una consulta por lote; varias seleccionadas →
  todas sus OC). Una sola función (p. ej. `oc_de_cas(session, codigos) -> dict[str, set[str]]`) usada en el recorrido y
  en la suma de CA sin OC.
- Si calza solo tras normalizar: normalizar en esa función (y anotar la regla).
- Si no calza en ningún caso: no inventar un cruce por nombre o monto; dejar (c) en `None`, documentarlo en
  `00-estado.md` y `decisiones.md`, y avisar en el resultado del job (`aviso: cruce CA↔OC no disponible`).
- No tocar la ingesta en esta fase (poblar `compras_agiles.id_orden_compra` desde `mercado.py` sería otra fase).

## 3. Tests y cierre
Test con una CA cuya OC solo está en `ca_cotizaciones.id_oc` (cruza), otra con `id_orden_compra` (cruza), otra sin
ninguna (sin OC), y doble conteo con línea `ok` (no suma dos veces). `ruff check .`, `python -m mypy app`,
`python -m pytest -rs` completo contra Postgres local (lo corre Boris). Commit:
`F-plat-6-cruce: cruce CA ↔ OC por la OC de la oferta seleccionada`. Incluir este prompt.
