# Prompt F-ca-ventana: ingesta de Compra Ágil en ventanas acotadas

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Modelo: el Opus más reciente (`/model`). Requiere F-429-concurrencia desplegada
> (`b0ff832`): la sonda usa el enfriamiento tras un 504.
>
> **Tiene DOS PARADAS.** Claude Code prepara una sonda y se detiene; Boris la corre a mano;
> con ese resultado se implementa. Razón: la hipótesis de que el 504 lo causa el tamaño de la
> ventana es **[I]**, y el tamaño de ventana correcto sale de medirlo, no de suponerlo.
>
> **Evidencia [V] (Paso 0 de F-429-concurrencia, 22-sep-2026):**
> - El cursor de `sync_state` `compra_agil` está en `2026-09-20T21:05` (UTC naive), unas 47 h
>   de atraso.
> - La última corrida `ok` de `ca` fue el 21-sep 00:29 UTC. Todas las siguientes murieron con
>   `504 Endpoint request timed out` en la **página 1** del listado.
> - El cursor solo avanza si la corrida completa sale bien, así que cada intento pide una
>   ventana de cambios más larga. Si el tamaño es la causa, el problema se alimenta solo.
>
> **Contrato [V]** (`docs/09-compra-agil-500.md` §3, guía oficial v3.0): la ventana de cambios
> admite `cambio_desde`/`cambio_hasta`, o `ttl_cambio_ms`, no ambas. El cliente actual NO
> implementa `cambio_hasta`.
>
> **Bug heredado [V], se corrige aquí:** en `sync_incremental`, si `commit_con_retry` falla,
> la página se "descarta", el loop sigue y al final `exitoso = True` avanza el cursor por
> encima de esa página. Esa página se pierde para siempre.

---

