# Prompt de implementación — F-merc-2 (órdenes de compra de Compra Ágil de Ñuble desde datos abiertos, a la base) · 31-oct-2026

> Para Claude Code. **Modelo: Sonnet** (sin cuota de API ni 429; sí migración y retención).
> Una fase = un commit, **sin push**. Va **después de F-merc-1** (usa su excepción de retención y su
> configuración `mercado_regiones`). Origen: `docs/16-pivote-compra-agil-nuble.md` §4, §8.

> **Correcciones 4-nov (Cowork, revisión contra el código tras `e4e1eb8`). Mandan sobre el texto de abajo.**
> 1. **Lock:** no hay lock `mercado` (corrección 31-oct de F-merc-1): `mercado-oc` usa el advisory lock de siempre
>    vía `_locked("mercado-oc", …)`, igual que `mercado-ca`. Ojo: `mercado-nocturno` lo tiene de 22:30 a ~06:45, por eso
>    el disparo diario va a las **07:20** Chile (no 05:30). La carga inicial `--desde` va desde el PC.
> 2. **§1.2 ya está hecho en parte:** `GRUPOS`, `EXCLUIR`, `G_PREP` y `grupo_de` viven en `app/catalogos/mercado.py`
>    desde F-merc-1. En esta fase se mueven solo `familia_alimento` + `FAMILIAS_ALIMENTOS`, `G_ALIM`, y la
>    normalización (`normalizar` con `num`, `norm`, `rut`, `es_nuble` y las tablas `_FMT/_PACK/_A_KG/_A_L/_U_FIS/
>    _U_ENVASE`) a `app/catalogos/mercado.py` (o `app/mercado/normalizar.py` si queda grande). El observatorio importa
>    de ahí y su salida no cambia (test: mismo resultado que antes sobre una fixture).
> 3. **Región del comprador:** en `oc-da` `RegionUnidadCompra` es **texto** ("Región del Ñuble" [V en `oc_ca2_*`,
>    102.825 filas, un solo valor]); no hay código. Crear `app/catalogos/regiones.py` con `region_codigo(texto) -> int |
>    None` (las 16 regiones, normalizado sin tildes, tolerante a "Región de/del", espacios finales y "NA"); `region_compra`
>    se guarda como int y el filtro es `region_compra in mercado_regiones`. Lo mismo para `region_proveedor` y
>    `proveedor_local = region_proveedor == region_compra`. Desconocido → `None` + log una vez por valor (regla 6).
>    Cuando exista F-plat-3, el filtro pasa a `regiones_activas()`; dejarlo detrás de una función.
> 4. **PK de línea:** el extracto local `oc_ca2_*` **no** trae id de ítem; si el `oc-da` crudo trae `IDItem` (u otro id
>    de línea) es [I]: verificarlo en el header del primer ZIP y anotarlo [V] en `docs/16-…` §4. Si no existe, PK =
>    `(codigo_oc, n)` con `n` = orden de la línea dentro de la OC en el archivo, y el upsert por mes **borra y reinserta**
>    las líneas de las OC de ese mes (idempotente sin depender del orden entre meses).
> 5. **Moneda y estado:** guardar `moneda` y `codigo_estado`. 4 de 102.825 líneas de Ñuble no son CLP (2 UTM, 2 CLF)
>    [V]: se guardan, pero `precio_base`, `precio_referencia` y `resumen_mercado` usan solo CLP. Estados presentes en
>    Ñuble [V]: 6 (58 %), 12 (42 %), 4, 7, 5; no aparece 9 (cancelada). No filtrar por estado (el juez de $682 M no lo hace).
> 6. **Cruce con F-merc-1:** índice por `codigo_oc` y test que una fila de `ca_cotizaciones.id_oc` se puede unir con
>    `oc_lineas_ca.codigo_oc` (si los formatos difieren, normalizar en una función y documentarlo [I→V con datos reales
>    después de la primera noche del backfill)]. No inferir ganadores: solo dejar el cruce posible.
> 7. **Tamaño:** 24 meses de Ñuble = **102.825 líneas** en grupos [V, `oc_ca2_2024-10…2026-09`], especificaciones de ~90
>    caracteres en promedio: estimar ~35–45 MB con índices (el plan decía ~20 MB [I]). Medir con `pg_total_relation_size`
>    tras la carga, anotarlo en el commit y en `decisiones.md`. `precio_referencia` es chica (claves × meses).
> 8. **`/salud`:** ya existe `tamano_tabla` (`app/core/retencion.py`, `pg_total_relation_size`): reutilizarlo para mostrar
>    filas y tamaño de `oc_lineas_ca`, `precio_referencia`, `ca_cotizaciones` y `ca_cotizacion_productos`.
> 9. **§1.7 y el juez:** la UI no va en "F-merc-4" (no existe en el plan 17): la consume F-plat-5 (Mesa). Juez exacto:
>    `resumen_mercado(16, 2025-10, 2026-09)` en alimentos (`G_ALIM` = envasados + preparados), comprador de Ñuble,
>    `totalLineaNeto` sumado = **$681.979.119** (`data/mercado/informe/resumen.json` → `alimentos.total12`) ±0,5 %; además
>    1.516 OC (`oc12`) y 105 compradores (`n_compradores`) exactos o explicada la diferencia.
> 10. **Sin dependencias nuevas** (nada de pandas): percentiles con `statistics.quantiles` o el `pct` del observatorio.
>     `precio_referencia` nacional: por mes, en streaming, guardando en memoria solo listas de precios por clave
>     (regla 12); medir RAM pico en el commit.
> 11. **Regla 24/26:** sin ticket ni API. Los datos de OC son públicos y comunes (no pertenecen a una empresa).

