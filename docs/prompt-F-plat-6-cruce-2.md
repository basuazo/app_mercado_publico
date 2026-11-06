# Prompt — F-plat-6-cruce-2 (cruce CA ↔ OC por el `ID` de oc-da) · 5-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit, **con migración** (`down_revision` = cabeza vigente, una sola;
> al 5-nov `c8f3a1e6d2b4` [V]), sin push. Sin ticket (oc-da es datos abiertos; regla 24 no aplica). Va sobre
> F-plat-6-cruce (`3d762f8`). [V] verificado, [I] inferido.

## Por qué
F-plat-6-cruce dejó `demanda.oc_de_cas` y los tests del mecanismo, pero el cruce da 0 [V, sonda en prod 5-nov]:
`ca_cotizaciones.id_oc` y `compras_agiles.id_orden_compra` traen el **`ID` numérico** de la OC (`55672374`) y
`oc_lineas_ca` guarda solo el **`Codigo`** (`2724-973-AG26`). La cabecera de oc-da trae la columna `ID` (primera de
79) y la ingesta la descarta; 10 de 10 `ID` buscados en `oc-da/2026-10.zip` apuntan a una OC AG del mismo comprador
de su CA [V]. Sin el cruce: (c) no se mide (clave del comprador fija en RUT) y, en cuanto F-plat-anexos-2 produzca
líneas `ok`, la demanda sumaría lo pedido y lo adjudicado dos veces. **Por eso va antes de la ventana D.**

## 1. Esquema (solo `app/models`)
- `oc_lineas_ca.id_oc`: `String(20)`, nullable (texto, no entero: se compara con `ca_cotizaciones.id_oc`, que es texto).
- Índice `ix_oc_lineas_ca_id_oc` sobre `id_oc` (no único: una OC tiene varias líneas).
- Medir el tamaño agregado tras la recarga (columna + índice) y anotarlo; esperado < 5 MB [I].

## 2. Ingesta (`app/ingest/mercado_oc.py`)
- `_fila`: `"id_oc": codigo_oc_normalizado(r.get("ID"))[:20] or None`. Verificar el nombre exacto de la columna
  en la cabecera real (¿`ID`, `Id`, con BOM en la primera columna?) y cubrir el BOM: es la PRIMERA columna del CSV,
  justo donde un `﻿` la renombraría a `﻿ID` y el campo quedaría siempre vacío. Test con un CSV con BOM.
- Si una fila no trae `ID`, la línea se carga igual (parseo defensivo, regla 6) y se cuenta en el resultado
  (`lineas_sin_id`).
- **Recarga: el cursor por `Last-Modified` la saltaría** [V, `sync_mercado_oc`: «sin cambios» si el cursor no
  cambió]. Agregar `--forzar` (CLI y `run-once`) que ignora el cursor; sin él, nada cambia en la corrida diaria.
  `cargar_mes` ya borra y reinserta el mes, así que la recarga llena la columna sin tocar nada más.

## 3. Cruce (`app/mercado/demanda.py`)
- `recorrer`: el mapa OC → (RUT, unidad) y `Directorio.oc_codigos` se indexan por `id_oc` normalizado (el `ID`), no
  por `codigo_oc`. `oc_a_ca` también va por `ID`; `_sumar_oc` busca con el `id_oc` de la línea. Líneas sin `id_oc`
  no cruzan (cuentan como OC sin CA, igual que hoy).
- `oc_de_cas` no cambia (ya devuelve `ID` normalizados).
- El aviso «cruce CA↔OC no disponible» se mantiene para cuando dé 0; si da > 0, la medición (c) vuelve a decidir la
  clave del comprador (si la cambia, el job ya recalcula todos los meses: verificar que siga así).
- `codigo_oc_normalizado`: actualizar el docstring (ahora compara `ID` con `ID`).

## 4. Tests
- Corregir `tests/test_mercado_oc.py::test_id_oc_de_cotizaciones_se_une_con_oc_lineas`: su premisa
  (`id_oc` = `codigo_oc`) es falsa [V]; debe unir `ca_cotizaciones.id_oc` con `oc_lineas_ca.id_oc`.
- `test_plat6`: el test de cruce y de doble conteo pasa a usar `ID` en ambos lados (OC con `codigo_oc` AG distinto
  del `ID`); `test_sin_cruce_avisa_y_c_queda_en_none` queda para líneas sin `id_oc`.
- Ingesta: CSV con `ID` (con y sin BOM), fila sin `ID`, `--forzar` ignora el cursor y sin él lo respeta.
- De paso (falla en el PC de Boris, 5-nov): `tests/test_workflows.py::_simular` pasa `COMPLETO` y `JOBS` por `env=`;
  en Windows `shutil.which("bash")` suele dar el `bash.exe` de WSL, que no hereda esas variables (sin `WSLENV`), y salen
  0 líneas. Ponerlas dentro del script (`COMPLETO=...`, `JOBS=...`, `ESPERAR_LOCK_MIN=10` al inicio) para que funcione con
  cualquier bash; sin `skip` (regla de 0 saltados).
- `ruff check .`, `python -m mypy app`, `python -m pytest -rs` completo contra Postgres local (lo corre Boris).

## 5. Juez (lo corre Boris tras el deploy y la recarga)
- `scripts/mercado/sonda_cruce_ca_oc.py --prod` extendida: de los `id_oc` seleccionados, cuántos calzan con
  `oc_lineas_ca.id_oc`, **separado por familia de la CA** (alimentos vs. resto). `oc_lineas_ca` solo guarda `GRUPOS`
  y regiones activas, así que el resto puede no calzar sin que sea error.
- **Meta [I, a confirmar con el primer número]:** ≥ 90 % de las CA de alimentos con oferta seleccionada y `id_oc`
  cruzan con su OC. Si queda bajo, informar los que no calzan (¿mes fuera de rango, grupo, región?) antes de tocar nada.
- `mercado-demanda`: `c_cas_con_oc` > 0, `c_pct_misma_clave_unidad` con número y sin el aviso de cruce.

## 6. Pasos de Boris
1. Tests locales (`alembic upgrade head` en la base de tests + `pytest -rs`).
2. Deploy de día con `GH mercado`, `GH mercado-oc` y `GH mercado-indices` pausados y Actions vacío; en Render
   `Running upgrade c8f3a1e6d2b4 -> …`; reactivar.
3. Recarga (desde donde corrió la carga inicial de F-merc-2):
   `python -m app.ingest run-once --job mercado-oc --desde 2024-10 --forzar` → pegar `lineas_sin_id` y meses.
4. Sonda extendida (§5) y Actions → `mercado-indices` → Run workflow con `completo` → pegar `medicion` y `backtest`.

Commit: `F-plat-6-cruce-2: cruce CA ↔ OC por el ID de oc-da`. Entrada en `app/changelog.py` solo si cambia algo
visible (no debería). Incluir este prompt.
