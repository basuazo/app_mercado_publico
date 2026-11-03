# Prompt de implementación — F-plat-6 (demanda por comprador y recomendaciones de abastecimiento) · 3-nov-2026

> Para Claude Code. **Modelo: Opus** (pronóstico con backtest como juez; fácil de hacer mal con promedios
> bonitos). Una fase = un commit, **sin push**. **Con migración.** Va después de F-merc-2, F-plat-1 y F-plat-3.
> Origen: `docs/17-plan-pivote-plataforma-ca.md` §2 C7.

## 0. Antes de escribir código
- Qué se quiere: saber **qué va a comprar cada unidad compradora de Ñuble, cuánto y cuándo**, para pedir
  precio firme a proveedores y preparar stock. Datos [V]: 105 unidades compradoras de alimentos en 12 meses,
  30 compran ≥ 6 meses al año; municipalidades 77 % del monto (`16-pivote` §4-bis).
- Leer `CLAUDE.md` (10, 12), `17-plan` §2 C7, `16-pivote` §4–§4-bis, `scripts/mercado/mercado_observatorio.py:
  analizar_alimentos` (series por mes y comprador), `oc_lineas_ca`, `ca_lineas`, `producto_canonico`,
  `proveedor_productos`, `app/mercado/indice.py` (F-plat-4, si ya existe; si no, esta fase no depende de él).
- **Sin modelos de series complejos** (nada de ARIMA/Prophet): recurrencia + estacionalidad + tendencia,
  explicables en una frase cada uno. Sin dependencias nuevas.

## 1. Cambios

### 1.1 `demanda_series` (tabla + `app/mercado/demanda.py`)
- Clave: `rut_unidad` (unidad compradora) × `producto_id` (o `familia` cuando no hay línea `ok`) × `mes`.
  Columnas: `n_ca`, `cantidad_base` (suma), `monto_neto`, `n_oferentes_mediana`, `precio_mediana`. Fuente:
  `oc_lineas_ca` (24 meses, lo adjudicado) + `ca_lineas` `ok` de CA cerradas (cantidades pedidas) + CA
  desiertas (señal de demanda insatisfecha: `n_desiertas`).
- Recalculo por lotes (regla 12) en el job `mercado-indices` (F-plat-4) o, si no existe aún, job propio
  `mercado-demanda` nocturno sin API.

### 1.2 Pronóstico v1 (`pronosticar(rut_unidad, producto_id, meses_adelante=3) -> Pronostico`)
- `recurrencia` = meses con compra / meses observados (desde la primera compra).
- `estacionalidad` = para cada mes calendario, promedio de `cantidad_base` en ese mes de años anteriores
  dividido por el promedio general (con 2 años: 2 observaciones; con 1: 1, marcada `debil`).
- `tendencia` = promedio móvil 3 meses / promedio móvil 12 meses.
- `cantidad_esperada[m]` = promedio general × estacionalidad[m] × tendencia; `p_compra[m]` = recurrencia
  ajustada por estacionalidad de ocurrencia (¿compró en ese mes calendario antes?); intervalo = p25–p75
  histórico de cantidad escalado.
- Solo se emite pronóstico con `n_ca ≥ 4`; si no, `insuficiente`. Guardado en `pronosticos` (`rut_unidad`,
  `producto_id`, `mes_objetivo`, `p_compra`, `cantidad_esperada`, `intervalo`, `generado_en`, `metodo`).

### 1.3 Recomendaciones (`recomendar(empresa_id) -> list[Recomendacion]`)
Agregación por producto para la empresa (según sus comunas priorizadas primero, R-p2):
- **Precio firme**: productos con `Σ cantidad_esperada` próximos 3 meses ≥ umbral por unidad base
  (configurable por familia, p. ej. 200 kg, 300 L, 500 u) → "pedir precio firme a {mejor proveedor} por
  {cantidad} de {producto} para {meses}"; muestra el costo actual, la variación de 90 días y el precio ganador
  de referencia (F-plat-4 si existe) para calcular el margen que ese precio firme necesita.
- **Próxima compra esperada**: unidad compradora × producto con `p_compra ≥ 0,6` en el mes siguiente →
  "la {unidad} suele comprar {producto} en {mes}: preparar oferta".
- **Hueco de proveedor**: productos con demanda ≥ umbral y **sin** `proveedor_productos` disponible en la
  empresa → "buscar proveedor de {producto}".
- **Demanda desierta**: productos con `n_desiertas ≥ 2` en 6 meses → oportunidad sin competencia.
Tabla `recomendaciones` (`empresa_id`, `tipo`, `producto_id`, `rut_unidad` nullable, `mes_objetivo`,
`cantidad`, `texto`, `datos` JSONB, `generada_en`, `estado` `nueva|vista|hecha|descartada`, `usuario_id`).

### 1.4 UI `/abastecimiento` (usuarios con empresa)
- Pestañas: **Recomendaciones** (por tipo, marcar hecha/descartada), **Calendario** (próximos 3 meses ×
  producto: cantidad esperada, p_compra, compradores), **Compradores** (unidad compradora: qué compra, cada
  cuánto, a quién, precio; desde `demanda_series`), **Producto** (serie 24 meses, estacionalidad, proveedores
  y variación de costo).
- Gráficos: inline SVG simples (barras por mes) sin librerías nuevas. Pie "Fuente: Dirección ChileCompra".

### 1.5 Juez: backtest (`app/mercado/evaluacion.py` o script `scripts/mercado/backtest_demanda.py`)
- Para cada uno de los últimos 6 meses M: pronosticar con datos < M, comparar con M.
- Métricas: precisión y recall de "compra en M" con umbral `p_compra ≥ 0,6`; MAPE de `cantidad_esperada` en
  los pares que sí compraron; cobertura (% del monto de alimentos con pronóstico no `insuficiente`).
- **Meta: precisión ≥ 70 %, recall ≥ 50 %, MAPE reportado** (sin meta en v1: es la línea base). Guardar en
  `evaluacion_modelo` (`metrica` con prefijo `demanda_`) y mostrar en `/salud`.

## 2. Tests
Series sintéticas (comprador que compra cada mes, cada 2 meses, solo en marzo y septiembre, uno solo); el
backtest no mira el mes evaluado; `n_ca < 4` → insuficiente; recomendaciones con y sin proveedor; comunas
priorizadas primero; ownership; idempotencia del recálculo. `ruff`, `mypy app`, `pytest -rs` sin saltados.

## 3. Cierre
Changelog ("Demanda por comprador y recomendaciones de abastecimiento"). `00-estado.md` con los números del
backtest. Commit: `F-plat-6: demanda por comprador, pronóstico y recomendaciones de abastecimiento`.

## 4. Pasos de Boris
1. Migración; recálculo completo desde el PC; pegar el backtest.
2. Revisar las 20 primeras recomendaciones con ojo de negocio: ¿cuáles son obvias, cuáles sorprenden, cuáles
   están mal? Anotar en `docs/decisiones.md` los umbrales que cambien.
