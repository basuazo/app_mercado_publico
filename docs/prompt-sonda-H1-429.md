# Prompt — Sonda H1: ¿el 429 "límite de tasa" es por ticket o por IP? · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet** (script chico sin tocar la app; Haiku sirve si Boris revisa el diff antes de
> correrlo: lo único delicado es la regla 1). **Sin commit de fase**: es una sonda; se commitea solo el script y la
> anotación en docs. No toca la base, no toca `app/`. Origen: `docs/15-diagnostico-ca-429.md` (H1 abierta desde el
> 29-oct) y la decisión pendiente de usar una segunda key (`17-plan-pivote-plataforma-ca.md`).

## 0. Qué se quiere saber y por qué importa
Desde el 18-oct la API devuelve 429 `codigo: "429"` ("límite de tasa") tras ~110–190 requests seguidas [I, doc 15].
No sabemos si ese límite cuenta **por ticket** o **por IP**. Boris tiene ahora dos tickets (el suyo, `MP_TICKET`, y
el de un socio, `MP_TICKET_2`). Si el límite es por ticket, dos keys duplican el ritmo del backfill (24 meses en ~14
noches en vez de ~28); si es por IP, solo sirven desde máquinas distintas. La prueba cuesta < 600 requests.

Reglas duras que aplican (`CLAUDE.md`): **1** (ningún ticket en código, logs, salida ni archivos), **3** (ante 429
no se reintenta), **4** (1 req/s con jitter), **9** (solo API oficial), **20/23** (anotar [V]/[I]).

## 1. Script `scripts/sonda_h1_429.py` (lo ejecuta solo Boris)
- **No usa el cliente de la app** (`MercadoPublicoV2Client`): ese cliente lee y escribe la pausa compartida
  `sync_state.api_pausa` en Neon, y la prueba necesita justamente llamar con la key B **mientras la A está en
  pausa**. Usa `httpx` directo contra `https://api2.mercadopublico.cl/v2/compra-agil` (la misma URL base que
  `app/clients/mp_v2.py`; importar la constante si está expuesta, si no copiarla con un comentario). No importa
  nada de `app/core/db.py`; no abre conexión a Postgres.
- Argumentos: `--key MP_TICKET|MP_TICKET_2` (nombre de la variable de entorno; el script lee `.env` con
  `python-dotenv` si está en el stack, o `os.environ`), `--modo sonda|rafaga`, `--max-requests 300`
  (solo ráfaga), `--rps 1.0`, `--log data/logs/sonda_h1_<AAAA-MM-DD>.txt`.
- **`sonda`**: 1 request `GET /v2/compra-agil?region=16&publicado_desde=<ayer>&publicado_hasta=<hoy>&tamano_pagina=10`
  con header `ticket`. Imprime: hora Chile (`ZoneInfo("America/Santiago")`), status HTTP, y si es 429 el `codigo`
  y el mensaje del envelope `errors` (recortado a 120 chars). Nunca imprime la URL completa con el header ni el
  valor del ticket; `httpx` no lo mete en la URL, pero el script tampoco debe loguear `request.headers`.
- **`rafaga`**: requests de listado a `--rps` con jitter ±20 %, variando `publicado_desde/hasta` por día (ventanas
  de un día hacia atrás desde ayer) para no pegarle a caché, hasta recibir el primer 429 o llegar a
  `--max-requests`. Imprime cada 25 requests el conteo y, al 429, el **número de request en que llegó**, la hora y
  el `codigo`. Se detiene al primer 429 (regla 3: cero reintentos). Si llega un 10500 (concurrencia) lo imprime y
  se detiene también: significa que otro proceso estaba llamando a la vez (ver §2, paso 0).
- Al terminar escribe el mismo resumen al `--log` (hora, key usada **por nombre de variable**, modo, n requests,
  resultado). Test rápido del propio script: `grep -c` del valor del ticket en el log debe dar 0 (el script lo
  verifica antes de cerrar el archivo y aborta con error si lo encuentra).
