# Prompt de implementación — F-plat-6 (demanda por comprador, pronóstico y abastecimiento) · 5-nov-2026

> Para Claude Code. **Modelo: Opus** (pronóstico con backtest como juez; fácil de hacer mal con promedios bonitos).
> Una fase = un commit, **sin push**. **Con migración**: `down_revision = "b3e7c1d9f4a2"` (cabeza tras F-plat-5 [V];
> confirmar con `alembic heads`).
> **Reescrito y revisado contra el código el 5-nov-2026 (HEAD `da57827`).** Reemplaza al prompt del 3-nov y su
> revisión del 4-nov (en el historial de git). Lo marcado [V] está verificado; si algo no calza, gana el código:
> anotarlo en el commit y no inventar. Origen: `17-plan` §2 C7; `16-pivote` §4–§4-bis.

## Objetivo
Saber **qué va a comprar cada comprador de Ñuble, cuánto y cuándo**, para pedir precio firme a proveedores, preparar
stock y llegar antes a la CA. Es la pieza con **más historia disponible**: no depende del hueco de ofertas
(abril–septiembre 2026 sin `ca_cotizaciones`), porque sale de las órdenes de compra.

## 0. Lo que ya existe y los datos [V]
- **`oc_lineas_ca`** (desde 2024-10, ~26 meses): `codigo_oc`, `id_item`, `mes`, `fecha_creacion`, `rut_unidad`,
  `unidad_compra` (texto), `organismo`, `comuna_compra`, `region_compra`, `rut_proveedor`, `codigo_producto` (UNSPSC),
  `producto_generico`, `grupo`, `especificacion`, `cantidad`, `unidad_medida`, `precio_neto`, `total_linea_neto`,
  `precio_base` y `base` (`kg|L|u`). **No tiene `producto_id`** ni código de CA.
- **Cruce CA ↔ OC:** `compras_agiles.id_orden_compra` = `oc_lineas_ca.codigo_oc`.
- **Comprador:** la clave única es `comprador.clave_unidad(organismo_rut, unidad_compra)`. F-plat-4 ya cruzó
  `compras_agiles.organismo_rut` con `oc_lineas_ca.rut_unidad` (`indice.mapa_comunas_oc`, con `indice._rut`): el 5-nov
  encontró comuna por esa vía para **7.952 de 8.089 CA (98 %)** [V], o sea que el RUT del organismo de la CA casi
  siempre es un `rut_unidad` de las OC. **No está verificado [I]** que el texto `unidad_compra` de la OC coincida con el
  de la CA, ni cuántas unidades distintas comparten un RUT (una municipalidad compra por DAEM, salud, etc.).
- **Producto:** el catálogo canónico está poco curado: en la corrida del 5-nov, 6.675 líneas de OC quedaron sin
  producto único y solo 179 entraron al índice [V]. `catalogos.canonico.sugerir_producto` existe [V].
- **Reutilizar, no rehacer:** `adjudicacion.familia_de_codigo` / `familia_de_ca` (familia de la mesa),
  `competencia_comprador_mes.n_desiertas` (CA desiertas por comprador × familia × mes, F-plat-comp), precio ganador de
  referencia de `precio_indice` (F-plat-4), costos de `proveedor_productos`, comunas con `indice.mapa_comunas_oc`,
  territorio con `territorio.regiones_activas` (regla 25), `evaluacion_modelo` (`modelo` String(12): usar `demanda`),
  `_tablas_mercado_salud` (`app/api/salud_data.py`) para el tamaño. SVG inline (no hay librería de gráficos).
- Referencia de análisis: `scripts/mercado/mercado_observatorio.py: analizar_alimentos`.

## 1. Cambios

### 1.0 Medir antes de diseñar (anotar en el commit)
Sobre las OC de las regiones activas: (a) % de líneas de OC cuyo `rut_unidad` aparece como `organismo_rut` de alguna CA;
(b) unidades distintas (`unidad_compra` normalizada) por `rut_unidad`; (c) % de CA con OC cuyo
`clave_unidad(organismo_rut, unidad_compra)` = `clave_unidad(rut_unidad, unidad_compra de la OC)`; (d) cobertura de
producto por nivel (§1.1) en % del monto. Con eso decidir la clave del comprador (§1.1) y escribir la decisión.

### 1.1 Niveles y clave
- **Comprador:** si (c) ≥ 80 %, usar `clave_unidad`; si no, usar el **RUT** (`indice._rut(rut_unidad)`) como clave y
  mostrar las unidades como detalle. Documentar cuál ganó y por qué. En ningún caso inventar un cruce por nombre libre.
