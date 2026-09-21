# Prompt F-actions-2 — los 5 workflows restantes y los horarios

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> **Prerrequisito:** F-actions-1 desplegada y el canario `ciclo-ca` corrido a mano con éxito,
> con `ultimo_ok` de `ca` refrescado en `/api/salud/jobs` y datos nuevos visibles en el
> dashboard. Si eso no pasó, no sigas: esta fase da por probado que el CLI conecta a Neon
> production desde Actions.
>
> Esta fase es casi toda YAML y horarios. El riesgo está en el **cambio de hora de Chile**:
> el cron de GitHub es solo UTC y Chile alterna UTC−3 y UTC−4.

---

```
Fase F-actions-2. F-actions-1 dejó el workflow reutilizable `_job.yml` y el canario
`ciclo-ca.yml` (manual). Ahora completamos los 5 workflows que faltan y activamos los
horarios en los 6. Lee CLAUDE.md antes de empezar: la regla 5 (ventana nocturna validada en
código, no en el cron) es la que manda acá. No cambies la lógica de negocio de ningún runner
ni el contenido de `_job.yml` salvo lo que se indica.

1. LOS CINCO WORKFLOWS NUEVOS
   Todos con la misma forma que `ciclo-ca.yml`: `uses: ./.github/workflows/_job.yml`,
   `secrets: inherit`, `concurrency: { group: mp-jobs, cancel-in-progress: false }`, y la
   lista de jobs en una línea con comillas dobles (`jobs: "..."`) como exige el test
   tests/test_workflows.py.

   | Archivo | `jobs:` | `timeout_min` |
   |---|---|---|
   | `ciclo-activas.yml` | `"activas detalles match alerts"` | 30 |
   | `nocturno.yml`      | `"nocturno"`   | 60 |
   | `resumen.yml`       | `"resumen"`    | 15 |
   | `retencion.yml`     | `"retencion"`  | 30 |
   | `catalogos.yml`     | `"catalogos"`  | 30 |

   `nocturno` lleva 60 min porque arrastra datos abiertos, lifecycle, competencia y el
   backfill de ayer.

2. HORARIOS — CRON EN UTC, CON EL CORRIMIENTO ACEPTADO A PROPÓSITO
   Agregar `schedule` a los 6 workflows (el canario incluido, que hasta ahora solo tenía
   `workflow_dispatch`; conservale el `workflow_dispatch` a todos).

   | Workflow | cron (UTC) | Chile UTC−3 | Chile UTC−4 |
   |---|---|---|---|
   | `ciclo-ca`      | `0 11,13,15,17,19,21,23 * * *` | 08→20 c/2h | 07→19 c/2h |
   | `ciclo-activas` | `10 11,16,21 * * *`            | 08:10 / 13:10 / 18:10 | 07:10 / 12:10 / 17:10 |
   | `nocturno`      | `0 4 * * *`                    | 01:00 | 00:00 |
   | `retencion`     | `0 6 * * *`                    | 03:00 | 02:00 |
   | `catalogos`     | `30 5 * * 1`                   | lun 02:30 | lun 01:30 |

   Dos cosas que hay que dejar escritas como comentario en cada archivo:
   - Los minutos están **escalonados a propósito**. El advisory lock es una sola llave
     global: dos workflows en el mismo minuto hacen que uno se omita en silencio.
   - `nocturno` a las 04:00 UTC cae en 01:00 o 00:00 hora Chile — **dentro** de la ventana
     22:00–07:00 con los dos offsets, así que `en_ventana_nocturna()` nunca lo va a abortar.
     Verificá esa función antes de dar el horario por bueno; si tu lectura no coincide con
     esto, no lo fuerces: decilo.

3. `resumen` — EL ÚNICO A PRUEBA DE DST
   Una hora de corrimiento en el correo diario se nota, así que este no acepta el
   corrimiento. Dos entradas de cron, `30 11 * * *` y `30 12 * * *` (UTC), y el input
   `guard_hora_chile: "08"` que `_job.yml` ya soporta: de las dos corridas, solo la que
   coincide con las 08 hora Chile ejecuta el job; la otra sale sin hacer nada. Resultado:
   08:30 hora de Chile todo el año, en verano y en invierno.
   Confirmá que ese valor concuerda con la variable de repositorio DIGEST_HOUR (=8). Si
   alguien la cambia, el guardia y el digest quedan desfasados: dejá un comentario en el YAML
   avisándolo.

4. TESTS
   - tests/test_workflows.py (ya existe): que siga verde con los 6 workflows. Extendelo con
     dos casos nuevos:
       a) los 6 workflows tienen `schedule` y `workflow_dispatch`;
       b) no hay dos entradas de cron con el mismo minuto+hora entre workflows distintos
          (el escalonamiento del punto 2, verificado por test y no por buena memoria).
   - Sin dependencias nuevas: seguí con regex, nada de PyYAML.

5. VERIFICACIÓN Y LÍMITES
   - `ruff check .`, `python -m mypy app`, `python -m pytest`. Suite verde.
   - NO toques app/, ni el endpoint, ni render.yaml: esta fase es .github/ y tests/.
   - NO apagues todavía los crons de cron-job.org ni el endpoint: eso es F-actions-3.
   - Un commit, prefijo `F-actions-2:`. Entrada en app/changelog.py.
   - NO uses `git add -A` (`_to_delete/` tiene un `.env` con secretos de producción).
```

---

## Paso operativo (Boris, después del merge)

**1. Los `schedule` solo corren en la rama por defecto.** Hasta que esto esté en `main`, no
se dispara nada. Si lo mergeás un lunes después de las 02:30 Chile, `catalogos` recién corre
el lunes siguiente — no es un bug.

**2. La primera corrida programada llega tarde.** GitHub encola los `schedule` y en horas
pico los atrasa varios minutos. Es normal y no afecta a nada acá.

**3. Dejar pasar un ciclo diario completo** antes de tocar F-actions-3. Al día siguiente,
`/api/salud/jobs` tiene que mostrar `activas`, `ca`, `datos-abiertos` y `resumen` todos
frescos, y el correo de resumen tiene que haber llegado **a las 08:30 hora Chile**. Ese
correo es la prueba de que el guardia de DST funciona.

**4. Mirar la cuota de la API una vez.** `ciclo-ca` pasa de 7 disparos diarios a 7 (igual) y
`ciclo-activas` sigue en 3, así que el consumo no debería moverse. Pero es la primera vez que
el CLI corre desatendido: chequeá en `/api/salud` que el contador del día quede donde
esperabas. Si aparece cerca del presupuesto de 9.000, pará y revisamos — son los tres bugs de
`F-cuota` que siguen abiertos (`licitaciones.py:246`, el `consume()` ciego a los 429, y el
`Retry-After` que nunca se lee).
