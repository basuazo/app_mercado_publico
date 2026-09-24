# Prompt F-ca-ventana-volumen: ventanas de Compra Ágil según volumen y cierre parcial de `ca`

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Modelo: el Opus más reciente (`/model`).
> **Va antes de F-actions-2.** Es la última pieza para que `ciclo-ca` termine en verde.
>
> **Evidencia (canario 4, 24-sep, 17:18–18:28 UTC, commit `15b9b0b`, log en
> `data/logs/canario4/`) [V]:**
> - El workflow completo tardó 71 min, sin cancelación. `match` (1,5 min), `alerts` y
>   `detalles-match` (20 guardados, corte a los 20,1 min) terminaron OK.
> - **`ca` terminó en ERROR a los 46 min y dejó el workflow en rojo.** En hora punta una ventana
>   de 1 h (`CA_VENTANA_HORAS=1`, `CA_TAMANO_PAGINA=10`) trae ~900 cambios: la 1ª ventana tuvo
>   93 páginas (23 min) y la 2ª 82. Cada página tarda ~15 s, y ese tiempo NO crece con el número
>   de página: lo que pesa es el volumen, no la profundidad.
> - La 1ª ventana cerró y el cursor avanzó. En la 2ª, la página 82 dio 504 dos veces (reintento
>   incluido), `sync_incremental` re-lanzó el error y el job terminó en ERROR. Como el cursor solo
>   avanza al cerrar una ventana, se perdieron ~22 min de avance: la próxima corrida vuelve a pedir
>   esas 82 páginas.
> - `CA_MAX_REQUESTS_POR_CORRIDA` (150) se revisa solo antes de abrir una ventana, así que no
>   acota una ventana grande: la corrida llegó a ~175 requests.
>
> **Relación con F-ca-ventana:** esa fase descartó "reducir la ventana ante un 504", porque suma
> requests justo cuando la API está saturada. Esto es otra cosa: se parte la ventana **antes** de
> paginarla, según el `total_paginas` que ya informa la página 1, sin esperar a que falle. Ya
> existe el mismo mecanismo para el tope de 10.000 resultados. Aquí solo se agrega un umbral
> mucho más bajo.

---

