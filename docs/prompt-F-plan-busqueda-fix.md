# Prompt F-plan-busqueda-fix — el Plan Anual no muestra filas duplicadas y los tests de Postgres corren

> **Fase:** F-plan-busqueda-fix (una fase, un commit) · **Migración:** ninguna · **Dependencias:** ninguna
> **Orden:** ANTES del primer push de F-plan-busqueda (`8269bf2`, sin push al 27-sep). Si ese
> commit ya tiene push, avisar: entonces urge, porque el `catalogos` del lunes 06:35 corre el job.
> **Modelo:** Sonnet (`/model sonnet`).

Reglas del proyecto: CLAUDE.md completo. `app/changelog.py`; `ruff check .`, `python -m mypy app`,
`python -m pytest` verdes **con `DATABASE_URL` de dev exportada y 0 tests de plan skipped**;
commit "F-plan-busqueda-fix: …" sin push; nunca `git add -A`.

## Hallazgos de la auditoría (Cowork, 27-sep) [V, código]
1. `sync_plan_anual_completo` inserta el lote nuevo con **commit cada 5.000 filas** y recién al
   final borra el lote viejo. Mientras carga, y para siempre si la corrida muere a medias
   (timeout, 504 de la descarga, SIGTERM), la tabla tiene **dos lotes del mismo año**.
   `app/plan_busqueda.py` no filtra por `lote_id` → líneas duplicadas y montos sumados dos veces.
   El prompt original pedía "sin dejar la tabla a medias".
2. `tests/test_plan_busqueda.py` y `tests/test_plan_busqueda_routes.py` hacen
   `create_engine(_DB_URL)` con la URL cruda del entorno. Con `postgresql://…` SQLAlchemy busca
   `psycopg2`, que no está en el stack: 11 errores al correrlos contra dev (Boris, 27-sep). La app
   sí normaliza la URL.

## Qué construir
- **Lote vigente explícito:** al terminar bien la carga, guardar el `lote_id` vigente del año en
  `SyncState` (campo propio, no dentro de `notas` como texto). Todas las consultas de
  `app/plan_busqueda.py` y el `get_plan` del año en curso filtran por ese lote. Sin lote vigente
  registrado → estado vacío "se está cargando" (ya existe).
- **Limpieza de lotes huérfanos:** si la carga falla, borrar las filas del `nuevo_lote` antes de
  relanzar la excepción (`try/except`). Al empezar cada corrida, borrar lotes del año que no sean
  el vigente (restos de una corrida muerta sin llegar al `except`).
- **Tests con la URL normalizada:** usar la misma función de normalización que la app (o el
  fixture de `tests/conftest.py`); nunca `create_engine` con la URL cruda.

## Tests (mínimo)
- Con dos lotes del mismo año en la tabla, búsqueda, conteo, vista por organismo y export ven solo
  el vigente.
- Carga que falla a mitad (mock que lanza en el segundo lote) → no quedan filas del lote nuevo y el
  lote vigente sigue intacto y visible.
- Corrida siguiente con un huérfano en la tabla → lo borra.
- Los 11 tests de Postgres pasan contra dev.

*Fuente de los datos de dominio: Dirección ChileCompra.*
