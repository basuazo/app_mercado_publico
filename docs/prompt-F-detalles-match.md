# Prompt F-detalles-match: sacar los detalles de `match` y arreglar el parseo del detalle de CA

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Modelo: el Opus más reciente (`/model`).
> **Va antes de F-actions-2.** Con F-raw-json el detalle ya se guarda, pero `ciclo-ca` sigue
> muriendo por el timeout de 90 min antes de llegar a `alerts`.
>
> **Evidencia [V]:**
> - **Canario 3** (24-sep, 11:32–13:02 UTC, commit `1faf78d`, log en `data/logs/canario3/`):
>   0 errores de serialización. `ca` tardó 40 min y el matching 2 min. Después se intentaron 39
>   detalles: 27 guardados y 11 fallidos, todos por un 504 que se repitió en el reintento.
>   GitHub canceló el workflow a los 90 min y `alerts` no corrió.
> - **Costo por detalle:** ~78 s en promedio. 1 de cada 3 da 504, y cada 504 cuesta ~3 min
>   (~30 s del gateway, 60 s de enfriamiento, reintento, otros ~30 s y otros 60 s).
> - **Sonda `claves-ca`** (`scripts/smoke_test.py claves-ca`, ya escrita y sin commitear, va en
>   este commit): el **listado v2 no trae** `descripcion` ni `productos_solicitados` (10 de 10
>   ítems). El detalle es inevitable. Claves reales del detalle, en una muestra:
>   `codigo, convocatoria{descripcion, estado_convocatoria, fecha_cierre_primer_llamado,
>   fecha_cierre_segundo_llamado}, descripcion, documentos, entrega, estado, fechas{fecha_cancelacion,
>   fecha_cierre, fecha_publicacion, fecha_ultimo_cambio}, flags, id_orden_compra, institucion,
>   motivos, nombre, presupuesto{moneda, monto_disponible, monto_disponible_clp,
>   presupuesto_estimado, ...}, productos_solicitados[{cantidad, codigo_producto (int),
>   descripcion, nombre, unidad_medida}], proveedores_cotizando, resumen`.
>   El detalle **no trae `montos`** (el listado sí) ni un objeto `orden_compra`.
> - **Bugs de `_parse_ca_detalle` / `_parse_ca_basica`, cruzando esa muestra con el código:**
>   1. `id_orden_compra` se lee de `orden_compra.id_orden_compra`, que no existe → siempre None.
>   2. `estado_convocatoria` se lee en el primer nivel; viene en `convocatoria` → siempre None.
>   3. El monto se lee de `montos`, que el detalle no trae → `monto_clp=None`, y
>      `upsert_ca_detalle` → `upsert_ca_basica` **pisa `monto_disponible_clp` con None** en
>      toda CA con detalle.
>   4. La `descripcion` de cada producto (373 caracteres en la muestra) se descarta:
>      `CompraAgilItem` no la tiene y `ca_productos.descripcion` se guarda como `""`.
>   5. (Ya estaba en el backlog.) `organismo_nombre` y `organismo_rut` guardan el texto
>      `'None'` cuando falta `institucion` (`str(...)` antes del `or None`).
> - **Existe un job `detalles`** (detalles de licitaciones activas, dentro de `ciclo-activas`).
>   El job nuevo necesita otro nombre.

---