- Sin reintentos, sin paralelismo, sin escribir en Neon. Unos 120 líneas.

## 2. Protocolo (Boris, desde su PC, de noche o de día con la ingesta de la app en pausa como está hoy)

Todo desde **la misma máquina e IP** (es la condición de la prueba). Antes de empezar: ninguna otra cosa llamando
a la API desde ese PC ni desde Actions (`mercado-ca` apagado, `backfill_cot_nuble.py` detenido).

| Paso | Qué correr | Qué anotar |
|---|---|---|
| 0 | `--key MP_TICKET_2 --modo sonda` | Debe dar **200**. 401 = key inválida (parar). 429 = el socio la tiene en pausa (esperar 20 min y repetir) |
| 0' | `--key MP_TICKET --modo sonda` | Debe dar 200. Si da 429, esperar a que termine la pausa (`api_pausa` en `/salud` o 20 min) |
| 1 | `--key MP_TICKET --modo rafaga` | Número de request del primer 429 (N_A) y hora (T_A) |
| 2 | **Inmediatamente** (< 2 min): `--key MP_TICKET_2 --modo sonda` | **Resultado R1**: 200 o 429 |
| 3 | Si R1 = 200: `--key MP_TICKET_2 --modo rafaga` | N_B, T_B. Si N_B es parecido a N_A (±30 %), el límite se cuenta por ticket y no se compartió nada con A |
| 4 | Inmediatamente: `--key MP_TICKET --modo sonda` | **R2**: debería seguir en 429 si la pausa de A es de ~20 min; si da 200 antes de 20 min, anotar cuánto duró la pausa real de A |
| 5 | A los 25 min de T_A: `--key MP_TICKET --modo sonda` y lo mismo con `MP_TICKET_2` | Control: ambas 200 → la pausa es transitoria también para B |

## 3. Cómo leer el resultado

| R1 (sonda B tras 429 de A) | Paso 3 | Conclusión | Decisión |
|---|---|---|---|
| **200** | N_B ≈ N_A | **Por ticket** [V] | Fase F-merc-1b: segunda key con cuota y pausa por ticket; key A = `mercado-ca`, key B = backfill; ritmo ×2 |
| **200** | N_B mucho menor que N_A (p. ej. < 50 %) | Mixto: hay un componente por IP o ventana global además del ticket [I] | Segunda key útil pero desde otra IP (PC vs. Actions); no correr las dos en el mismo runner |
| **429** | — | **Por IP** (o global) [V para esta IP] | Sin F-merc-1b. La segunda key solo duplica la cuota diaria; usarla desde el PC para backfill mientras Actions corre `mercado-ca` con la A |
| 10500 en cualquier paso | — | Hubo concurrencia: algo más estaba llamando | Repetir el protocolo con todo apagado |

Si el 429 nunca llega en 300 requests (paso 1): anotarlo; el límite cambió desde el 29-oct, repetir otro día antes
de concluir (no escribir "ya no hay límite" sin dos corridas, regla 20).

## 4. Dónde se anota
- `docs/15-diagnostico-ca-429.md`: sección nueva "H1 — resultado (fecha)" con la tabla de los pasos, N_A, N_B, R1,
  R2 y la conclusión marcada [V]. Si la conclusión es "por ticket", la H1 pasa de [I] a [V] y se cita en
  `docs/01-analisis-api-mercado-publico.md` §6 (fila "Throttling por IP").
- `docs/00-estado.md`, pendiente operativo 6: hecho, con la fecha.
- El log queda en `data/logs/` (fuera de git). El socio recibe el resumen (cuántos requests se hicieron con su key).

## 5. Cierre
`ruff check scripts/sonda_h1_429.py`. Commit solo del script y los dos docs: `docs: sonda H1 del 429 por ticket o
por IP`. Sin entrada de changelog (no es cambio visible de la app). **Antes de correr nada**, Boris revisa el diff y
confirma que `MP_TICKET_2` está solo en `.env` (y `.env` en `.gitignore`, ya lo está).
