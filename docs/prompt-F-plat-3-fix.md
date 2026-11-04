# Prompt de corrección — F-plat-3-fix (auditoría de `0a35363`) · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit, **sin push**, **sin migración**. Origen: auditoría de Cowork
> del commit `0a35363` (F-plat-3). Lo demás de la fase quedó aprobado: alcance del commit limpio, los 5
> consumidores de `regiones_activas`, ownership con 404 en todas las rutas con id, CSRF en las mutaciones,
> historial append-only y `mejor_costo` correctos.

## 0. Antes de escribir código
Leer `CLAUDE.md` (10, 11, 17, 26), `app/mercado/importar.py`, `app/mercado/proveedores.py`,
`app/api/routes/mesa.py` (importar y confirmar), `app/ingest/orchestrator.py` (`run_retencion`),
`app/mercado/territorio.py`, `app/api/templates/base.html` (menú Mesa), `tests/test_plat3.py`.

## 1. Cambios

### 1.1 La confirmación de importación falla con archivos sobre ~768 KB [V]
`/proveedores/importar` acepta hasta `MAX_BYTES = 5 MB`, pero la confirmación reenvía el archivo en base64 en un
campo de formulario, y Starlette 1.3.1 corta cada campo en 1 MB (`FormParser` y `MultiPartParser`,
`max_part_size = 1024 * 1024`, en `starlette/formparsers.py`). Con un archivo de 1 MB la vista previa funciona y
la confirmación devuelve 400. Corrección: `MAX_BYTES = 700 * 1024`, con el mensaje "El archivo supera 700 KB;
quita imágenes o formato, o divídelo". No subir `max_part_size`: es una defensa de memoria (Render, 512 MB).
Test: un archivo de 701 KB da 422 en la vista previa, y uno de 690 KB pasa la vista previa **y** la confirmación.

### 1.2 La purga puede borrar las CA de Ñuble si cambian las regiones de las empresas
Hoy `run_retencion` protege solo `regiones_activas`. Si la única empresa activa queda con regiones que no
incluyen la 16 (por un error en el formulario o por un cambio de foco), las CA terminales de Ñuble con más de
90 días se purgan y no se recuperan sin cuota. Corrección: en la purga, `regiones_protegidas =
sorted(set(regiones_activas(...)) | set(settings.mercado_regiones))`. La ingesta no cambia: sigue usando solo
`regiones_activas`. Test: una empresa activa con `[8]` → una CA terminal vieja de la región 16 no se purga.

### 1.3 Una lista de precios antigua pisa el precio actual
`guardar_producto` reemplaza `precio_neto_actual` aunque el `vigente_desde` nuevo sea **anterior** al vigente.
Al importar después una lista vieja, el "precio actual" retrocede, y `variacion` (que ordena por fecha) muestra
un actual distinto de la última fila. Corrección: si `vigente_desde < pp.vigente_desde`, insertar la fila en
`costo_historial` (si no existe ya una con la misma fecha y precio) y **no** tocar `precio_neto_actual` ni
`vigente_desde`. El estado devuelto es `'historico'`, y el resumen de importación lo cuenta aparte ("precios
históricos agregados"). Test: producto con precio 1.000 vigente el 2026-11-01 → se importa 900 con fecha
2026-09-01 → el actual sigue en 1.000, el historial tiene 2 filas y la variación contra el anterior es +11,1 %.

### 1.4 Menú Mesa con empresa inactiva (cosmético)
`base.html` muestra "Mesa" si `current_user.empresa_id` existe, aunque la empresa esté inactiva (después da
403). Pasar al contexto un booleano `tiene_empresa_activa` (con `empresa_activa_id`) y usarlo en el menú.

## 2. Tests y cierre
Los 4 tests de arriba. `ruff check .`, `python -m mypy app`, `python -m pytest -rs` (0/0/0) contra Postgres
local. Sin entrada en el changelog (no hay cambio visible salvo el mensaje de 700 KB). Commit:
`F-plat-3-fix: tope de importación de 700 KB, purga que protege el setting y precios históricos`.
`git add` solo de los archivos tocados.
