# Prompt F-ajustes — vigencia única en el matching y dos arreglos de "Descartar y excluir"

> **Destino en el repo:** `docs/prompt-F-ajustes.md` (Cowork, 02-oct, revisado contra `176f168`)
> **Fase:** F-ajustes (una fase, un commit) · **Migración:** NO · **Dependencias nuevas:** ninguna
> **Orden:** después de F-guardar (`176f168`, en producción, cabeza `c9e4b2f7a1d8`). Independiente de
> F-registro (puede ir antes o después; si F-registro ya entró, rebasar sobre ella sin conflicto
> esperado: F-registro no toca `engine.py` ni `perfiles.py`). Si `alembic heads` no da
> `c9e4b2f7a1d8`, detenerse y avisar.
> **Modelo:** Sonnet. Sin API, sin cuota nueva (la baja), sin borrar datos.
> **Origen:** pendientes de la auditoría de F-guardar (02-oct) + hallazgo del Paso 0 de F-guardar
> (vigencia de CA del matching ≠ explorador).

Reglas del proyecto: CLAUDE.md completo, en especial 6, 12 (512 MB), 17 (ownership), 18 (CSRF),
queries parametrizadas y tsquery siempre con `inmutable_unaccent` (`_tsq`). Español de Chile; entrada
en `app/changelog.py`; `ruff check .`, `python -m mypy app`, `python -m pytest -rs` verdes **con
`DATABASE_URL` de dev** (`postgresql+psycopg://`, host `ep-dawn-sunset`; 0 saltados); commit
"F-ajustes: …" sin push; **nunca `git add -A`**; nada de `ruff format` masivo.

---

## Hechos que condicionan [V, código en `176f168`]
**A. Dos definiciones de "vigente".**
- Oficial (`app/core/vigencia.py`): `es_vigente` en Python y `condicion_ca_vigente(ahora)` en SQL
  (con test de equivalencia en `tests/test_ca_explorar.py`): familia ABIERTA o DESCONOCIDO con cierre
  futuro, o CA **sin cierre**, ABIERTA, publicada hace ≤ `CA_SIN_CIERRE_VIGENCIA_DIAS = 7` días. La
  usan el feed (`query.py:558`), el resumen por correo (`email.py:370`) y el explorador.
- Matching (`app/matching/engine.py`): `_vigencia_lic` = `estado == 'publicada'` y cierre futuro;
  `_vigencia_ca` = `estado == 'publicada'` y (cierre futuro **o cierre NULL, sin tope de antigüedad**).
  La usan `_candidatos_licitaciones`/`_candidatos_ca` (recall, con `.limit(_MAX_CANDIDATOS = 500)`
  **sin ORDER BY**) y `_where_limpieza` (limpieza de F-guardar).
- `_candidatas_detalles_match` (`app/ingest/orchestrator.py:216`) tiene una tercera copia
  (`estado == publicada` y cierre NULL o futuro).
- Paso 0 de F-guardar (01-oct): **17.273 CA "vigentes" para el matching vs ~7.800 para el
  explorador.** Consecuencias: (1) CA sin cierre de hace semanas siguen entrando como candidatas; con
  el tope de 500 sin orden, pueden **desplazar vigentes reales** en perfiles amplios; (2)
  `detalles-match` gasta cuota en CA que ya no se pueden postular; (3) el feed igual las esconde
  (usa `es_vigente`), así que el trabajo es en vano.

**B. "Deshacer exclusión" corre el matching dentro de la petición.** `perfil_deshacer_exclusion`
(`pages.py`, ~línea 1228) llama `match_perfil(perfil, session)` antes de redirigir. En Render free
son segundos de espera. Al editar un perfil ya se usa `BackgroundTasks` +
`_match_perfil_background(engine, perfil_id)` (`pages.py:146`).

**C. Una exclusión puede vaciar el perfil.** `palabras_sugeridas` (`app/matching/perfiles.py:230`)
descarta las keywords del perfil por igualdad de texto sin tildes, pero no por **raíz**: en un perfil
con "salud", sugiere "saludable" (`websearch_to_tsquery('spanish', 'saludable')` = `'salud'`) y
excluirla saca todo lo que traía "salud". `excluir_palabras` y el formulario de `/perfiles` tampoco lo
impiden. Hay vista previa y Deshacer, pero es fácil equivocarse.

