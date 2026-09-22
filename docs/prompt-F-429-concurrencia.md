# Prompt F-429-concurrencia: distinguir el 429 de concurrencia del tope diario

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Modelo: el Opus más reciente (`/model`).
> **Va antes de F-actions.** Con los jobs corriendo desatendidos, un 429 transitorio que la
> app trata como tope diario corta corridas sin que nadie lo vea.
>
> **Evidencia de producción (log de Render, 22-sep-2026, noche).** Una sola secuencia
> `ciclo-ca` (ca → match → alerts), sin ningún otro job corriendo:
> ```
> HTTP 504 body={"message": "Endpoint request timed out"}      <- ca, v2, listado pág. 1
> MPServerError intento 1/2; reintentando en 2.0 s
> HTTP 504 body={"message": "Endpoint request timed out"}      <- ca falla
> HTTP 429 headers_cuota=(ninguna) body={"Codigo":10500,"Mensaje":"Lo sentimos. Hemos
>   detectado que existen peticiones simultáneas."}            <- match, 1ª request de detalle (v1)
> run_match: corte del canal al traer detalles — Cuota agotada (429). Reintentar en 26381 s
> ```
> **[V]** El 10500 aparece sin simultaneidad de nuestro lado: los jobs corren en serie bajo un
> solo advisory lock y la navegación web no llama a la API.
> **[I] Hipótesis de trabajo:** "Endpoint request timed out" es el corte de un gateway (~30 s)
> mientras el backend de ChileCompra **sigue procesando** la consulta. Nuestro reintento a los
> 2 s y la request siguiente de `match` llegan con esa consulta todavía viva y con el mismo
> ticket, y la API las cuenta como simultáneas. Como el 10500 salió en la v1 justo después del
> 504 de la v2, el límite sería por ticket y no por API. Calza también con el 21-sep, cuando
> 504 y 429 aparecieron juntos.
>
> **Auditoría previa de F-cuota (`26d6f30`) y del código actual:**
> - [V] El corte del loop ante errores del canal está bien en `licitaciones.py`,
>   `lifecycle.py` (los dos loops) y `orchestrator.run_match`. `compra_agil.py` re-lanza todo.
> - [V] Hay UN solo advisory lock para todos los jobs (`_LOCK_KEY = 7_891_011`), tomado por
>   scheduler, `POST /api/jobs/run` y CLI. El handoff decía lo contrario: era falso.
> - [V] `retry_after_seconds` no lo lee nadie fuera de `base.py`. El "reintentar en N s" es solo
>   texto: el job se corta y el siguiente disparo corre normal.
> - [V] En `_request`, los reintentos (5xx y timeouts) no vuelven a pasar por
>   `rate_limiter.acquire()` ni por `check_budget()`: solo el primer intento los pasa.
> - [V] v1 y v2 crean cada uno su `RateLimiter`, aunque usan el mismo ticket.

---

