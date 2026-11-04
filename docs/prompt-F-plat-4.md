# Prompt de implementación — F-plat-4 (índice de precios, probabilidad de adjudicación y evaluación walk-forward) · 3-nov-2026

> Para Claude Code. **Modelo: Opus** (estadística con juez externo, calibración, riesgo de autoengaño con el
> propio modelo). Una fase = un commit, **sin push**. **Con migración.** Va después de F-merc-1, F-merc-2 y
> F-plat-1. Origen: `docs/17-plan-pivote-plataforma-ca.md` §1, §2 C5; `docs/16-pivote-compra-agil-nuble.md` §2, §5.
> **Revisado contra el código el 4-nov-2026 (HEAD `0a35363` + F-plat-3-fix).** Lo marcado [V] está verificado.
> Si algo no calza con el código (sobre todo con lo que deje F-plat-1), gana el código: anotarlo en el commit y
> no inventar.

## 0. Antes de escribir código
- Hechos [V] que el modelo tiene que respetar: en CA de un producto con ≥ 2 ofertas gana el más barato el
  59 % (ago-2026 API) / 78–81 % (`COT_` meses previos); brecha mediana ganador–segundo 2,7–5,5 %; ruido
  intra-CA 17,7 %; 23–26 % de las CA reciben una sola oferta; el ganador ofrece ~92 % del presupuesto.
- Leer `CLAUDE.md` (10, 11, 12), `16-pivote` §2 (escalera y jueces) y §5 (índice robusto, hedónico, cuantil
  en vez de promedio), `bandas_precio.md`, `scripts/mercado/bandas_precio.py` (ya calcula bandas y ruido).
- Código: `ca_cotizaciones`, `ca_cotizacion_productos`, `oc_lineas_ca`, tabla `precio_referencia`
  (`PrecioReferencia`, F-merc-2: percentiles **nacionales** por `clave` de producto normalizado del
  observatorio y `mes` [V]), `ca_lineas`, `producto_canonico`, `proveedor_productos`, `tarifas_logistica`,
  `app/mercado/proveedores.py: mejor_costo`, `app/mercado/territorio.py: regiones_activas`,
  `app/ingest/__main__.py` (`_locked`: un solo advisory lock para todos los jobs [V]), `app/api/query.py` (existe
  [V]).
- **Qué hay y qué no en los datos [V, `app/models/tables.py`]:**
  - `ca_cotizaciones`: `es_emt`, `seleccionado`, `valor_neto`, `monto_despacho`, `rut_proveedor`. **No** guarda
    comuna ni región del proveedor ni el plazo de entrega **ofrecido**.
  - `compras_agiles`: `plazo_entrega_dias` (pedido), `direccion_entrega` (texto), `unidad_compra`, `organismo_rut`
    y `region`. **No** tiene comuna.
  - `oc_lineas_ca`: `comuna_compra`, `rut_unidad`, `comuna_proveedor`, `region_proveedor`, `proveedor_local`,
    `precio_base` y `base` (kg|L|u).
  - `ca_cotizacion_productos` no tiene vínculo con `ca_lineas`: se une por `(ca_codigo, codigo_producto)`.
- **Dependencia: Python puro (recomendación de Cowork, 4-nov; Boris confirma antes de pasar el prompt).** La logística con L2 se ajusta
  por IRLS/Newton con ~20 variables (sistema de 20×20 por eliminación gaussiana) sobre < 100.000 filas: segundos.
  Sin `numpy`, que no está en el lock [V] y además cargaría ~30 MB en el proceso web de Render (512 MB), porque
  `p_ganar` se llama desde la web. Si el ajuste no converge en Python puro, proponer `numpy` en el commit
  con números y **no** agregarlo sin permiso.

## 1. Cambios

### 1.1 `precio_indice` (tabla + cálculo en `app/mercado/indice.py`)
- Clave: `producto_id` × `ambito` (`comuna:<nombre>|provincia:<nombre>|region:16|pais`) × `mes`.
  Columnas: `n_ganador`, `p25_ganador`, `mediana_ganador`, `p75_ganador`, `n_ofertas`, `p25_ofertas`,
  `mediana_ofertas`, `p75_ofertas`, todo en **precio neto por unidad base**. Fuente: `ca_cotizacion_productos`
  unidas a `ca_lineas` `ok` (para el formato) por `(ca_codigo, codigo_producto)`; si dos líneas de la CA
  comparten `codigo_producto`, esa línea se descarta y se cuenta (no se adivina). `oc_lineas_ca`
  (`precio_base`/`base` [V]) como respaldo donde no hay cotización.
