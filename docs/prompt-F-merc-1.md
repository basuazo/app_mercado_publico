# Prompt de implementación — F-merc-1 (segunda key: cotizaciones de Compra Ágil de Ñuble, en vivo y hacia atrás) · 31-oct-2026

> Para Claude Code. **Modelo: Opus** (toca cuota, 429, una regla dura de `CLAUDE.md` y retención).
> Una fase = un commit, **sin push**. **Con migración.** Origen: `docs/16-pivote-compra-agil-nuble.md`
> (§4-bis, §7, §9) y el informe `data/mercado/informe/informe-compra-agil-nuble.pdf`.


> **Corrección 31-oct (Boris): la ingesta actual de la app se pausa; hay una sola key (`MP_TICKET`).**
> Se eliminan de esta fase: §1.2 (segunda key y `MP_TICKET_MERCADO`), §1.3 (cuota y pausa por ticket, y la
> migración de `quota_log`) y el advisory lock propio de §1.6: los jobs `mercado-*` usan `MP_TICKET`, el
> `QuotaTracker` y la pausa `api_pausa` de siempre, sin `reserva`, y el lock existente. La sonda de §1.1 usa
> `MP_TICKET`. No se edita `CLAUDE.md`. Todo lo demás (§1.1, 1.4–1.8, tests y cierre) sigue igual.

> **Ajustes 3-nov (plan 17):** (a) la sonda del 31-oct [V, `01-analisis` §8] mostró que **no existe `seleccion`**:
> el ganador es `proveedores_cotizando[].proveedor_seleccionado` y el estado es `estado`; el parser de F-merc-0b ya
> lo hace, reutilizarlo tal cual (§1.4 queda cubierto por F-merc-0b salvo lo que falte de `CompraAgilDetalle`).
> (b) Como §1.2 se eliminó, crear igual el setting `mercado_regiones: list[int] = [16]` (sin segunda key) para que
> los jobs `mercado-*` filtren por región; F-plat-3 lo reemplaza por las regiones de las empresas activas.
> (c) §1.1 ya está hecha (sonda 31-oct): no repetirla; solo el punto 1 (antigüedad) si no quedó anotado.
> (d) Regla 24: ningún job de esta fase pide licitaciones.

## 0. Antes de escribir código

### 0.a Por qué
- El pivote necesita, para cada Compra Ágil (CA) de Ñuble, **quién cotizó, a qué precio por producto y
  quién ganó**. Hoy la app guarda el detalle solo de las CA con match y **no lee `proveedores_cotizando`**
  (backlog de `00-estado.md`).
- Los datos abiertos no alcanzan: `COT_` trae solo el monto total por oferta (sin precio por producto) y
  **[V, 31-oct] `COT_2026-03` viene sin Ñuble, Los Ríos ni Arica** (13 regiones); abril, junio, julio y
  agosto tienen el mismo síntoma [I]. Esos 5 meses solo se pueden reconstruir con la API.
- Boris tiene una **segunda key** (ticket) para esto. Con la key 1 no se toca nada de lo que ya corre.

### 0.b Lo que dice la guía oficial (Guía API Compra Ágil v2, v3.0 mayo 2026, §5–§7) — leer y respetar
- Detalle `GET /v2/compra-agil/{codigo}`: `proveedores_cotizando[]` con `rut_proveedor`, `razon_social`,
  `es_emt`, `codigo_empresa`, `codigo_sucursal_empresa`, `id_cotizacion`, `estado_cotizacion.{id,glosa}`
  (**no confirmado**), `estado_por_comprador`, `activo`, `justificacion_inadmisibilidad`,
  `seleccion.{proveedor_seleccionado, motivo_seleccion, criterio_seleccion}` (**no confirmado**),
  `fecha_creacion`, `fecha_vigencia`, `valor_neto`, `total_impuesto`, `monto_despacho`, `monto_total`,
  `nombre_impuesto`, `porcentaje_impuesto`, `descripcion_cotizacion`, `descripcion`, y
  **`productos_cotizados[]`** con `codigo_producto`, `nombre_producto`, `descripcion`, `cantidad`,
  **`precio_unitario` (neto)**, `monto_total_producto`.
