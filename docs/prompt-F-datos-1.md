# Prompt de implementación — F-datos-1 (datos que se pierden en la ingesta) · 08-oct-2026

> Para Claude Code. **Modelo: Opus** (toca el manejo de 429 y la cuota). Una fase = un commit,
> **sin push**. Origen: `docs/14-auditoria-integral.md` §2 y §7-bis (Paso 0 en producción).
> Lee `CLAUDE.md` antes de empezar: las reglas 1–15 mandan sobre este prompt.

## 0. Antes de escribir código

### 0.a Lectura (sin cambios)
`CLAUDE.md`; `docs/14-auditoria-integral.md` §2 y §7-bis; `docs/01-analisis-api-mercado-publico.md`
§6 y §10. Código:
- `app/clients/mp_v1.py`: `_parse_licitacion_basica` y `_parse_licitacion_detalle`;
- `app/clients/mp_v2.py`: `_parse_ca_basica` y `_parse_ca_detalle`;
- `app/clients/base.py`: `QuotaTracker`, `_handle_response` y `_request`;
- `app/ingest/licitaciones.py`: `upsert_basica`, `upsert_detalle` y `fetch_detalles_pendientes`;
- `app/ingest/compra_agil.py`: `_ESTADOS_VALIDOS`, `upsert_ca_basica`, `_completar_desde_detalle` y `sync_incremental`;
- `app/ingest/orchestrator.py`: `_make_clients`, `_bajar_detalle` y `_ciclo_nocturno`;
- `app/models/tables.py` (`Licitacion`) y `app/models/seeds.py` (`REGIONES`);
- `scripts/smoke_test.py`, en especial `sonda_claves_ca`, que se usa como modelo.

### 0.b Sondas (las corre Boris, la regla 23 lo exige)
Hay nombres de campos que no están verificados y `raw_json` no los guarda, porque guarda el dataclass parseado.
1. Agregar a `scripts/smoke_test.py` dos sondas de solo lectura, con el mismo patrón que `sonda_claves_ca` (nunca imprimir el ticket):
   - **`claves-lic`**: pide `licitaciones.json?codigo=<código>` de **1** licitación vigente y lista las claves hoja de `Listado[0]` (recursivo). Imprime además los valores de `Comprador.*` y `Fechas.*`, más `Tipo` y `CodigoTipo`. Esos valores no son sensibles: son el organismo y las fechas. El código se toma de la base (licitación vigente más reciente) o de un argumento.
   - **`ca-sin-fechas`**: toma de la base 3 CA `publicada` con `fecha_publicacion IS NULL`, de las más recientes por `creado_en`. Para cada una pide el **detalle** v2 e imprime el bloque `fechas` completo (claves y valores). Después pide **1** página del listado con la misma ventana `cambio_desde`/`cambio_hasta` que la hora de `fecha_ultimo_cambio` de una de ellas, con `estado=publicada` y `tamano_pagina=20`, e imprime para cada ítem si trae `fechas` y con qué claves. Total: ≤ 5 requests.
