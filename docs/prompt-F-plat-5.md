# Prompt de implementación — F-plat-5 (precio, margen y P(ganar) en la mesa; mi oferta; modo sombra) · 5-nov-2026

> Para Claude Code. **Modelo: Opus** (predicciones inmutables para el juez de 4 semanas, esquema nuevo, ownership).
> Una fase = un commit, **sin push**. **Con migración**: `down_revision = "a8d4f2c6e9b1"` (cabeza tras F-equipo [V];
> confirmar con `alembic heads`).
> **Reescrito y revisado contra el código el 5-nov-2026 (HEAD `cfc5f54`).** Reemplaza al prompt del 3-nov y sus
> revisiones del 4 y 5-nov (en el historial de git). Lo marcado [V] está verificado; si algo no calza, gana el código:
> anotarlo en el commit y no inventar. Origen: `17-plan` §2 C6 y §7; `16-pivote` §2 peldaño 2.

## Objetivo
Que el equipo, en la mesa, vea **qué cotizar, a qué precio y con qué margen**, arme su oferta, la registre, y que la
plataforma **mida en sombra** cuánto acierta (precio ganador, P(ganar), margen) durante 4 semanas. Es el juez del
peldaño 2.

## 0. Lo que ya existe (no rehacer) [V]
- **Mesa** (F-mesa-v0): `/mesa` en `app/api/routes/mesa_dia.py` y `app/mercado/mesa_dia.py` (vigentes del
  territorio, filtros con conteo HTMX, comunas prioritarias primero, historial del comprador). Esta fase la **amplía**.
- **Evaluación** (F-plat-4, sin UI todavía): `adjudicacion.evaluar_ca(session, ca_codigo, empresa_id, ...)` →
  `EvaluacionCA` (líneas con referencia e intervalo 80 %, costo con proveedor, logística, costo financiero,
  `precio_sugerido`, `intervalo_80`, `curva`, `p_ganar` {agresivo, sugerido, comodo}, `margen_esperado`, `margen_pct`,
  `cobertura`, `completa`, `modelo`, `avisos`). Envoltorio para rutas: `app/api/query.py: evaluacion_ca` (la empresa
  sale de la sesión, nunca del cliente). `adjudicacion.curva_p_ganar` para simular. **No definir margen ni P(ganar)
  de nuevo.**
- **Modelo:** `modelo_adjudicacion` guarda una fila por entrenamiento, `tipo` = `logistico | base`, una `activa`. Hoy
  la activa es **base** y hay 0 meses evaluables (corrida del 5-nov).
- **Equipo** (F-equipo, `app/mercado/equipo.py`): `ca_trabajo` (estado compartido, versión), `cambiar_estado`,
  `filtrar_equipo`, `equipo_de`, `resultado_de` (ganada / perdida / no_registrada / sin_dato / desierta con
  `eleccion.elegir_por_rut` y `comprador.rut_competidor`), `generar_alarmas`; bloque `_equipo_ca.html` y panel del hilo
  en tarjetas y ficha. **El flujo (priorizar, cotizar, enviar, descartar) es de F-equipo**: no crear
  `ca_descartes_mesa` ni otro estado.
- **Competencia** (F-plat-comp, `app/mercado/competencia.py`): `competidores_de(clave_comprador, familia, regiones,
  nosotros=...)`, `perfil_competidor`. Clave de comprador: `comprador.clave_unidad`; familia: `familia_de_ca`.
- **Ficha interna:** `/oportunidad/compras_agiles/{codigo}` (`_ficha_contenido.html`, ya con líneas, anexo y bloque
  Equipo). **No crear una segunda ficha** (`/mesa/ca/{codigo}` del prompt viejo queda descartado): se amplía esta.
- **Retención:** `purgar_terminales` (`app/core/retencion.py`) ya excluye CA con `ca_trabajo` [V]; extender igual.
- `openpyxl` ya está en `requirements.lock` [V]. No hay librería de gráficos: SVG/CSS inline como en `/competencia`.

## 1. Cambios

### 1.1 Esquema (solo `app/models`)
- **`evaluaciones_cache`**: PK `(ca_codigo, empresa_id)`, `evaluada_en`, `modelo_id` (FK nullable), `datos` JSONB (lo
  que la tarjeta necesita: precio sugerido, intervalo, costo, logística, margen, margen %, P(ganar) 3 puntos,
  cobertura, completa, avisos). Se reescribe; la mesa la lee y muestra «evaluada hace X min».