- Las cotizaciones completas aparecen "desde estado Cerrada en segundo llamado en adelante" y el arreglo
  refleja **solo el llamado vigente**; antes de eso puede venir vacío o parcial [I].
- Otros campos del detalle que hoy no se leen: `institucion.unidad_compra`, `entrega.direccion_entrega`,
  `entrega.plazo_entrega_dias`, `presupuesto.tipo_presupuesto`, `presupuesto.moneda`,
  `convocatoria.fecha_cierre_primer_llamado/segundo_llamado`, `motivos.motivo_desierta/cancelacion`,
  `documentos[].{id,nombre}` (sin endpoint de descarga documentado: **no descargar**).
- Listado: `publicado_desde/hasta`, `region=16`, `estado=cerrada,proveedor_seleccionado,desierta,cancelada`,
  `ordenar_por=FechaPublicacion`, `tamano_pagina` ≤ 50. **No hay límite de antigüedad documentado** [I]: se
  verifica con la sonda (§1.1).
- La guía dice que el 429 es "cuota diaria"; nuestra regla 3 (verificada en producción) manda: el 429
  `codigo 429` es transitorio con pausa. No cambiar esa política.

### 0.c Lectura (sin cambios)
`CLAUDE.md` (reglas 1, 3, 4, 5, 6, 10, 11, 13); `docs/16-pivote-compra-agil-nuble.md` §7–§9;
`docs/01-analisis-api-mercado-publico.md` §5; `docs/decisiones.md` (Datos y API, retención).
Código: `app/clients/mp_v2.py` (`_parse_ca_detalle`, `MercadoPublicoV2Client.__init__`),
`app/clients/base.py` (`BaseClient`, `QuotaTracker`, `_CREATE_QUOTA_TABLE`, pausa en `sync_state`
`api_pausa`, `rate_limiter_compartido`), `app/clients/types.py`, `app/core/settings.py`,
`app/core/logging.py` (máscara de secretos), `app/ingest/compra_agil.py`, `app/ingest/orchestrator.py`
(`_JOBS`, `_run_with_lock`, ids de advisory lock), `app/ingest/__main__.py`, `app/core/retencion.py`
(`purgar_terminales`), `app/models/tables.py` (`CompraAgil`, `CaProducto`, `SyncState`),
`.github/workflows/_job.yml` y `ca.yml`, `scripts/smoke_test.py`, `docs/operacion-disparos.md`.

## 1. Cambios

### 1.1 Sonda humana primero (`scripts/smoke_test.py --mercado`)
Agregar una opción que **solo ejecuta el humano** (regla de tests), con `MP_TICKET_MERCADO`:
1. Listado `region=16`, `publicado_desde=2026-03-01`, `publicado_hasta=2026-03-07`, estados terminales,
   `tamano_pagina=10`: imprime `total_resultados` y 3 códigos. **Responde la pregunta de antigüedad.**
2. Detalle de uno de esos códigos en `proveedor_seleccionado`: imprime las **claves** (no valores) de
   `payload`, de `proveedores_cotizando[0]` y de `productos_cotizados[0]`, y cuántos proveedores tienen
   `seleccion.proveedor_seleccionado == true`. Sin RUT ni montos en la salida.
3. Lo mismo con una CA de septiembre 2026 (control).
Resultado esperado a anotar en `docs/01-analisis-api-mercado-publico.md` con [V]/[I]. **El backfill (§1.6)
no se habilita en Actions hasta que Boris corra la sonda y confirme 1 y 2.**

### 1.2 Segunda key (`settings.py`, `logging.py`, `CLAUDE.md`)
- `mp_ticket_mercado: str | None = Field(None, repr=False)`; `mercado_regiones: list[int] = [16]`;
  `mercado_daily_budget: int = 9000`. Si falta el ticket, los jobs `mercado-*` salen **omitidos** con
  `::warning::`, nunca rojos.
