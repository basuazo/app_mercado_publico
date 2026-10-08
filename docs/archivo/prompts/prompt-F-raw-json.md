# Prompt F-raw-json: guardar el detalle de las oportunidades con match

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Modelo: el Opus más reciente (`/model`).
> **Va antes de F-actions-2.** Mientras no se arregle, `match` nunca termina: cada corrida
> vuelve a bajar los mismos detalles, sin tope, y `alerts` no alcanza a correr.
>
> **Evidencia (canario de F-actions-1, 23-sep-2026, 23:08–00:38 UTC).** El log completo está en
> `data/logs/canario.txt` (gitignored). Resumen [V]:
> - `ca` OK en 33 min (979 nuevas, 199 requests). El matching tardó 1 min (4 perfiles, 673 matches).
> - Después `run_match` pasó 55 min bajando detalles de CA: 127 intentos, **ninguno guardado**.
>   118 fallaron con `TypeError: Object of type datetime is not JSON serializable` en
>   `session.commit()` (`orchestrator.py:188`) y el resto con 504. Promedio: ~26 s por detalle.
> - A los 90 min GitHub canceló el workflow por timeout. `alerts` no corrió.
>
> **Auditoría del código [V]:**
> - `run_match` asigna `lic.raw_json = asdict(det)` (línea 171) y `ca.raw_json = asdict(det_ca)`
>   (línea 187). `LicitacionBasica` y `CompraAgilBasica` tienen campos `datetime` desde F1
>   (`0eb9848`), así que el bug existe desde entonces, también en Render.
> - Como el commit falla, se deshace además lo de `upsert_detalle` / `upsert_ca_detalle`: la
>   descripción, los productos o ítems y `id_orden_compra` de las oportunidades con match nunca
>   quedan guardados.
> - `match_perfil` marca como "sin detalle" toda oportunidad con `raw_json IS NULL`
>   (`engine.py:556` y `:591`), y esa lista no tiene tope. Como nunca se guarda, crece en cada corrida.
> - Nadie lee el contenido de `raw_json`: solo se compara con `None` (engine) y se pone en `NULL`
>   (retención). Guardarlo con fechas como texto ISO no rompe ningún lector.
> - Ningún test cubre el camino detalle → commit contra Postgres (JSONB). Por eso no se detectó.
> - `job_runs` graba UNA fila al terminar (`ok | error | omitido`). Una corrida cancelada desde
>   fuera (timeout de Actions, Render que duerme el proceso) **no deja fila**. No queda nada
>   pegado en "corriendo"; simplemente falta la fila.
> - Licitaciones: mismo código, pero ningún log lo ha mostrado todavía **[I]**.

---

