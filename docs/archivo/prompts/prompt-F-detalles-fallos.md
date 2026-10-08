# Prompt F-detalles-fallos: contador de fallos por oportunidad en `detalles-match`

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Modelo: el Opus más reciente (`/model`).
>
> **Evidencia (primer día en Actions, 25-sep, logs en `data/logs/test_ciclo1/`) [V]:**
> - `detalles-match` pide primero lo que cierra antes. Hay detalles que fallan en TODAS las
>   corridas (504 o 500 de la API) y, como su cierre está cerca, siempre quedan al principio de
>   la cola. Ejemplos repetidos entre `ciclomatch3` y `ciclomatch4`: `2792-842-COT26`,
>   `4468-125-COT26`, `4468-126-COT26`, `2917-262-COT26` y `1274189-440-COT26`. El nocturno y
>   `ciclomatch1` repitieron otra lista (`1285526-57/58`, `1431841-1112/1117`, `3658-440`,
>   `3778-251`).
> - Cada fallo cuesta ~90 s: ~30 s hasta el 504 más 60 s de enfriamiento (regla 3). Con 5 o 6
>   de estos por corrida se van 8 o 9 de los 20 min del presupuesto en detalles que no avanzan.
> - Hoy no hay memoria entre corridas: un detalle fallido vuelve a la cola igual que uno nuevo.
>
> **Deploy — ojo con el orden:** Render aplica las migraciones al arrancar (`alembic upgrade
> head` en `render.yaml`), pero Actions no. Una corrida de Actions que arranque con el código
> nuevo ANTES de que Render migre va a fallar por la columna que falta. Por eso, después de este
> commit, el push se hace justo después de que arranque un `ca` de los :05, y hay que confirmar
> en el log de Render que la migración corrió antes del siguiente disparo (ver "Después").

---

```
Fase F-detalles-fallos. Lee Claude.md antes de empezar: las reglas 3, 6, 11 y "Jobs idempotentes"
y "Solo app/models define esquema" están en juego. Toca solo el modelo y una migración de
Alembic, `run_detalles_match` / `_cola_detalles_match` / `_bajar_detalle` en
app/ingest/orchestrator.py, settings y tests. No toques el job `detalles` de licitaciones
activas, la ingesta de listados ni el matching.

Contexto: hay detalles que fallan en todas las corridas (504/500) y, como su cierre está cerca,
siempre quedan al principio de la cola. Cada uno gasta ~90 s por corrida sin avanzar. Hace falta
memoria de fallos por oportunidad. Evidencia arriba de este bloque, en
docs/prompt-F-detalles-fallos.md.

1. ESQUEMA (app/models + UNA migración Alembic, aditiva).
   En `licitaciones` y `compras_agiles`:
     detalle_fallos        int NOT NULL, server_default 0
     detalle_ultimo_fallo  timestamp NULL (UTC naive, como el resto)
   La migración tiene que ser reversible (downgrade) y no reescribir filas existentes más allá
   del default. Sin índices nuevos, salvo que el EXPLAIN de la cola muestre que hacen falta; si
   es así, justifícalo.

2. REGISTRO DE FALLOS Y ÉXITOS.
   a) Éxito: en el MISMO commit de `_bajar_detalle`, detalle_fallos = 0 y
      detalle_ultimo_fallo = NULL.
   b) Fallo del DETALLE (cualquier excepción que hoy cae en el `except Exception` del loop: 5xx,
      timeout, parseo): detalle_fallos += 1 y detalle_ultimo_fallo = ahora, en una transacción
      corta y aparte (la del detalle ya se deshizo). Un UPDATE atómico parametrizado
      (`SET detalle_fallos = detalle_fallos + 1`), no leer-sumar-escribir. Si ese UPDATE falla,
      log WARNING y seguir: el contador nunca puede botar el job.
   c) Errores del CANAL (_ERRORES_DE_CANAL: 429 no-10500, 10500 agotado, cuota, 401) NO cuentan:
      no son culpa de la oportunidad.
   d) Log WARNING una sola vez, cuando una oportunidad llega a DETALLES_FALLOS_MAX.

3. COLA CON ESPERA.
   a) Settings nuevos (opcionales, con default): DETALLES_FALLOS_MAX (3) y
      DETALLES_ESPERA_HORAS (6).
   b) Una oportunidad con detalle_fallos >= DETALLES_FALLOS_MAX queda fuera de la cola mientras
      ahora < detalle_ultimo_fallo + espera. La espera se duplica por cada fallo extra sobre el
      máximo, con techo de 48 h: 3 fallos → 6 h, 4 → 12 h, 5 → 24 h, 6 o más → 48 h. Pasada la
      espera, vuelve a la cola y tiene un intento: si falla, sube el contador y la espera; si
      funciona, se resetea.
   c) Orden dentro de la cola: primero las que no tienen fallos, luego las que tienen fallos
      (bajo el máximo o ya fuera de su espera). Dentro de cada grupo, el orden de hoy
      (fecha_cierre ascendente, sin fecha al final). Sigue siendo UNA query parametrizada. La
      espera exponencial se puede calcular en SQL o, si queda ilegible, filtrar las candidatas en
      Python después de la query. Justifica lo que elijas.
   d) Resultado de run_detalles_match: agrega `detalles_en_espera` (cuántas quedaron fuera por
      la espera) y `detalles_llegaron_al_maximo` (cuántas cruzaron el máximo en esta corrida).

4. SETTINGS Y WORKFLOW. Los dos settings nuevos van a settings, a .env.example y a la lista de
   opcionales de _job.yml (y a lo que revisa test_workflows.py).

5. TESTS (Postgres para la cola y los UPDATE; confirma que no quedaron skipped).
   - Un fallo sube el contador y la fecha; un éxito los resetea en el mismo commit.
   - Un error del canal no toca el contador.
   - Con 3 fallos y el último hace 1 h queda fuera de la cola; hace 7 h vuelve. Con 4 fallos,
     hace 7 h sigue fuera y hace 13 h vuelve. El techo de 48 h se respeta.
   - Orden: sin fallos primero, luego con fallos, cada grupo por fecha_cierre.
   - `detalles_en_espera` y `detalles_llegaron_al_maximo` cuadran.
   - La migración sube y baja limpia (si el repo tiene un test de migraciones, extiéndelo; si
     no, prueba upgrade/downgrade contra el Postgres de dev).
   - Idempotencia: re-ejecutar no duplica incrementos por un mismo fallo.

Cierre: ruff check, mypy, pytest. Commit "F-detalles-fallos: contador de fallos por oportunidad
en detalles-match". No hagas push. No toques docs/. NO uses `git add -A`.
En el resumen: punto por punto, cuánto tiempo por corrida estimas que se recupera, y qué no
pudiste verificar.
```

---

## Después del commit (Boris)

1. **Elige el momento del push:** justo después de que arranque un `ca` de los :05 (esa corrida
   usa el código viejo, que ya estaba descargado). Así hay unos 40 min hasta el siguiente disparo.
2. Haz push y abre Render → Logs. En el arranque del deploy tiene que aparecer
   `Running upgrade ... -> ...` de Alembic y después `Application startup complete`.
3. Si la migración ya corrió antes del siguiente disparo (el `ciclo-match` de las :50 o el `ca`
   de las :05), listo. Si no, desactiva por un rato `GH ca` y `GH ciclo-match` en cron-job.org
   hasta que Render termine, y vuelve a activarlos.
4. En el siguiente `ciclo-match`, el resultado de `detalles-match` debería mostrar
   `detalles_en_espera` > 0 después de un par de corridas, y menos minutos gastados en fallos.