```
Fase F-detalles-match. Lee Claude.md antes de empezar: las reglas 3, 5, 6, 10, 13 y 17 y
"Jobs idempotentes" están en juego. No cambies el score ni los filtros del matching, salvo lo del
punto 3.d. No toques la ingesta de listados (`ca`, `activas`).

Contexto: bajar un detalle cuesta ~78 s en promedio por los 504 de la API, y hoy `run_match` los
baja antes de que corra `alerts`. En el canario eso llevó la corrida a los 90 min y GitHub la
canceló. Una sonda contra la API real confirmó que el listado no trae descripción ni productos:
el detalle es inevitable, pero no tiene por qué bloquear las alertas. Evidencia y claves reales
del detalle arriba de este bloque, en docs/prompt-F-detalles-match.md.

1. JOB NUEVO `detalles-match`, SEPARADO DE `match`.
   a) `run_match` solo calcula matches. Ya no baja detalles. Su resultado lleva CONTEOS
      (sin_detalle_licitaciones y sin_detalle_ca como enteros), no listas de códigos: el
      resultado se guarda en job_runs y las listas lo inflan en cada corrida.
   b) `run_detalles_match(settings, engine)` arma su propia cola con UNA query parametrizada:
      oportunidades (licitaciones y CA) con al menos una fila en oportunidades_match, sin
      detalle, publicadas y sin cerrar (fecha_cierre nula o futura). "Sin detalle" es
      `raw_json IS NULL OR jsonb_typeof(raw_json) = 'null'`: hay 599 licitaciones con JSON
      `null` guardado en vez de NULL de SQL. Orden: fecha_cierre ascendente, las sin fecha al
      final. Reusa lo que sirva de `_priorizar_detalles` y `_bajar_detalle` (F-raw-json): el
      guardado en UN commit por detalle con `dataclass_a_json` no cambia.
   c) TOPE POR TIEMPO, no por cantidad. Antes de pedir cada detalle, si el tiempo transcurrido
      supera el presupuesto, corta y deja el resto para la corrida siguiente. Presupuesto:
      DETALLES_MINUTOS_DIA (default 20) y DETALLES_MINUTOS_NOCHE (default 120), según
      `en_ventana_nocturna()` (la noche es la ventana de la regla 5). Estos dos settings
      reemplazan a MATCH_MAX_DETALLES_POR_CORRIDA: quítalo de settings, de .env.example y de
      _job.yml, y agrega los nuevos a la lista de opcionales de _job.yml (y a lo que revisa
      test_workflows.py).
   d) SIN REINTENTO DE 504 NI DE TIMEOUT, solo para estos detalles: el detalle vuelve a la cola
      solo, así que reintentarlo en la misma corrida gasta ~1,5 min por fallo. El enfriamiento
      de 60 s tras un 504 o un timeout SE MANTIENE (regla 3). El 429/10500 conserva su política
      actual, y un 429 que no sea 10500 corta el job como hoy. Impleméntalo como una opción
      explícita del cliente (p. ej. un parámetro en `detalle_compra_agil` y
      `licitacion_detalle`), sin cambiar el comportamiento por defecto para los demás
      llamadores. httpx y la política de reintentos siguen viviendo solo en app/clients.
   e) Resultado: detalles_intentados, detalles_guardados, detalles_fallidos,
      detalles_pendientes, minutos_usados, presupuesto_minutos.
   f) Regístralo en todos los caminos: `_JOBS` y dispatch del CLI, el dict de jobs del endpoint
      y el scheduler en proceso, si corresponde.

2. SECUENCIAS Y WORKFLOWS.
   - `ciclo-ca` pasa a "ca match alerts detalles-match", en el workflow y en `_secuencia` del
     endpoint (Render sigue disparándolo hasta F-actions-3). `alerts` ya no espera a los detalles.
   - Agrega `detalles-match` como ÚLTIMO paso de `_ciclo_nocturno`, con el presupuesto nocturno.
     Revisa que el timeout del workflow nocturno (F-actions-2 lo va a crear) quede anotado: con
     120 min de detalles + el resto del ciclo, hay que dimensionarlo. Déjalo escrito como
     comentario donde corresponda y en el resumen. No crees ese workflow acá.
   - No toques `ciclo-activas` ni el job `detalles` existente.

3. PARSEO DEL DETALLE DE CA (app/clients/mp_v2.py). Defensivo (regla 6): primero la ruta real
   observada y luego la antigua como respaldo; nunca romper.
   a) id_orden_compra: primer nivel `id_orden_compra`; respaldo `orden_compra.id_orden_compra`.
   b) estado_convocatoria: `convocatoria.estado_convocatoria`; respaldo en el primer nivel.
   c) monto: en el detalle, `presupuesto.monto_disponible_clp`; respaldo `montos`. Además,
      `upsert_ca_detalle` NO debe pisar con None un monto que ya existe (mismo criterio que
      `upsert_basica` de licitaciones con las fechas). Revisa si otros campos de
      `upsert_ca_basica` sufren lo mismo al venir del detalle y protégelos igual.
   d) descripcion de cada producto: agrégala a `CompraAgilItem` y guárdala en
      `ca_productos.descripcion`, truncada a su largo de columna (1000). Súmala a la búsqueda
      por palabras clave de CA: en `_FTS_CA_INCLUDE` y `_FTS_CA_EXCLUDE`, al lado de `p.nombre`.
      Es el único cambio de matching permitido en esta fase. Di en el resumen si afecta el
      tiempo del matching.
   e) organismo_nombre / organismo_rut: None real cuando falta `institucion`, nunca 'None'.
   f) Actualiza el fixture de detalle de tests/test_clients.py (hoy trae `montos` y
      `orden_compra`, inventados) a la forma real observada, y conserva un caso con la forma
      vieja para probar los respaldos.

4. MONITOR. En /api/salud/jobs agrega `match` y `alerts` a _JOBS_VIGILADOS (30 h, críticos):
   hoy un `match` cancelado en todas las corridas pasa inadvertido. `detalles-match` NO se
   vigila: un día sin detalles no es una caída.

5. TESTS (Postgres donde haya JSONB o FTS; confirma que no quedaron skipped).
   - run_match ya no llama a ningún cliente y su resultado no trae listas.
   - detalles-match: presupuesto de tiempo con reloj inyectable (corta al pasarse, deja
     pendientes); orden por cierre; JSON null contado como sin detalle; un 504 no se reintenta
     pero sí respeta el enfriamiento; un 429 no-10500 corta; idempotencia (la segunda corrida
     no vuelve a pedir lo guardado).
   - Parseo: forma real y forma vieja de los puntos 3.a a 3.e; el monto existente no se pisa.
   - FTS: una CA que calza solo por la descripción de un producto aparece como candidata.
   - Workflows: ciclo-ca nombra jobs que existen en el CLI; los settings nuevos son opcionales.
   - El test que depende de la hora (`test_jobs_run_acepta_jobs_antes_inalcanzables[nocturno]`,
     que falla entre 22:00 y 07:00 hora Chile) arréglalo inyectando el reloj, si es trivial. Si
     no, déjalo anotado.

Cierre: ruff check, mypy, pytest. Commit "F-detalles-match: detalles fuera de match con tope por
tiempo y parseo real del detalle de CA", que incluye scripts/smoke_test.py (sonda claves-ca,
ya escrita). No hagas push. No toques docs/. En el resumen: punto por punto, cuánto estimas que
dura `ciclo-ca` ahora, qué no pudiste verificar y cualquier otro campo del detalle real que veas
mal parseado.
```