- El logger enmascara también `MP_TICKET_MERCADO`. Test que lo pruebe (mismo patrón que `MP_TICKET`).
- `CLAUDE.md` regla 1: agregar `MP_TICKET_MERCADO` a la lista de secretos. Regla 3: "presupuesto **por
  ticket**". Es la única edición de reglas duras de esta fase.

### 1.3 Cuota y pausa por ticket (`base.py`, migración)
- `quota_log`: nueva columna `ticket VARCHAR(20) NOT NULL DEFAULT 'principal'`, PK `(fecha, ticket)`.
  Migración Alembic que conserva las filas actuales como `principal`. Ajustar `_CREATE_QUOTA_TABLE`
  (tests con SQLite) y los upserts.
- `QuotaTracker(engine, budget, reserva=0, ticket="principal")`; `remaining`, `check_budget` y el upsert del contador
  filtran por ticket. **Default `principal`: nada de lo existente cambia de comportamiento.**
- Pausa de 429: `sync_state.fuente = "api_pausa"` para `principal` (sin cambio) y
  `"api_pausa:mercado"` para la segunda key. Un 429 de una key **no pausa la otra** (H1 de
  `15-diagnostico-ca-429.md` sigue abierta: si resulta ser por IP, se ajusta después con datos).
- `MercadoPublicoV2Client(settings, engine, *, reserva=0, ticket="principal")`: con `ticket="mercado"`
  usa `settings.mp_ticket_mercado`, su `QuotaTracker` y su pausa. Mismo rate limit propio (1 req/s con
  jitter), mismos reintentos 10500 (30/60/120 s) y mismo enfriamiento de 60 s tras 504/timeout.

### 1.4 Parser del detalle (`mp_v2.py`, `types.py`) — capa anti-corrupción
- Dataclasses nuevos: `CotizacionCA` y `CotizacionCAProducto` (todos los campos de 0.b, opcionales salvo
  `rut_proveedor`). `CompraAgilDetalle` suma `cotizaciones: list[CotizacionCA]`, `unidad_compra`,
  `direccion_entrega`, `plazo_entrega_dias`, `tipo_presupuesto`, `moneda`,
  `fecha_cierre_segundo_llamado`, `motivo_desierta`, `motivo_cancelacion`, `n_documentos`.
- Parseo **defensivo** (regla 6): montos con `parse_float`, booleanos que pueden venir como texto o
  número, `codigo_producto` int|str → str, campo ausente → `None` + `log.debug` una vez por clave,
  **nunca** romper. `seleccion` ausente → `seleccionado=None` (no `False`).
- Ganador: `seleccionado=True` si `seleccion.proveedor_seleccionado` lo dice; si el campo no existe en la
  respuesta real (sonda), el ganador queda **sin inferir** en la base y se resuelve en el análisis (oc-da
  por `CodigoOC`/proveedor). No inventar reglas de inferencia en el cliente.
- Fixtures de test: **sintéticas**, construidas desde la guía (sin RUT ni datos reales). Casos: detalle
  completo; sin `proveedores_cotizando`; `[]`; `productos_cotizados` sin `precio_unitario`; `seleccion`
  ausente; `codigo_producto` int; montos como string con coma.

### 1.5 Esquema (`tables.py` + migración, solo `app/models`)
- `compras_agiles`: columnas nuevas nullable `unidad_compra`, `direccion_entrega`, `plazo_entrega_dias`,
  `tipo_presupuesto`, `fecha_cierre_segundo_llamado`, `motivo_desierta`, `motivo_cancelacion`,
  `cotizaciones_obtenidas_en` (DateTime: última vez que se guardaron cotizaciones).
