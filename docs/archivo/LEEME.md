# docs/archivo — historia, no instrucciones

Lo que está aquí **ya no manda**. Se archivó el 08-oct-2026 (auditoría integral, `../14-auditoria-integral.md`).
Sirve para entender por qué algo es como es; para trabajar, usar los docs de `docs/`.

| Archivo | Qué fue | Lo vigente quedó en |
|---|---|---|
| `00-estado-actual.md` | Bitácora larga jul–sep | `../00-estado.md`, `../decisiones.md`, `../02-arquitectura-y-operacion.md` §5 |
| `handoff-2026-09-26.md`, `handoff-2026-09-27.md` | Handoffs entre sesiones | `../00-estado.md`, `../decisiones.md` |
| `02-plan-desarrollo-y-auditoria.md`, `03-prompts-claude-code.md` | Plan y prompts originales F0–F7 (jun) | ejecutados; reglas en `../../CLAUDE.md` |
| `11-auditoria-ping-y-arquitectura.md`, `12-free-vs-pago.md` | Auditoría del 20-ago y decisión "Opción D" | `../decisiones.md` |
| `13-auditoria-ux.md` | Auditoría UX del 22-sep | `../14-auditoria-integral.md` §4 |
| `arquitectura.md`, `ingesta.md` | Arquitectura con APScheduler/SMTP | `../02-arquitectura-y-operacion.md` |
| `prompt-D-ficha-modal.md` | Brief de diseño (no se ejecutó) | reemplazado por F-ficha-modal |
| `prompts/prompt-F-*.md` | Prompts de fases ya en producción | `app/changelog.py` y `git log` |

Eliminados el mismo día: `contexto/` (copia vieja de docs 01–03), `prompt-F-actions-3-cutover.md`
(marcado "no correr"), `scripts/ingesta_manual.ps1` (puente temporal previo a Actions).