```
Fase F-429-concurrencia. Lee Claude.md antes de empezar: las reglas 1, 3, 4 y 13 están en
juego. Esta fase cambia la regla 3, y ese cambio está aprobado por el humano (punto 6).
No cambies la lógica de negocio de ningún runner ni la de sync_incremental de Compra Ágil.

Contexto: en producción, un 504 "Endpoint request timed out" de la v2 fue seguido de un 429
{"Codigo":10500,"Mensaje":"...peticiones simultáneas."} en la request siguiente (v1), sin
ningún otro job corriendo. Hipótesis de trabajo: el gateway corta a ~30 s pero el backend sigue
procesando, y lo que mandamos en ese intervalo cuenta como simultáneo. El diseño de abajo
parte de esa hipótesis.

PASO 0 — DIAGNÓSTICO, SOLO LECTURA. Informativo: repórtalo y sigue con el punto 1.
- Conéctate a producción con DATABASE_URL_PROD del .env, con un script desechable FUERA del
  repo (no lo commitees). SOLO SELECT. Nunca imprimas la URL ni ningún secreto.
- Los timestamps de job_runs y sync_state son UTC sin zona.
    -- a) corridas recientes
    SELECT id, job, estado, iniciado_en, terminado_en, left(error, 300)
    FROM job_runs WHERE iniciado_en >= '2026-09-20' ORDER BY iniciado_en;

    -- b) solapes entre corridas no omitidas
    SELECT a.id, a.job, a.iniciado_en, a.terminado_en, b.id, b.job, b.iniciado_en, b.terminado_en
    FROM job_runs a JOIN job_runs b
      ON a.id < b.id
     AND a.estado <> 'omitido' AND b.estado <> 'omitido'
     AND a.iniciado_en < b.terminado_en AND b.iniciado_en < a.terminado_en
    WHERE a.iniciado_en >= '2026-09-20';

    -- c) estado del cursor de Compra Ágil
    SELECT fuente, cursor, ultima_ejecucion, ultimo_ok, notas
    FROM sync_state WHERE fuente = 'compra_agil';

    -- d) última corrida buena de ca
    SELECT max(terminado_en) FROM job_runs WHERE job = 'ca' AND estado = 'ok';
- Reporta:
  1. Si hay solapes (b). Si los hay, márcalo como hallazgo PRIORITARIO para la fase
     siguiente (el lock no estaría funcionando), pero NO lo arregles aquí y sigue.
  2. Cuántos 504 y cuántos 429 hay en error desde el 20-sep, y cuántos 429 vinieron dentro
     de los 5 minutos siguientes a un 504 (en la misma corrida o en la siguiente).
  3. Cuántas horas de atraso tiene el cursor de compra_agil respecto de ahora, y cuándo fue
     la última corrida ok de ca. Es insumo para otra fase (ventana acotada); no lo toques.

1. DISTINGUIR EL 10500 EN app/clients/base.py
   - Nueva excepción MPConcurrencyError(MPRateLimitError). Tiene que ser SUBCLASE a propósito:
     los runners ya cortan el loop ante MPRateLimitError y deben seguir haciéndolo cuando se
     agoten los reintentos. Así no se toca ningún runner.
   - En la rama del 429 de _handle_response, lee el código del cuerpo de forma defensiva:
     `Codigo` o `codigo` en la raíz del JSON y, si no está, en los elementos de una lista
     `errors` (envelope v2). Cuerpo no JSON, sin código o con otro código → comportamiento de
     hoy, sin cambios (MPRateLimitError, fallback a 00:01 Chile).
   - Con 10500 → MPConcurrencyError con retry_after_seconds=900.
   - Agrega el código leído al WARNING que ya se loguea (se mantienen headers_cuota y body).
   - Corrige el texto: "Cuota agotada (429)" solo cuando de verdad se trata como tope diario.
     Para 10500: "Concurrencia (429/10500)". Actualiza el docstring de MPRateLimitError.

2. UN SOLO RATE LIMITER POR PROCESO, CON ENFRIAMIENTO
   - MercadoPublicoV1Client y MercadoPublicoV2Client crean cada uno su RateLimiter, pero usan
     el mismo ticket. Que compartan UNA instancia por proceso: una función en base.py que
     devuelva un limiter a nivel de módulo, creado la primera vez con settings.rate_limit_rps y
     protegido con threading.Lock. Si una tasa pedida difiere de la ya creada, gana la primera
     y se loguea un WARNING. Deja una forma de resetearlo para los tests.
   - Agrega a RateLimiter un método enfriar(segundos) que fije un "no antes de" (monotónico).
     acquire() espera hasta ese instante antes de su lógica normal. Si ya hay un enfriamiento
     más largo en curso, no se acorta.
   - Es por proceso, no global: entre procesos serializan el advisory lock y, en F-actions, el
     grupo `concurrency` de los workflows (ya está en esos prompts).

3. ENFRIAMIENTO TRAS UN 504 O UN TIMEOUT
   - Un 504 y un httpx.TimeoutException llaman a rate_limiter.enfriar(60) antes de decidir si
     reintentar. Razón: el servidor probablemente sigue procesando la consulta cortada, y
     cualquier request nuestra en ese intervalo, de v1 o de v2, puede contarse como simultánea.
   - Como el limiter es compartido, el enfriamiento también frena al job siguiente de la
     secuencia (p. ej. match después de ca), que es justo el caso del log.
   - Los demás 5xx (500, 502, 503) conservan su espera actual: el 500 de Compra Ágil es
     determinista (docs/09-compra-agil-500.md), no una consulta en curso.
   - La cantidad de reintentos de 5xx y timeout no cambia; solo la espera efectiva.
   - Loguea en WARNING cuando se aplica un enfriamiento, con los segundos y la causa.

4. REINTENTO SOLO PARA EL 10500, EN _request
   - Hasta 3 reintentos con esperas de 30, 60 y 120 s, más un jitter aleatorio de 0 a 20 %.
     Implementa la espera con rate_limiter.enfriar(...), no con un sleep suelto, para que
     frene también a cualquier otra request del proceso.
   - Si el cuarto intento vuelve a dar 10500, se propaga MPConcurrencyError y el runner corta.
   - Cualquier otro 429 sigue SIN reintentarse, igual que hoy.
   - Loguea cada reintento en WARNING con el intento y la espera.
   - Los contadores de intentos del 10500, del 5xx y del timeout son independientes.

5. TODO INTENTO PASA POR EL PRESUPUESTO Y EL RATE LIMITER
   Hoy check_budget() y rate_limiter.acquire() se llaman una sola vez, antes del while. Muévelos
   dentro del loop para que corran antes de CADA intento: 10500, 5xx y timeout. Si el
   presupuesto se agota a mitad de los reintentos, QuotaExceededError sin emitir la request.
   El consume() en el finally de F-cuota se queda como está: cada intento emitido cuenta.

6. ACTUALIZAR LA REGLA 3 DE Claude.md (el archivo trackeado es Claude.md)
   Reemplaza la frase "429 = agotado hasta el cambio de DÍA CALENDARIO en America/Santiago;
   jamás reintentar un 429 el mismo día" por:
     "429 con Codigo 10500 (peticiones simultáneas) = concurrencia: máx. 3 reintentos con
     backoff (30/60/120 s) y luego cortar el job; el siguiente disparo corre normal. Tras un
     504 o un timeout, enfriar 60 s antes de cualquier request (el backend puede seguir
     procesando). Cualquier otro 429 = tratarlo como tope diario: no reintentar hasta el cambio
     de DÍA CALENDARIO en America/Santiago. El 10500 es el único código de 429 verificado
     (22-sep-2026)."
   El resto de la regla 3 no cambia.

7. TESTS (tests/test_cuota.py o uno nuevo). Todo con respx y con el tiempo parchado
   (time.sleep y time.monotonic): ninguna espera real, ninguna llamada real.
   - 10500 y luego 200 → devuelve los datos; 2 requests contadas; espera entre 30 y 36 s.
   - 10500 cuatro veces → MPConcurrencyError, que también es MPRateLimitError; 4 requests
     contadas; esperas en el orden 30/60/120 (± jitter); retry_after_seconds == 900.
   - 429 sin cuerpo, con cuerpo no JSON y con otro Codigo → MPRateLimitError que NO es
     MPConcurrencyError, 1 sola request, sin espera. Ajusta test_el_429_sigue_sin_reintentarse
     para que siga cubriendo este caso y no el 10500.
   - Código dentro de `errors` (forma v2) → se reconoce el 10500.
   - 504 → la siguiente request, aunque sea de OTRO cliente (v1 tras v2), espera al menos 60 s.
   - Timeout de httpx → mismo enfriamiento de 60 s.
   - 500 → sin enfriamiento; espera actual.
   - enfriar() con un valor menor no acorta uno más largo en curso.
   - acquire() se llama una vez por intento, también en los reintentos de 5xx y timeout.
   - Presupuesto agotado entre reintentos → QuotaExceededError y ninguna request más.
   - v1 y v2 construidos en el mismo proceso comparten el mismo RateLimiter.
   - Un runner (fetch_detalles_pendientes en licitaciones.py) con 10500 persistente corta el
     loop y guarda el progreso parcial, igual que con un 429 común.
   - Verifica que los tests del 10500, del enfriamiento y del acquire FALLAN sin su arreglo,
     y dilo en el commit.

8. VERIFICACIÓN Y LÍMITES
   - ruff check ., python -m mypy app, python -m pytest. Suite verde.
   - NO toques la cadencia de los jobs, los endpoints, .github/, render.yaml, el esquema de
     quota_log, el advisory lock ni la lógica de ventana/cursor de sync_incremental.
   - NO arregles los exit codes del CLI: eso es F-actions-1.
   - Un commit de código con prefijo `F-429-concurrencia:`, con entrada en app/changelog.py y
     el resultado del Paso 0 en el mensaje.
   - Aparte, un commit `docs:` con docs/prompt-F-429-concurrencia.md y docs/00-estado-actual.md
     si tienen cambios sin commitear.
   - Agrega archivos por nombre. Nunca `git add -A`.
```