- `ca_cotizaciones`: `id` PK, `ca_codigo` FK→`compras_agiles` ON DELETE CASCADE, `llamado` (int|null),
  `rut_proveedor`, `razon_social`, `es_emt`, `codigo_empresa`, `codigo_sucursal`, `estado_glosa`,
  `activo`, `seleccionado` (bool **nullable**), `valor_neto`, `total_impuesto`, `monto_despacho`,
  `monto_total`, `fecha_creacion`, `fecha_vigencia`, `descripcion` (Text, recortada a 2.000).
  `UNIQUE (ca_codigo, llamado, rut_proveedor)`; índice por `rut_proveedor`.
- `ca_cotizacion_productos`: `id` PK, `cotizacion_id` FK ON DELETE CASCADE, `codigo_producto`,
  `nombre_producto`, `descripcion` (recortada a 1.000), `cantidad`, `precio_unitario`, `monto_total_producto`.
- Escritura **idempotente**: por CA, en una transacción, borrar y reinsertar sus cotizaciones del llamado
  recibido. Re-ejecutar no duplica.
- **Sin `raw_json`** para estas CA (regla 11).
- Medir tamaño: test o script que estime bytes por CA con la fixture; anotar en el commit el estimado
  para ~1.300 CA/mes × 24 meses. Si supera 120 MB, guardar solo `productos_cotizados` de CA cuyo producto
  esté en los grupos del observatorio (`scripts/mercado/mercado_observatorio.py: GRUPOS`) y anotarlo.

### 1.6 Jobs nuevos (`orchestrator.py`, `__main__.py`, workflows)
Ambos usan `ticket="mercado"`, **advisory lock propio** (`mercado`, distinto del de la ingesta actual, para
no esperar a `ca`/`nocturno`) y escriben `job_runs`.

**`mercado-ca` (en vivo, cada 15 min)**
1. Listado `region ∈ mercado_regiones` con `cambio_desde` = cursor `sync_state` `mercado_ca` (ventana
   máx. 2 h, igual que `_sync_en_ventanas`), todos los estados. Upsert básico en `compras_agiles` (misma
   función que `ca`; no duplicar lógica).
2. Cola de detalle: CA de esas regiones en `cerrada|proveedor_seleccionado|desierta|cancelada` con
   `cotizaciones_obtenidas_en IS NULL` o `< fecha_ultimo_cambio`; además CA `publicada` cuyo
   `fecha_cierre` pasó (para fijar el primer llamado). Orden: `fecha_cierre` desc. Tope por corrida:
   `MERCADO_DETALLES_MAX` (default 120) y 10 min.
3. Guardar detalle + cotizaciones. Ante pausa/429/cuota: cortar guardando avance (regla 3).

**`mercado-backfill` (nocturno)**
- Corre **solo entre 22:00 y 07:00 de Chile**, validado en código con `ZoneInfo("America/Santiago")`
  (regla 5); fuera de ventana sale `omitido`.
- Meses objetivo (`MERCADO_BACKFILL_MESES`, default en este orden): `2026-03, 2026-04, 2026-06, 2026-07,
  2026-08` y luego `2024-10 … mes anterior al actual`, del más reciente al más antiguo.
- Por mes, ventanas de **un día** de `publicado_desde/hasta` con `ordenar_por=FechaPublicacion`,
  estados terminales, `tamano_pagina=50`; upsert básico y detalle de cada CA sin cotizaciones.
- Cursor por mes en `sync_state` `mercado_backfill:{AAAA-MM}` = último día completo + página; un mes
  terminado queda con cursor `completo`. Reanudable; re-ejecutar no repite detalles ya guardados.
- Para con `mercado_daily_budget` − 300 (margen para `mercado-ca` del día siguiente) o a las 06:45.
- Resumen al final: CA listadas, detalles guardados, cotizaciones, productos, meses completos.

**Workflows**: `mercado.yml` (`jobs: mercado-ca`, timeout 20 min, esperar lock 5 min) y
`mercado-nocturno.yml` (`jobs: mercado-backfill`, timeout 330 min). Solo `workflow_dispatch` (regla de
disparos: cron-job.org). Agregar `MP_TICKET_MERCADO` a los secretos que pasa `_job.yml`. Documentar en
`docs/operacion-disparos.md` los dos disparos nuevos (cada 15 min y 22:30 Chile) **sin crearlos**.