- `referencia_precio(producto_id, comuna, mes) -> ReferenciaPrecio` (nombre distinto de la tabla
  `precio_referencia` de F-merc-2, para no confundirlos): caída jerárquica comuna → provincia → región → país
  cuando `n < 5`; devuelve también de qué ámbito salió y `n`. El nivel `pais` puede leer la tabla
  `precio_referencia` cuando el producto canónico tenga equivalente; si no, queda en región.
- Mapa comuna → provincia de Ñuble en `app/catalogos/territorio_nuble.py`. Lista [I, de conocimiento general;
  Boris la confirma o la contrasta con la fuente oficial antes de marcarla [V]]:
  - Diguillín: Bulnes, Chillán, Chillán Viejo, El Carmen, Pemuco, Pinto, Quillón, San Ignacio y Yungay.
  - Itata: Cobquecura, Coelemu, Ninhue, Portezuelo, Quirihue, Ránquil y Treguaco.
  - Punilla: Coihueco, Ñiquén, San Carlos, San Fabián y San Nicolás.
  - Normalizar con `norm`. "Trehuaco" (como lo escribe el municipio) es alias de Treguaco.
- **Comuna de una CA** (no existe como columna [V]): `comuna_de_ca(ca)` =
  1. la comuna más frecuente de `oc_lineas_ca.comuna_compra` para el mismo `rut_unidad` u `organismo_rut`;
  2. si no hay, una comuna del mapa que aparezca en `organismo_nombre` o en `direccion_entrega`;
  3. si no, `None` (la referencia cae a provincia o región).
  Persistirla como `compras_agiles.comuna_inferida` (String, nullable) en la misma migración, con
  `comuna_fuente` (`oc|texto|None`). Test con los 4 casos.

### 1.2 Modelo de adjudicación (`app/mercado/adjudicacion.py`)
- Dataset: una fila por **oferta** en CA cerradas con ganador de las regiones activas, últimos 18 meses.
  Objetivo `gano`. Variables (todas calculables **antes del cierre** para una CA nueva, salvo las de la
  propia oferta):
  - `log_ratio` = log(precio ofertado / mediana de las ofertas de la misma CA) — comparación pareada, sin
    normalizar; para una CA nueva la mediana se estima con `precio_referencia` (ofertas) de la unidad
    compradora/comuna;
  - `n_ofertas` (para CA nueva: mediana histórica de la unidad compradora × familia; default 3);
  - `es_local`: región del proveedor = región de la CA, tomada de `oc_lineas_ca.region_proveedor` por
    `rut_proveedor` (el proveedor que ya vendió por OC); si no se conoce, categoría `desconocido` (no `False`);
  - `es_emt` (columna [V]);
  - **sin** plazo ofrecido: no se guarda [V]. Queda fuera del modelo y se anota como mejora;
  - `familia` (one-hot), `unidad_compradora` (efecto fijo solo para las que tienen ≥ 20 CA; resto "otra").
  - CA con **una sola oferta** (23–26 %): entran al dataset, pero las métricas se reportan también solo sobre
    CA con ≥ 2 ofertas (es donde el modelo decide algo; con una oferta, p ≈ 1 infla el Brier a favor).
- Logística con regularización L2, estandarización guardada con el modelo. Tabla `modelo_adjudicacion`:
  `id`, `entrenado_en`, `datos_hasta_mes`, `n_filas`, `coeficientes` JSONB, `metricas` JSONB, `activo`.
- `p_ganar(ca, lineas, precio_total) -> float` y `curva_p_ganar(ca, lineas) -> [(precio, p)]` en 3 puntos:
  agresivo (p25 ganador), sugerido (cuantil que maximiza `margen × p`), cómodo (mediana ofertas).
- `evaluar_ca(ca_codigo, empresa_id) -> EvaluacionCA`: por línea `ok`: referencia, costo (`mejor_costo`),
  logística (tarifa de la comuna de entrega, kg desde `cantidad_base` cuando `unidad_base=kg`, si no fijo),
  financiero (30 días × tasa configurable `costo_financiero_mensual`, default 1,5 %); por CA: `precio_sugerido`,
  `intervalo_80`, `p_ganar` en los 3 puntos, `margen_esperado`, `cobertura` (% del monto con línea `ok` y con
  costo). Sin costo → margen `None`, no 0.