2. **Detente** y pídele a Boris que corra las sondas contra **producción**. Las sondas se eligen por argumento posicional, como `claves-ca`. Hay que exportar la URL de producción porque `ca-sin-fechas` lee la base y el `.env` apunta a dev; la variable de entorno tiene prioridad:
   ```powershell
   $env:DATABASE_URL = ((Get-Content .env | Where-Object { $_ -match '^DATABASE_URL_PROD=' }) -replace '^DATABASE_URL_PROD=','' -replace '^["'']|["'']$','' -replace '^postgres(ql)?://','postgresql+psycopg://')
   $env:PYTHONIOENCODING = "utf-8"
   python scripts\smoke_test.py claves-lic 2>&1 | Tee-Object data\logs\sonda_claves_lic.txt
   python scripts\smoke_test.py ca-sin-fechas 2>&1 | Tee-Object data\logs\sonda_ca_sin_fechas.txt
   Remove-Item Env:DATABASE_URL
   ```
   Sigue cuando Boris te pegue el resultado o te diga que los logs están en `data\logs\`.
   - Si los campos no son los esperados en §1, **adapta** el parseo a lo observado y anótalo en el reporte como "Desvío del prompt".
   - Si `fechas` de verdad no viene en el listado ni en el detalle, deja la regla de "no pisar" y repórtalo sin inventar.

## 1. Cambios

### D1 · Organismo y región de las licitaciones
- **Organismo.** `_parse_licitacion_basica` hoy lee `CodigoOrganismo` en el primer nivel; en v1 viene bajo `Comprador` (doc 10 §2.a, [V]). El parseo debe:
  - leer `Comprador.CodigoOrganismo` y `Comprador.NombreOrganismo`, conservando el primer nivel como respaldo;
  - leer la región de la unidad compradora. El nombre esperado es `Comprador.RegionUnidad` [I], que se confirma con la sonda.
- **Migración (Alembic, única de la fase):** agregar a `licitaciones` las columnas `organismo_nombre` (String(500), nullable) y `region` (Integer, nullable), con un índice en `region`. Esto replica las columnas de `compras_agiles`.
- **Región como número.** La API da el nombre de la región. Mapearlo al código de `app.models.seeds.REGIONES` normalizando el texto: minúsculas, sin tildes, sin "región", "de", "del", "y".
  - Lo que no calce queda en `None` y genera un log, una vez por valor distinto (parseo defensivo, regla 6).
  - Tests con los 16 nombres oficiales y variantes ("Región Metropolitana de Santiago", "Región del Libertador General Bernardo O´Higgins", "Región de Ñuble").
- **No pisar con None** (`upsert_basica`): `codigo_organismo`, `organismo_nombre`, `region` y **`tipo`** solo se escriben si el ítem trae valor o si la licitación es nueva. Mismo criterio que ya se usa con las fechas.
  - Motivo: el listado de activas no trae `Tipo` ni organismo, y hoy los pisa. El Paso 0 midió 18.120 de 34.442 licitaciones con `tipo` NULL y 0 con organismo.
- **Relleno de las vigentes sin organismo.** Las licitaciones existentes ya tienen detalle, pero se bajó con el parser viejo.
  - Agregar el paso `rellenar-organismo` **dentro de `nocturno`** (respeta la ventana 22:00–07:00, regla 5): vuelve a pedir el detalle de licitaciones **vigentes** (`condicion_lic_vigente`) con `codigo_organismo IS NULL`, ordenadas por cierre ascendente, con un tope de `RELLENO_ORGANISMO_MAX` requests por noche (setting, default 600).
  - Reutiliza `upsert_detalle`. Hoy hay ~4.400 vigentes, así que el relleno se completa en ~1 semana.
  - Sin reintento de 5xx (`reintentar_transitorios=False`), igual que `detalles-match`.
  - Las licitaciones nuevas lo traen solas, por el job `detalles`.

### D11 · Fecha de publicación de las licitaciones
Hoy es NULL en todas las de 2026 porque el parser lee `FechaPublicacion` en el primer nivel.
- Leer `Fechas.FechaPublicacion` (nombre a confirmar con la sonda), con el primer nivel como respaldo.
- Revisar con la sonda si `FechaCierre` del detalle también vive en `Fechas`. Si es así, usar `Fechas.FechaCierre` como respaldo, sin cambiar el comportamiento cuando el primer nivel existe.
- Las fechas v1 son hora de Chile: usar el mismo `parse_fecha_v1_dt` de siempre.

### D4 · Fechas de Compra Ágil pisadas con None
`upsert_ca_basica` asigna `fecha_publicacion`, `fecha_cierre` y `fecha_ultimo_cambio` sin condición.
- Aplicar el mismo criterio que `_completar_desde_detalle`: un None no pisa un valor existente.
- Hacer lo mismo con `region`, `organismo_rut`, `organismo_nombre` y `monto_disponible_clp`.
- `total_ofertas` se queda con el mayor.
- Si la sonda muestra que el listado trae las fechas bajo **otra clave** cuando falta `fechas`, leer esa clave como respaldo.
- Contexto: el Paso 0 midió 8.295 CA `publicada` sin ninguna de las dos fechas. Con eso **nunca son vigentes** y no aparecen en el feed.

### D3 · CA desiertas y canceladas
`_ESTADOS_VALIDOS` en `compra_agil.py` solo pide `publicada`, `cerrada` y `proveedor_seleccionado`, así que el cambio a desierta o cancelada nunca llega por el listado.
- Agregar los slugs reales de **desierta** y **cancelada** que acepte el parámetro `estado` de v2. Tomarlos de `_MAP_CA` en `app/models/enums.py`.
- Antes de cambiarlo, verificar en `docs/09-compra-agil-500.md` y en `sonda_claves_ca` que la API los acepta. Si no hay evidencia, agregar a la sonda `ca-sin-fechas` **1** request de listado con `estado=desierta` y comprobar que responde 200. Esa request cuenta dentro del tope de ≤ 5.
- Mantener el filtro local como defensa.

### R1 · 429 diario persistido (regla 3)
Hoy, tras un 429 que no es 10500, la corrida siguiente vuelve a intentar.
- **Diseño sugerido, sin migración:** al recibir un `MPRateLimitError` que no sea concurrencia, `QuotaTracker` marca el día **de Chile** como agotado, con `requests_usadas = GREATEST(requests_usadas, budget)` en `quota_log`.
  - Así `check_budget` bloquea a todos los procesos y jobs hasta el cambio de día calendario en `America/Santiago`, que es exactamente la regla 3.
  - Que quede registrado en el log con nivel WARNING.
- `/salud` ya muestra la cuota, así que no hay que tocar la UI.
- Si encuentras un diseño mejor, explícalo en el reporte.

### R2 · Reserva de cuota para `ca`
`QuotaTracker` es un solo contador. Si de noche se gasta la cuota, `ca` queda sin requests hasta el día siguiente.
- Agregar el setting `CUOTA_RESERVA_CA` (default 2500).
- Los clientes que **no** son de `ca` usan un presupuesto efectivo de `api_daily_budget − reserva`. Por ejemplo, `_make_clients` puede recibir `reserva_ca: bool`, y `run_sync_ca` lo crea sin reserva.
- Contexto: el máximo medido es ~3.600 requests al día, así que hoy la reserva no frena nada. Solo protege el peor caso.

### R3 · Errores de transporte
`httpx.ConnectError`, `httpx.RemoteProtocolError` y `httpx.ReadError` no se atrapan en `_request`.
- Tratarlos como el timeout: enfriar 60 s y aplicar los mismos reintentos y la misma bandera `reintentar_transitorios`.
- Al agotarse los reintentos, lanzarlos como `MPServerError(status_code=0)`.
- En `detalles-match`, un error de **canal** (`status_code == 0` por transporte) **no** suma `detalle_fallos` a la oportunidad.
- En `ca`, un error de transporte cierra la corrida guardando el avance de las ventanas completadas, igual que hoy con 5xx. Revisar cómo lo maneja `sync_incremental`.

## 2. Fuera de alcance
- Score, feed, digest y organismos seguidos: eso es **F-match-1**, que depende de que esta fase esté en producción.
- Retención de filas (F-retencion-filas), índices (F-indices) y fuentes nuevas (OCDS, `COT_`).
- **D12, el Plan Anual casi vacío:** Boris lo diagnostica aparte con el log de Actions. Si el arreglo resulta ser de código, va en una fase chica separada.
- R5 y R6 (competencia O(n·m), descarga fallida de ítems): backlog.

## 3. Tests
Todos con red mockeada (respx). Postgres de dev con `postgresql+psycopg://`.
- **Parseo v1** con fixtures que reflejen lo observado en la sonda:
  - organismo y región bajo `Comprador`;
  - `Fechas.FechaPublicacion`;
  - un nombre de región desconocido da `None` sin romper.