- **`predicciones`** (inmutables, el juez): `id`, `ca_codigo` FK, `empresa_id` FK, `creada_en`, `cierre_ca`,
  `modelo_id` FK (la fila activa al crearla), `modelo_tipo` (`logistico|base`), `precio_sugerido`,
  `intervalo_80_min/max`, `p_ganar_sugerido`, `margen_esperado`, `cobertura`, `detalle` JSONB (por línea),
  `estado` (`abierta|cerrada|sin_adjudicacion`), `resultado_precio_ganador`, `resultado_en_intervalo` (bool),
  `resultado_propio` (copia del resultado de F-equipo si ofertamos), `cerrada_en`.
  **`UNIQUE (ca_codigo, empresa_id)`** — una sola predicción por CA y empresa. Ojo: `modelo_adjudicacion` crea una
  fila nueva en cada entrenamiento diario, así que un UNIQUE que incluyera `modelo_id` permitiría una predicción nueva
  cada día y rompería el modo sombra. Los campos de la predicción no se editan nunca; solo se llenan los `resultado_*`,
  `estado` y `cerrada_en`.
- **`ofertas_propias`**: `id`, `ca_codigo`, `empresa_id`, `usuario_id` (último que guardó), `lineas` JSONB
  (`[{linea_id, precio_unitario, proveedor_producto_id}]`), `total_neto`, `plazo_entrega_dias`, `notas`,
  `guardada_en`, `version` (mismo patrón de conflicto que `ca_trabajo`), `precio_ganador_real`, `margen_realizado`.
  `UNIQUE (ca_codigo, empresa_id)`. **Sin columna de estado**: el estado del flujo vive en `ca_trabajo`.
- Retención: una CA con `predicciones` u `ofertas_propias` no se purga (misma exclusión que `ca_trabajo`, con test).

### 1.2 Job `mercado-mesa` (sin API; `_locked("mercado-mesa", ...)`)
Para cada empresa activa y cada CA vigente de sus regiones (por lotes; regla 12):
(a) reescribe `evaluaciones_cache` con `evaluar_ca`;
(b) si `cobertura ≥ 0,5`, no existe predicción y faltan ≥ 2 h para el cierre → crea la predicción (inserción
idempotente: `ON CONFLICT DO NOTHING`);
(c) cierra predicciones `abierta` cuyo cierre pasó y cuyas ofertas ya están leídas: `resultado_precio_ganador`
(`valor_neto` de la oferta seleccionada con `elegir_por_rut`; varias seleccionadas → suma y `detalle.multi_ganador`),
`resultado_en_intervalo`, y `resultado_propio` = `equipo.resultado_de` si la CA estuvo en `enviada`. CA desierta o
sin seleccionada al terminar → `sin_adjudicacion`. Con oferta propia ganada: `precio_ganador_real` y
`margen_realizado` (= total − costo de las líneas guardadas − logística − financiero; reutilizar las funciones de
costo y logística de `adjudicacion`, no reescribirlas).
Dónde corre: en `mercado.yml` después de `mercado-ca` y `mercado-catalogo` (cada 15 min de día). Medir la duración
con ~400 vigentes y 1 empresa; si pasa de 2 min, proponer bajarlo a cada hora en un workflow propio. Documentar en
`operacion-disparos.md`.

### 1.3 Tarjeta de `/mesa` (amplía la de F-mesa-v0)
- Suma: precio sugerido con intervalo 80 %, costo + logística, margen ($ y %), P(ganar) en 3 puntos, cobertura, y
  la etiqueta del modelo («línea base» mientras no haya logístico: decirlo en la tarjeta, sin esconderlo).
- Sin costo o sin líneas `ok` → la tarjeta lo dice («falta costo de 3 productos», «sin detalle») y no inventa margen.
- **Competidores:** con lo medido el 5-nov (el top 1 «esperado» ofertó solo en 8–11 % de las CA), la tarjeta **no**
  dice «competidores probables». Muestra «quiénes le han ofertado a este comprador en esta familia (12 meses)» con
  `competidores_de`, como historial, con su `n` y tasa de victoria, enlazando a `/competencia/{rut}`.
- Orden nuevo por defecto: comunas prioritarias primero; luego `margen_esperado × p_ganar_sugerido` desc (sin
  evaluación → al final de su grupo); luego cierre asc. Mantener el orden «por cierre» como opción.
- Filtros nuevos: «con costo», «margen ≥ X %», «con predicción». Se suman a los de F-mesa-v0 y al filtro «Equipo»
  (que oculta descartadas por defecto). Conteo HTMX como hoy.
- La mesa **solo lee** `evaluaciones_cache` y `predicciones` (nunca llama `evaluar_ca` en el request de la lista).

