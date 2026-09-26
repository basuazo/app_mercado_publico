# Prompt F-ca-rubro — detalles nocturnos de CA candidatas por rubro

> **Destino en el repo:** `docs/prompt-F-ca-rubro.md`
> **Fase:** F-ca-rubro (una fase, un commit) · **Migración:** ninguna (el contador diario vive en
> `sync_state`; ver §4) · **Dependencias nuevas:** ninguna
> **Orden (actualizado 26-sep):** EN PAUSA. Va después de **F-ca-explorar**, que resuelve el cuello
> de botella en esta versión con un explorador. Al retomar: leer `rubro_vocabulario` (creada por
> F-ca-explorar) en vez de calcular el vocabulario en cada corrida (§1), y priorizar en la cola
> nocturna las CA "posibles" de los `rubros_favoritos` de los usuarios.
> **Modelo:** **Opus**. Toca cuota, 429, ventana nocturna y el lock (reglas duras 3, 4, 5 y 13).
> **Decisiones de Boris (26-sep):** prefiltro por texto con vocabulario aprendido; tope inicial de
> **300 CA por rubro al día**, ajustable por setting; solo de noche.

Reglas del proyecto: CLAUDE.md completo, en especial 3–5, 10, 12, 13 y queries 100 %
parametrizadas. Español de Chile; entrada en `app/changelog.py`; `ruff check .`,
`python -m mypy app`, `python -m pytest` verdes; commit "F-ca-rubro: …" sin push; **nunca
`git add -A`**. Nada de `ruff format` masivo.

---

## Hechos que condicionan [V: Paso 0 del 26-sep, `data/paso0_ca_rubro.py` y `data/paso0_ca_vocabulario.py`]
- El match por rubro de CA (`_candidatos_ca`, `CaProducto.codigo_producto LIKE prefijo%`) solo ve
  CA con productos, y los productos solo llegan con el detalle. `detalles-match`
  (`_candidatas_detalles_match`) solo pide el detalle de lo que YA tiene match: círculo vicioso.
- Entran ~3.100 CA nuevas por día (21.724 en 7 días). Abiertas sin detalle: 14.735.
- `detalles-match`: 30,7 s por detalle de media en 7 días (511 intentados, 126 fallidos), pero usó
  solo ~37 min/día de los ~260 previstos. [I] ~70 % de ese tiempo fue en fallos crónicos, que
  F-detalles-fallos ya pone en espera.
- Cuota: máximo 3.606 requests/día (26-sep) de 9.000. La cuota no es el cuello; el tiempo de lock sí.
- Plazo publicación → cierre de CA: p10 24 h, p50 47 h, p90 119 h.
- Prefiltro por texto sobre el **nombre** de la CA (lo único que trae el listado v2), con
  vocabulario por prefijo aprendido de `licitacion_items` (tienen UNSPSC y nombre): con 10 términos
  por prefijo y lift ≥ 10, recall 91 % sobre las 80 CA que sí calzan, precisión 7 %, elige el 74 %
  → ~860 detalles/día. Con 5 términos: recall 61 %, ~660/día. Causa: términos comunes en nombres
  de CA ("agua", "central", "control", "salud"). El lift del Paso 0 se midió contra
  `licitacion_items`, no contra nombres de CA.
- Hoy el único perfil con rubros es el 8, **de prueba** (30 prefijos, sin regiones ni montos, sin
  keywords). Sirve de canario; la fase es para los perfiles reales que usen rubros.

## Paso 0 — ajustar el vocabulario antes de programar (pegar resultados al final)
Extender `data/paso0_ca_vocabulario.py` (gitignored, solo lectura en producción, lo corre Boris):
1. Lift contra **nombres de CA de los últimos 30 días**, no contra `licitacion_items`: es la
   población que se filtra y castiga las palabras comunes en CA.
2. Vocabulario con los lexemas de Postgres (`unnest(to_tsvector('spanish',
   inmutable_unaccent(nombre)))`, ver §1), no con la normalización en Python del script.
3. Tabla k × lift con recall, precisión y detalles/día. Elegir la combinación de mayor recall con
   costo ≤ 300/día; si ninguna llega a recall ≥ 60 %, detenerse y reportar.
4. Repetir la consulta 7 de `data/paso0_ca_rubro.py` (s por detalle con F-detalles-fallos activo).

## Qué construir

### 1. Vocabulario por rubro (`app/matching/vocabulario_rubro.py`, sin estado en disco)
- `vocabulario_por_prefijo(session, prefijos, k, lift_min) -> dict[str, list[str]]`. Por prefijo:
  lexemas de `to_tsvector('spanish', inmutable_unaccent(li.nombre))` de los `licitacion_items`
  con `codigo_producto LIKE :prefijo || '%'`, vía `unnest(tsvector)` y `GROUP BY lexeme` (sin
  `ts_stat`: no admite parámetros). Frecuencia en el rubro ≥ 3 y lift ≥ `lift_min` contra la
  frecuencia en nombres de CA de 30 días; top `k` por frecuencia en el rubro.
- Se calcula al inicio de cada corrida que lo usa (unos miles de filas; lotes si crece, regla 12).
  Sin tabla nueva: el vocabulario es derivado y se recalcula barato.