## 0. Antes de escribir código
- Hoy el análisis vive en `scripts/mercado/mercado_observatorio.py` y corre en el PC de Boris. Para que la app
  (y después la "mesa del día") lo use, las órdenes de compra de CA de Ñuble tienen que estar en Neon.
- Fuente: `oc-da/{año}-{mes}.zip` (sin ticket, sin cuota). Columnas [V 31-oct] en
  `16-pivote-compra-agil-nuble.md` §4; el RUT del proveedor es `RutSucursal` [V].
- Lectura: `app/clients/datos_abiertos.py` e `app/ingest/datos_abiertos.py` (patrón HEAD +
  `Last-Modified` + cursor por mes en `SyncState`, lectura en streaming Latin-1, R6: una descarga fallida
  no aborta el resto), `scripts/mercado/mercado_observatorio.py` (`GRUPOS`, `grupo_de`, `normalizar`, `COLS_OC`),
  `app/core/retencion.py`, `CLAUDE.md` reglas 6, 10, 11, 12.
- **Ojo**: el ZIP del PAC da 403 desde Actions; `lic-da` funciona [V]. `oc-da` desde Actions: [I] — el
  primer paso de la fase es un `HEAD` desde un job de prueba; si da 403, la carga se hace desde el PC
  de Boris con el mismo CLI (igual que el PAC) y se documenta.

## 1. Cambios
1. **Cliente** (`datos_abiertos.py`): `iterar_oc_da(anio, mes)` en streaming, con las columnas de
   `COLS_OC` + `RutSucursal`; nunca el mes completo en memoria (regla 12: lotes de 2.000 filas).
2. **Catálogo de grupos**: mover `GRUPOS`, `EXCLUIR`, `grupo_de` y `familia_alimento` a
   `app/catalogos/mercado.py` (fuente única); `scripts/mercado/mercado_observatorio.py` lo importa.
3. **Tabla `oc_lineas_ca`** (solo `app/models` + migración): `codigo_oc`, `id_item`, `mes`,
   `fecha_creacion`, `rut_unidad`, `unidad_compra`, `organismo`, `sector`, `comuna_compra`,
   `region_compra` (int), `rut_proveedor`, `proveedor`, `comuna_proveedor`, `region_proveedor`,
   `proveedor_local` (bool), `codigo_producto`, `producto_generico`, `grupo`, `especificacion`
   (recortada a 500), `cantidad`, `unidad_medida`, `precio_neto`, `total_linea_neto`, `precio_base`
   (normalizado), `base` (`kg|L|u|null`). PK `(codigo_oc, id_item)`. Solo filas `EsCompraAgil = Si`,
   en `GRUPOS` y con comprador en `mercado_regiones`. Upsert idempotente por mes.
4. **Referencia nacional de precios** sin guardar el país línea a línea: tabla `precio_referencia`
   (`clave` = UNSPSC|base, `mes`, `n`, `p25`, `mediana`, `p75`) calculada en el mismo recorrido.
5. **Job `mercado-oc`** (sin API, lock `mercado`): mes en curso y 2 anteriores con HEAD/`Last-Modified`;
   `--desde AAAA-MM` para la carga inicial de 24 meses (de noche o desde el PC). Workflow
   `mercado-oc.yml`, diario 07:20 Chile vía cron-job.org (corrección 1) (documentar, no crear).
6. **Retención**: `oc_lineas_ca` y `precio_referencia` no se purgan (excepción del piloto, ya anotada en
   F-merc-1). `/salud` muestra filas y tamaño de ambas tablas.
7. **Vista para la app**: consulta parametrizada `resumen_mercado(region, desde, hasta)` en
   `app/api/query.py` que devuelve lo mismo que `analizar_alimentos()` del observatorio (monto, familias,
   compradores, proveedores, tickets). Solo backend + test; la UI va en F-merc-4.

## 2. Tests
Fixture sintética de `oc-da` (Latin-1, `;`, campo multilínea, `CodigoProductoONU` de 9 dígitos, región
con espacio al final, `NA`); idempotencia del upsert; normalización; retención; el resumen coincide con
`analizar_alimentos()` sobre la misma fixture. `ruff`, `mypy app`, `pytest -rs` sin saltados.

## 3. Cierre
Changelog, `00-estado.md`, commit `F-merc-2: órdenes de compra de Compra Ágil de Ñuble en la base`.
Paso de Boris: carga inicial `python -m app.ingest run-once mercado-oc --desde 2024-10` (PC o Actions
según el HEAD), y comparar el total de 12 meses de alimentos con el informe ($682 M) — **juez: deben
coincidir dentro de 0,5 %**.