---

## Después de correrlo (Boris)

1. Push. **Espera a que el push termine** y recién ahí dispara `ciclo-ca`. En el canario 2 se
   disparó unos segundos antes y corrió el commit viejo. Confirma el commit en el paso "Checkout".
2. En el log:
   - `match` termina en 2–3 min sin bajar detalles, y `alerts` corre y termina en OK;
   - `detalles-match` corta cerca de los 20 min, con `detalles_guardados` > 0;
   - el workflow completo termina en verde, bien antes de los 90 min.
3. Con eso, F-actions-2 (sumando el timeout del nocturno).

## Backlog que deja esta fase

- `_job.yml` exige `DIGEST_HOUR` y `TASA_*` como obligatorias (desvío de F-actions-1).
- `_run_with_lock` no graba fila cuando la corrida se cancela desde fuera (`BaseException`,
  SIGTERM). Propuesta: fila `cancelado` y SIGTERM → `SystemExit` en el CLI.
- El detalle de licitaciones v1 no trae fechas en la ruta que lee el parser [V por datos, la ruta
  real está sin verificar]. Hoy no hace daño.
- Match de CA por rubro: sin detalle no hay productos. Con `detalles-match` y la ventana
  nocturna ya existe la pieza para bajar detalle a CA sin match. Falta decidir a cuáles.
