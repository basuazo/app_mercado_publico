# Prompt de implementación — F-plat-1 (catálogo canónico y líneas 1:1 por Compra Ágil) · 3-nov-2026

> Para Claude Code. **Modelo: Opus** (esquema central del pivote, normalización con juez estadístico, backfill
> sobre tablas grandes). Una fase = un commit, **sin push**. **Con migración.** Va después de F-merc-1,
> F-merc-2 y F-plat-3. Origen: `docs/17-plan-pivote-plataforma-ca.md` §1, §2 C2.
> **Revisado contra el código el 4-nov-2026 (HEAD `7b0bae6`).** Lo marcado [V] está verificado. Si algo no
> calza con el código, gana el código: anotarlo en el commit y no inventar.

## 0. Antes de escribir código
- El problema [V]: en agosto 2026 (API, Ñuble) 19 % de las CA son multiproducto y en alimentos lo típico es
  "ALIMENTOS SEGÚN ANEXO ADJUNTO", cantidad 1. Con UNSPSC como producto, 201 de 245 códigos con n ≥ 5 son
  heterogéneos (`data/mercado/informe/bandas_precio.md`). Sin producto + formato no hay precio comparable.
- Leer: `CLAUDE.md` (6, 10, 11, 12, 17), `docs/17-plan-pivote-plataforma-ca.md` §1–§3,
  `docs/16-pivote-compra-agil-nuble.md` §5 (catálogo como el INE), `data/mercado/informe/bandas_precio.md`.
- Código: `app/catalogos/mercado.py` (GRUPOS, familias, `norm`, `normalizar`, de F-merc-2),
  `scripts/mercado/mercado_observatorio.py` (`normalizar`, `_precios_unitarios`, walk-forward),
  `scripts/mercado/bandas_precio.py`, `app/models/tables.py` (`CompraAgil`, `CaProducto`, `ca_cotizaciones`,
  `ca_cotizacion_productos`, `oc_lineas_ca`, `proveedor_productos`), `app/ingest/compra_agil.py` (~l. 165:
  `ca_productos` se **borran y reinsertan** en cada detalle [V]), `app/ingest/__main__.py` (`_locked`),
  `app/api/routes/pages.py` (ficha de CA: `GET /oportunidad/{fuente}/{codigo}`, ~l. 949 [V]).
  `app/matching/text.py` **no** normaliza texto: solo arma tsquery [V]; la normalización está en
  `app/catalogos/mercado.py` (`norm`).
- Canasta [V]: `data/mercado/canasta_borrador_candidatos.xlsx` (125 candidatos + 10 genéricos, generado por
  `scripts/mercado/canasta_borrador.py`) y `scripts/mercado/canasta_reglas.py` (regex por producto).
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
- `normalizar_texto(s) -> str` (reutilizar `app.catalogos.mercado.norm`; agregar solo lo que falte, p. ej.
  quitar cantidades sueltas para el alias).
- `extraer_formato(s) -> (cantidad, unidad, factor_base, unidad_base) | None`: patrones "400 g", "1,5 L",
  "25 kg", "pack 6", "caja de 12 unidades", "x 24", "docena", "kilo", "litro", "sachet 1 kg"; números con coma;
  "o similar" se ignora. Tabla de tests con 60 textos reales de `cot_api_2026-08.csv.gz` (sin RUT ni montos).
- `detectar_sin_detalle(s) -> bool`: "según anexo", "adjunto", "detalle en excel", "ver documento", "listado"
  + cantidad 1 y monto > $100.000 [heurística; medir falsos positivos en el test con 100 textos].
- `sugerir_producto(texto, unspsc) -> (producto_id, formato_id, confianza) | None` sobre
  `producto_alias` + reglas: coincidencia exacta de alias (1,0), alias contenido (0,8), familia por UNSPSC +
  palabra clave (0,5). Solo ≥ 0,8 pasa a `ok` automáticamente; 0,5–0,8 → `dudosa`; < 0,5 → `pendiente`.
  Las palabras clave salen de las regex de `scripts/mercado/canasta_reglas.py` (copiarlas a
  `app/catalogos/canasta_alimentos.py` junto a cada producto; `app/` no importa de `scripts/`).

### 1.3 Canasta inicial y backfill (`python -m app.ingest run-once mercado-catalogo --inicial`)
- Semilla de `producto_canonico` + formatos en `app/catalogos/canasta_alimentos.py`, desde
  `canasta_borrador_candidatos.xlsx`: filas con `decision_boris = entra`; si Boris aún no la llenó, las de
  `propuesta = entra` (top 80 por monto). Nombre = `nombre_final_boris` si existe, si no `producto`;
  formato inicial = `formato_sugerido`; `unidad_base` de la columna. Los 10 genéricos "sin detalle" no
  entran. **La semilla solo inserta lo que falta**: nunca renombra, fusiona ni desactiva productos
  existentes (lo que Boris corrija en `/catalogo` manda). Así se puede sembrar el borrador y curarlo después.