### 1.4 Ficha (`/oportunidad/compras_agiles/{codigo}`): sección «Precio y mi oferta»
- **Precio y margen** (en vivo con `evaluacion_ca`): por línea, referencia con ámbito y `n`, intervalo, costo con
  proveedor elegido (selector entre los `proveedor_productos` de la empresa), logística, margen; total y curva
  P(ganar) en SVG. Si hay predicción, mostrarla aparte, congelada («así se vio al predecir»).
- **Mi oferta:** parte de los precios sugeridos; el usuario ajusta por línea y ve margen y P(ganar) recalculados
  (`POST .../simular`, HTMX, sin persistir). «Guardar» (crea o actualiza `ofertas_propias`; si la CA está en
  `nueva`/`priorizada`, pasa a `cotizando` con `equipo.cambiar_estado`). «Marcar enviada» → `ca_trabajo` = `enviada`
  vía `equipo.cambiar_estado` (exige empresa con RUT; si no, aviso con enlace a `/admin/empresas`). «Exportar» → XLSX
  (líneas, cantidades, precio unitario, total, plazo) para transcribir al portal, con «Fuente: Dirección ChileCompra».
- Enlace a la ficha pública (`_url_ficha("compras_agiles", codigo)`) para subir la oferta.
- No hay API para enviar ofertas [I]: la oferta se sube a mano en el portal. No afirmar que no existe (regla 20).

### 1.5 `/mesa/resultados` (el juez del peldaño 2)
Predicciones cerradas de la empresa (últimas 8 semanas, filtro por semana): predicho vs. ganador real, acierto del
intervalo 80 %, error mediano de precio, Brier de `p_ganar_sugerido` donde ofertamos, ofertas propias y su resultado,
margen realizado. Totales arriba con `n` siempre visible. Debe funcionar con 0 filas (mensaje claro).

### 1.6 Fuera de esta fase (anotar en `00-estado.md`)
- `/estrategia` con P(ganar) y margen por comprador × familia → **F-plat-5b**, cuando haya costos cargados y algún mes
  evaluable (hoy no hay ninguno; con la línea base sería decorativo).
- Envío automático de ofertas, correo, IA.

## 2. Seguridad
Ownership por `empresa_id` en `evaluaciones_cache`, `predicciones` y `ofertas_propias` (reglas 17 y 26; GET/POST
cruzados → 404). La empresa sale de la sesión. CSRF en guardar, enviar y exportar (POST). Sin `|safe`.

## 3. Tests
Predicción inmutable (segunda corrida del job y entrenamiento de un modelo nuevo no crean otra ni la cambian);
no se crea con cobertura < 0,5 ni a < 2 h del cierre; cierre: ganador con multi-ganador, en/fuera del intervalo,
`sin_adjudicacion`, resultado propio reutilizando `equipo.resultado_de`; margen realizado con las funciones de F-plat-4;
simulación sin persistir; guardar oferta con conflicto de versión; «Guardar» mueve `priorizada → cotizando` y
«Marcar enviada» → `enviada` con sus eventos; empresa sin RUT; XLSX con columnas y fuente; la mesa no llama
`evaluar_ca` en la lista (listener o monkeypatch); orden y filtros nuevos; tarjeta sin costo; ownership cruzado;
retención; `/mesa/resultados` con 0 y con n filas. `ruff check .`, `python -m mypy app`, `python -m pytest -rs`
completo contra Postgres local (lo corre Boris), sin saltados.

## 4. Cierre
Changelog («Precio, margen y mi oferta en la mesa»). `00-estado.md`: **fecha de inicio del modo sombra** y fecha de
fin (+4 semanas). Commit: `F-plat-5: precio, margen y P(ganar) en la mesa, mi oferta y modo sombra`.

## 5. Pasos de Boris
1. **Antes:** importar la planilla de proveedores **con costos puestos en bodega (flete incluido)** en
   `/proveedores/importar`. Sin costos, casi ninguna CA llega a `cobertura ≥ 0,5` y no se crean predicciones.
   Y completar en `/tarifas` el costo fijo por viaje de las 3 provincias (y de las comunas que convenga): vienen en $0
   desde F-plat-3 y, en $0, la logística no resta nada y el margen sale inflado sin aviso.
2. Deploy de día con `GH mercado` y `GH mercado-indices` pausados y Actions vacío; en Render, `Running upgrade
   a8d4f2c6e9b1 -> …`; reactivar.
3. Usar la mesa 4 semanas: cotizar de verdad al menos 10 CA de alimentos y marcarlas enviadas.
4. Al cumplir 4 semanas: pegar `/mesa/resultados` → decide el paso al peldaño 3 (`16-pivote` §2).
