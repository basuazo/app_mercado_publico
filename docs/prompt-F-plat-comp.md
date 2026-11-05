# Prompt de implementación — F-plat-comp (análisis de competencia) · 4-nov-2026

> Para Claude Code. **Modelo: Opus** (estadística con juez, variables nuevas para el modelo de F-plat-4, esquema).
> Una fase = un commit, **sin push**. **Con migración.** Corre **en paralelo con F-plat-4** (ventana B' de
> `docs/17-plan-pivote-plataforma-ca.md` §7, con las reglas de §4-bis: worktree, rama de Neon y `down_revision`
> propios) y **se audita después de F-plat-4**: antes de auditarla, `git rebase main` y re-apuntar la migración.
> Origen: `17-plan` §7 (decisión de Boris, 4-nov noche).
> **Revisado contra el código el 4-nov-2026 (HEAD `67160e9`).** Lo marcado [V] está verificado; si no calza con lo
> que dejen F-plat-2, F-mesa-v0 o F-plat-4, gana el código: anotarlo en el commit y no inventar.

## Objetivo
Saber **contra quién competimos, dónde y cómo**: quién gana qué, a qué precio relativo, con qué compradores y en
qué rubros, y usarlo para (a) decidir estrategia y (b) mejorar P(ganar). Entrenamos con Compra Ágil y luego se
llevará a licitaciones: la lógica no debe depender de nada propio de CA (ver §1.5).

## 0. Antes de escribir código
- `CLAUDE.md` (6, 8, 10, 11, 12, 17, 24, 26), `17-plan` §1 (diagnóstico: 59 % "gana el más barato", brecha
  ganador–segundo 2,7 %), §4-bis y §7; `decisiones.md` F-merc-1c (**regla de elección**: precio de un competidor =
  su oferta de menor `valor_neto`; precio del ganador = la oferta seleccionada; n.º de oferentes = RUT distintos,
  no filas); `docs/prompt-F-plat-4.md` §1.2–§1.3 (modelo y walk-forward).
- Datos [V, `app/models/tables.py`]:
  - `ca_cotizaciones`: una fila por **oferta** (un RUT puede tener varias); `rut_proveedor`, `razon_social`,
    `es_emt`, `seleccionado` (nullable: None = la API no lo dijo, no "perdió"), `valor_neto`, `monto_despacho`,
    `llamado`, `estado_glosa`.
  - `ca_cotizacion_productos`: `precio_unitario` por `codigo_producto`.
  - `compras_agiles`: `organismo_rut`, `unidad_compra`, `region`, `estado`, `fecha_cierre`, `total_ofertas`,
    `motivo_desierta`, `monto_disponible_clp`.
  - `oc_lineas_ca`: `region_proveedor`, `comuna_proveedor` por RUT → ubicación conocida del competidor.
  - `empresas.rut` (nullable) → "nosotros" entre los oferentes.
- Reutilizar, no duplicar: la clave de comprador (`clave_unidad` de F-plat-4, movida a `app/mercado/comprador.py` en
  F-mesa-v0), el registro de variables de `app/mercado/adjudicacion.py` (punto de extensión de F-plat-4), `grupo_de` /
  `familia_alimento` (`app/catalogos/mercado.py`). **RUT:** hay tres normalizadores [V] (`app/matching/engine.py:
  normalizar_rut`, `app/mercado/proveedores.py: normalizar_rut`, `app/catalogos/mercado.py: rut`): usar el de
  `app/mercado/proveedores.py` y anotar en el commit si los otros difieren en algún caso.
- Medir antes de diseñar: cuántos meses completos de `ca_cotizaciones` hay en Neon dev (el backfill sigue llenando
  la historia; `meses_completos`) y cuántos RUT distintos. Anotarlo en el commit.

## 1. Cambios

### 1.1 Agregado mensual (tabla `competencia_mes`; solo `app/models` define esquema)
Una fila por `rut` × `clave_comprador` (como `organismo_rut` + `unidad_norm`) × `familia` × `mes`:
`n_ca` (CA en que ofertó), `n_ofertas`, `n_ganadas` (`seleccionado = true`), `n_sin_dato` (`seleccionado` nulo),
`suma_log_ratio` y `n_log_ratio` (para la mediana aproximada usar también `p50_log_ratio` calculado en el job),
`monto_ganado`, `n_ca_un_oferente`. Donde `log_ratio = log(precio del RUT / mediana de los precios de la CA)` con la
regla de elección de F-merc-1c, solo en CA con ≥ 2 oferentes. Índices por `rut` y por `(organismo_rut, familia)`.
Medir bytes por fila y total (regla 11) y anotarlo; si pasa de 20 MB, agregar por trimestre en vez de mes.

### 1.2 Cálculo (`app/mercado/competencia.py`, sin httpx)
- `recalcular_competencia(session, desde_mes)` por lotes de 2.000 CA (regla 12), idempotente (borra y reinserta
  los meses tocados). CA desiertas y canceladas: cuentan para "CA sin oferentes / desiertas" del comprador, no para
  tasas de victoria.
