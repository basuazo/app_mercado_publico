# Prompt de implementación — F-plat-6 (demanda por comprador y recomendaciones de abastecimiento) · 3-nov-2026

> Para Claude Code. **Modelo: Opus** (pronóstico con backtest como juez; fácil de hacer mal con promedios
> bonitos). Una fase = un commit, **sin push**. **Con migración.** Va después de F-plat-5 (usa `mercado-indices`, `evaluacion_modelo` y `referencia_precio` de F-plat-4, `producto_canonico` y `ca_lineas` de F-plat-1, y `clave_comprador` de F-plat-5).
> Origen: `docs/17-plan-pivote-plataforma-ca.md` §2 C7.

> **Revisado contra el código el 4-nov-2026 (HEAD `a9c0cfe`).** La sección "Revisión 4-nov" manda sobre el resto
> donde choquen. Si algo no calza con lo que dejen F-plat-1, 4 o 5, gana el código: anotarlo en el commit.

## Revisión 4-nov (Cowork) — leer primero
1. **Clave del comprador.** `oc_lineas_ca` tiene `rut_unidad` [V]; `compras_agiles` tiene `organismo_rut` y
   `unidad_compra` (texto), **no** `rut_unidad` [V]. Nadie verificó que `organismo_rut` sea el mismo RUT que
   `rut_unidad`, ni que un RUT equivalga a una sola unidad (una municipalidad compra por DAEM, salud, etc.) [I].
   - Usar `clave_comprador` de F-plat-5.
   - Antes de construir las series, medir sobre las CA con `id_orden_compra` presente en `oc_lineas_ca`: % con
     `organismo_rut = rut_unidad` y unidades por RUT. Reportarlo en el commit [I→V].
2. **Doble conteo [V].** Una CA adjudicada aparece dos veces: en `ca_lineas` (lo pedido) y en `oc_lineas_ca` (lo
   adjudicado). `oc_lineas_ca` no tiene código de CA; el cruce es `compras_agiles.id_orden_compra` (o
   `ca_cotizaciones.id_oc`) = `oc_lineas_ca.codigo_oc`. Regla:
   - CA con OC en `oc_lineas_ca` → cuenta la OC;
   - CA cerrada sin OC cargada → cuenta `ca_lineas`;
   - CA desierta → suma a `n_desiertas`, no a la cantidad.

   Test con los tres casos. Normalizar el formato del código de OC si difiere (F-merc-2 §6).
3. **`oc_lineas_ca` no tiene `producto_id` [V]**: tiene `codigo_producto` (UNSPSC), `producto_generico`,
   `especificacion`, `precio_base` y `base`. Mapear a `producto_canonico` con `sugerir_producto` (F-plat-1) sobre la
   especificación; lo que no mapee queda a nivel `familia` (`familia_alimento`, `app/catalogos/mercado.py` [V]).
   Reportar la cobertura (% del monto con `producto_id`).
4. **Historia disponible [V]:**
   - `oc_lineas_ca`: desde 2024-10 (26 meses, carga del 4-nov).
   - `ca_cotizaciones` y el detalle histórico: siguen en backfill desde 2024-11 (ver `meses_completos`).

   La estacionalidad con 2 años solo es posible para lo que viene de OC; lo demás sale `debil`. No pasa nada: queda
   marcado.
5. **Job:** `mercado-indices` (07:45, F-plat-4) ya existe para entonces → recalcular ahí, sin job propio.
6. **Umbrales por familia** (200 kg, 300 L, 500 u): en `Settings` como JSON (`DEMANDA_UMBRALES`), con default en
   código. Boris anota los cambios en `decisiones.md` (§4).
7. **Idempotencia de `recomendaciones`:** upsert por `(empresa_id, tipo, producto_id, rut_unidad, mes_objetivo)` que
   conserva el `estado` que puso el usuario. Re-ejecutar no resucita una descartada ni duplica una hecha.
8. **Línea base del backtest** (mismo criterio que F-plat-4): "compra en M si compró en el mismo mes del año
   anterior o en M−1". La precisión y el recall del pronóstico se reportan **al lado** de los de la línea base, y en
   `/salud` el pronóstico solo pasa a verde si además le gana. Si no le gana, se muestra la línea base.
9. **Propiedad (regla 26):** `demanda_series` y `pronosticos` son datos públicos comunes, como `oc_lineas_ca` (sin
   `empresa_id`). `recomendaciones` es de la empresa: ownership y tests de acceso cruzado (404).
10. **Tamaño:** agregar `demanda_series`, `pronosticos` y `recomendaciones` a `_tablas_mercado_salud`
    (`app/api/salud_data.py` [V]).

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
- Recálculo por lotes (regla 12) en el job `mercado-indices` (F-plat-4, 07:45).

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