```
Fase F-ca-ventana. Lee Claude.md antes de empezar: las reglas 3, 6, 9, 10, 12 y 13, y la
sección "Tests de red SIEMPRE mockeados", están en juego. Lee también
docs/09-compra-agil-500.md completo. Esta fase tiene DOS PARADAS: respétalas.

Contexto: la ingesta de Compra Ágil está detenida desde el 21-sep. El cursor tiene ~47 h de
atraso y cada `ca` muere con 504 en la página 1 del listado v2. Hipótesis [I]: la ventana
cambio_desde → ahora es demasiado grande para el backend de la API. Solución propuesta:
recorrer el atraso en ventanas acotadas con cambio_desde/cambio_hasta y avanzar el cursor
ventana a ventana.

PARTE 1 — SONDA (sin tocar la ingesta). Luego, PARADA 1.
1. En app/clients/mp_v2.py, agrega `cambio_hasta: datetime | None = None` a
   listar_compra_agil. Se serializa con _iso_para_la_api, igual que cambio_desde. Si viene
   junto con ttl_cambio_ms → ValueError, igual que cambio_desde. Tests con respx del
   parámetro y del round-trip de huso.
2. En scripts/smoke_test.py, agrega un modo `ventana-ca`, que se invoca con
   `python scripts/smoke_test.py ventana-ca`. Es el ÚNICO lugar con llamadas reales
   (Claude.md). Sin argumentos, el script hace lo mismo que hoy. El modo:
   - Lee el cursor actual de sync_state 'compra_agil' de la base apuntada por DATABASE_URL
     (Boris la apuntará a producción al correrlo). SOLO SELECT.
   - Corre estas variantes contra el listado, SOLO página 1, tamano_pagina=50, con los
     mismos estados que manda hoy sync_incremental:
       A) cambio_desde = cursor − 5 min, sin cambio_hasta   (lo que hace hoy la ingesta)
       B) cambio_desde = ahora − 2 h, sin cambio_hasta      (ventana chica, reciente)
       C) cambio_desde = cursor − 5 min, cambio_hasta = cursor + 6 h
       D) cambio_desde = cursor − 5 min, cambio_hasta = cursor + 2 h
       E) igual que C pero con cambio_hasta = cursor + 12 h
   - Por cada variante imprime: status u excepción, segundos que tardó, total_paginas,
     total de ítems si la API lo informa, y cuántos ítems vinieron en la página.
   - Espera 60 s fijos entre variantes, además del enfriamiento del cliente. Si una variante
     da 429/10500, espera 120 s y sigue. Si da cualquier otro 429, DETENTE: no sigas pidiendo
     ese día (regla 3).
   - Imprime al final cuántas requests contó el QuotaTracker durante la sonda.
   - Nunca imprimas el ticket ni la URL de la base.
3. ruff, mypy y pytest verdes. NO commitees todavía.
4. PARADA 1: dime el comando exacto para correr la sonda desde PowerShell, con la variable
   de producción, y espera a que te pegue la salida. No sigas a la Parte 2 sin ella.

PARTE 2 — IMPLEMENTACIÓN. Solo después de ver la salida de la sonda.
Con la salida, dime primero qué concluyes, marcando [V] e [I]:
- Si A da 504 y C o D dan 200 → la hipótesis se sostiene. Propón VENTANA_HORAS como la
  ventana más grande que respondió 200 con holgura (menos de ~15 s). Sigue.
- Si todas dan 504, o si A da 200 → la hipótesis cae. PARADA 2: no implementes; reporta y
  espera instrucciones.

5. SETTINGS: en app/core/settings.py, `ca_ventana_horas: int` (default el que salga de la
   sonda) y `ca_max_ventanas_por_corrida: int = 12`, ajustables por env. Agrega las dos a
   .env.example con un comentario.

6. sync_incremental EN VENTANAS (app/ingest/compra_agil.py):
   - Sin cursor (arranque en frío) → comportamiento de hoy, sin cambios.
   - Con cursor: desde = cursor − 5 min (el solapamiento de hoy). Por cada ventana:
       hasta = min(desde + ca_ventana_horas, ahora_utc())
     Pagina TODAS las páginas de esa ventana con cambio_desde=desde, cambio_hasta=hasta y los
     mismos estados de hoy. Mantén el commit por página con commit_con_retry y el filtro
     local de estado.
   - Ventana completa → avanza el cursor y commitea:
       cursor = max(cursor_actual, max fecha_ultimo_cambio visto en la ventana,
                    min(hasta, ahora_utc() − 10 min))
     El margen de 10 min evita saltarse cambios que la API todavía no indexa en la ventana
     abierta. Con esto, una ventana vacía también hace avanzar el cursor; hoy lo dejaría
     clavado.
   - La ventana siguiente parte en desde = cursor − 5 min. Se corta al alcanzar el presente o
     al llegar a ca_max_ventanas_por_corrida; lo que quede, lo recorre la corrida siguiente.
   - Error del canal (MPRateLimitError y subclases, QuotaExceededError, MPAuthError) o
     MPServerError en cualquier página → el cursor queda en la última ventana COMPLETA, se
     registra el intento en sync_state como hoy y se re-lanza. Lo de la ventana a medias ya
     quedó commiteado por página; al repetirse, el upsert es idempotente.
   - BUG HEREDADO: si commit_con_retry devuelve False en una página, la ventana NO está
     completa. No avances el cursor, loguea en ERROR y termina la corrida lanzando una
     excepción clara, para que job_runs la registre como error. Nunca saltar una página y
     seguir.
   - NO implementes reducción adaptativa de la ventana ante un 504. Se evaluó y se descartó:
     suma requests justo cuando la API está saturada. El tamaño lo fija la sonda.
   - Devuelve en el resultado, además de nuevas/actualizadas/descartadas: ventanas_completadas,
     cursor_final (ISO) y atraso_horas (ahora − cursor_final). Así job_runs muestra el avance.

7. TESTS (respx, ninguna llamada real, tiempo parchado donde haga falta):
   - Con 30 h de atraso y ventana de 6 h → 5 o 6 ventanas en orden, cada una con su
     cambio_desde/cambio_hasta correcto (5 min de solapamiento) y el cursor avanzando tras
     cada una.
   - Ventanas vacías → el cursor igual avanza.
   - ca_max_ventanas_por_corrida = 2 con 30 h de atraso → exactamente 2 ventanas y el cursor
     en el final de la segunda.
   - La última ventana no pasa de ahora_utc() − 10 min en el cursor, aunque hasta = ahora.
   - 504 en la página 2 de la ventana 3 → cursor en el final de la ventana 2, se re-lanza, y
     las páginas ya procesadas de la ventana 3 quedan en la base.
   - 429/10500 persistente → mismo comportamiento que el 504.
   - commit_con_retry False en una página → sin avance de cursor y excepción.
   - Arranque en frío (sin cursor) → requests idénticas a las de hoy (sin cambio_hasta).
   - Re-ejecutar después de un corte no duplica filas (idempotencia).
   - Los tests existentes de sync_incremental se ajustan solo donde el contrato cambió;
     explica cada ajuste en el commit.
   - Verifica que los tests de avance por ventana, de ventana vacía y del bug heredado FALLAN
     con el código anterior, y dilo en el commit.

8. VERIFICACIÓN Y LÍMITES
   - ruff check ., python -m mypy app, python -m pytest. Suite verde.
   - NO toques base.py, la política de reintentos, el advisory lock, los endpoints, .github/,
     render.yaml ni la lógica de licitaciones.
   - NO modifiques el cursor de producción a mano: el recorrido del atraso lo hace el código.
   - Un commit de código con prefijo `F-ca-ventana:`, que incluye el cambio del cliente y el
     modo de la sonda, con entrada en app/changelog.py y la salida resumida de la sonda (sin
     secretos) en el mensaje. Aparte, un commit `docs:` con este prompt si no está trackeado.
   - Agrega archivos por nombre. Nunca `git add -A`.
```