- Lexemas validados con `^[a-zñ]+$` antes de armar la tsquery; la tsquery va como parámetro a
  `to_tsquery('simple', :q)` contra el mismo `to_tsvector('spanish', inmutable_unaccent(nombre))`
  del resto del motor (los lexemas ya están stemizados).

### 2. Cola "candidatas por rubro"
`_candidatas_rubro(session, ahora, ...)`: CA con `estado = publicada`, sin detalle
(`_sin_detalle`), **sin** match, vigentes según `es_vigente` (incluye la regla de 7 días para CA
sin cierre), que para **algún perfil activo con rubros y con `compras_agiles` en `fuentes`**
cumplen sus regiones y montos, y cuyo nombre calza con el vocabulario de sus prefijos. Excluir
las que estén en espera por fallos (`_espera_detalle`). Orden: `fecha_cierre` ascendente, sin
fecha al final. Una query parametrizada por perfil como máximo (son 3–10 usuarios).

### 3. Integración en `run_detalles_match`
- Primero la cola actual (con match), sin cambios. **Después**, solo si
  `en_ventana_nocturna()` (regla 5), la cola por rubro, dentro del MISMO presupuesto de minutos.
- Tope diario `DETALLES_RUBRO_MAX_DIA` (default 300) y guarda de cuota
  `DETALLES_RUBRO_RESERVA_CUOTA` (default 3000): si `QuotaTracker.remaining()` baja de la reserva,
  la cola por rubro no arranca o se corta. La cola con match no cambia.
- Mismo manejo de fallos y errores de canal que hoy (`_registrar_fallo_detalle`,
  `_ERRORES_DE_CANAL`, enfriamiento de 60 s tras 504/timeout).
- Resultado de la corrida con claves nuevas: `rubro_candidatas`, `rubro_intentados`,
  `rubro_guardados`, `rubro_tope_dia`, `rubro_hechos_hoy`.
- Settings nuevos (`DETALLES_RUBRO_MAX_DIA`, `DETALLES_RUBRO_RESERVA_CUOTA`, `RUBRO_VOCAB_K`,
  `RUBRO_VOCAB_LIFT`) **opcionales** en `_job.yml`, con default en `settings.py` (no repetir el
  error de `DIGEST_HOUR`/`TASA_*`).

### 4. Contador diario persistido (regla 10, sin migración)
Fila de `sync_state` con `fuente = 'detalles-rubro'`: `requests_usadas_hoy` = detalles por rubro
intentados en el día y `fecha_contador` = fecha en `America/Santiago`; se reinicia al cambiar el
día calendario de Chile. Incrementar en la misma transacción que registra el resultado del detalle.

### 5. Que el match llegue al resumen de las 08:30
Hoy `nocturno` termina en `detalles-match` y el siguiente `match` es a las 08:50: lo bajado de
noche se perdería el resumen del día. Agregar `run_match` al final de `_ciclo_nocturno`
(sin llamadas a la API, `match_perfil` no usa HTTP) y subir `timeout_min` de `nocturno.yml` solo
si hace falta. Documentar en `docs/operacion-disparos.md`.

### 6. Salud
`/salud` muestra en la tarjeta de detalles: candidatas por rubro, hechos hoy / tope, reserva de cuota.

## Tests (mínimo, red mockeada con respx)
- Vocabulario: con ítems fixture, los términos comunes en nombres de CA quedan fuera por lift; el
  prefijo `85` no captura ítems `58…`; lexema con caracteres raros se descarta.
- Cola por rubro: excluye CA con match, con detalle, no vigentes (incluida la de 8 días sin
  cierre), fuera de región o monto del perfil, perfiles inactivos o sin `compras_agiles`, y las
  en espera por fallos; orden por cierre.
- Integración: de día no corre; de noche corre después de la cola con match; respeta el tope diario
  (persistido: dos corridas el mismo día suman), se reinicia al cambiar el día de Chile, corta con
  la reserva de cuota, y un 429 de canal corta todo sin sumar fallos.
- Ownership: una CA candidata por el perfil de un usuario no genera match para otro (regla 17).
- `run_match` al final del nocturno: una CA bajada por rubro queda con match antes del resumen.
- SQLite y Postgres: el FTS es solo Postgres. Los tests de vocabulario y de la query de cola usan
  el fixture de Postgres de `tests/conftest.py` (se saltan sin `DATABASE_URL`, como
  `tests/test_matching.py`); la lógica de tope, reserva y ventana se prueba en SQLite con la
  función de vocabulario inyectada. Correr la suite con `DATABASE_URL` de dev exportada para que
  no queden skipped.

## Después del deploy (Boris)
Push justo después de un `ca` de los :05. A la mañana siguiente: resultado del `nocturno`
(`rubro_*`), cuántos matches nuevos del perfil 8 salieron por `campo_hit = producto`/rubro, y la
cuota del día. Si hay margen de tiempo y cuota, subir `DETALLES_RUBRO_MAX_DIA` en las variables de
GitHub sin tocar código.

## Fuera de alcance
Detalles por rubro de día; licitaciones (ya tienen ítems por datos abiertos); tocar el motor de
score; sugerir keywords desde el vocabulario en `/perfiles` (buena idea para la mejora de perfiles).

*Fuente de los datos de dominio: Dirección ChileCompra.*