- Consultas para la UI (todas sobre `competencia_mes`, nunca sobre las ofertas crudas en un request):
  - `perfil_competidor(rut, meses=12)`: CA, tasa de adjudicación (con intervalo de Wilson y `n`), precio relativo
    típico, compradores y familias top, tendencia mensual, ubicación conocida (`oc_lineas_ca`), EMT sí/no.
  - `competidores_de(clave_comprador, familia, meses=12)`: ranking con participación, tasa de victoria y precio
    relativo; marca a "nosotros" si `empresas.rut` aparece.
  - `mapa_mercado(empresa_id, meses=12)`: comprador × familia con monto, n.º de CA, oferentes medianos, % de CA con
    un solo oferente, % desiertas, competidor dominante (mayor `monto_ganado`), y **% en que no ganó el más
    barato** (señal de que el comprador premia otra cosa). Solo regiones activas de la empresa.
  - `cara_a_cara(rut_a, rut_b)`: CA donde ofertaron los dos, quién ganó y diferencia de precio relativo.

### 1.3 Variables para el modelo de F-plat-4
`variables_competencia(ca) -> dict`, calculable **antes del cierre**: competidores habituales esperados del
comprador × familia (top 3 por participación en 12 meses, excluyendo el mes de la CA), su agresividad (precio
relativo mediano) y la tasa de un solo oferente del comprador. Integración en `app/mercado/adjudicacion.py` (ya en
`main` cuando esta fase se audita): las variables entran **solo si mejoran el Brier** en el mismo walk-forward de
F-plat-4 (con y sin; ambos números en `evaluacion_modelo` y en el commit). Si no mejoran, quedan calculadas y fuera
del modelo. Sin fuga de datos: nada del mes evaluado ni posterior.

### 1.4 UI (usuarios con empresa; pie "Fuente: Dirección ChileCompra", regla 8)
- `/competencia`: buscador por RUT o razón social y ranking de competidores en el territorio.
- `/competencia/{rut}`: perfil (§1.2) con tendencia (gráfico simple con la librería que ya use la plataforma; sin
  dependencias nuevas).
- `/estrategia`: el mapa de mercado (§1.2) como tabla filtrable por familia y ordenable; es la base de la vista de
  estrategia que F-plat-5 ampliada combinará con P(ganar) y margen.
- En la tarjeta de `/mesa` (F-mesa-v0): el historial del comprador enlaza a los competidores de ese comprador ×
  familia.
- Entradas «Competencia» y «Estrategia» en la barra lateral.
Los competidores son datos públicos de ChileCompra; no se exportan en bloque ni se envían a terceros (regla 26: lo
que pertenece a la empresa son sus ofertas y su RUT, no los perfiles de competidores).

### 1.5 Portabilidad a licitaciones
`competencia.py` recibe filas genéricas (`proceso`, `comprador`, `familia`, `fecha`, `rut`, `precio`, `gano`)
desde un adaptador `_filas_desde_ca(...)`. Licitaciones solo tendrá que escribir otro adaptador. **No** se piden
licitaciones a la API (regla 24).

### 1.6 Job
`mercado-competencia` (sin API, `_locked("mercado-competencia", ...)`), en el workflow de `mercado-indices`
después de ese job; recalcula los últimos 2 meses (`--completo` para todo). Medir duración y ajustar
`timeout_min`. Documentar en `operacion-disparos.md`.

## 2. Juez
- **Predicción de competidores (walk-forward, últimos 6 meses con datos suficientes):** para cada CA cerrada del mes
  M con ≥ 2 oferentes, los top 3 esperados (datos < M) vs. los que ofertaron. Reportar precisión (de los 3
  predichos, cuántos ofertaron) y recall. **Meta: el top 1 oferta en ≥ 60 % de las CA de su comprador × familia.**
  Se reporta aunque no llegue; no se afloja.
- **Aporte al modelo:** Brier con y sin variables de competencia (§1.3).
- **Cuadratura:** suma de `n_ganadas` del mes = CA con oferta seleccionada del mes (±0,5 %).

## 3. Tests
Regla de elección (varias ofertas del mismo RUT; n.º de oferentes = RUT distintos); `seleccionado` nulo no cuenta
como derrota; CA de un oferente fuera del `log_ratio`; idempotencia del recálculo; Wilson con n chico; sin fuga
temporal en `variables_competencia`; ownership (`/estrategia` solo regiones activas de la empresa; sin empresa →
aviso); "nosotros" con `empresas.rut` nulo; adaptador genérico; pie de fuente. `ruff`, `mypy app`, `pytest -rs` sin
saltados, contra Postgres local.

## 4. Cierre
Changelog ("Competencia y mapa de mercado"). `00-estado.md`. Commit:
`F-plat-comp: análisis de competencia, mapa de mercado y variables para P(ganar)`.

## 5. Pasos de Boris
1. Deploy de día con `GH mercado` en pausa (migración). 2. `python -m app.ingest run-once mercado-competencia
--completo` desde el PC. 3. Revisar `/estrategia` y 5 perfiles de competidores que el equipo conozca: ¿calza con lo
que saben del mercado? Anotar discrepancias.