## Paso 0 (solo lectura, Boris contra producción, ANTES de escribir código)
`data/paso0_ajustes.py` (gitignored, ya escrito; reusa `condicion_ca_vigente`, `_criterio_*` y
`criterio_perfil` del código). Salida en `data/logs/ajustes.txt`. Mide: vigentes por fuente (regla
actual del matching → oficial); por perfil, candidatos que pasan su criterio con cada regla (marca si
hoy supera 500); matches de CA que salen del alcance; cola de `detalles-match` de CA (actual → oficial).
Pegar la salida al final de este archivo.

---

## Qué construir

### 1. Una sola vigencia en el matching y en detalles-match
- En `app/core/vigencia.py`, agregar `condicion_lic_vigente(ahora)` (la regla de `es_vigente` para
  licitaciones en SQL: cierre no nulo y futuro, familia ABIERTA o DESCONOCIDO — mismo patrón que
  `condicion_ca_vigente`) y extender el test de equivalencia Python↔SQL de `test_ca_explorar.py` (o
  uno nuevo en `tests/test_vigencia.py`) a licitaciones.
- `_vigencia_lic(ahora)` → `[condicion_lic_vigente(ahora)]`; `_vigencia_ca(ahora)` →
  `[condicion_ca_vigente(ahora)]`. Cambia a la vez recall, limpieza y (por ende) `contar_limpieza`.
- `_candidatas_detalles_match`: reemplazar su condición propia por las mismas dos funciones.
- Recall: agregar orden determinista antes del `.limit(_MAX_CANDIDATOS)`:
  `fecha_cierre ASC NULLS LAST, codigo` (lo que cierra antes primero; lo mismo que prioriza
  `detalles-match`). Así, si un perfil supera 500, se quedan fuera las de cierre más lejano, no al azar.
- **No se borran** los matches de CA que dejan de ser vigentes: quedan como no vigentes (la limpieza
  no los toca, el feed ya no los mostraba, F-registro los verá como vencidas solo si caen en su
  ventana). Documentarlo en el docstring de `limpiar_matches_perfil`.

### 2. "Deshacer exclusión" en segundo plano
`perfil_deshacer_exclusion` recibe `background_tasks: BackgroundTasks`, quita las palabras, hace
commit y agenda `_match_perfil_background(request.app.state.engine, perfil_id)`, igual que
`/perfiles/{id}/editar`. El aviso del feed tras deshacer dice "Restauramos la exclusión; las
oportunidades vuelven en unos segundos".

### 3. No excluir lo que el propio perfil busca
- Función nueva en `app/matching/engine.py`:
  `exclusiones_que_chocan(session, keywords, palabras) -> list[str]`: devuelve las `palabras` cuya
  tsquery de exclusión **calza con alguna keyword del perfil**:
  `to_tsvector('spanish', inmutable_unaccent(:keyword)) @@ <_tsq(':palabra')>` (una query con
  `unnest` de ambos arreglos, parametrizada). Ejemplos esperados: keyword "salud" × "saludable" →
  choca; "salud" × "salud mental" → no choca (exige ambas palabras); "eléctrico" × "eléctricos" →
  choca (misma raíz con tilde); "construcción" × "ferretería" → no choca. En SQLite (tests sin Postgres) devuelve `[]` y lo
  dice el docstring.
- Usarla en tres lugares:
  - `palabras_sugeridas`: recibe `session` y filtra las que chocan (una sola llamada para todas).
  - `excluir_palabras` y la vista previa: si alguna choca, **no agregar nada** y responder con un
    mensaje claro: "«saludable» sacaría todo lo que trae «salud» en este perfil; elige otra palabra".
    La ruta `descartar-y-excluir` devuelve 400 con ese texto (o re-renderiza el modal con el mensaje;
    no descarta si no excluye).
  - Formulario de `/perfiles` (crear y editar): misma validación → `PerfilInvalido` con el mensaje
    (se muestra como los demás errores del formulario). Las exclusiones **ya guardadas** que chocan no
    se tocan; solo se avisa al editar si se intentan volver a guardar.

