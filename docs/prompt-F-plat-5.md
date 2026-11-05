# Prompt de implementación — F-plat-5 (Mesa del día, ficha ampliada, ofertas propias y modo sombra) · 3-nov-2026

> Para Claude Code. **Modelo: Sonnet** (UI HTMX sobre funciones ya existentes; contrato claro). Una fase = un
> commit, **sin push**. **Con migración.** Va después de F-merc-1, F-merc-2, F-plat-1, F-plat-2, F-plat-3,
> F-plat-4. Origen: `docs/17-plan-pivote-plataforma-ca.md` §2 C6; `16-pivote` §2 peldaño 2.

> **Revisado contra el código el 4-nov-2026 (HEAD `a9c0cfe`).** Lo marcado [V] está verificado en el código. La
> sección "Revisión 4-nov" manda sobre el resto del prompt donde choquen. Si algo no calza con lo que dejen F-plat-1, 2
> o 4, gana el código: anotarlo en el commit y no inventar.

## Revisión F-equipo (5-nov) — manda sobre todo lo demás
F-equipo ya define el estado compartido de cada CA (`ca_trabajo`, `ca_eventos`, `ca_comentarios`; `app/mercado/equipo.py`).
1. **`ca_descartes_mesa` no se crea:** descartar es `ca_trabajo.estado = 'descartada'` con motivo
   (`equipo.cambiar_estado`, que escribe su evento).
2. **«Marcar enviada»** cambia `ca_trabajo` a `enviada` con `equipo.cambiar_estado` (y su evento); `ofertas_propias` guarda
   solo el contenido de la oferta (líneas, precios, total), no el estado del flujo.
3. **El cierre de §1.4 reutiliza F-equipo §1.3:** `equipo.resultado_de` / `equipo.generar_alarmas` (resultado ganada,
   perdida, no_registrada, sin_dato, desierta, con `elegir_por_rut` y `rut_competidor`). No se define dos veces.
4. **La mesa respeta el filtro «Equipo»** (`equipo.filtrar_equipo`) y oculta las descartadas por defecto; las tarjetas
   nuevas conservan el bloque `_equipo_ca.html` y el panel del hilo.

## Revisión 4-nov noche (Cowork) — manda sobre todo lo demás
1. **`/mesa` ya existe** (F-mesa-v0, `app/api/routes/mesa_dia.py`) con vigentes, filtros, banderas e historial del
   comprador, y `clave_comprador` en `app/mercado/comprador.py`. Esta fase **amplía** esa pantalla (precio sugerido,
   margen, P(ganar), ofertas propias, sombra); no la reescribe.
2. **Competencia:** los "competidores probables" de la tarjeta y la pestaña Comprador usan `competidores_de` y
   `perfil_competidor` de F-plat-comp (`app/mercado/competencia.py`); no se recalcula desde las ofertas crudas.
3. **Vista de estrategia:** `/estrategia` (F-plat-comp) se completa acá con P(ganar) al precio sugerido y margen
   esperado por comprador × familia, usando los costos de la empresa. Es la pantalla de "a quién apuntamos, en qué
   rubro y a qué precio".
4. Prerrequisitos nuevos: F-mesa-v0 y F-plat-comp, además de F-plat-4.

## Revisión 4-nov (Cowork) — leer primero
1. **Prerrequisito nuevo: F-merc-3 (detalle de CA vigentes).** Hoy nadie baja el detalle de las CA abiertas [V]:
   `cola_mercado_ca` (`app/ingest/mercado.py`) solo toma terminales o `publicada` con el cierre ya pasado, y el job
   `ca` está apagado desde el 31-oct (`decisiones.md`). Sin F-merc-3, las vigentes no tienen `ca_productos`, ni
   `ca_lineas`, ni `n_documentos`, y la mesa queda vacía. Ver `docs/prompt-F-merc-3.md`.
