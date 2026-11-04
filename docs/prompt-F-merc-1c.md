# Prompt — F-merc-1c: el backfill nocturno espera la pausa del 429 y sigue · 4-nov-2026

> Para Claude Code. **Modelo: Opus** (toca 429, pausa y lock; `decisiones.md`: Opus para cuota/429/lock).
> Una fase, un commit: `F-merc-1c: el backfill nocturno sigue tras la pausa del 429`. Sin push hasta auditar.
> **Con migración** (§3.1: UNIQUE de `ca_cotizaciones`). Leer antes: `CLAUDE.md` (reglas 3, 5, 10, 13), `docs/00-estado.md` (F-merc-1),
> `docs/15-diagnostico-ca-429.md`, `app/ingest/mercado.py` (`Backfill`), `scripts/backfill_cot_nuble.py`
> (`_una_corrida` / `_esperar_pausa` / `correr`: el patrón que ya funciona) y `app/ingest/__main__.py` (`_esperar_pausa`).

## 0. Evidencia (log de Actions del 4-nov, `mercado-nocturno`, disparo manual 00:45 Chile) [V]
- 137 requests en 16 min (~1 cada 7 s), todas 200: 2 listados de marzo-2026 (días 2 y 3), 131 detalles, 1.045
  cotizaciones, 2.690 productos.
- 01:01 Chile: `429 codigo=429` ("límite de tasa"). El cliente persistió la pausa hasta 01:21 (bien, regla 3) y
  `Backfill.correr()` terminó (`cortado_por=MPTasaError`). **Quedaban ~5 h de ventana sin usar.**
- 137 está dentro del rango 110–190 del doc 15: el límite cuenta requests en una ventana de tiempo, no la velocidad
  (llegó a 0,14 req/s) [I fuerte]. A una corrida de ~137 requests por noche, los ~24 meses no se terminan.
- Mismo log: 45 avisos `oferta repetida del mismo RUT` en 131 detalles (§3). Log de `mercado-oc` del 4-nov: OK; el
  único aviso es `columnas de ítem ['tieneItems']`, un falso positivo (§4).

## 1. Cambio principal: esperar la pausa dentro de la corrida (`app/ingest/mercado.py`)

### 1.1 Comportamiento
`Backfill.correr()` pasa a ser un bucle de **tramos**. Un tramo es lo que hoy es la corrida entera (recorrer
`meses_backfill`, cursor por mes). Si el tramo termina por `MPTasaError` (el cliente ya persistió la pausa) o por
`APIPausadaError` (pausa vigente puesta por otro proceso):
1. Leer el fin de la pausa **de Postgres** con `leer_pausa(engine)` (no del objeto del error: otro proceso pudo
   alargarla; regla 10).
