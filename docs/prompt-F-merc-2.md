# Prompt de implementación — F-merc-2 (órdenes de compra de Compra Ágil de Ñuble desde datos abiertos, a la base) · 31-oct-2026

> Para Claude Code. **Modelo: Sonnet** (sin cuota de API ni 429; sí migración y retención).
> Una fase = un commit, **sin push**. Va **después de F-merc-1** (usa su excepción de retención y su
> configuración `mercado_regiones`). Origen: `docs/16-pivote-compra-agil-nuble.md` §4, §8.

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
   `mercado-oc.yml`, diario 05:30 Chile vía cron-job.org (documentar, no crear).
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