### 1.3 Evaluación walk-forward (`app/mercado/evaluacion.py`, tabla `evaluacion_modelo`)
- Para cada mes M de los últimos 6 **que tenga ≥ 50 CA cerradas con ≥ 2 ofertas y al menos 3 meses
  anteriores con datos**: entrenar con datos < M, predecir las CA cerradas en M, comparar. Los meses que no
  cumplen se informan como "sin datos suficientes" (el backfill aún está llenando la historia; ver
  `meses_completos`), no como fallo.
- Métricas guardadas (`mes`, `metrica`, `valor`, `n`): Brier; curva de confiabilidad por decil (JSONB);
  acierto del intervalo 80 % sobre el precio ganador por línea `ok`; error mediano del precio ganador; % de
  CA "ganables" (margen ≥ 8 % con `p ≥ 0,5`, requiere costos cargados: **sin costos de la empresa se reporta
  "sin costos", no 0 %**); % del monto cubierto.
- **Metas (peldaño 2 de `16-pivote` §2)**: intervalo 80 % acierta ≥ 70 %; Brier ≤ 0,20; ganables ≥ 10 %. Si
  no se cumplen, el job lo reporta tal cual (`::warning::` y `/salud`), no se afloja la meta.
- Línea base obligatoria: modelo "ranking simple" (gana el más barato con p = 1/n_ofertas ajustado por
  ratio). El logístico solo se marca `activo` si supera la línea base en Brier; si no, queda guardado y se
  usa la línea base. Esto evita adoptar un modelo peor por complejidad.

### 1.4 Job `mercado-indices` (sin API, `_locked("mercado-indices", ...)` como los demás)
Recalcula `precio_indice` de los últimos 3 meses (y completo con `--completo`), reentrena el modelo, corre
la evaluación del último mes cerrado, escribe `job_runs`. Workflow `mercado-indices.yml`, `workflow_dispatch`.
**Horario: 07:45 Chile** (documentar en `operacion-disparos.md`, sin crear el cron). De noche no hay hueco: el
lock lo tienen `mercado-nocturno` (22:30–06:45), `nocturno` (01:10), `retencion` (05:40) y `mercado-oc` (07:20)
[V, `operacion-disparos.md`]. A las 04:00 quedaría siempre `omitido`. Lotes de 2.000 filas (regla 12).

### 1.5 Exposición (solo backend; UI en F-plat-5)
`app/api/query.py: evaluacion_ca(session, ca_codigo, empresa_id)` y `resumen_evaluacion_modelo(session)`
(para `/salud`: última evaluación, metas en verde/rojo). Ownership por empresa en costos y margen.

## 2. Tests
Índice con fixture sintética (caída jerárquica, n < 5); logística recupera coeficientes de datos sintéticos
con signo conocido (ratio ↑ → p ↓); `curva_p_ganar` monótona decreciente en precio; evaluación walk-forward
no usa datos del mes evaluado (test que mete un dato "del futuro" y comprueba que no cambia la predicción);
línea base vs. logístico; `evaluar_ca` sin costos → margen `None`; `comuna_de_ca` (4 casos); unión de
`ca_cotizacion_productos` con `ca_lineas` cuando dos líneas comparten `codigo_producto` (se descarta la línea
ambigua y se cuenta); meses sin datos suficientes. `ruff`, `mypy app`, `pytest -rs` sin saltados, contra
Postgres local (`TEST_DATABASE_URL`).

## 3. Cierre
Changelog ("Precio esperado y probabilidad de adjudicación por Compra Ágil"). `00-estado.md` con los números
reales de la evaluación. Commit: `F-plat-4: índice de precios, probabilidad de adjudicación y evaluación walk-forward`.

## 4. Pasos de Boris
1. Migración según `docs/despliegue.md` (de día, pausar `GH mercado` → push → `Running upgrade` →
   reactivar). Luego `python -m app.ingest run-once mercado-indices --completo` desde el PC, con
   `DATABASE_URL` exportado desde `DATABASE_URL_PROD`.
2. Pegar el bloque de evaluación (Brier, intervalo 80 %, ganables, cobertura) por mes.
3. Decidir con esos números si F-plat-5 entra en modo sombra con el logístico o con la línea base.
