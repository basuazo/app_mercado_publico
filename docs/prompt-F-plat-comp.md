# Prompt de implementación — F-plat-comp (análisis de competencia) · 4-nov-2026

> Para Claude Code. **Modelo: Opus** (estadística con juez, variables nuevas para el modelo de F-plat-4, esquema).
> Una fase = un commit, **sin push**. **Con migración** (`down_revision = "e4a7c2f9d1b3"`, la cabeza actual [V]).
> Se implementa **sobre `main`**, en la carpeta normal: F-plat-4 y F-mesa-v0 ya están en `main` (la idea de worktree
> paralelo de §4-bis quedó obsoleta). Origen: `17-plan` §7 (decisión de Boris, 4-nov noche).
> **Revisado contra el código el 5-nov-2026 (después de `5d9b36b` F-mesa-v0 y F-tasa-respuesta).** Lo marcado [V]
> está verificado. Si algo no calza con el código, gana el código: anotarlo en el commit y no inventar.
> **Leer primero §6 «Revisión 5-nov»: corrige y precisa partes de §0–§5.**

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
- Medir antes de diseñar: ver §6.1 (datos reales de producción; Neon dev puede no responder desde la máquina de
  Claude Code y tiene menos historia).

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
1. Deploy de día, antes de las 22:30, con `GH mercado` pausado y Actions vacío (la migración toma locks); en el log
de Render, un `Running upgrade e4a7c2f9d1b3 -> …`; reactivar `GH mercado`.
2. Carga completa desde el PC contra producción:
   ```powershell
   $env:DATABASE_URL = ((Get-Content .env | Select-String '^DATABASE_URL_PROD=').Line -split '=',2)[1]
   python -m app.ingest run-once --job mercado-competencia --completo --esperar-lock-min 10
   Remove-Item Env:DATABASE_URL
   ```
3. Revisar `/estrategia` y 5 perfiles de competidores que el equipo conozca: ¿calza con lo que saben del mercado?
   Anotar discrepancias.

## 6. Revisión 5-nov (Cowork, contra el código en `main` + F-mesa-v0 + F-tasa-respuesta) — manda sobre §0–§5

### 6.1 Datos reales (producción, corrida `mercado-indices --completo` del 5-nov) [V]
- Dataset de F-plat-4: **1.194 CA cerradas con ofertas, 6.987 ofertas**. CA con ≥ 2 oferentes: **0 de mayo a
  septiembre y 775 en octubre**. La historia de ofertas empieza en la práctica en **octubre de 2026**; el backfill de
  detalles la va extendiendo hacia atrás (más rápido desde F-tasa-respuesta).
- `evaluacion.elegibilidad` exige ≥ 50 CA con ≥ 2 oferentes en el mes **y ≥ 3 meses previos** con ≥ 20 CA
  (`MIN_MESES_PREVIOS = 3`, `app/mercado/evaluacion.py`) [V]. Hoy **ningún mes es evaluable** y el modelo activo es
  `base`.
- Consecuencias para el juez (§2), sin aflojar metas:
  - **Aporte al modelo (Brier con y sin):** usar exactamente la elegibilidad y el walk-forward de F-plat-4. Si no
    hay meses evaluables, las variables quedan **registradas pero fuera del modelo**, y el commit dice
    «Brier con/sin: pendiente, 0 meses evaluables». No crear una evaluación paralela más laxa.
  - **Predicción de competidores:** puede correr con 1 mes previo (es un ranking, no el modelo). Reportar por mes
    con su `n` y marcar `datos_insuficientes` si el mes evaluado tiene < 50 CA con ≥ 2 oferentes. Hoy se espera
    solo noviembre (entrenado con octubre) y parcial: anotarlo.
  - Cuadratura de `n_ganadas`: sí se puede verificar ya.

### 6.2 Reutilizar lo que dejaron F-plat-4 y F-mesa-v0 [V]
- **Comprador:** `app/mercado/comprador.py: clave_unidad(organismo_rut, unidad_compra)` es la ÚNICA clave. En
  `competencia_mes` guardar esa clave como columna (`clave_comprador`, string) además de `organismo_rut`; no armar
  `organismo_rut + unidad_norm` por otro camino.
