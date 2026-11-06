# Prompt — F-plat-purga-legado (respaldo, borrado del legado y compactación de Neon) · 7-nov-2026

> Para Claude Code. **Modelo: Opus**, porque son operaciones destructivas sobre producción. Un commit, **sin push**.
> **Sin migración.** Va en un script que **ejecuta Boris**: nada de esto corre en un job, en el deploy ni en los
> tests contra Neon.
> No toca `app/clients/`, ni la ingesta, ni las plantillas del rediseño.

## Contexto (verificado por Boris en Neon production el 6-nov)
- La base pesa **376 MB de 0,5 GB (75 %)**.
- La retención (`app/core/retencion.py`) **nunca borra filas**: solo vacía `raw_json` y los ítems. Además, una fila
  vaciada sigue ocupando disco hasta que corre un `VACUUM FULL`.

| Tabla | Tamaño | Filas | Nota |
|---|---|---|---|
| `compras_agiles` | 130 MB | 195.178 | **186.710 de otras regiones (74 MB de datos)**, 8.468 de Ñuble; 29.003 tuplas muertas |
| `licitaciones` | 59 MB | 43.136 | de 2008 a oct-2026; 14.995 con `raw_json` |
| `licitacion_items` | 28 MB | 149.864 | 12.204 tuplas muertas |
| `ca_productos` | 11 MB | 32.191 | |
| `oportunidades_match` | 8,9 MB | 21.183 | |

## Decisiones de Boris (6-nov)
1. **Licitaciones: borrarlas todas, con respaldo previo.** La app antigua queda archivada. Si algún día vuelve, se
   re-ingesta desde la API o se restaura el respaldo. Junto con ellas se van sus hijas y sus referencias:
   `licitacion_items`, `ofertas_competencia` (con CASCADE), y las filas con `fuente = 'licitaciones'` de
   `oportunidades_match` (sus `alertas` caen por CASCADE), `oportunidades_seguidas` y `match_feedback`.
2. **CA de otras regiones: borrar las no protegidas, con respaldo previo.** Ñuble y cualquier región activa no se
   tocan.

## 1. Criterio de las CA a borrar (función pura y testeable, en `app/core/retencion.py`)
Una CA se borra solo si cumple **todas** estas condiciones:
- `region` no está en `regiones_activas(...)`, o es NULL.
- Ya no está vigente: `estado` en `ESTADOS_TERMINALES`, o `fecha_cierre` anterior a ahora menos 90 días.
- No está en el conjunto protegido que ya usa `purgar_terminales`: match con alerta pendiente, seguida,
  `ca_trabajo`, `predicciones` u `ofertas_propias`.
- No la referencia **ninguna** de estas tablas: `lote_cas`, `ca_requisitos`, `ventas`, `ca_cotizaciones`,
  `ca_lineas`, `ca_anexos` o `evaluaciones_cache`. Algunas de estas FK no tienen CASCADE (`ca_trabajo`,
  `predicciones`, `ofertas_propias`, `lote_cas`, `ca_requisitos`, `ventas`). Otras sí tienen CASCADE, pero son datos
  del pivote que no se deben perder sin querer.

Al borrar una CA se borran también sus `ca_productos` (CASCADE). Además, en la misma transacción, sus filas de
`oportunidades_match` (las `alertas` caen por CASCADE) y de `match_feedback` con `fuente = 'compras_agiles'`.

## 2. Script `scripts/mantenimiento/purgar_legado.py`
Modos, siempre explícitos. Sin argumentos solo imprime la ayuda.

### `--medir`, solo lectura
Imprime:
- `pg_database_size`.
- Tamaño total, filas vivas y muertas de las tablas de la tabla de arriba.
- Cuántas filas borraría cada paso según el criterio de §1 y las decisiones.

### `--respaldar DIR`
- Escribe en `DIR/legado_AAAAMMDD/` un `CSV.gz` por tabla con exactamente las filas que se van a borrar, usando
  `COPY (SELECT …) TO STDOUT WITH CSV HEADER` y psycopg. No usa `pg_dump`, porque Boris está en Windows y quizá no
  lo tenga.
- Junto a los archivos deja un `manifiesto.json` con tabla, filas, sha256 y la consulta usada, y un `RESTAURAR.md`
  con los `COPY … FROM` en el orden correcto: primero los padres, después las hijas.
- Al terminar, lee de vuelta cada archivo y compara su número de filas con el manifiesto. Si no calza, falla.
- `DIR` no puede estar dentro del repo (se valida). Contiene `raw_json` con datos públicos, pero no se commitea.