### 1.7 Retención (`retencion.py`)
`purgar_terminales` **no** borra CA de `mercado_regiones` (ni, por cascada, sus cotizaciones). Es una
excepción explícita a la regla 11 para el piloto; agregar la decisión a `docs/decisiones.md` con fecha y
el tamaño medido. Test: una CA terminal de región 16 de hace 200 días sobrevive; una de región 13 se purga.

### 1.8 Exportación para el observatorio (solo lectura)
`data/exportar_cot_api.py` (mismo estilo que `data/export_longevidad.py`: `SET TRANSACTION READ ONLY`,
nada se escribe): genera `data/mercado/cot_api_{AAAA-MM}.csv.gz` con las columnas del archivo `COT_`
que usa `mercado_observatorio.py` (`CodigoCotizacion`, `Region`, `RUTUnidaddeCompra`,
`NombreUnidaddeCompra`, `MontoTotalDisponble`, `CodigoProducto`, `NombreProductoGenerico`,
`ProductoCotizado`, `CantidadSolicitada`, `Estado`, `RazonSocialProveedor`, `RUTProveedor`, `Tamano`,
`ProveedorSeleccionado`, `MontoTotal`, `archivo`) **más** `PrecioUnitario` y `MontoTotalProducto` por
producto. En `mercado_observatorio.py`, `analizar_cot` lee también `cot_api_*` y, para un mismo mes,
**prefiere API sobre `COT_`**; agrega al resumen un análisis por producto (precio unitario ganador vs.
mediana de oferentes) para alimentos.

## 2. Tests (respx; nada de red real)
- Cliente: parseo de cada fixture de 1.4; `ticket="mercado"` manda el header `ticket` de la segunda key
  y nunca el de la primera.
- Cuota: dos tickets el mismo día llevan contadores separados; la migración deja las filas viejas en
  `principal`; el 429 de una key pausa solo esa key.
- `mercado-ca`: cursor avanza; cola de detalle respeta tope y orden; re-ejecutar no duplica cotizaciones.
- `mercado-backfill`: fuera de 22–07 Chile → `omitido` (reloj inyectado, incluido el cambio de horario);
  cursor por mes y reanudación tras un 429; mes `completo` no se vuelve a pedir; para por presupuesto.
- Retención 1.7. Máscara del logger 1.2.
- Antes de cerrar: `ruff check .`, `python -m mypy app`, `python -m pytest -rs` (0 fallos, 0 errores,
  0 saltados) contra Neon dev con `postgresql+psycopg://`.

## 3. Cierre
- Entrada en `app/changelog.py` ("Cotizaciones de Compra Ágil de Ñuble con precio por producto").
- `docs/00-estado.md`: F-merc-1 en "commit local sin push" y los pasos de Boris.
- Commit: `F-merc-1: segunda key con cotizaciones de Compra Ágil de Ñuble en vivo y hacia atrás`.
  `git add` solo de los archivos de la fase.

## 4. Pasos de Boris después del commit (para el paso a paso de la auditoría)
1. `python scripts/smoke_test.py --mercado` desde el PC con `MP_TICKET_MERCADO` → pegar salida.
2. Si la sonda confirma antigüedad y `productos_cotizados`: crear el secreto `MP_TICKET_MERCADO` en
   GitHub, aplicar la migración en Neon, push, y crear en cron-job.org los disparos de `mercado` (cada
   15 min) y `mercado-nocturno` (22:30 Chile).
3. Si la sonda dice que la API no entrega CA de marzo 2026: anotarlo [V], quitar esos meses del default y
   escribir a ChileCompra (datos abiertos) por los `COT_` incompletos.
4. Tras la primera noche: `python data/exportar_cot_api.py` y `python scripts/mercado/mercado_observatorio.py informe`.