- **Familia:** usar `familia_de_codigo` / `familia_de_ca` de `app/mercado/adjudicacion.py` (las mismas que usan el
  modelo y la mesa, `mesa_dia.familias_por_ca`), no `grupo_de`/`familia_alimento` directo. Así la familia que ve el
  usuario en `/mesa` es la misma con la que se filtra en `/competencia` y `/estrategia`.
- **Regla de elección F-merc-1c:** hoy está implementada **dos veces** (`comprador._elegir` e
  `indice._elegir_por_rut`). No escribir una tercera: dejar una sola función pública (p. ej. en `comprador.py`) y que
  `indice.py`, `comprador.py` y `competencia.py` la usen. Si las dos actuales difieren en algún caso (p. ej. precio
  `None`), anotarlo en el commit y cubrirlo con un test.
- **Variables para el modelo:** el punto de extensión es `registrar_variable(nombre, extractor)` sobre `VARIABLES` en
  `adjudicacion.py`, y se piden con `entrenar_logistico(filas, variables=(*VARIABLES_BASE, ...))` [V]. Usarlo; no
  tocar `VARIABLES_BASE`.
- **Wilson:** si ya existe un intervalo de Wilson en `app/mercado/`, reutilizarlo; si no, uno solo en `competencia.py`.

### 6.3 RUT
- `ca_cotizaciones.rut_proveedor` se guarda **tal como viene de la API** (`app/ingest/mercado.py`) [V], y
  `clave_unidad` usa `indice._rut` (sin puntos ni espacios, en mayúscula; **no** agrega guion). Ya son cuatro
  normalizadores. Para el RUT del **competidor** y `empresas.rut` usar `app/mercado/proveedores.py:
  normalizar_rut`; si lanza `ValueError`, caer a `indice._rut` y contar el caso (`ruts_no_validos` en el resumen).
  Usar el mismo normalizador en `historial_comprador` (top proveedores de la mesa) para que el enlace de la mesa a
  `/competencia/{rut}` siempre encuentre al competidor. Medir en los datos cuántos RUT cambian de forma entre
  normalizadores y anotarlo. La clave de comprador **no** se toca (cambiarla movería el modelo de F-plat-4).

### 6.4 UI
- **No hay librería de gráficos** en la plataforma [V] (`app/api/static`: solo JS propio). La tendencia mensual va
  como barras con CSS o SVG inline generado en la plantilla, sin dependencias nuevas.
- Enlace desde la mesa: en `_mesa_comprador.html` (desplegable del historial), enlazar a
  `/competencia?comprador=<clave>&familia=<familia de la tarjeta>`. Si la tarjeta dice «Varias» o «Sin productos»,
  enlazar sin familia. Cada proveedor del top enlaza a `/competencia/{rut}`.
- Queries de la UI solo sobre `competencia_mes` (y `oc_lineas_ca` para la ubicación), por `rut` y por
  `clave_comprador`; ningún request recorre `ca_cotizaciones`.

### 6.5 Job y operación
- `mercado-competencia` se registra en `_JOBS` de `app/ingest/__main__.py` con `_locked(...)`, igual que
  `mercado-indices` [V], y acepta `--completo`.
- Workflow: agregarlo a `mercado-indices.yml` después de `mercado-indices` (`jobs: "mercado-indices
  mercado-competencia"`). Medir la duración del recálculo de 2 meses en la base de tests con volumen sintético
  parecido al real (unas 1.200 CA cerradas al mes, 7 ofertas por CA) y subir `timeout_min` si hace falta. Recordar
  que `mercado-indices --completo` tardó 19 min en producción: si la suma diaria pasa de ~15 min, proponer un
  workflow propio en vez de alargar el lock de las 07:45. Documentar en `operacion-disparos.md`.
- Regla 11: medir bytes por fila y estimar el total a 12 meses con el volumen real de §6.1; anotarlo en el commit.

### 6.6 Fuera de alcance
Licitaciones (regla 24), exportar perfiles en bloque, IA. No cambiar `MIN_MESES_PREVIOS` ni la elegibilidad de
F-plat-4.