```
Fase F-raw-json. Lee Claude.md antes de empezar: las reglas 3, 6, 11 y 12 y "Jobs idempotentes"
están en juego. No cambies la lógica de matching (score, filtros, candidatos) ni la ingesta de
ca/licitaciones.

Contexto: run_match guarda raw_json = asdict(det), y el detalle trae datetime. El commit falla
con "Object of type datetime is not JSON serializable", se deshace todo el detalle, y como
raw_json sigue NULL la próxima corrida vuelve a pedir los mismos detalles, sin tope. En el
canario de Actions eso tuvo a match 55 min bajando detalles hasta que el timeout mató el
workflow. Detalle y evidencia arriba de este bloque, en docs/prompt-F-raw-json.md.

1. SERIALIZACIÓN SEGURA DE raw_json.
   - Un helper puro que convierta el dataclass de detalle en un dict que se pueda serializar a
     JSON: datetime/date → isoformat(), Decimal → str, Enum → .value, recursivo en listas,
     tuplas y dataclasses anidados. Cualquier otro tipo desconocido → str() y log DEBUG, sin
     romper (regla 6). No uses json.dumps(default=str) a ciegas: quiero ver los tipos explícitos.
   - Úsalo en las DOS asignaciones de run_match (licitaciones y CA). Las fechas se guardan tal
     cual las tiene el dataclass (naive UTC): no agregues zona ni conviertas.
   - Ubicación: donde tenga más sentido sin romper la capa anti-corrupción (app/clients no debe
     conocer SQLAlchemy; el orchestrator no debe conocer formatos de la API). Justifica la elección.

2. TOPE DE DETALLES POR CORRIDA.
   - Setting nuevo MATCH_MAX_DETALLES_POR_CORRIDA (int, default 40, repr normal, no es secreto).
     Aplica a licitaciones + CA juntas: es un presupuesto de tiempo, no de cuota.
   - Orden de prioridad para elegir cuáles bajar: primero las que cierran antes (fecha_cierre
     ascendente, solo futuras), luego las sin fecha_cierre, y al final las ya cerradas. Si ese
     orden obliga a una query extra, que sea una sola y parametrizada. Si ves un orden mejor,
     propónlo en el resumen en vez de implementarlo.
   - El resultado de run_match agrega: detalles_intentados, detalles_guardados,
     detalles_fallidos, detalles_pendientes (los que quedaron fuera por el tope).
   - Un detalle que falla siempre (504 persistente) no debe bloquear la cola para siempre.
     Con el orden de arriba eso ya pasa poco, porque las oportunidades cerradas bajan de
     prioridad. No agregues una tabla ni una columna nueva para esto. Si crees que hace falta,
     dilo en el resumen.
   - Agrégalo a la lista de variables OPCIONALES de .github/workflows/_job.yml (las que se hacen
     unset si vienen vacías, junto a CA_*), y a lo que revise tests/test_workflows.py si aplica.

3. TESTS.
   - Unitario del helper: datetime, date, Decimal, Enum, None, anidados, tipo desconocido.
   - De integración CONTRA POSTGRES (fixture pg_session o equivalente): un detalle de CA y uno
     de licitación con fechas reales pasan por run_match (clientes mockeados, sin red) y el
     commit funciona: queda raw_json no nulo, con las fechas en ISO, y quedan la descripción y
     los productos o ítems. Este es el test que faltaba: confirma que NO quedó skipped (corre
     contra el DATABASE_URL de dev de tu .env).
   - El tope: con 50 pendientes y tope 40, se intentan 40 en el orden pedido y
     detalles_pendientes = 10.
   - Idempotencia: la segunda corrida de match sobre los mismos datos no vuelve a pedir detalles.

4. job_runs Y CORRIDAS CANCELADAS — SOLO DIAGNÓSTICO, sin cambiar código.
   Confirma leyendo el código que una corrida cancelada desde fuera no deja fila y que el
   advisory lock se suelta al caer la conexión. Luego di en el resumen si /api/salud/jobs (el
   dead-man's switch) detecta ese caso, y cuánto tarda. Si no lo detecta, propone el arreglo
   para el backlog; no lo implementes.

5. PASO 0 (SOLO LECTURA, informativo). Script desechable FUERA del repo, con DATABASE_URL_PROD
   del .env. SOLO SELECT, nunca imprimas la URL ni ningún secreto:
     - cuántas licitaciones y CA tienen al menos un match y raw_json IS NULL, y cuántas no NULL;
     - de esas CA, cuántas tienen descripcion vacía o nula y cuántas no tienen filas en
       ca_productos.
   Repórtalo tal cual: dimensiona el atraso que el tope va a ir cubriendo.

Cierre: ruff check, mypy, pytest. Commit "F-raw-json: raw_json serializable y tope de detalles
por corrida en match". No hagas push. No toques docs/ (tiene cambios del humano sin commitear).
En el resumen: qué hiciste punto por punto, los números del Paso 0, qué no pudiste verificar
y cuántas corridas de match estimas para ponerse al día con tope 40.
```

---

## Después de correrlo (Boris)

1. Push y disparar `ciclo-ca` a mano de nuevo. Verificar en el log:
   - ningún `not JSON serializable`;
   - `job=match: OK` con `detalles_guardados` > 0 y `detalles_pendientes` bajando entre corridas;
   - que `alerts` sí corra y termine.
2. Recién con eso en verde, F-actions-2.

## Backlog que deja esta fase

- `_job.yml` exige `DIGEST_HOUR` y `TASA_*` como obligatorias (desvío de F-actions-1). Hoy están
  cargadas en GitHub con los defaults del código. Dejarlas opcionales como `CA_*`.
- Lo que salga del punto 4 sobre corridas canceladas y el dead-man's switch.
