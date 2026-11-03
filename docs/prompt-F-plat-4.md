# Prompt de implementación — F-plat-4 (índice de precios, probabilidad de adjudicación y evaluación walk-forward) · 3-nov-2026

> Para Claude Code. **Modelo: Opus** (estadística con juez externo, calibración, riesgo de autoengaño con el
> propio modelo). Una fase = un commit, **sin push**. **Con migración.** Va después de F-merc-1, F-merc-2 y
> F-plat-1. Origen: `docs/17-plan-pivote-plataforma-ca.md` §1, §2 C5; `docs/16-pivote-compra-agil-nuble.md` §2, §5.

## 0. Antes de escribir código
- Hechos [V] que el modelo tiene que respetar: en CA de un producto con ≥ 2 ofertas gana el más barato el
  59 % (ago-2026 API) / 78–81 % (`COT_` meses previos); brecha mediana ganador–segundo 2,7–5,5 %; ruido
  intra-CA 17,7 %; 23–26 % de las CA reciben una sola oferta; el ganador ofrece ~92 % del presupuesto.
- Leer `CLAUDE.md` (10, 11, 12), `16-pivote` §2 (escalera y jueces) y §5 (índice robusto, hedónico, cuantil
  en vez de promedio), `bandas_precio.md`, `scripts/mercado/bandas_precio.py` (ya calcula bandas y ruido).
- Código: `ca_cotizaciones`, `ca_cotizacion_productos`, `oc_lineas_ca`, `precio_referencia`, `ca_lineas`,
  `producto_canonico`, `proveedor_productos`, `tarifas_logistica`, `app/mercado/proveedores.py: mejor_costo`,
  `app/ingest/orchestrator.py` (jobs sin API, lock `mercado`).
- **Dependencia**: proponer `numpy` (logística por descenso de gradiente, 100 líneas, sin scikit-learn ni
  statsmodels). Si Boris prefiere cero dependencias, la logística se escribe en Python puro (es viable:
  < 100.000 filas).

## 1. Cambios

### 1.1 `precio_indice` (tabla + cálculo en `app/mercado/indice.py`)
- Clave: `producto_id` × `ambito` (`comuna:<nombre>|provincia:<nombre>|region:16|pais`) × `mes`.
  Columnas: `n_ganador`, `p25_ganador`, `mediana_ganador`, `p75_ganador`, `n_ofertas`, `p25_ofertas`,
  `mediana_ofertas`, `p75_ofertas`, todo en **precio neto por unidad base**. Fuente: `ca_cotizacion_productos`
  unidas a `ca_lineas` `ok` (para el formato); `oc_lineas_ca` como respaldo donde no hay cotización.
- `precio_referencia(producto_id, comuna, mes) -> ReferenciaPrecio`: caída jerárquica comuna → provincia →
  región → país cuando `n < 5`; devuelve también de qué ámbito salió y `n`. Mapa comuna → provincia de Ñuble
  en `app/catalogos/territorio_nuble.py` (21 comunas, 3 provincias; verificar contra la lista oficial del
  SUBDERE y marcar [V]).

### 1.2 Modelo de adjudicación (`app/mercado/adjudicacion.py`)
- Dataset: una fila por **oferta** en CA cerradas con ganador de las regiones activas, últimos 18 meses.
  Objetivo `gano`. Variables (todas calculables **antes del cierre** para una CA nueva, salvo las de la
  propia oferta):
  - `log_ratio` = log(precio ofertado / mediana de las ofertas de la misma CA) — comparación pareada, sin
    normalizar; para una CA nueva la mediana se estima con `precio_referencia` (ofertas) de la unidad
    compradora/comuna;
  - `n_ofertas` (para CA nueva: mediana histórica de la unidad compradora × familia; default 3);
  - `es_local` (comuna del proveedor en la región), `es_emt`, `plazo_entrega_dias` ofrecido vs. pedido,
  - `familia` (one-hot), `unidad_compradora` (efecto fijo solo para las que tienen ≥ 20 CA; resto "otra").
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
- Para cada mes M de los últimos 6: entrenar con datos < M, predecir las CA cerradas en M, comparar.
- Métricas guardadas (`mes`, `metrica`, `valor`, `n`): Brier; curva de confiabilidad por decil (JSONB);
  acierto del intervalo 80 % sobre el precio ganador por línea `ok`; error mediano del precio ganador; % de
  CA "ganables" (margen ≥ 8 % con `p ≥ 0,5`, requiere costos cargados); % del monto cubierto.
- **Metas (peldaño 2 de `16-pivote` §2)**: intervalo 80 % acierta ≥ 70 %; Brier ≤ 0,20; ganables ≥ 10 %. Si
  no se cumplen, el job lo reporta tal cual (`::warning::` y `/salud`), no se afloja la meta.
- Línea base obligatoria: modelo "ranking simple" (gana el más barato con p = 1/n_ofertas ajustado por
  ratio). El logístico solo se marca `activo` si supera la línea base en Brier; si no, queda guardado y se
  usa la línea base. Esto evita adoptar un modelo peor por complejidad.

### 1.4 Job `mercado-indices` (nocturno, sin API, lock `mercado`)
Recalcula `precio_indice` de los últimos 3 meses (y completo con `--completo`), reentrena el modelo, corre
la evaluación del último mes cerrado, escribe `job_runs`. Workflow `mercado-indices.yml`, `workflow_dispatch`;
documentar disparo 04:00 Chile en `operacion-disparos.md` (no crear). Lotes de 2.000 filas (regla 12).

### 1.5 Exposición (solo backend; UI en F-plat-5)
`app/api/query.py: evaluacion_ca(session, ca_codigo, empresa_id)` y `resumen_evaluacion_modelo(session)`
(para `/salud`: última evaluación, metas en verde/rojo). Ownership por empresa en costos y margen.

## 2. Tests
Índice con fixture sintética (caída jerárquica, n < 5); logística recupera coeficientes de datos sintéticos
con signo conocido (ratio ↑ → p ↓); `curva_p_ganar` monótona decreciente en precio; evaluación walk-forward
no usa datos del mes evaluado (test que mete un dato "del futuro" y comprueba que no cambia la predicción);
línea base vs. logístico; `evaluar_ca` sin costos → margen `None`. `ruff`, `mypy app`, `pytest -rs` sin saltados.

## 3. Cierre
Changelog ("Precio esperado y probabilidad de adjudicación por Compra Ágil"). `00-estado.md` con los números
reales de la evaluación. Commit: `F-plat-4: índice de precios, probabilidad de adjudicación y evaluación walk-forward`.

## 4. Pasos de Boris
1. Migración; `python -m app.ingest run-once mercado-indices --completo` desde el PC.
2. Pegar el bloque de evaluación (Brier, intervalo 80 %, ganables, cobertura) por mes.
3. Decidir con esos números si F-plat-5 entra en modo sombra con el logístico o con la línea base.