- **`upsert_basica`:** un ítem de listado sin tipo ni organismo no pisa los valores existentes; una licitación nueva sí los escribe.
- **`upsert_ca_basica`:** None no pisa fechas, región ni RUT; `total_ofertas` se queda con el mayor.
- **CA:** la request del listado incluye los estados nuevos.
- **429 no-10500:** después de recibirlo, `check_budget` lanza `QuotaExceededError` en otro `QuotaTracker` nuevo (simula otra corrida) el mismo día de Chile, y deja de lanzarlo al día siguiente (reloj inyectado).
- **Reserva:** un cliente con reserva se corta al llegar a `budget − reserva`; el de `ca`, no.
- **Transporte:** `ConnectError` → enfriamiento, reintento y, al agotarse, `MPServerError(0)`. En `detalles-match` no suma `detalle_fallos`.
- **`rellenar-organismo`:**
  - respeta el tope;
  - solo toma vigentes sin organismo;
  - solo corre dentro de la ventana nocturna (tests con reloj inyectado).

## 4. Cierre
- Correr `ruff check .`, `python -m mypy app` y `python -m pytest -rs` completo con `DATABASE_URL` de dev en `postgresql+psycopg://`. Debe dar 0 fallos, 0 errores y 0 saltados. Si tu proceso no hereda la variable, la suite la corre Boris.
- `alembic upgrade head` en dev antes de los tests.
- Entrada en `app/changelog.py`: "Las licitaciones muestran su organismo y su región, y aparecen Compras Ágiles que antes quedaban ocultas por no traer fecha".
- `git add` solo de los archivos tocados. Commit: `F-datos-1: organismo, región y fechas de licitaciones, fechas de CA, 429 diario persistido`. **Sin push.**
- **Reporte:**
  - hash del commit;
  - archivos tocados;
  - salida de las sondas, resumida;
  - resultado de la suite;
  - lista de "Desvío del prompt", si hubo.
- **Push (lo hace Boris tras la auditoría):** tiene migración, así que va justo después de un `ca` de los :05. Confirmar en Render `Running upgrade c9e4b2f7a1d8 -> <nueva>`.

*Fuente de los datos de dominio: Dirección ChileCompra.*