### `--borrar --respaldo DIR/legado_AAAAMMDD`
- **Se niega a correr** si el manifiesto no existe, tiene más de 24 h o no calza con lo que hoy se borraría
  (± 0,5 %).
- Toma el mismo `pg_advisory_lock` de la ingesta (regla 13). Si no lo obtiene en 10 min, termina sin hacer nada.
- Borra por tandas de 5.000 códigos, con una transacción y un commit por tanda, y va registrando el avance. Respeta
  la regla 12: nunca carga en memoria más que los códigos de una tanda.
- Orden: primero las referencias de `fuente`, después las hijas y al final los padres.
- Es idempotente: si se corta, se puede volver a correr y sigue desde donde quedó.

### `--compactar`
- Corre `VACUUM (FULL, ANALYZE)` tabla por tabla: `licitacion_items`, `licitaciones`, `ofertas_competencia`,
  `ca_productos`, `compras_agiles`, `oportunidades_match`, `alertas` y `match_feedback`.
- Antes de cada tabla verifica que el espacio libre alcance: el tamaño que esa tabla tendrá después, más un 20 % de
  margen, tiene que caber bajo los 0,5 GB.
- Muestra el tamaño antes y después de cada tabla. Exige el advisory lock y solo corre **entre 22:00 y 07:00 hora de
  Chile** (regla 5, con `ZoneInfo("America/Santiago")`), porque `VACUUM FULL` bloquea la tabla mientras corre.

Comportamiento común a todos los modos:
- La URL de la base se lee de `DATABASE_URL`, que Boris exporta desde `DATABASE_URL_PROD` como en las otras fases.
- El script imprime el host al que se conecta y pide escribir `si` antes de `--borrar` y de `--compactar`.

## 3. Retención para que no vuelva a crecer
En `retencion.PURGAS` agregar la entrada `ca_fuera_territorio`. Hace el borrado de §1 **por fila**, con un tope
de 20.000 filas por corrida para no alargar el job, y deja el conteo `ca_fuera_territorio_borrados`.
Con eso, si alguna vez se reactiva la ingesta nacional, las CA ajenas al territorio se borran solas a los 90 días.
Las licitaciones no necesitan regla, porque su ingesta está apagada (regla 24).

## 4. App antigua
Revisar que «Archivo» (Perfiles, Mi registro, Explorar CA y Plan Anual) funcione con las tablas vacías o casi
vacías: sin errores 500 y con un estado vacío legible. En las pantallas de licitaciones, agregar una nota corta:
«Licitaciones archivadas el 7-nov-2026 (respaldo local)».

## 5. Tests y cierre
- Criterio de §1, con un caso por cada condición que protege una CA: región activa, NULL, vigente, seguida, alerta
  pendiente, trabajo, predicción, oferta, cotización, línea, anexo, lote, requisito y venta.
- Borrado por tandas, idempotente y reanudable.
- Respaldo: manifiesto, conteos y validación de `DIR` fuera del repo.
- Negativas del script: sin manifiesto, manifiesto viejo, manifiesto que no cuadra y fuera de horario para
  `--compactar`.
- Entrada de `PURGAS` con su tope.
- «Archivo» sin errores 500 con las tablas vacías.
- Los tests usan Postgres local (`TEST_DATABASE_URL`) y no la base de producción, como siempre.
- `ruff check .`, `python -m mypy app` y `python -m pytest -rs` (0 fallos, 0 errores, 0 saltados).
- Entrada en `app/changelog.py`, visible para el admin.
- Anotar en `docs/decisiones.md` las dos decisiones con fecha.
- Commit: `F-plat-purga-legado: respaldo, borrado de licitaciones y CA fuera del territorio, compactación`.

## 6. Pasos de Boris (de noche, en una sola sesión)
1. Revisar el diff, hacer push y esperar el deploy.
2. En cron-job.org, pausar **todos** los crons GH.
3. En PowerShell, exportar `DATABASE_URL` desde `DATABASE_URL_PROD` y correr `--medir`. Pegar el resultado.
4. `--respaldar D:\respaldos` (o la carpeta que elijas fuera del repo). Copiar esa carpeta también a la nube.
5. `--borrar --respaldo …`, y después `--compactar`, después de las 22:00.
6. `--medir` otra vez y anotar el antes y el después en `decisiones.md`. Revisar que /salud muestre el tamaño nuevo.
7. Reactivar los crons GH.
