# Prompt F-actions-3 — disparo externo: sacar los `schedule` de los workflows

> Copiar íntegro en una **conversación nueva** de Claude Code, en la raíz del repo.
> Modelo: el Opus más reciente (`/model`).
>
> **Reemplaza a `docs/prompt-F-actions-3-cutover.md`**, que queda obsoleto.
>
> **Evidencia (primer día con F-actions-2, 24–25 sep, logs en `data/logs/test_ciclo1/` y lista
> de corridas en Actions) [V]:**
> - Todas las corridas terminaron en verde. La espera del lock funcionó (esperas de 9 a 18 min
>   hasta conseguirlo).
> - **Los `schedule` de GitHub llegaron 2 a 5,5 h tarde:** nocturno 04:10→09:34 UTC,
>   retencion 08:40→14:07, resumen 11:30/12:30→15:54/17:21, ciclo-activas 13:15→17:56.
> - **`ca` corrió 6 veces en ~24 h en vez de 24:** GitHub descartó ~18 disparos.
> - El guardia "08" rechazó los dos disparos atrasados de `resumen`, así que el correo diario
>   no salió. El nocturno partió a las 06:34 Chile, a minutos de salir de la ventana 22–07.
> - GitHub documenta que `schedule` se atrasa con carga alta y puede descartar corridas.
>
> **Decisión:** los workflows se disparan desde cron-job.org con la API de GitHub
> (`POST /repos/{owner}/{repo}/actions/workflows/{archivo}/dispatches`, evento
> `workflow_dispatch`). cron-job.org es puntual y trabaja en hora de Chile (maneja solo el
> cambio de hora). Esta fase solo saca los `schedule` para que no haya disparos dobles. La
> configuración de cron-job.org la hace Boris (ver `docs/operacion-disparos.md`).

---

```
Fase F-actions-3 (disparo externo). Lee Claude.md. Esta fase toca SOLO .github/workflows/,
tests/test_workflows.py y app/changelog.py. No toques app/ (salvo el changelog), render.yaml ni
docs/.

Contexto: los `schedule` de GitHub llegaron 2 a 5,5 h tarde y descartaron 18 de 24 corridas
horarias de `ca`. A partir de ahora los workflows se disparan desde cron-job.org con la API de
GitHub (evento workflow_dispatch). Hay que quitar los `schedule` para que no se disparen dos
veces (un nocturno o un ciclo-activas doble gasta cuota, y un resumen doble manda dos correos).
Evidencia arriba de este bloque, en docs/prompt-F-actions-3-disparo-externo.md.

1. En los 7 workflows programados (ca, ciclo-match, ciclo-activas, nocturno, retencion,
   catalogos, resumen): elimina el bloque `schedule:` y conserva `workflow_dispatch:` SIN
   inputs (la API se llama solo con {"ref": "main"}). No cambies jobs, timeout_min,
   esperar_lock_min ni concurrency.

2. Reescribe el comentario de cabecera de cada archivo. Tiene que decir que se dispara desde
   cron-job.org (hora de Chile), a qué hora, y que el horario vive allá y está documentado en
   docs/operacion-disparos.md. Quita las menciones a UTC, al escalonamiento de minutos UTC y al
   corrimiento por cambio de hora. Horarios (hora de Chile):
     ca             cada hora a las :05
     ciclo-match    08:50, 10:50, 12:50, 14:50, 16:50, 18:50, 20:50
     ciclo-activas  10:15, 14:15, 19:15
     nocturno       01:10
     retencion      05:40
     catalogos      lunes 06:35
     resumen        08:30
   Conserva las notas sobre el lock (p. ej. que resumen puede esperar al `ca` de las 08:05).

3. resumen.yml: quita `guard_hora_chile: "08"`. Con workflow_dispatch el guardia ya se salta
   (ver el paso "Guardia" de _job.yml), y cron-job.org dispara a las 08:30 exactas. Deja un
   comentario: si alguien cambia DIGEST_HOUR, hay que mover el cron en cron-job.org. No toques el
   soporte de guard_hora_chile en _job.yml.

4. tests/test_workflows.py:
   - Ningún workflow tiene `schedule` (evita disparos dobles si alguien lo vuelve a agregar).
   - Todos tienen `workflow_dispatch` y ninguno declara inputs propios (el disparo externo manda
     solo `ref`).
   - Borra los tests que ya no aplican (choques de minuto/hora UTC, dos crons y guardia de
     resumen) y actualiza los que mencionen schedule.
   - Se mantienen: jobs del CLI existen, grupo `mp-${{ github.workflow }}`, timeout ≥ espera +
     10, ningún SECRET_KEY/JOBS_TOKEN desde secrets.

5. Una entrada en app/changelog.py. ruff check, mypy, pytest. Un commit con prefijo
   "F-actions-3:". No hagas push. NO uses `git add -A` (`_to_delete/` tiene un `.env`).
   En el resumen: lista de archivos cambiados y confirmación de que no queda ningún `cron:` en
   .github/.
```

---

## Después del commit (Boris)

Sigue `docs/operacion-disparos.md` en este orden: push → token → crons en cron-job.org →
prueba. Hazlo en la misma sesión: mientras no estén los crons, no corre nada solo.
