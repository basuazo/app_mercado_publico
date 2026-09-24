# Prompt F-estados-vencidos — que el estado de las licitaciones deje de quedarse pegado

> **Destino en el repo:** `docs/prompt-F-estados-vencidos.md`
> **Fase:** F-estados-vencidos (una fase, un commit) · **Migración:** ninguna · **Dependencias nuevas:** ninguna
> **Orden:** 1.º de la serie vigencia → guardar → registro → modal (ver `docs/00-estado-actual.md`, 24-sep).
> **Origen:** revisión en vivo 24-sep: la licitación 1417913-96-L126 figura "Abierta" con "Cerró el 22/07".

Reglas del proyecto: CLAUDE.md completo, en especial 3 (cuota), 5 (masivo solo 22:00–07:00 Chile,
validado en código), 6 (parseo defensivo), 10 (estado en Postgres), 12 (lotes, 512 MB). Español de
Chile; entrada en `app/changelog.py` en el mismo commit; `ruff check .`, `python -m mypy app`,
`python -m pytest` verdes; commit "F-estados-vencidos: …"; **nunca `git add -A`**.

---

## Diagnóstico [V, código, 24-sep]
- `refresh_estados` (`app/ingest/lifecycle.py`) re-consulta por código SOLO licitaciones/CA no
  terminales con `fecha_cierre` entre −7 y +3 días, más las seguidas. Tope 100 requests, corre en
  `nocturno`.
- `sync_activas` hace upsert de lo que viene en `estado=activas`; lo que **sale** del listado no se
  toca. Resultado: una licitación que cerró hace más de 7 días y nadie sigue queda con su último
  estado (normalmente `publicada`) para siempre.
- `cerrada` NO es terminal (`ESTADOS_TERMINALES` = adjudicada, cancelada, desierta, revocada): una
  licitación que cerró y se adjudica 3 semanas después tampoco se entera.
- Hay una fuente de **cuota cero** que ya bajamos cada noche: los ZIP mensuales `lic-da`
  (`app/clients/datos_abiertos.py`, `docs/04-datos-abiertos.md` §2). Traen `CodigoExterno` y
  `CodigoEstado` por fila. El mes en curso y el anterior se republican al menos a diario (§ de
  frecuencia del doc 04).
- CA: el ciclo horario `ca` baja cambios por `fecha_ultimo_cambio`, así que un cambio de estado
  de CA debería entrar solo. **[I] no verificado**: medirlo en el Paso 0.

## Paso 0 — medir en producción (solo lectura) y pegar el resultado al final de este archivo
Contra `DATABASE_URL_PROD`, en una transacción `READ ONLY`, sin imprimir la URL:
1. Licitaciones por estado con `fecha_cierre < now() - interval '7 days'` y estado no terminal.
   Lo mismo solo para las que tienen fila en `oportunidades_match`.
2. Lo mismo para `compras_agiles`.
3. De (1), cuántas tienen `fecha_cierre` en el mes en curso o en los dos anteriores (lo que cubren
   los ZIP que ya bajamos).
4. `1417913-96-L126`: estado, estado_codigo, fecha_cierre, actualizado_en.
Si (2) da ~0, la parte CA de esta fase se reduce a un test que lo fije.

## Qué construir
1. **Estados desde datos abiertos (cuota 0).** En el mismo recorrido nocturno que ya descarga los
   ZIP `lic-da`, leer `CodigoExterno` + `CodigoEstado` (primera fila por código: el CSV repite la
   licitación por ítem × oferta) y actualizar `estado`/`estado_codigo` de licitaciones propias **no
   terminales** cuyo código aparezca con otro estado. Reusar `estado_licitacion()` (desconocido →
   `DESCONOCIDO` + log, regla 6). Por lotes con `commit_con_retry`; nunca un ZIP entero en memoria
   (usar el streaming existente). No retroceder un estado terminal a uno abierto.
   Si el ZIP del mes ya se descargó en esta corrida para ítems o competencia, **no bajarlo dos
   veces**: pasar la ruta.
2. **Rezagadas por API (fallback con tope).** Tras (1), las licitaciones con match, no terminales,
   con `fecha_cierre < ahora − 7 días` que siguen igual → detalle por código, priorizando las más
   recientes, con tope nuevo `ESTADOS_VENCIDOS_MAX_REQUESTS` (default 150) en `settings.py`,
   opcional en `_job.yml` (con default, NO obligatoria: ya tuvimos ese problema con `TASA_*`).
   Solo dentro de 22:00–07:00 Chile (regla 5, validado con `ZoneInfo`). Mismo manejo de 429/504 que
   `refresh_estados` (regla 3).
3. **Registro**: `SyncState`/`job_runs` con cuántas se actualizaron por DA, cuántas por API y
   cuántas quedaron rezagadas. Visible en `/salud`.
4. **No tocar** la ventana de `refresh_estados` (−7/+3): sigue siendo la vía rápida cerca del cierre.

## Tests (mínimo)
- DA: fila con estado 8 actualiza una `publicada` a `adjudicada`; una ya terminal no retrocede; una
  licitación que no es nuestra se ignora; estado desconocido → `DESCONOCIDO` + log.
- Código repetido en varias filas del CSV → una sola actualización.
- Fallback: respeta el tope, no corre fuera de 22:00–07:00 (reloj inyectado), corta ante 429 como
  `refresh_estados`.
- Idempotencia: correr dos veces no cambia nada la segunda.
- Red siempre mockeada (respx); el ZIP de test es un fixture pequeño generado en el test.

## Después del deploy (Boris)
Correr `nocturno` a mano en Actions y repetir la consulta (1) del Paso 0: debe bajar. Confirmar que
1417913-96-L126 dejó de estar `publicada`.

*Fuente de los datos de dominio: Dirección ChileCompra.*