- **Producto, tres niveles** (el más fino disponible por línea):
  1. `producto_id` (catálogo canónico, por `sugerir_producto` sobre la especificación, solo si la sugerencia es única);
  2. **UNSPSC × base** (`codigo_producto` de 8 dígitos y `base` kg/L/u): cobertura casi total y unidades homogéneas;
  3. **familia** (`familia_de_codigo`).
  La **cantidad** solo se suma dentro de una misma `base` (nunca kg con unidades). Cantidad en base =
  `total_linea_neto / precio_base` cuando ambos existen [I: verificar con 20 líneas que calza con `cantidad` ×
  factor de `unidad_medida`; si no calza, usar la regla que calce y anotarla]. A nivel familia, solo **monto**.

### 1.2 `demanda_series` (tabla + `app/mercado/demanda.py`)
Clave: `comprador` × `nivel` (`producto|unspsc|familia`) × `item` (id, código o familia) × `mes`. Columnas: `region`,
`n_oc`, `n_ca`, `cantidad_base` (nullable), `base`, `monto_neto`, `precio_mediana` (por base), `n_proveedores`.
- **Sin doble conteo [V del prompt viejo]:** CA con OC en `oc_lineas_ca` → cuenta la OC; CA cerrada sin OC cargada →
  cuenta `ca_lineas` `ok` (pocas hoy); CA desierta → no suma cantidad (las desiertas salen de
  `competencia_comprador_mes`). Test con los tres casos.
- Recálculo por lotes (regla 12), idempotente (borra y reinserta los meses tocados, **acotado a las regiones que
  recalcula**: no repetir el error de `recalcular_competencia`, que borra el mes en todas las regiones).

### 1.3 Pronóstico v1 (`pronosticar(...)`, sin modelos de series complejos ni dependencias nuevas)
- `recurrencia` = meses con compra / meses observados desde la primera compra.
- `estacionalidad[m]` = promedio en ese mes calendario de años anteriores / promedio general (2 años → 2
  observaciones; 1 → marcada `debil`).
- `tendencia` = media móvil 3 meses / media móvil 12 meses, acotada a [0,5; 2].
- `p_compra[m]` = recurrencia ajustada por si compró en ese mes calendario antes; `cantidad_esperada[m]` (o monto a nivel
  familia) = promedio × estacionalidad × tendencia; intervalo = p25–p75 histórico escalado.
- Solo con `n_meses_con_compra ≥ 4`; si no, `insuficiente`. Tabla `pronosticos` (`comprador`, `nivel`, `item`,
  `mes_objetivo`, `p_compra`, `cantidad_esperada`, `monto_esperado`, `intervalo`, `debil`, `generado_en`, `metodo`).
  Horizonte: 3 meses. `demanda_series` y `pronosticos` son datos públicos comunes, sin `empresa_id` (regla 26).

### 1.4 Juez: backtest (sin aflojar metas)
- Para cada uno de los últimos 6 meses M con datos: pronosticar con datos < M y comparar con M.
- **Línea base** (mismo criterio que F-plat-4): «compra en M si compró en el mismo mes del año anterior o en M−1».
- Métricas por nivel, **al lado de la línea base**: precisión y recall de «compra en M» con `p_compra ≥ 0,6`; MAPE de
  cantidad (o monto) en los pares que compraron; cobertura (% del monto de alimentos con pronóstico no
  `insuficiente`). **Meta: precisión ≥ 70 % y recall ≥ 50 %, y ganarle a la línea base.** Si no le gana, la UI
  muestra la línea base (y lo dice). Guardar en `evaluacion_modelo` (`modelo = "demanda"`) sin borrar las filas de
  `indice`, `base`, `logistico` ni `competencia` al reevaluar.
- El backtest no mira el mes evaluado ni posteriores (test).

### 1.5 Recomendaciones (`recomendar(empresa_id)`, tabla `recomendaciones`, de la empresa)
- **Precio firme:** ítem (nivel producto o UNSPSC × base) con Σ `cantidad_esperada` de los próximos 3 meses ≥ umbral
  por base (default 200 kg, 300 L, 500 u; `DEMANDA_UMBRALES` en `Settings` como JSON) → «pedir precio firme a
  {proveedor con mejor costo} por {cantidad} de {ítem} para {meses}», con costo actual, variación de 90 días y precio
  de referencia de `precio_indice` (y el margen que deja).
- **Próxima compra esperada:** comprador × ítem con `p_compra ≥ 0,6` el mes siguiente → «{comprador} suele comprar
  {ítem} en {mes}: preparar oferta».
