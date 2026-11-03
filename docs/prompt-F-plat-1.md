# Prompt de implementación — F-plat-1 (catálogo canónico y líneas 1:1 por Compra Ágil) · 3-nov-2026

> Para Claude Code. **Modelo: Opus** (esquema central del pivote, normalización con juez estadístico, backfill
> sobre tablas grandes). Una fase = un commit, **sin push**. **Con migración.** Va después de F-merc-1,
> F-merc-2 y F-plat-3. Origen: `docs/17-plan-pivote-plataforma-ca.md` §1, §2 C2.

## 0. Antes de escribir código
- El problema [V]: en agosto 2026 (API, Ñuble) 19 % de las CA son multiproducto y en alimentos lo típico es
  "ALIMENTOS SEGÚN ANEXO ADJUNTO", cantidad 1. Con UNSPSC como producto, 201 de 245 códigos con n ≥ 5 son
  heterogéneos (`data/mercado/informe/bandas_precio.md`). Sin producto + formato no hay precio comparable.
- Leer: `CLAUDE.md` (6, 10, 11, 12, 17), `docs/17-plan-pivote-plataforma-ca.md` §1–§3,
  `docs/16-pivote-compra-agil-nuble.md` §5 (catálogo como el INE), `data/mercado/informe/bandas_precio.md`.
- Código: `app/catalogos/mercado.py` (GRUPOS, familias, `normalizar`, de F-merc-2),
  `scripts/mercado/mercado_observatorio.py` (`normalizar`, `_precios_unitarios`, walk-forward),
  `scripts/mercado/bandas_precio.py`, `app/models/tables.py` (`CompraAgil`, `CaProducto`, `ca_cotizaciones`,
  `ca_cotizacion_productos`, `oc_lineas_ca`, `proveedor_productos`), `app/matching/text.py` (normalización
  de texto con unaccent), `app/api/routes/pages.py` (ficha de CA `/oportunidad/ca/{codigo}`).
- **Sin modelo de lenguaje en esta fase**: reglas + curación humana. El LLM entra en F-plat-2 con
  presupuesto. Esta fase mide cuánto resuelven las reglas solas.

## 1. Cambios

### 1.1 Esquema (`tables.py` + migración)
- `producto_canonico`: `id`, `nombre` (único), `familia` (de `app/catalogos/mercado.py`), `unspsc_sugerido`,
  `unidad_base` (`kg|L|u`), `activo`, `creado_por` (usuario, nullable), timestamps.
- `producto_formato`: `id`, `producto_id` FK, `nombre` ("caja 1 L", "saco 25 kg", "unidad"), `factor_base`
  (Float > 0), `ean` nullable. `UNIQUE (producto_id, nombre)`. Todo producto nace con el formato "unidad base"
  (factor 1).
- `producto_alias`: `id`, `texto_normalizado` (único; minúsculas, sin tildes, espacios colapsados, sin
  cantidades sueltas), `producto_id` FK, `formato_id` FK nullable, `fuente` (`regla|llm|humano`), `confianza`
  (0–1), `revisado_por` nullable, `revisado_en`, `veces_visto` (int). Índice por `producto_id`.
- `ca_lineas`: `id`, `ca_codigo` FK → `compras_agiles` ON DELETE CASCADE, `n` (orden), `producto_id`
  nullable, `formato_id` nullable, `cantidad` (Float), `cantidad_base` (= cantidad × factor), `texto_origen`
  (Text, ≤ 1.000), `codigo_unspsc`, `origen` (`api|anexo|manual`), `estado` (`ok|dudosa|pendiente`),
  `revisado_por`, `revisado_en`, timestamps. `UNIQUE (ca_codigo, n)`. Índices por `producto_id` y `estado`.
- `compras_agiles.detalle_lineas` (`completo|incompleto|sin_detalle`, default `sin_detalle`) + índice: la
  **bandera "CA sin detalle"** que pide Boris. `sin_detalle` = alguna línea `pendiente` o ninguna línea;
  `incompleto` = alguna `dudosa`; `completo` = todas `ok`.
- `proveedor_productos.producto_id` recibe ahora su FK a `producto_canonico` (y `formato_id` nullable).
- Retención: no se purgan.

### 1.2 Normalización por reglas (`app/catalogos/canonico.py`, puro, sin DB)
- `normalizar_texto(s) -> str` (reutilizar `app/matching/text.py`).
- `extraer_formato(s) -> (cantidad, unidad, factor_base, unidad_base) | None`: patrones "400 g", "1,5 L",
  "25 kg", "pack 6", "caja de 12 unidades", "x 24", "docena", "kilo", "litro", "sachet 1 kg"; números con coma;
  "o similar" se ignora. Tabla de tests con 60 textos reales de `cot_api_2026-08.csv.gz` (sin RUT ni montos).