---

## Checklist de auditoría

**Comandos:** `ruff check .`, `python -m mypy app`, `python -m pytest`.

**A mano en el diff:**
1. La conclusión de la sonda está escrita con [V]/[I], y `ca_ventana_horas` sale de ella.
2. `cambio_hasta` pasa por `_iso_para_la_api`, con el mismo huso que `cambio_desde`.
3. El cursor se commitea al cerrar CADA ventana, no solo al final.
4. Una ventana vacía avanza el cursor, pero nunca más allá de `ahora − 10 min`.
5. Una página descartada corta la corrida sin avanzar el cursor: el bug heredado murió.
6. El arranque en frío quedó igual.
7. No hay reducción adaptativa de la ventana.
8. La sonda vive en `scripts/smoke_test.py`, espera 60 s entre variantes, se detiene ante un
   429 que no sea 10500 y no imprime secretos.
9. El resultado del job trae `ventanas_completadas`, `cursor_final` y `atraso_horas`.

## Paso operativo (Boris)

- **La sonda:** córrela cuando NO haya ningún job corriendo (ni el script manual ni un
  disparo). Toma unos 5 minutos por las esperas y gasta alrededor de 5 a 10 requests.
- **Después del deploy:** dispara `ciclo-ca` a mano y mira en `/api/salud/jobs` o en el log que
  `ca` quede en `ok` con `atraso_horas` bajando. Con 12 ventanas por corrida, un atraso de
  ~50 h se recupera en una o dos corridas.

---

## Resultado de la sonda y diseño revisado (22-sep-2026)

La hipótesis del prompt ("la ventana es demasiado grande") **cayó**: A, la ventana de ~50 h,
respondió 200. La sonda siguió con dos preguntas más, y el diseño de los puntos 5–7 se
reemplazó por el de abajo.

### Hechos

- **[V] La v2 da TODAS sus fechas en hora de Chile; la `Z` es falsa.** Tres evidencias:
  - En producción, el piso de `actualizado_en − fecha_ultimo_cambio` (el primero lo pone nuestro
    reloj, en UTC real) es 4,02–4,03 h en todas las semanas hasta el 31-ago y 3,09 h desde el
    14-sep: sigue el cambio de horario de Chile del 6-sep (UTC−4 → UTC−3). Ninguna de las
    65 515 filas con fecha baja de ese piso. Un retraso de indexación no cambiaría con el horario.
  - B2, enviada "en UTC" (21:31 → 23:21), volvió vacía: leída como hora de Chile era el futuro.
  - `fecha_cierre` `'2026-09-23 13:00'` = `fecha_cierre_primer_llamado` `'2026-09-23T13:00:00Z'`.

  El ENVÍO de `cambio_desde`/`cambio_hasta` (UTC → Chile, `_iso_para_la_api`) siempre estuvo bien.
  Lo que estaba mal era LEER la `Z` como UTC: `fecha_ultimo_cambio` y el cursor quedaban 3 h atrás
  (4 h en invierno). Efecto: cada corrida releía 3 h de más; no se perdían datos.
