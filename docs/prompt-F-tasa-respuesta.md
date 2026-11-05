# Prompt — F-tasa-respuesta (+ fix de conftest de F-plat-4)

*Modelo: Sonnet. Dos commits, sin migración, sin push. Leer antes: `CLAUDE.md` (reglas 1, 3, 4) y
`app/clients/base.py` (`RateLimiter`, `_request`).*

## Contexto (logs de Actions del 5-nov-2026, verificado en los logs; la causa es INFERIDA, 2 de 2 casos)
Dos corridas diurnas de `mercado-ca` (09:15 y 09:30 Chile, `RATE_LIMIT_RPS=0.5`) cortaron con 429 codigo=429
("límite de tasa") y cada una dejó 20 min de pausa, que además hizo `omitido` la corrida de las 09:48.
En ambos casos el 429 llegó a una petición emitida < 1 s después de que el detalle anterior respondiera,
y ese detalle anterior había tardado 7–14 s:

    12:46:38 200  4364-526-COT26
    12:46:54 200  3706-409-COT26   (≈ 14 s de respuesta)
    12:46:55 429  5615-785-COT26   (≈ 1 s después de la respuesta)

    12:23:49 200  4120-99-COT26
    12:23:58 200  4328-1207-COT26  (≈ 7 s)
    12:23:58 429  3706-409-COT26   (mismo segundo)

En el resto de los logs ninguna petición salió < 3 s después de la respuesta anterior, y ninguna dio 429.
Causa en el código: `RateLimiter.acquire()` es un token bucket medido desde el **inicio** de cada petición.
Durante una respuesta lenta el bucket recarga su token y la siguiente sale de inmediato al volver la respuesta.

## Commit 1 — `F-plat-4: tests sin la key real de OpenRouter`
- `tests/conftest.py` construye `Settings()` leyendo el `.env` real, que trae `OPENROUTER_API_KEY`. En todo
  `Settings` creado por los fixtures forzar `openrouter_api_key=""` y `llm_modelo_extraccion=""`.
- Agregar un test que falle si algún fixture de settings entrega una key de OpenRouter no vacía.
- No tocar el guardia `DATABASE_URL == DATABASE_URL_PROD`.

## Commit 2 — `F-tasa-respuesta: espera mínima después de cada respuesta de la API`
1. `RateLimiter`: nuevo método (p. ej. `tras_respuesta()`) que fija
   `_no_antes_de = max(_no_antes_de, monotonic() + 1/rps)`. Sin log (no es un enfriamiento). Un enfriamiento
   más largo en curso no se acorta (mismo criterio que `enfriar`). El jitter de `acquire()` se mantiene.
2. `_request`: llamarlo en el `finally` que ya rodea `self._http.request(...)` (junto a `consume()`), para que
   valga con cualquier desenlace: 200, 429, 504, timeout, error de transporte. Los `enfriar()` posteriores
   (429/504/timeout) siguen mandando porque son más largos.
3. Resultado esperado con 0,5 req/s: entre el fin de una respuesta y la siguiente petición pasan ≥ 2 s;
   entre inicios de petición sigue habiendo ≥ 2 s. No cambia el máximo teórico de req/s; solo baja el ritmo
   cuando la API está lenta.
4. Tests (sin red real, reloj y `sleep` monkeypatcheados, sin dormir de verdad):
   - respuesta lenta (p. ej. 14 s) → la siguiente `acquire()` espera ≥ 1/rps desde el fin de la respuesta;
   - respuesta rápida → el comportamiento es el de antes (gobierna el bucket);
   - `tras_respuesta()` no acorta un `enfriar(1200)` en curso;
   - `_request` lo llama también cuando la respuesta es 429, 504 y timeout (respx).
5. `docs/00-estado.md`: una línea en lo pendiente de verificar ("tras el deploy, mirar si siguen los 429 diurnos").
   Sin entrada en `app/changelog.py` (no es visible para el usuario).

## Cierre
`ruff check .`, `python -m mypy app`, y **suite completa** `python -m pytest -rs` contra Postgres local
(`TEST_DATABASE_URL`): 0 fallos, 0 errores, 0 saltados. Esa misma corrida cubre la suite de Postgres que quedó
pendiente en F-mesa-v0 (`5d9b36b`). `git add` solo de los archivos de cada commit. Sin push.