2. `despertar = fin_pausa + jitter` (30–90 s, `random.uniform`, inyectable como en el puente).
3. Seguir **solo si** se cumplen todas:
   - `pausas < settings.mercado_backfill_max_pausas`;
   - `despertar` cae antes de las 06:45 Chile (`_FIN_NOCHE`) y dentro de la ventana 22–07 (`en_ventana_nocturna`
     con `ZoneInfo("America/Santiago")`, regla 5);
   - `despertar` cae antes de `inicio + mercado_backfill_minutos − 5 min` (el reloj **cuenta el sueño**: el tope de
     300 min sigue protegiendo el timeout de 330 del workflow).
   Si no, termina como hoy (avance guardado, `cortado_por`, `motivo_fin` con la razón: "max pausas", "la pausa
   termina después de las 06:45" o "la pausa no cabe en el tope de N min").
4. Dormir con `sleep_fn` (inyectable; default `time.sleep`). **Sin conexiones abiertas mientras duerme**: hoy cada
   paso abre y cierra su `Session`; verificarlo y no dejar ninguna viva antes del `sleep_fn`.
5. Tras despertar, volver a leer la pausa: si sigue vigente (otro proceso la alargó), repetir desde 1 (cuenta como
   otra pausa). Si no, empezar otro tramo: los cursores por mes ya dejan todo en su lugar; la página cortada se
   vuelve a listar (1 request) y sus detalles ya guardados no se piden (`_sin_cotizaciones`).

**No cambia:** `MPConcurrencyError` (10500 con reintentos agotados), `QuotaExceededError` que no sea pausa, `_Corte`
(hora, minutos, cuota, tamaño de base, 3 fallos seguidos, "revisar filtros") y `MPAuthError` siguen terminando el job
como hoy. Ningún 429 se reintenta: el request que recibió el 429 no se repite; el tramo siguiente empieza **después**
de la pausa (regla 3: "sin reintento, pausa persistida, cortar guardando el avance" se cumple por tramo).

### 1.2 Lock (regla 13)
El job mantiene el advisory lock mientras duerme (no soltar y retomar: abriría la puerta a dos backfills). Efecto
conocido y aceptado: un `mercado-ca` disparado durante la noche espera el lock 10 min y sale **omitido** (código 0),
igual que hoy mientras corre el backfill. Durante la pausa tampoco podría llamar a la API. Dejarlo escrito en el
docstring de `run_backfill` y en `decisiones.md`.

### 1.3 Resumen y logs
`ResumenBackfill` suma: `pausas` (int), `minutos_esperados` (float, 1 decimal) y `requests_por_tramo` (lista de
int: requests hechas en cada tramo antes de su 429). **Esta lista es dato para la sonda H1**: cuántas requests
caben entre pausas en una misma IP y ticket. Log INFO al dormir:
`mercado-backfill: 429 tras N requests del tramo; espero X min, hasta HH:MM Chile (pausa k/max)`. Nada de URL,
headers ni ticket (regla 1; el log ya pasa por `_SecretFilter`, no agregar nada que lo esquive).
`cortado_por` queda `None` si el job terminó por hora/minutos tras haber esperado pausas; queda con el tipo del
error solo si el último tramo terminó en 429 y no se pudo esperar.

### 1.4 Settings y workflow
- `app/core/settings.py`: `mercado_backfill_max_pausas: int = Field(default=12, ge=0, ...)`. Con 0 el
  comportamiento es el de hoy (cortar al primer 429): sirve de interruptor.
- `.github/workflows/_job.yml`: pasar `MERCADO_BACKFILL_MAX_PAUSAS: ${{ vars.MERCADO_BACKFILL_MAX_PAUSAS }}` y
  agregarla a la lista del `for v in ...` que hace `unset` de las opcionales vacías.
- `mercado-nocturno.yml`: solo actualizar el comentario de cabecera (ya no "un 429 corta"). `timeout_min: 330`
  no cambia.

### 1.5 Cobertura de la noche (paso de Boris, no código)
Con un disparo a las 22:30 y el tope de 300 min, el job para a las ~03:30: quedan 3 h sin usar hasta las 06:45. No
subir `MERCADO_BACKFILL_MINUTOS` (Actions corta a los 360). En su lugar, **segundo disparo de `mercado-nocturno` en
cron-job.org a las 03:40 Chile**: el `concurrency` del workflow lo encola si el primero sigue vivo, y el cursor por mes
retoma donde quedó. Si arranca en pausa, `_esperar_pausa` del CLI espera hasta 15 min (F-429-pausa-ca). Estimado con
lo de anoche: ~137 requests cada ~37 min → **~1.700–1.900 requests por noche** entre los dos disparos [I, verificar
con `requests_por_tramo` de la primera noche].

## 2. Tests (`tests/test_mercado.py`, respx; reloj, `now_fn`, `sleep_fn` y jitter falsos; sin dormir de verdad)
1. 429 `codigo=429` en el request k → duerme hasta fin de pausa + jitter → sigue y completa el mes; `pausas == 1`,
   `requests_por_tramo == [k]`, el request del 429 **no** se repite tal cual (el tramo 2 relista la página y no pide
   los detalles ya guardados).
2. Pausa que termina después de las 06:45 → no duerme, termina con avance guardado y `motivo_fin` con la razón.
3. Pausa que no cabe en `mercado_backfill_minutos − 5` → no duerme.
4. `mercado_backfill_max_pausas` alcanzado → no duerme. Con `max_pausas=0` el comportamiento es el de hoy: el test
   existente `test_backfill_429_guarda_avance_y_reanuda` debe pasar **sin cambios** con `max_pausas=0` y adaptarse
   (o duplicarse) para el default.
5. Al despertar la pausa sigue vigente (otro proceso la alargó en `sync_state`) → vuelve a esperar, cuenta 2 pausas.
6. `MPConcurrencyError` y `_Corte` siguen terminando sin dormir.
7. Ninguna `Session`/conexión abierta al momento de `sleep_fn` (por ejemplo, `engine.pool.checkedout() == 0` dentro
   del `sleep_fn` falso).
8. Ningún log contiene el ticket (patrón de los tests de regla 1 que ya existen).

## 3. Ofertas repetidas del mismo RUT: son ofertas distintas y hay que guardarlas todas
**Sonda del 4-nov** (`smoke_test.py --detalle-ca`, 3 CA, log `data/logs/sonda_ofertas_2026-11-04.txt`) [V]:
- Un mismo RUT manda **varias ofertas válidas y distintas** a la misma CA: 44 ofertas de 38 RUT en `2728-9-COT26`,
  23 de 18 en `3881-33`, 16 de 13 en `4158-26`. Cada una tiene su `id_cotizacion`, su producto y su precio (p. ej.
  $419.800 y $719.800 netos del mismo RUT), y se crearon con minutos de diferencia. No son una anulada y su reemplazo:
  el estado es el mismo (2 o 3) y la primera no siempre es la más barata. Todas en la misma sucursal.
- Hay un caso de duplicado exacto (mismo monto y producto, `4158-26`, RUT 76.308.099-4, 8 min de diferencia).
- `monto_total` = `valor_neto` × 1,19 (**con IVA** [V]: 499.562 / 419.800 = 1,19) y `valor_neto` incluye
  `monto_despacho` (480.000 = 440.000 + 40.000). Cierra el [I] de `00-estado.md` ("monto_total con o sin IVA").
- `estado` llega como entero (2 y 3 en la misma CA); su significado sigue [I] (¿3 = inadmisible? hay una clave
  `justificacion_inadmisibilidad` que la sonda no imprimió).

**Consecuencia:** el UNIQUE `uq_ca_cotizacion (ca_codigo, llamado, rut_proveedor)` y `_sin_repetir` **descartan
precios reales de la competencia** (~45 de ~1.090 ofertas anoche, 4 %), justo el dato de la mesa de precios y de
P(ganar). Hay que corregirlo **antes** de que el backfill vaya 14 veces más rápido.

**3.1 Migración (Alembic, `app/models/tables.py`):** reemplazar `uq_ca_cotizacion` por un UNIQUE sobre
`(ca_codigo, llamado, id_cotizacion)`. `id_cotizacion` es nullable: si llega nulo, el parser usa una clave
sustituta estable (`"rut:<rut>"`) para que el UNIQUE siga funcionando y el comportamiento sea el de hoy en ese caso.
Ojo con `llamado` nullable en el UNIQUE (NULL no choca en Postgres): mantener la semántica actual (hoy ya es así;
no empeorarla). `downgrade` restaura el UNIQUE anterior y **falla con mensaje claro** si hay filas que lo violarían
(no borrar datos en un downgrade). Sin columnas nuevas: `id_cotizacion`, `valor_neto`, `monto_despacho`,
`fecha_creacion`, `fecha_vigencia` y `activo` ya existen.

**3.2 Escritura (`app/ingest/mercado.py`):** `_sin_repetir` deduplica por `id_cotizacion` (o la clave sustituta),
no por RUT. Un `id_cotizacion` repetido dentro de la misma respuesta → se guarda una y un WARNING por CA. Un
duplicado exacto con **distinto** `id_cotizacion` se guarda (son dos ofertas para la API) y se cuenta en
`ofertas_identicas` del resumen, sin WARNING. Contadores nuevos en `_Conteo`: `ofertas_mismo_rut` (ofertas de un RUT
que ya ofertó en esa CA) y `ofertas_identicas`. El delete + insert por CA y llamado se mantiene.

**3.3 Lo ya guardado con ofertas de menos:** las CA cuyo número de filas en `ca_cotizaciones` es menor que
`compras_agiles.total_ofertas` (si `total_ofertas` cuenta ofertas y no RUT: **verificarlo** con los tres códigos de
la sonda, 44 / 23 / 16, mirando lo que quedó en la base) se vuelven a pedir: poner `cotizaciones_obtenidas_en = NULL`
y el backfill / `mercado-ca` las recoge solos (`_pendientes_del_mes`). Hacerlo con un script de `scripts/mercado/`
con `--dry-run` por defecto que imprime cuántas CA y cuántas requests costaría; **lo ejecuta Boris**, no la migración.

**3.4 Consumidores:** revisar quién supone una oferta por RUT y ajustarlo **sin cambiar lo que calcula para el caso
de una oferta por RUT**: `scripts/mercado/exportar_cot_api.py`, `scripts/mercado/mercado_observatorio.py` (ganador /
perdedores por producto) y cualquier consulta sobre `ca_cotizaciones` en `app/`. Regla para "precio del competidor":
la oferta de menor `valor_neto` de cada RUT, y "n.º de oferentes" = RUT distintos (no filas). Anotar en
`decisiones.md`. Si alguno de los dos scripts no deduplicaba (el puente escribía CSV), anotarlo [V].

**3.5 Tests:** dos ofertas del mismo RUT con distinto `id_cotizacion` → dos filas; mismo `id_cotizacion` repetido →
una fila y un WARNING por CA; `id_cotizacion` nulo → clave sustituta (comportamiento de hoy); re-ingestar la misma CA
no duplica (idempotencia); la migración sube y baja en SQLite y Postgres; el script de 3.3 en `--dry-run` no escribe.

**3.6 Docs:** `docs/01-analisis-api-mercado-publico.md` §10: "un RUT puede enviar varias ofertas a una CA", IVA de
`monto_total` y composición de `valor_neto`, con [V] y la fecha; significado de `estado` 2/3 como [I] pendiente.

## 4. Falso positivo de `mercado-oc` (`app/ingest/mercado_oc.py`, ~l. 326)
`item_cols = [c for c in extra if "item" in c.lower()]` marca `tieneItems` (una marca sí/no, no una clave). Agregar
`_COLUMNAS_ITEM_CONOCIDAS = frozenset({"tieneitems"})` y excluirlas de la heurística (comparar en minúsculas). Una
columna nueva con "item" en el nombre sigue avisando. Test: un CSV con `tieneItems` no avisa; uno con `IDItemNuevo` sí.

## 5. Docs
- `docs/decisiones.md`: "Backfill nocturno espera la pausa del 429 (4-nov-2026, F-merc-1c)": evidencia del §0,
  interpretación de la regla 3 por tramo, lock retenido mientras duerme, `MERCADO_BACKFILL_MAX_PAUSAS` como
  interruptor, segundo disparo de las 03:40, y la regla de elección de ofertas repetidas.
- `docs/00-estado.md`: sección F-merc-1c con los pasos de Boris (§6); en F-merc-1 tachar "Un 429 corta guardando el
  avance".
- **Propuesta (no aplicar sin el OK de Boris):** precisar la regla 3 en `CLAUDE.md`: "… cortar la corrida guardando
  el avance. Un job largo (backfill) puede esperar el fin de la pausa leída de Postgres y empezar otro tramo, sin
  repetir el request del 429". Dejar el texto en el resumen final de la fase para que Boris decida.
- `app/changelog.py`: sin entrada (no hay cambio visible en la app).

## 6. Pasos de Boris (después del commit auditado)
1. Push. Variable de Actions `MERCADO_BACKFILL_MAX_PAUSAS` vacía (usa 12) o `0` para volver al comportamiento de hoy.
2. `alembic upgrade head` en Neon prod (Alembic no lee `.env`: exportar la URL) y, después, el script del §3.3 en
   `--dry-run`; si el costo en requests es razonable, correrlo sin `--dry-run`.
3. cron-job.org: confirmar el disparo de las 22:30 y crear el segundo a las 03:40 Chile (`mercado-nocturno`).
4. A la mañana siguiente: pegar el resumen de las dos corridas (`pausas`, `requests_por_tramo`, `meses_completos`) y
   `/salud` (tamaño de la base). Con `requests_por_tramo` en la mano, la sonda H1 (`prompt-sonda-H1-429.md`) tiene
   su N_A sin gastar requests extra.

## 7. Cierre
`ruff check .`, `python -m mypy app`, `python -m pytest -rs` (0 fallos, 0 errores, 0 saltados, contra Neon dev con
`postgresql+psycopg://`). `git add` solo de: `app/ingest/mercado.py`, `app/ingest/mercado_oc.py`,
`app/core/settings.py`, `.github/workflows/_job.yml`, `.github/workflows/mercado-nocturno.yml`,
`app/models/tables.py`, `alembic/versions/<nueva>.py`,
`scripts/mercado/` (script 3.3 y consumidores 3.4), `docs/01-analisis-api-mercado-publico.md`, `tests/test_mercado.py`, `tests/test_mercado_oc.py`, `docs/decisiones.md`,
`docs/00-estado.md`, `docs/prompt-F-merc-1c.md`. Resumen final: qué se cambió, desvíos del prompt con su porqué,
y el texto propuesto para la regla 3.
