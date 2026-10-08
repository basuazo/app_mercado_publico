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

---

## Resultado del Paso 0 [V, producción READ ONLY, 24-sep-2026 21:35 UTC]
Total: 28.825 licitaciones, 19.636 no terminales.
1. Licitaciones no terminales con `fecha_cierre` hace más de 7 días: **11.632** — cerrada 6.673,
   publicada 4.544, desconocido 415. Con match: **1.852** — publicada 1.008, cerrada 829, desconocido 15.
2. CA no terminales vencidas: 99 (todas `cerrada`); **con match: 0** → la parte CA queda en un test.
   Hallazgo de código: `sync_incremental` lista solo publicada/cerrada/proveedor_seleccionado, así que
   una CA que pasa a desierta o cancelada no llega nunca por `ca` (explica las 99). Sin match, no se toca.
3. Por mes de `fecha_cierre` (de 1): 2026-09 3.099, 2026-08 738, 2026-07 7.265, 2026-06 530. Todas
   tienen `fecha_publicacion` NULL. **Cobertura real medida contra lic-da 2026-6..9: 10.776 de 11.632**
   aparecen (1.715 de las 1.852 con match; quedan 137 con match para la API, bajo el tope de 150).
4. `1417913-96-L126`: publicada, estado_codigo 5, fecha_cierre 2026-07-22, actualizado_en 2026-07-17.
   En `lic-da/2026-7.zip` figura **8 = Adjudicada** (FechaAdjudicacion 2026-09-04).

**Hallazgo que cambia el diseño [V]:** `CodigoEstado` de lic-da NO usa los códigos v1 (15 = Revocada,
16 = Suspendida, 9 = Adjudicada, 11–14 = Cerrada; no hay 5). Reusar `estado_licitacion()` habría dejado
revocadas y variantes como DESCONOCIDO: se agregó `estado_licitacion_da()` y `estado_codigo` guarda el
código v1 equivalente. Detalle en `docs/04-datos-abiertos.md` §8.

*Fuente de los datos de dominio: Dirección ChileCompra.*

---

## Auditoría de `d984432` (Cowork, 24-sep) — corregir antes del push

Veredicto: el diseño y el hallazgo de los códigos de datos abiertos están bien (21 tests pasan
también acá). Hay tres problemas de lógica que conviene corregir en este mismo commit (`--amend`,
no está pusheado):

1. **Un código desconocido de datos abiertos pisa un estado conocido.** `_es_avance` devuelve
   True si `nuevo` es DESCONOCIDO, y el test `test_estado_desconocido_queda_desconocido_y_se_loguea`
   lo fija: una `publicada`/`cerrada` pasa a `desconocido` con `estado_codigo` NULL. La regla 6 pide
   no romper la ingesta con un desconocido, no borrar un dato bueno con uno malo. Corregir: si
   `nuevo` es DESCONOCIDO → no aplicar, contar y loguear (como hoy), y **no** marcarla como vista
   (queda para el fallback por API). Si el ACTUAL es DESCONOCIDO → cualquier estado conocido gana
   (eso está bien). Cambiar el test a la nueva expectativa.
2. **Visto en datos abiertos ≠ resuelto.** `vistas_da` saca del fallback por API toda licitación
   que apareció en lic-da, aunque su estado ahí no sea terminal. Según `docs/04` (frecuencia), solo
   el mes en curso y el recién cerrado se republican; los meses más viejos del recorrido pueden estar
   congelados. Una licitación que en un ZIP congelado dice `cerrada` nunca se adjudicaría: datos
   abiertos no avanza y la API la salta. Corregir: excluir del fallback solo las que datos abiertos
   dejó en estado **terminal**. Test: vista en lic-da como cerrada → sigue elegible para la API.
3. **El tope de la API se gasta siempre en las mismas.** `_rezagadas` ordena por `fecha_cierre desc`:
   una licitación que sigue legítimamente `cerrada` (esperando adjudicación) se re-consulta cada
   noche y las más viejas nunca entran en las 150. Corregir: ordenar por `actualizado_en asc`
   (la menos refrescada primero) y desempatar por `fecha_cierre desc`; o excluir las consultadas
   en los últimos 7 días. Test: con tope 1 y dos rezagadas, noches consecutivas consultan las dos.

Menor (opcional): `actualizadas_api` cuenta consultas, no cambios de estado; renombrar a
`consultadas_api` o contar aparte `cambiadas_api`, para que `/salud` no sobrestime.

Anotado para F-organismo-lic, no tocar acá: `upsert_detalle` → `upsert_basica` asigna
`codigo_organismo = None` en cada detalle (el parser lo lee del nivel equivocado). Hoy es inofensivo
porque ya viene vacío también del listado, pero ese arreglo tiene que cubrir los dos caminos.

Tras las correcciones: `ruff`, `mypy`, `pytest`, `git commit --amend` y volver a traer el resumen.

**Aplicado (24-sep, mismo commit):**
1. `_es_avance`: un DESCONOCIDO de lic-da ya no se aplica (se cuenta y loguea); un actual DESCONOCIDO
   sí se reemplaza por cualquier estado conocido. Tests: `test_estado_desconocido_no_pisa_uno_conocido`,
   `test_conocido_reemplaza_a_desconocido`.
2. `sync_estados_datos_abiertos` devuelve solo las que dejó **terminales**; el resto sigue elegible
   para la API. Test: `test_vista_en_da_como_cerrada_sigue_elegible_para_la_api`. (Nota [V]: el 24-sep
   los 4 meses tenían Last-Modified del día, ver doc 04 §8; el arreglo vale igual si eso cambia.)
3. `_rezagadas` ordena por `actualizado_en asc, fecha_cierre desc`. Test: `test_el_tope_rota_entre_noches`.
- Menor: `actualizadas_api` → `consultadas_api` + `cambiadas_api` (resultado, log y notas de /salud).