- `detectar_sin_detalle(s) -> bool`: "según anexo", "adjunto", "detalle en excel", "ver documento", "listado"
  + cantidad 1 y monto > $100.000 [heurística; medir falsos positivos en el test con 100 textos].
- `sugerir_producto(texto, unspsc) -> (producto_id, formato_id, confianza) | None` sobre
  `producto_alias` + reglas: coincidencia exacta de alias (1,0), alias contenido (0,8), familia por UNSPSC +
  palabra clave (0,5). Solo ≥ 0,8 pasa a `ok` automáticamente; 0,5–0,8 → `dudosa`; < 0,5 → `pendiente`.

### 1.3 Canasta inicial y backfill (`python -m app.ingest run-once mercado-catalogo --inicial`)
- Semilla de `producto_canonico` + formatos: los **80 productos de alimentos de mayor monto en Ñuble (12
  meses)** del observatorio, en `app/catalogos/canasta_alimentos.py` (lista revisable por Boris; nombres en
  español chileno: "marraqueta", "hallulla", "bebida 3 L", "jugo en caja 200 mL", "leche entera 1 L", etc.).
- Backfill por lotes de 2.000 (regla 12) sobre `ca_productos` (productos solicitados) y
  `ca_cotizacion_productos` (lo que ofertaron) de las regiones activas: crea `ca_lineas` con `origen=api`,
  aplica `sugerir_producto`, fija `detalle_lineas`. Idempotente: re-ejecutar no duplica (upsert por
  `(ca_codigo, n)`) y no pisa líneas revisadas por humano.
- Job `mercado-catalogo` (sin API, lock `mercado`): lo mismo para las CA nuevas desde el cursor
  `sync_state` `mercado_catalogo`. Se encadena al final de `mercado-ca`.

### 1.4 Curación (`/catalogo`, usuarios con empresa; crear producto: cualquier usuario; fusionar: admin)
- Pestañas: **Pendientes** (líneas `pendiente` y `dudosa`, ordenadas por monto de la CA, con el texto origen,
  la sugerencia y un selector de producto + formato con búsqueda HTMX), **Productos** (lista, crear, editar,
  formatos), **Alias** (los aprendidos, con quién los revisó).
- Confirmar una línea: guarda `ca_lineas` (`ok`, `revisado_por`), crea o refuerza el alias
  (`fuente=humano`, `confianza=1`) y **re-aplica** el alias a todas las líneas `pendiente|dudosa` con el
  mismo `texto_normalizado` (en la misma transacción, con conteo mostrado).
- "Sin detalle (anexo)": botón que marca la línea `pendiente` con `origen=anexo` y deja la CA en
  `sin_detalle`; es lo que F-plat-2 resuelve. Hasta entonces, "Agregar líneas a mano" permite tipear
  producto, formato y cantidad (`origen=manual`).
- Ficha de CA (`/oportunidad/ca/{codigo}`): sección "Líneas" con estado, bandera y acceso a corrección. Solo
  para CA de regiones activas y usuarios con empresa; el resto de la ficha no cambia.
- Ownership: las correcciones son compartidas (el catálogo es común), pero `revisado_por` queda registrado
  y los usuarios sin empresa no editan (R-p3).

### 1.5 Juez: walk-forward por producto canónico (`scripts/mercado/walk_forward_canonico.py`, solo lectura)
- Para el último mes cerrado con cotizaciones: por línea `ok` con `cantidad_base`, precio ganador por unidad
  base vs. mediana de los 3 meses anteriores del mismo `producto_id` (comuna → provincia → región).
- Reporta: % del monto de alimentos (12 meses) con línea `ok`; error mediano; % dentro de ±20 %; los 10
  productos que más fallan. Línea base [V]: 33 % de error mediano en alimentos con UNSPSC
  (`16-pivote` §4-bis). **Meta: ≥ 70 % del monto con línea `ok` y error ≤ 25 %.** Si no pasa, reportar
  los números reales (no ajustar la meta).

## 2. Tests
Reglas de 1.2 (tabla de textos reales); backfill idempotente y que no pisa revisiones humanas; estados de
`detalle_lineas`; re-aplicación de alias; permisos; migración con `proveedor_productos` existente. `ruff`,
`mypy app`, `pytest -rs` sin saltados, contra Neon dev.

## 3. Cierre
Changelog ("Catálogo de productos y líneas por Compra Ágil"). `00-estado.md`. Commit:
`F-plat-1: catálogo canónico, líneas 1:1 por Compra Ágil y bandera de CA sin detalle`.

## 4. Pasos de Boris
1. Migración en Neon; `python -m app.ingest run-once mercado-catalogo --inicial` (desde el PC; sin API).
2. Una hora en `/catalogo` → Pendientes, por monto. Anotar cuántas líneas se confirman por hora.
3. `python scripts/mercado/walk_forward_canonico.py` y pegar la salida (es el juez de la fase).