```
Fase F-ca-ventana-volumen. Lee Claude.md antes de empezar: las reglas 3, 6, 10 y 12 y "Jobs
idempotentes" están en juego. Toca solo `_sync_en_ventanas` / `sync_incremental` de
app/ingest/compra_agil.py, settings, _job.yml y sus tests. No cambies el parseo, el matching ni
los otros jobs. El arranque en frío (`_sync_arranque_en_frio`) queda como está.

Contexto: en hora punta una ventana de 1 h trae ~90 páginas de 10 (~23 min). Un 504 doble en la
página 82 hizo fallar `ca` y perdió 22 min de avance, porque el cursor solo avanza al cerrar la
ventana. Evidencia arriba de este bloque, en docs/prompt-F-ca-ventana-volumen.md.

1. PARTIR LA VENTANA SEGÚN VOLUMEN, ANTES DE PAGINARLA.
   a) Setting nuevo CA_MAX_PAGINAS_POR_VENTANA (int, default 20: ~5 min a ~15 s por página).
      Tras pedir la página 1, si total_paginas > CA_MAX_PAGINAS_POR_VENTANA, parte la ventana a la
      mitad (hasta = desde + mitad) y vuelve a pedir la página 1, igual que hoy con
      _TOPE_RESULTADOS. Repite hasta quedar bajo el umbral o llegar a _VENTANA_MINIMA (10 min).
   b) En el mínimo NO se lanza error (a diferencia del tope de 10.000, que sí debe seguir
      lanzando): se pagina la ventana de 10 min aunque tenga más páginas que el umbral, y se deja
      un log WARNING. Un tope blando no puede frenar la ingesta.
   c) El ancho reducido se mantiene para la ventana siguiente de la MISMA corrida (en hora punta
      la siguiente hora también viene cargada). Si una ventana trae menos de la mitad del umbral,
      el ancho se duplica para la siguiente, sin pasar de CA_VENTANA_HORAS. No se persiste entre
      corridas: cada corrida parte en CA_VENTANA_HORAS. Así se evita pedir 3 o 4 páginas 1
      extra por ventana en hora punta.
   d) Cuenta las ventanas partidas por volumen aparte de las partidas por el tope de 10.000
      (`ventanas_partidas_volumen` y la `ventanas_partidas` existente) y el ancho final en
      minutos (`ancho_final_min`).

2. TOPE DE REQUESTS DENTRO DE LA VENTANA. Hoy CA_MAX_REQUESTS_POR_CORRIDA se revisa solo antes
   de abrir una ventana. Con ventanas de ≤ ~20 páginas, basta con mantenerlo así: dilo en el
   resumen si ves que no alcanza. No cortes una ventana a la mitad por el tope, porque el cursor
   no puede avanzar sin cerrarla.

3. CIERRE PARCIAL DE `ca`.
   a) Si durante una ventana ocurre un error TRANSITORIO (MPServerError o timeout de httpx, ya
      agotados los reintentos del cliente) y la corrida YA cerró al menos una ventana, `ca`
      termina OK: devuelve el resultado con `cortado_por_error` = nombre del tipo de excepción
      (sin el mensaje), el cursor en la última ventana cerrada y el `atraso_horas` real. Log
      WARNING. En `sync_state`, registra el intento como parcial de forma coherente con cómo se
      registra hoy un éxito o un fallo; justifica en el resumen qué elegiste.
   b) Siguen siendo ERROR, como hoy: el mismo transitorio si NO se cerró ninguna ventana en la
      corrida; cualquier 429 (la regla 3 ya decide qué hacer con cada uno); CompraAgilIngestaError
      (incluido el tope de 10.000 que no se puede partir); un fallo de commit_con_retry; y
      cualquier otra excepción.
   c) Motivo del criterio, déjalo como comentario en el código: si la corrida cerró ventanas,
      hizo trabajo útil y lo guardó. Si no cerró ninguna, un "OK" escondería una caída. El
      dead-man's switch de /api/salud/jobs mira el último OK de `ca`, así que una corrida parcial
      con avance cuenta como viva, y `atraso_horas` muestra si se va quedando atrás.

4. SETTINGS Y WORKFLOW. CA_MAX_PAGINAS_POR_VENTANA va a settings, a .env.example y a la lista de
   opcionales de _job.yml (y a lo que revisa test_workflows.py). No cambies los defaults de
   CA_VENTANA_HORAS ni de CA_TAMANO_PAGINA. En GitHub y en Render siguen en 1 y 10.

5. TESTS (respx o fakes del cliente, sin red; reloj parchado donde haga falta).
   - Ventana de 1 h con total_paginas=90 en la página 1: se parte a 30 y luego a 15 min, y
     recién ahí pagina. El cursor avanza al cerrar cada sub-ventana.
   - El ancho reducido se mantiene en la ventana siguiente y se duplica cuando una ventana trae
     pocas páginas, sin pasar de CA_VENTANA_HORAS.
   - En el mínimo de 10 min con más páginas que el umbral: pagina igual, sin error.
   - El tope de 10.000 sigue partiendo, y sigue lanzando CompraAgilIngestaError en el mínimo.
   - 504 agotado en la 2ª ventana, con la 1ª ya cerrada: `ca` OK con cortado_por_error y el
     cursor al final de la 1ª ventana.
   - 504 agotado en la 1ª ventana: ERROR, sin avanzar el cursor.
   - 429 (10500 agotado y no-10500) con ventanas cerradas: sigue siendo ERROR.
   - Idempotencia: re-ejecutar tras un cierre parcial retoma desde el cursor, sin duplicar.

Cierre: ruff check, mypy, pytest. Commit "F-ca-ventana-volumen: ventanas de CA según volumen y
cierre parcial de ca". No hagas push. No toques docs/. En el resumen: punto por punto, cuánto
estimas que dura `ca` en hora punta con el umbral 20, cuántas páginas 1 extra agrega la
partición por corrida, y qué no pudiste verificar.
```

---

## Después de correrlo (Boris)

1. Push. **Espera a que termine** y dispara `ciclo-ca`. Confirma el commit en "Checkout".
2. En el log:
   - aparecen ventanas partidas por volumen y ninguna pasa de ~20 páginas;
   - `ca` termina en OK (o en OK con `cortado_por_error` si hubo un 504 doble);
   - el workflow completo termina en verde.
3. Si sale verde, **F-actions-2**. Hay que sumarle el timeout del nocturno (120 min de
   `detalles-match` más el resto del ciclo) y pausar cron-job.org el mismo día del merge.

## Backlog que sigue abierto

- `_job.yml` exige `DIGEST_HOUR` y `TASA_*` como obligatorias (desvío de F-actions-1).
- `_run_with_lock` no graba fila cuando la corrida se cancela desde fuera.
- Match de CA por rubro: definir a qué CA sin match bajarles detalle de noche.
- Campos del detalle aún sin usar: `presupuesto.moneda`, `fecha_cierre_segundo_llamado`,
  `proveedores_cotizando`.
