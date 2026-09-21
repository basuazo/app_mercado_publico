# Prompt F-cuota — dejar de quemar cuota a ciegas

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> **Esta fase va ANTES de F-actions.** Razón: con los jobs corriendo desatendidos cada 2 h
> desde GitHub Actions, los tres bugs de abajo se ejecutan solos y nadie los ve.
>
> **Evidencia de producción del 21-sep-2026** (no es teoría):
> - A las 11:21 hora Chile, `fetch_detalles` recibió **429 en absolutamente todas** las
>   peticiones de detalle y **siguió el loop igual**: ~60 requests contra una API que estaba
>   rechazando todo, durante 6 minutos.
> - En paralelo, la API v2 devolvía `504 Endpoint request timed out` en la pág. 1 del listado
>   de Compra Ágil, en dos intentos separados por 7 minutos.
> - `_run_with_lock` perdió el advisory lock a mitad de la corrida:
>   `IdleInTransactionSessionTimeout — terminating connection`.
>
> **Hipótesis [I] que esta fase debe verificar, no asumir:** que el 429 de hoy sea
> **limitación por tasa** y no el tope diario de 10.000. Lo que la apoya: `activas` (v1)
> corrió bien y, acto seguido, **cero** detalles pasaron. Si fuera el tope diario, al menos
> los primeros detalles habrían funcionado antes de cruzarlo. Y el volumen del día era
> mínimo: la ingesta estuvo parada desde las 00:30. Si la hipótesis se confirma, la regla 3
> de CLAUDE.md ("429 = agotado hasta el cambio de día calendario") está escrita sobre un
> supuesto equivocado y hay que corregirla — pero **eso no se toca en esta fase**, se
> instrumenta para decidirlo con datos.

---