2. **Router:** `app/api/routes/mesa.py` ya existe y es de F-plat-3 (empresas y proveedores, ~1.000 líneas [V]). Las
   rutas `/mesa*` van en un archivo nuevo (`app/api/routes/mesa_dia.py`), reutilizando `_sin_empresa` y el patrón de
   ownership de `mesa.py`.
3. **Caché de evaluaciones, decidido:** tabla `evaluaciones_cache` (PK `(ca_codigo, empresa_id)`, `evaluada_en`,
   `modelo_id`, `datos` JSONB). Va **separada** de `predicciones`, porque las predicciones son inmutables y solo se
   crean con `cobertura ≥ 0,5`; mezclarlas rompería el modo sombra. La mesa lee la caché y muestra "evaluada hace X
   min"; la ficha (`/mesa/ca/{codigo}`) recalcula en vivo.
4. **Dónde se calcula: no dentro de `mercado-ca`.** Ese job usa API, tiene `timeout_min: 20` en `mercado.yml` y 10
   min de detalles [V]. Va en un job nuevo, `mercado-mesa`, sin API, con `_locked("mercado-mesa", ...)`, en el mismo
   workflow después de `mercado-catalogo` (F-plat-1). Ese job (a) recalcula `evaluaciones_cache` de las vigentes en
   alcance de cada empresa activa, (b) crea las predicciones que falten y (c) cierra las que ya tienen ganador.
   Medir cuánto tarda y ajustar `timeout_min` del workflow en el mismo commit.
5. **`modelo_id`:** es FK a `modelo_adjudicacion.id` (F-plat-4). Si F-plat-4 no guardó la línea base como fila de esa
   tabla, usar `modelo_id` nullable + `metodo` (`logistico|linea_base`), con `UNIQUE (ca_codigo, empresa_id,
   metodo)`.
6. **`predicciones.estado`** (`abierta|cerrada|sin_adjudicacion`) entra **en esta migración**, para que F-plat-7
   (§1.3) no necesite una.
7. **Ganador [V]:** en la base no hay un campo `proveedor_seleccionado` en el detalle. Ese nombre es un valor de
   `compras_agiles.estado`; el ganador es la oferta con `ca_cotizaciones.seleccionado = true`. El cierre (§1.4) lo
   hace `mercado-mesa` mirando la base, sin API:
   - precio ganador = `valor_neto` de la oferta seleccionada;
   - precio ganador por línea = `ca_cotizacion_productos`, unidos por `(ca_codigo, codigo_producto)` como en F-plat-4;
   - si hay más de una oferta seleccionada [I: adjudicación parcial], se suman y se marca `detalle.multi_ganador`.
8. **RUT de la empresa:** `empresas.rut` es nullable [V].
   - "Marcar enviada" exige que la empresa tenga RUT; si no lo tiene, mostrar un aviso con enlace a `/admin/empresas`.
   - Comparar con `normalizar_rut` (`app/matching/engine.py`) en los dos lados. Existe otra función, `rut()`, en
     `app/catalogos/mercado.py`: usar una sola.
   - Si la oferta marcada `enviada` no aparece entre las `ca_cotizaciones` de la CA ya cerrada → estado visible
     `no_registrada` (RUT mal escrito u oferta no subida), no `perdida`.
9. **Margen realizado** = total − costo − logística − costo financiero: el mismo cálculo que `evaluar_ca` de F-plat-4,
   que incluye el costo financiero. No definirlo dos veces.
10. **Competidores probables por unidad compradora:** `compras_agiles` no tiene `rut_unidad` [V]; tiene
    `organismo_rut` y `unidad_compra` (texto). Definir una vez `clave_comprador(ca)` en `app/mercado/`, como
    `(organismo_rut, norm(unidad_compra))`. F-plat-6 la reutiliza.
