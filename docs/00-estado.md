# Estado actual — mp-oportunidades

*08-oct-2026. Documento corto y vivo: se reescribe, no se apila. La bitácora larga de jul–sep
quedó en `archivo/00-estado-actual.md` y los handoffs de sep en `archivo/`.*

**Al retomar:** leer este archivo → `14-auditoria-integral.md` (auditoría vigente y plan) →
`decisiones.md`. Reglas duras: `../CLAUDE.md`.

## En producción
- `main` = `origin/main`. Última fase: **F-ficha-modal** (`29c0dc6`, 07-oct).
- Fases de sep–oct: F-vigencia · F-ca-explorar · F-ca-vocab · F-acentos · F-guardar · F-ajustes ·
  F-registro · F-ficha-modal. Detalle de cada una en `app/changelog.py` y en
  `archivo/prompts/`.
- Lo que hace la app: perfiles (keywords, regiones, montos, exclusiones, rubros UNSPSC,
  organismos), matching con score, feed solo vigente con filtros y ficha en modal, Guardar /
  Descartar (solo o excluyendo términos) / Archivar con Deshacer, Mi registro, explorador de
  Compras Ágiles con rubros favoritos, Plan Anual con búsqueda inversa, competencia al adjudicar,
  resumen diario + alertas de guardadas, `/salud` y admin.

## Siguiente
Plan de fases en `14-auditoria-integral.md` §8 y paso a paso en §9. En corto:
1. Paso 0 de la auditoría (Boris, solo lectura en producción).
2. **F-datos-1** (Opus): organismo/región de licitaciones, CA desiertas/canceladas, fechas de CA,
   429 diario persistido, reservas de cuota, job `detalles`.
3. **F-match-1** (Sonnet): región/monto antes del tope, organismos en CA, relevancia separada de
   urgencia, digest y feed sin duplicados.
4. F-indices → F-perfiles-1/2 → F-retencion-filas → F-bandeja → spikes de fuentes (OCDS, `COT_`,
   RFI) → F-mcp-1.

## Prompts pendientes
- `prompt-F-ca-rubro.md` — próxima versión (rediseñar el prefiltro; ver auditoría 14).
- `prompt-F-secretos.md` — rotación de credenciales; la parte de código (`repr=False`) ya está.

## Pendientes operativos (Boris)
1. Limpieza de Render: crons viejos hacia `/api/jobs/run`, decidir si se apagan endpoint y
   scheduler, retirar o rotar `JOBS_TOKEN`.
2. Vencimiento anual del PAT de cron-job.org → GitHub (`operacion-disparos.md` §2).
3. Revisar CU-horas de Neon del mes y tamaño de la base (umbral 70 %).
4. Pendiente de F-guardar: confirmar `borrados` del primer `ciclo-match` y `/salud` sin ráfaga.

## Backlog chico
`alembic/env.py` que lea `.env`; `_job.yml` exige `DIGEST_HOUR`/`TASA_*`; `_run_with_lock` sin
fila `cancelado` y CLI sin SIGTERM; retención escribe JSON `null` en `raw_json`; campos del detalle
de CA sin usar (`presupuesto.moneda`, `fecha_cierre_segundo_llamado`, `proveedores_cotizando`);
`ruff format` marca 48 archivos heredados (no formatear en masa); tasas UF/UTM/USD/EUR fijas;
docstring de `condicion_lic_vigente` cita `tests/test_vigencia.py` (está en
`test_ajustes_pg.py`); mensaje de choque de exclusión no nombra la keyword; conteos de pestañas
de Mi registro sin filtros; pestaña Descartadas carga todo en Python.

*Fuente de los datos de dominio: Dirección ChileCompra.*
