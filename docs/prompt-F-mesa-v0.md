# Prompt de implementación — F-mesa-v0 (Mesa del día sin predicción) · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet** (UI HTMX sobre tablas existentes, contrato claro). Una fase = un commit,
> **sin push**. **Sin migración** (si resulta imprescindible una, parar y proponerla). Va después de F-plat-2.
> Origen: `docs/17-plan-pivote-plataforma-ca.md` §7 (decisión de Boris, 4-nov noche, en `decisiones.md`).
> **Revisado contra el código el 4-nov-2026 (HEAD `67160e9`, con F-plat-2 sin commitear en el árbol).** Lo marcado
> [V] está verificado. Si F-plat-2 cambió algo de lo que se cita, gana el código: anotarlo en el commit.

## Objetivo
Que el equipo vea **hoy** las Compras Ágiles vigentes de su territorio y trabaje sobre CA vivas (abrir, revisar
líneas, subir anexo), sin esperar al modelo de F-plat-4. F-plat-5 crece sobre esta misma pantalla: nada de lo que
se haga acá se descarta después.

## 0. Antes de escribir código
- Leer `CLAUDE.md` (8, 17, 18, 25, 26), `docs/prompt-F-plat-5.md` (la mesa final; esta fase es su v0) y su
  "Revisión 4-nov", y la sección "Revisión 4-nov noche" que se agrega ahí.
- Código [V]:
  - `app/api/routes/mesa.py` es de F-plat-3 (empresas y proveedores). Las rutas nuevas van en
    **`app/api/routes/mesa_dia.py`** (F-plat-5 rev. §2), reutilizando `html_require_empresa` y
    `empresa_activa_id` (`app/api/deps.py`) y `_sin_empresa` de `mesa.py`.
  - Territorio: `regiones_activas(session, settings)` (`app/mercado/territorio.py`).
  - `compras_agiles`: `estado`, `fecha_cierre`, `fecha_publicacion`, `region`, `organismo_nombre`, `organismo_rut`,
    `unidad_compra`, `direccion_entrega`, `plazo_entrega_dias`, `monto_disponible_clp`, `total_ofertas`,
    `n_documentos`, `detalle_lineas` (`completo|incompleto|sin_detalle`), `detalle_vigente_en`.
  - Vigente = `estado = 'publicada'` (`EstadoOportunidad.PUBLICADA`) y `fecha_cierre` futura. **Fechas en hora de
    Chile** (regla 6): comparar contra `ahora` en `America/Santiago` como lo haga `app/core/tiempo.py`.
  - Familia: `grupo_de` / `familia_alimento` (`app/catalogos/mercado.py`) sobre `ca_productos.codigo_producto` o
    `ca_lineas.codigo_unspsc`. Una CA puede tener varias familias: mostrar la de mayor monto o "varias".
  - Ficha: `/oportunidad/compras_agiles/{codigo}` (con empresa, no solo admin [V, `pages.py`]), donde F-plat-2 deja
    la subida de anexo. Ficha pública: `_url_ficha` (`app/api/query.py`).
  - Portada «Hoy» (`hoy.html`): tarjetas de conteo.

## 1. Cambios

### 1.1 `/mesa` (usuarios con empresa; sin empresa → `_sin_empresa`)
- Query parametrizada (ORM): CA vigentes de `regiones_activas`, con paginación (50) y sin cargar `raw_json`
  (regla 12: nada de traer todo a memoria).
- Orden por defecto: cierre ascendente. Si la empresa tiene `comunas_prioritarias`, primero las CA cuya
  `direccion_entrega` contenga alguna de esas comunas (comparación normalizada, sin tildes; es [I] y se marca
  así en la tarjeta: "comuna detectada en la dirección").
- Filtros (GET, HTMX con conteo como `/compras-agiles/conteo`): familia; cierra hoy / mañana / esta semana;
  bandera de detalle (`sin_detalle`, `incompleto`, `completo`); "con anexos" (`n_documentos > 0`); monto mínimo;
  texto libre sobre `nombre` (FTS si ya existe para CA; si no, `ILIKE` parametrizado).
- Tarjeta: nombre, comprador (organismo + unidad), comuna detectada, cierre (hora de Chile y cuenta regresiva),
  monto disponible, n líneas y bandera de detalle, n.º de anexos, plazo de entrega pedido. Enlaces: ficha interna
  (líneas y anexo) y ficha pública.
- **Historial del comprador** (desplegable en la tarjeta, HTMX, se calcula al abrir): con
  `clave_comprador(ca) = (organismo_rut, norm(unidad_compra))`, definida **en esta fase** en
  `app/mercado/comprador.py` (F-plat-5 rev. §10 y F-plat-6 la reutilizan): últimos 12 meses → n.º de CA, monto
  adjudicado, mediana de oferentes (RUT distintos, regla de `decisiones.md` F-merc-1c), y los 3 proveedores que más
  le ganaron con su n.º de adjudicaciones. Es descriptivo; el análisis de competencia es F-plat-comp.
- Pie: "Fuente: Dirección ChileCompra" (regla 8).

### 1.2 Menú y portada
- Entrada **«Mesa»** en la barra lateral, primera después de «Hoy», visible para usuarios con empresa.
- En «Hoy», tarjeta "CA vigentes" (total y las que cierran hoy) que enlaza a `/mesa`.

### 1.3 Rendimiento
- Medir el tiempo de `/mesa` con ~250 vigentes (volumen real [V, 4-nov]: 252 abiertas) y del historial del
  comprador; anotarlos en el commit. Si el historial pasa de 300 ms, proponer el índice que falte (sería migración:
  parar y proponer, no agregarlo solo).

## 2. Tests
Ownership (sin empresa → aviso; región fuera del territorio no aparece); solo vigentes (cerrada, cierre pasado,
desierta no aparecen) con fechas en hora de Chile; filtros y conteo; orden por comunas prioritarias; `clave_comprador`
(RUT y unidad normalizados; unidad nula); historial con "n.º de oferentes = RUT distintos"; sin `raw_json` en la query;
pie de fuente. `ruff`, `mypy app`, `pytest -rs` sin saltados, contra Postgres local.

## 3. Cierre
Changelog ("Mesa: las Compras Ágiles vigentes de tu territorio"). `00-estado.md`. Commit:
`F-mesa-v0: mesa del día con las CA vigentes del territorio`.

## 4. Pasos de Boris
Push de día (sin migración, no hace falta pausar crons). Revisar `/mesa` con el equipo una semana y anotar qué
falta: eso alimenta F-plat-5 ampliada.