- Backfill por lotes de 2.000 (regla 12) sobre `ca_productos` de las regiones activas (usar
  `regiones_activas` de F-plat-3): crea `ca_lineas` con `origen=api`, aplica `sugerir_producto`, fija
  `detalle_lineas`.
  - `ca_lineas` = **lo solicitado** (`ca_productos`); `n` = posición por `id` dentro de la CA [I: la ingesta
    conserva el orden de la API]. `ca_cotizacion_productos` **no** crea líneas: su texto (a menudo más
    específico, p. ej. "Nescafé 170 g") se usa como segunda fuente para `sugerir_producto` cuando
    `codigo_producto` coincide con el de la línea.
  - Como `ca_productos` se reinsertan en cada detalle, `ca_lineas` no tiene FK a `ca_productos`. Si al
    re-procesar cambia el `texto_origen` de la línea `n`: sin revisión humana → recalcular; revisada →
    `dudosa` y conservar la revisión (nunca pisarla en silencio).
  - Idempotente: re-ejecutar no duplica (upsert por `(ca_codigo, n)`).
- Job `mercado-catalogo` (sin API) registrado con `_locked("mercado-catalogo", ...)` en
  `app/ingest/__main__.py` como los demás (un solo advisory lock [V]): lo mismo para las CA nuevas desde el
  cursor `sync_state` `mercado_catalogo`. Se ejecuta tras `mercado-ca` en el mismo workflow de Actions.

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
- Ficha de CA (`/oportunidad/{fuente}/{codigo}` con la fuente de CA): sección "Líneas" con estado, bandera y acceso a corrección. Solo
  para CA de regiones activas y usuarios con empresa; el resto de la ficha no cambia.
- Ownership: las correcciones son compartidas (el catálogo es común), pero `revisado_por` queda registrado
  y los usuarios sin empresa no editan (R-p3).

### 1.5 Juez: walk-forward por producto canónico (`scripts/mercado/walk_forward_canonico.py`, solo lectura)
- Para el último mes cerrado con cotizaciones: por línea `ok` con `cantidad_base`, precio ganador por unidad
  base vs. mediana de los 3 meses anteriores del mismo `producto_id` (comuna → provincia → región).
- Reporta dos coberturas, error mediano, % dentro de ±20 % y los 10 productos que más fallan:
  - **(a) cobertura con detalle propio**: % del monto de CA de alimentos **con líneas propias** (excluye las
    CA `sin_detalle` por anexo y los kits/servicios) que queda con línea `ok`;
  - **(b) cobertura total**: % del monto de alimentos de 12 meses con línea `ok`. Se reporta sin meta.
    Referencia [V, 4-nov-2026, `canasta_borrador.py` sobre OC oct-25–sep-26]: solo ~39 % del monto tiene un
    producto identificable en la línea; 29 % son anexos u órdenes de pedido, 12 % kits (colaciones, cajas),
    12 % servicios (cóctel, coffee). El resto lo cubre F-plat-2.
- Línea base de error [V]: 33 % de error mediano en alimentos con UNSPSC (`16-pivote` §4-bis).
  **Meta: (a) ≥ 70 % y error ≤ 25 %.** La meta anterior (70 % del monto total) era inalcanzable sin anexos
  y se cambió el 4-nov. Si no pasa, reportar los números reales (no ajustar la meta).

## 2. Tests
Reglas de 1.2 (tabla de textos reales); backfill idempotente y que no pisa revisiones humanas; estados de
`detalle_lineas`; re-aplicación de alias; permisos; migración con `proveedor_productos` existente. `ruff`,
`mypy app`, `pytest -rs` sin saltados, contra Postgres local (`TEST_DATABASE_URL`, `mp-test-pg`;
`docs/operacion.md` §15); los que necesitan Postgres llevan el marcador `postgres`.

## 3. Cierre
Changelog ("Catálogo de productos y líneas por Compra Ágil"). `00-estado.md`. Commit:
`F-plat-1: catálogo canónico, líneas 1:1 por Compra Ágil y bandera de CA sin detalle`.

## 4. Pasos de Boris
0. (Opcional, mejora la semilla) marcar `decision_boris` y `nombre_final_boris` en el Excel de la canasta.
1. Migración según `docs/despliegue.md` (pausar `GH mercado` → push → `Running upgrade` → reactivar; nunca
   `alembic upgrade` manual). Luego `python -m app.ingest run-once mercado-catalogo --inicial` desde el PC
   con `DATABASE_URL` exportado desde `DATABASE_URL_PROD` (sin API).
2. Una hora en `/catalogo` → Pendientes, por monto. Anotar cuántas líneas se confirman por hora.
3. `python scripts/mercado/walk_forward_canonico.py` y pegar la salida (es el juez de la fase).