```
Fase F-cuota. Tres bugs encadenados hacen que la app queme cuota real sin contarla y sin
avisar, más un cuarto bug de advisory lock descubierto hoy. Lee CLAUDE.md antes de empezar:
las reglas 3, 4, 6 y 13 están en juego. No cambies la lógica de negocio de ningún runner.

1. CORTAR ANTE UN 429, NO SEGUIR EL LOOP
   En app/ingest/licitaciones.py, dentro de `fetch_detalles`, el `except Exception` de la
   línea ~246 atrapa TODO —incluido MPRateLimitError— y sigue con la licitación siguiente.
   Verificado en producción: 60 requests seguidas contra una API que rechazaba todas.
   El patrón correcto ya existe en app/ingest/compra_agil.py:209: capturar primero
   MPRateLimitError y RE-LANZARLA, dejando el `except Exception` genérico después para los
   errores por-licitación (parseo, un código raro), que sí deben seguir el loop.
   - Re-lanzá también QuotaExceededError (presupuesto local agotado) y MPAuthError (ticket
     inválido: seguir pidiendo con un ticket malo no tiene sentido y suma requests).
   - Antes de re-lanzar, `session.commit()` del progreso parcial: lo ya procesado se guarda.
     Mirá cómo compra_agil.py resuelve "progreso parcial guardado, cursor intacto".
   - Revisá si el mismo patrón `except Exception` dentro de un loop de requests aparece en
     otros runners (lifecycle.py y catalogos.py son candidatos) y aplicá el mismo criterio.
     Enumerá en el commit cuáles revisaste, incluidos los que quedaron sin cambios.

2. CONTAR TODAS LAS REQUESTS, NO SOLO LAS EXITOSAS
   En app/clients/base.py, `_request` llama `self._quota.consume()` SOLO después de que
   `_handle_response` devuelve bien. Los 429, los 504 y los reintentos no se cuentan, así que
   /api/salud informa un consumo menor al real — hoy justamente, con la API devolviendo
   errores, el número que muestra es un piso, no el consumo.
   El presupuesto local de 9.000 es sobre requests que NOSOTROS emitimos, así que contá cada
   request enviada, cualquiera sea su resultado. Ojo con los reintentos: cada intento es una
   request y cuenta por separado.
   - No toques el esquema de quota_log ni el reseteo por día calendario en America/Santiago.
   - Test: una secuencia con 1 éxito, 1 error 504 con su reintento y 1 error 429 deja el
     contador en 4, no en 1.

3. INSTRUMENTAR EL 429 — LEER LA RESPUESTA ANTES DE DECIDIR NADA
   Hoy `_handle_response` ve un 429, calcula `_seconds_until_next_day_chile()` e inventa
   "00:01 Chile" sin mirar la respuesta. No sabemos si la API manda `Retry-After` ni si
   distingue tope diario de limitación por tasa. Antes de cambiar la política de reintento
   hay que averiguarlo.
   - En la rama del 429, loguear en WARNING: el status, los **headers** de la respuesta
     (buscando `Retry-After`, `X-RateLimit-*`, `RateLimit-*` o equivalentes) y el body
     truncado a 500 caracteres — igual que ya se hace para los 5xx. Pasa por el
     _SecretFilter del logger raíz, así que el ticket queda enmascarado; verificá que así sea
     y no loguees headers de *request*.
   - Si viene `Retry-After`, parsealo (acepta segundos o fecha HTTP) y usalo como
     `retry_after_seconds`. Si NO viene, mantené el fallback actual a 00:01 Chile.
   - **NO cambies la política de reintento en esta fase.** MPRateLimitError sigue sin
     reintentarse (regla 3). El objetivo acá es solo dejar de inventar el dato y empezar a
     registrar el real; con dos o tres días de logs decidimos si la regla 3 se corrige.
     Dejá un comentario en el código diciendo exactamente eso.

4. EL ADVISORY LOCK SE ESTÁ SOLTANDO SOLO
   En app/ingest/orchestrator.py, `_run_with_lock` abre `engine.connect()`, toma el lock y
   deja esa conexión **inactiva dentro de una transacción** mientras `fn()` trabaja con otras
   sesiones. Neon la mata por `idle_in_transaction_session_timeout` y el lock se libera a
   mitad de la corrida — verificado dos veces en el log del 21-sep. La garantía de la regla
   13 hoy no se cumple en los jobs largos.
   Arreglo: que esa conexión no tenga una transacción abierta, p. ej.
   `engine.connect().execution_options(isolation_level="AUTOCOMMIT")`. `pg_advisory_lock` es
   de SESIÓN, no de transacción, así que el lock se mantiene igual mientras la conexión viva,
   y sin transacción abierta ese timeout deja de aplicar.
   - Verificá que `pg_advisory_unlock` en el `finally` siga funcionando en AUTOCOMMIT.
   - El `except` que hoy tolera el fallo al liberar se queda: la conexión igual puede caerse
     por otras razones y el lock se suelta solo al cerrarse la sesión.
   - Test: el lock se toma y se libera igual que antes; el camino de "lock ocupado → omitido"
     no cambia.

5. VERIFICACIÓN Y LÍMITES
   - `ruff check .`, `python -m mypy app`, `python -m pytest`. Suite verde.
   - Tests de red SIEMPRE con respx. Ninguna llamada real.
   - NO toques la cadencia de los jobs, ni el endpoint, ni .github/, ni render.yaml.
   - NO cambies la regla 3 de CLAUDE.md ni la política de reintento del 429 (punto 3).
   - Un commit, prefijo `F-cuota:`. Entrada en app/changelog.py.
   - NO uses `git add -A` (`_to_delete/` tiene un `.env` con secretos de producción).
```

---

## Paso operativo (Boris)

**1. Mirar el contador hoy mismo.** Entrá logueado a
`https://app-mercado-publico.onrender.com/api/salud` (es solo admin) y anotá cuántas requests
dice que llevás. Si marca unos cientos mientras la API te devolvía 429 en todo, queda
confirmado que **el 429 no era el tope diario** — y con eso la regla 3 pasa a estar mal
escrita. Es el dato que más vale ahora mismo y lo conseguís en diez segundos.

**2. Después del deploy, revisar los logs de un 429.** Con el punto 3 en producción, el
próximo 429 va a dejar sus headers en el log de Render. Buscá `Retry-After`. Eso decide si
la regla 3 se corrige o se confirma.

**3. Mientras tanto, no insistir.** Si ves 429 en cadena, no vuelvas a disparar el ciclo en
el día: hoy el código se queda sin saber cuándo reintentar y lo único que se logra es sumar
requests. El 504 de Compra Ágil sí conviene reintentarlo cada un par de horas: es un fallo
del servidor de ChileCompra y se resuelve solo.

## Detalle menor que conviene saber

`HEAD /` devuelve `405 Method Not Allowed`. Si montás UptimeRobot u otro monitor externo,
configuralo con **GET** contra `/api/salud/jobs` — varios usan HEAD por defecto y te daría
una falsa alarma permanente.