### 4. Changelog
"Tus perfiles buscan solo en lo que todavía se puede postular (las Compras Ágiles sin fecha de
cierre cuentan una semana desde que se publican, igual que en Explorar CA). Ya no se puede excluir una
palabra que borraría lo que el mismo perfil busca (por ejemplo, 'saludable' en un perfil de 'salud').
Deshacer una exclusión ya no hace esperar. Fuente: Dirección ChileCompra."

---

## Tests (mínimo)
- **Vigencia (Postgres):** `condicion_lic_vigente` = `es_vigente` sobre una matriz (publicada/
  desconocido/cerrada × cierre pasado/futuro/NULL); `_candidatos_ca` ya no trae una CA sin cierre
  publicada hace 10 días y sí una de hace 2; `limpiar_matches_perfil` no borra el match de esa CA de
  hace 10 días (queda fuera del alcance) aunque ya no calce; `_candidatas_detalles_match` deja fuera la
  de hace 10 días.
- **Orden del tope:** con `_MAX_CANDIDATOS` parcheado a 2 y tres candidatas, entran las 2 de cierre más
  próximo.
- **Deshacer en background:** la ruta responde 303 sin llamar a `match_perfil` en la petición (mock) y
  agenda la tarea; las palabras quedan quitadas; CSRF y ownership.
- **Choques (Postgres):** los cuatro ejemplos de §3; `palabras_sugeridas` no ofrece "saludable" en un
  perfil "salud"; `excluir_palabras` con una palabra que choca no modifica el perfil y devuelve el
  error; la vista previa muestra el mensaje y no un número; crear/editar perfil con "salud" + excluir
  "saludable" → error de formulario. Si algún ejemplo no da lo esperado, el stemmer manda: ajustar
  el ejemplo (no la función) y anotarlo en el resumen.
- Los tests de F-guardar (`test_guardar*.py`), `test_matching*.py` y `test_ca_explorar*.py` siguen
  verdes; adaptar fixtures que sembraban CA sin cierre antiguas como "vigentes" (no borrar cobertura).

## Fuera de alcance
Cambiar `CA_SIN_CIERRE_VIGENCIA_DIAS` · borrar matches de no vigentes · cambiar el tope de 500 ·
revisar exclusiones ya guardadas en producción (solo se previene hacia adelante) · F-registro.

## Despliegue (para Boris, después de la auditoría)
Sin migración: push a cualquier hora. En el siguiente `ciclo-match`, revisar en el log que
`match_todos` no da error y que `borrados` es bajo (la limpieza ahora mira menos CA); en el siguiente
`detalles-match`, que la cola de CA baje según el Paso 0 punto 4.

*Fuente de los datos de dominio: Dirección ChileCompra.*

---

## Resultado del Paso 0 (producción, 02-oct, `data/logs/ajustes.txt`) [V]
- **Vigentes, regla actual del matching → oficial:** Compras Ágiles **15.106 → 6.762** (−55 %);
  licitaciones 4.393 → 4.393 (sin diferencia hoy; `condicion_lic_vigente` se agrega igual, por
  coherencia y para que un estado `desconocido` con cierre futuro no quede fuera).
- **Candidatos por perfil (lic · CA):** P5 117 · 349 → 247; P6 83 · 263 → 160; P7 10 · 130 → 78;
  P8 62 · 103 → 92; **P9 «Arcana Cultural» 352 · 560 → 458**. Hoy **el perfil 9 supera el tope de
  500 en CA**: con el `limit` sin orden, 60 candidatas quedan fuera al azar, y pueden ser vigentes
  reales. Con la regla oficial baja a 458 (bajo el tope) y el orden por cierre cubre el caso si vuelve
  a crecer.
- **Matches de CA que salen del alcance:** 388 (no se borran; el feed ya no los mostraba).
- **Cola de `detalles-match` de CA:** 179 → 122 (57 detalles menos gastando cuota en CA que ya no se
  pueden postular).
Conclusión: la fase se justifica; sin decisiones abiertas.
