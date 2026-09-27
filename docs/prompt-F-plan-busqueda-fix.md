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

- **Carga como máximo una vez al mes (decisión de Boris, 27-sep):** el Plan Anual cambia poco y
  ChileCompra regenera todos los archivos ~mensualmente aunque el contenido casi no cambie
  [V, `docs/07-plan-anual.md` §5-bis g]. Setting `PLAN_ANUAL_DIAS_MIN_ENTRE_CARGAS` (default 28,
  opcional en `_job.yml`): si la última carga OK fue hace menos, el job solo hace el HEAD y
  termina (`omitido_por_frecuencia` en el resultado), aunque `Last-Modified` haya cambiado. El
  primer lunes sin carga reciente, carga. El workflow sigue semanal (un HEAD no cuesta cuota).
- **Pico de espacio [V, Paso 0 27-sep]:** un año ocupa 84,6 MB con índices; producción quedaría en
  ~203 MB (40,6 %). Durante el reemplazo conviven dos lotes: pico estimado ~290 MB (~58 %) [I].
  Antes de insertar, si `tamano_bd + tamaño actual de plan_compra_lineas` supera el 70 % de
  500 MB, no cargar y registrar el motivo (`/salud` lo muestra). Documentarlo en el docstring.

## Tests (mínimo)
- Última carga hace 10 días y `Last-Modified` nuevo → no descarga ni carga; hace 30 días → carga.
- Guarda de espacio: sobre el 70 % no carga y lo informa.
- Con dos lotes del mismo año en la tabla, búsqueda, conteo, vista por organismo y export ven solo
  el vigente.
- Carga que falla a mitad (mock que lanza en el segundo lote) → no quedan filas del lote nuevo y el
  lote vigente sigue intacto y visible.
- Corrida siguiente con un huérfano en la tabla → lo borra.
- Los 11 tests de Postgres pasan contra dev.

*Fuente de los datos de dominio: Dirección ChileCompra.*