11. **Banderas:** "sin detalle" = `compras_agiles.detalle_lineas` (F-plat-1). "Anexo pendiente" =
    `ca_anexos.extraccion_estado` (F-plat-2).
12. **Retención:** una CA con `ofertas_propias` o `predicciones` no se purga, aunque su región deje de estar activa.
    Hacerlo con exclusión explícita en `purgar_terminales` (`app/core/retencion.py`) más un test, o con FK
    `ON DELETE RESTRICT`. Elegir una de las dos y justificarla.
13. **Horario:** `mercado-indices` corre a las **07:45** (F-plat-4), no "de noche". El paso 1 de Boris (§4) queda
    corregido abajo.
14. La API de envío de ofertas sigue [I]. Boris pide el adjunto de la guía v3.0; no se escribe que no existe sin
    haberlo visto (regla 20).

## 0. Antes de escribir código
- Leer `CLAUDE.md` (8, 17, 18), `17-plan` §2 C6 y R-p2/R-p3, `app/api/routes/pages.py` (feed `/`, ficha
  modal, `/compras-agiles` explorador, `_card_oportunidad.html`, `_modal_ficha.html`), `app/api/query.py`
  (`evaluacion_ca`, F-plat-4), `app/mercado/*`, `templates/base.html` (menú "Mesa" de F-plat-3).
- **No hay API para enviar ofertas** [I: confirmar en la guía v3.0 antes de escribir la exportación; si la
  hay, anotarlo [V] y dejar el envío automático como fase futura, no en esta]. La oferta se sube en el portal
  a mano; la plataforma prepara y registra.

## 1. Cambios

### 1.1 Esquema
- `predicciones`: `id`, `ca_codigo` FK, `empresa_id`, `creada_en`, `cierre_ca`, `modelo_id` FK,
  `precio_sugerido`, `intervalo_80_min/max`, `p_ganar_sugerido`, `margen_esperado`, `cobertura`, `detalle` JSONB
  (por línea), `resultado_precio_ganador` nullable, `resultado_gano_prediccion` nullable (si la empresa
  ofertó), `cerrada_en`. **Se escribe antes del cierre y no se edita** (modo sombra). `UNIQUE (ca_codigo,
  empresa_id, modelo_id)`.
- `ofertas_propias`: `id`, `ca_codigo`, `empresa_id`, `usuario_id`, `estado` (`borrador|enviada|ganada|
  perdida|desierta|no_enviada`), `lineas` JSONB (`[{linea_id, precio_unitario, proveedor_producto_id}]`),
  `total_neto`, `plazo_entrega_dias`, `enviada_en`, `resultado_en`, `precio_ganador_real`, `margen_realizado`,
  `notas`. `UNIQUE (ca_codigo, empresa_id)`.
- `ca_descartes_mesa`: `ca_codigo`, `empresa_id`, `usuario_id`, `motivo`, `en`. (Separado de los descartes
  de perfiles de la app antigua.)

### 1.2 `/mesa` (usuarios con empresa)
- Query: CA de `regiones_activas` con `estado` publicada y `fecha_cierre` futura, en familias del alcance
  (`app/catalogos/mercado.py`), no descartadas por la empresa. Para cada una, `evaluacion_ca` (cacheada por
  CA en la corrida de `mercado-ca`, tabla `evaluaciones_cache` o columna JSONB en `predicciones`: elegir y
  justificar; la mesa **no** recalcula el modelo en cada request).
- Orden por defecto: `margen_esperado × p_ganar` desc, con las **comunas priorizadas** de la empresa primero
  (R-p2); luego cierre asc. Filtros: familia, comuna, cierra hoy/mañana/esta semana, "sin detalle",
  "anexo pendiente", "con costo", "margen ≥ X %".