- **[V] El 504 depende de los ítems por página, no del ancho de la ventana.** La latencia es casi
  lineal en ítems: 0 → 2,8 s, 11 → 11,2 s, 16 → 12,4 s, 32 → 23,1 s, 50 → 27–31 s. El gateway corta
  cerca de los 29–30 s, así que una página de 50 vive en el límite.
- **[V] La API no pasa de 10 000 resultados** por consulta (`total_resultados=10000`, 200 × 50).
- **[V] `compras_agiles.fecha_publicacion` está NULL en las 65 696 CA de producción**, así que la
  verificación de la `Z` que comparaba `fecha_ultimo_cambio − fecha_publicacion` no se pudo hacer.
  Pendiente aparte (ver `docs/00-estado-actual.md`).
- **[I] La API ordena de más nuevo a más viejo.**
- **[I]** `"Endpoint request timed out"` tiene la forma del timeout de un API Gateway de AWS.

### Tiempos (página 1)

| Variante | Ventana | `tamano_pagina` | Resultado | Tiempo | Ítems | Total |
|---|---|---|---|---|---|---|
| B | ahora − 2 h, sin hasta | 50 | 504 ×2 | 120 s | — | — |
| D | cursor → +2 h (dom. noche) | 50 | 200 | 11,2 s | 11 | 11 |
| C | cursor → +6 h (dom. noche) | 50 | 200 | 12,4 s | 16 | 16 |
| E | cursor → +12 h (dom. noche) | 50 | 200 | 23,1 s | 32 | 32 |
| A | cursor → ahora (~50 h) | 50 | 200 | 30,6 s | 50 | 10 000 |
| B2 | ahora − 2 h → ahora − 10 min, "UTC" | 50 | 200 | 2,8 s | 0 | 0 |
| B3 | ahora − 2 h, sin hasta | 50 | 200 | — | 50 | — |
| G2 | 21-sep 15–18 UTC (diurna, 3 h) | 50 | 504 | — | — | — |
| H10 | igual que G2 | 10 | 200 | 15,7 s | 10 | 625 |
| H20 | igual que G2 | 20 | 200 | 12,5 s | 20 | 625 |
| A10 | cursor → ahora | 10 | 200 | 9,2 s | 10 | 10 000 |

Fuente: Dirección ChileCompra. La primera corrida (B–A) la leyó el asistente de la salida cruda;
las filas de B2 en adelante son el resumen de Boris de la segunda y tercera corrida. Las páginas de 50 con 50 ítems
tardaron entre 27 y 31 s; "—" es un dato que no quedó en ese resumen.

### Diseño implementado

- `parse_fecha_v2` (en `app/clients/types.py`) descarta la `Z` y delega en "sin offset = Chile";
  lo usan todos los campos de fecha del cliente v2. `parse_fecha_iso` no se tocó.
- Settings: `ca_tamano_pagina=20`, `ca_ventana_horas=2`, `ca_max_requests_por_corrida=150`.
- Con cursor, `sync_incremental` recorre el atraso en ventanas:
  `desde = cursor − 5 min`, `hasta = min(desde + 2 h, ahora − 10 min)`, siempre con `cambio_hasta`.
  Al completar la ventana, `cursor = hasta` y se commitea. Sin cursor, arranque en frío como antes.
- Termina OK si la ventana no haría avanzar el cursor (`hasta <= cursor`). **Desvío del diseño
  pedido**, que decía `hasta <= desde`: por el solapamiento de 5 min esa condición nunca se cumple
  al llegar al presente y la corrida repetía la misma ventana hasta gastar el tope de requests.
- Si la página 1 informa ≥ 10 000 resultados, la ventana se parte a la mitad (mínimo 10 min; si
  aun así llega al tope, `CompraAgilIngestaError`).
- El tope de requests se revisa antes de ABRIR cada ventana; una ventana abierta se termina.
- Un commit de página fallido corta la corrida sin avanzar el cursor (antes se saltaba la página).
- Si al cerrar una ventana hay menos códigos únicos que el `total_resultados` de su página 1:
  WARNING con ambos números, sin arreglo automático.
- Resultado: `nuevas`, `actualizadas`, `descartadas`, `ventanas_completadas`, `requests_usadas`,
  `cursor_final`, `atraso_horas`, `ventanas_partidas`.
- `fecha_ultimo_cambio` solo alimentaba el cursor (grep): no hace falta migrar datos. El cursor
  viejo queda 3 h atrás en la práctica, y la primera ventana relee 3 h sin daño.