- **Hueco de proveedor:** ítem con demanda ≥ umbral y sin `proveedor_productos` de la empresa → «buscar proveedor de
  {ítem}».
- **Demanda desierta:** comprador × familia con ≥ 2 desiertas en 6 meses (de `competencia_comprador_mes`) →
  oportunidad con poca competencia.
- Comunas prioritarias de la empresa primero (comuna del comprador por `mapa_comunas_oc`). Solo regiones de la empresa.
- Tabla: `empresa_id`, `tipo`, `nivel`, `item`, `comprador` (nullable), `mes_objetivo`, `cantidad`, `texto`, `datos`
  JSONB, `generada_en`, `estado` (`nueva|vista|hecha|descartada`), `usuario_id`, `actualizada_en`. **Upsert** por
  `(empresa_id, tipo, nivel, item, comprador, mes_objetivo)` que **conserva el `estado`** puesto por el usuario
  (re-ejecutar no resucita una descartada ni duplica una hecha). Para saber si insertó, `RETURNING`, nunca `rowcount`
  tras `ON CONFLICT` (en Postgres no es confiable; lección de F-plat-5).
- Sin alarmas en la campana en esta fase (evitar ruido); se puede sumar después.

### 1.6 UI `/abastecimiento` (usuarios con empresa)
Pestañas: **Recomendaciones** (por tipo; marcar hecha o descartada con CSRF), **Calendario** (próximos 3 meses ×
ítem: cantidad o monto esperado, p_compra, compradores), **Compradores** (qué compra, cada cuánto, a quién y a qué
precio; enlace a `/competencia?comprador=…` cuando la clave sea `clave_unidad`), **Ítem** (serie 24 meses,
estacionalidad, proveedores de la empresa y variación de costo). Cada número con su `n`; lo `debil` o `insuficiente`
se marca. Si el pronóstico no le gana a la línea base, un aviso visible. Entrada «Abastecimiento» en la barra lateral.
Pie «Fuente: Dirección ChileCompra» (regla 8).

### 1.7 Job y operación
Job propio `mercado-demanda` (sin API, `_locked`), en `mercado-indices.yml` **después de `mercado-competencia`**
(`jobs: "mercado-indices mercado-competencia mercado-demanda"`), con `--completo`. Recalcula los últimos 3 meses de
series, los pronósticos y las recomendaciones. Medir duración con volumen real y ajustar `timeout_min` (hoy 60); si la
suma diaria de las 07:45 pasa de ~25 min, proponer workflow propio. Documentar en `operacion-disparos.md`.
Regla 11: medir bytes por fila y total estimado; agregar las tres tablas a `_tablas_mercado_salud`.

## 2. Seguridad
`recomendaciones` es de la empresa: ownership en servidor (reglas 17 y 26), GET/POST cruzados → 404, CSRF en
mutaciones, empresa desde la sesión. Sin `|safe`.

## 3. Tests
Series sintéticas (compra cada mes, cada 2 meses, solo marzo y septiembre, una sola vez); el backtest no mira el mes
evaluado; línea base; `insuficiente` con < 4 meses; cantidades nunca mezclan bases; tres niveles de producto; doble
conteo (OC, CA sin OC, desierta); borrado acotado a regiones; recomendaciones con y sin proveedor; upsert que conserva
el estado (con `RETURNING`); comunas prioritarias primero; ownership; idempotencia. `ruff check .`,
`python -m mypy app`, `python -m pytest -rs` completo contra Postgres local (lo corre Boris), sin saltados.

## 4. Cierre
Changelog («Abastecimiento: qué van a comprar y cuándo»). `00-estado.md` con §1.0 y el backtest (con la línea base al
lado). `decisiones.md`: clave del comprador elegida. Commit:
`F-plat-6: demanda por comprador, pronóstico y recomendaciones de abastecimiento`.

## 5. Pasos de Boris
1. Deploy de día con `GH mercado` y `GH mercado-indices` pausados y Actions vacío; en Render, `Running upgrade
   b3e7c1d9f4a2 -> …`; reactivar.
2. Carga completa desde el PC contra producción:
   ```powershell
   $env:DATABASE_URL = ((Get-Content .env | Select-String '^DATABASE_URL_PROD=').Line -split '=',2)[1]
   python -m app.ingest run-once --job mercado-demanda --completo --esperar-lock-min 10
   Remove-Item Env:DATABASE_URL
   ```
   Pegar el resumen (mediciones de §1.0 y backtest).
3. Revisar las 20 primeras recomendaciones con ojo de negocio: obvias, sorprendentes, malas. Anotar en
   `decisiones.md` los umbrales que cambien.