- Tarjeta: comprador y unidad, comuna de entrega, cierre (hora Chile y cuenta regresiva), n líneas y estado
  del detalle (bandera), precio sugerido con intervalo, costo, logística, margen, P(ganar) en 3 puntos,
  competidores probables (top 3 proveedores por unidad compradora × familia en 12 meses, con tasa de
  victoria), botones Cotizar / Descartar / Marcar sin detalle. Conteo por filtro vía HTMX como en
  `/compras-agiles/conteo`.
- Pie: "Fuente: Dirección ChileCompra" (regla 8).

### 1.3 Ficha ampliada (`/mesa/ca/{codigo}`)
- Pestañas: **Líneas** (F-plat-1 y F-plat-2: edición, anexo), **Precio y margen** (por línea: referencia con
  ámbito y n, costo con proveedor elegido editable entre los de la empresa, logística, margen; total y
  curva P(ganar)), **Comprador** (histórico 24 meses: qué compró de la familia, a quién, precio, n ofertas;
  desde `oc_lineas_ca` y `ca_cotizaciones`), **Mi oferta**.
- **Mi oferta**: parte de los precios sugeridos; el usuario ajusta por línea y ve margen y P(ganar)
  recalculados (endpoint HTMX `POST /mesa/ca/{codigo}/simular`, sin persistir). "Guardar borrador",
  "Marcar enviada" (fecha), "Exportar" → XLSX (openpyxl) con líneas, cantidades, precio unitario, total y
  plazo, lista para transcribir al portal.
- Enlace a la ficha pública de la CA (`_url_ficha("compras_agiles", codigo)`; la ficha interna es `/oportunidad/compras_agiles/{codigo}` [V, F-plat-nav]) para subir la oferta y bajar anexos.

### 1.4 Modo sombra y cierre automático
- En `mercado-ca`, para cada CA vigente en alcance con `cobertura ≥ 0,5`: si no hay `prediccion` de la
  empresa y faltan ≥ 2 h para el cierre, se crea. Para cada empresa activa.
- Al llegar el detalle con `proveedor_seleccionado` (F-merc-1), se cierra la predicción: `resultado_precio_
  ganador`, y si hay `oferta_propia` enviada, su `estado` pasa a `ganada|perdida` comparando RUT de la
  empresa con el ganador (campo `empresas.rut`), `precio_ganador_real` y `margen_realizado` (si ganó:
  total − costo de las líneas al momento de la oferta − logística).
- `/mesa/resultados`: tabla de predicciones cerradas de la empresa (últimas 8 semanas): predicho vs. real,
  acierto del intervalo, ofertas propias y resultado; totales arriba (acierto %, ganadas, margen realizado).
  Es la vista del juez del peldaño 2.

### 1.5 Permisos y límites
Ownership por `empresa_id` en predicciones, ofertas, descartes (regla 17; tests GET/POST cruzados → 404).
CSRF en mutaciones. Un usuario sin empresa no ve `/mesa`.

## 2. Tests
Query de la mesa (alcance, territorio, descartes, orden con comunas priorizadas); simulación recalcula sin
persistir; exportación XLSX con las columnas; predicción inmutable (segundo intento no la cambia); cierre
automático con fixture de detalle con ganador = RUT de la empresa y ≠; ownership. `ruff`, `mypy app`,
`pytest -rs` sin saltados.

## 3. Cierre
Changelog ("Mesa del día: qué cotizar, a qué precio y con qué margen"). `00-estado.md`: arranca el modo
sombra de 4 semanas con fecha. Commit: `F-plat-5: mesa del día, ofertas propias y modo sombra`.

## 4. Pasos de Boris
1. Migración (de día, con `GH mercado` en pausa, como en F-plat-3). `mercado` cada 15 min ya está activo; `mercado-indices` va a las 07:45 (F-plat-4).
2. Usar `/mesa` 4 semanas: cotizar de verdad al menos 10 CA de alimentos; registrar enviadas y resultados.
3. Al cierre de las 4 semanas: pegar `/mesa/resultados` → decide el paso al peldaño 3 (`16-pivote` §2).