---

## Checklist de auditoría (para la conversación de revisión)

**Comandos:** `ruff check .`, `python -m mypy app`, `python -m pytest`.

**A mano en el diff:**
1. El Paso 0 está reportado con filas reales: solapes, 504 seguidos de 429 y atraso del cursor.
2. `MPConcurrencyError` hereda de `MPRateLimitError`, y ningún runner cambió.
3. El parseo del código no puede lanzar: cuerpo vacío, HTML, JSON sin `Codigo` y `errors` que
   no es lista caen todos al camino de hoy.
4. El limiter es uno por proceso, y `enfriar()` lo respeta `acquire()` de ambos clientes.
5. El enfriamiento se dispara con 504 y timeout, y NO con 500/502/503.
6. Las esperas del 10500 pasan por `enfriar()`, no por un `sleep` que solo frene a un cliente.
7. `check_budget()` y `acquire()` quedaron dentro del `while`, antes de la request.
8. Ningún reintento para un 429 que no sea 10500.
9. El tiempo está parchado en los tests: la suite no se hace más lenta.
10. La regla 3 de `Claude.md` quedó con el texto del punto 6 y nada más de ese archivo cambió.
11. Ningún log nuevo imprime headers de request (regla 1).

## Paso operativo (Boris)

- **Después del deploy:** la rutina manual sigue igual. Cambia la regla de oro: un 429 con
  10500 ya no significa "no insistir hoy"; si el job se cortó por 10500, se puede volver a
  disparar pasados unos 15 minutos. Un 429 con otro código sí es "no insistir hoy".
- **Si `ca` sigue muriendo con 504 en la página 1** después de esta fase, el problema probable
  es el cursor atrasado (la ventana de cambios crece con cada fallo). El Paso 0 trae el dato;
  la solución es la fase de ventana acotada, aparte.
