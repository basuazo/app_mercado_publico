# Prompt F-guardar — Guardar (= seguir) en una sola acción, también para CA sin match, y matches que se limpian

> **Destino en el repo:** `docs/prompt-F-guardar.md` (revisado por Cowork el 01-oct contra `4f797a5`;
> la versión del 24/27-sep queda en git, `b725e84`).
> **Fase:** F-guardar (una fase, un commit) · **Migración:** SÍ (Alembic, **solo datos**, sobre
> `b5d1f8a3c6e2`) · **Dependencias nuevas:** ninguna
> **Orden:** después de F-acentos (`4f797a5`, en producción) y antes de F-registro. Si `git log -1`
> no es `4f797a5` (o un commit de docs encima) o `alembic heads` no da `b5d1f8a3c6e2`, detenerse.
> **Modelo:** **Opus** (borra filas: matches y feedback). Sin llamadas a la API ni cuota nuevas.
> **Decisión de Boris (24-sep):** unificar. Guardar = queda en tu registro personal **y** te avisa
> de sus cambios. Las guardadas vigentes siguen en el dashboard con la etiqueta "Guardada".
> **Decisiones de Boris pendientes:** ver "Decisiones abiertas" al final; si no están contestadas en
> este archivo, usar la opción marcada **(por defecto)**.

Reglas del proyecto: CLAUDE.md completo, en especial 6 (no descartar por falta de dato), 11
(retención), 14 (≤ 250 correos/día), 17 (ownership), 18 (CSRF) y queries 100 % parametrizadas.
Toda tsquery con `inmutable_unaccent` (F-acentos: reusar `_tsq` / los fragmentos de
`app/matching/engine.py`, nunca escribir una tsquery nueva a mano). Español de Chile; entrada en
`app/changelog.py`; `ruff check .`, `python -m mypy app`, `python -m pytest` verdes **con
`DATABASE_URL` de dev (`postgresql+psycopg://`, host `ep-dawn-sunset`)**; si no ves la variable,
deja todo listo y Boris corre `pytest`. Commit "F-guardar: …" sin push; **nunca `git add -A`**;
nada de `ruff format` masivo.

---

## Hechos que condicionan [V, código en `4f797a5`]
**Modelo de datos (no cambia de esquema):**
- `OportunidadSeguida` (`oportunidades_seguidas`) y `MatchFeedback` (`match_feedback`) se indexan por
  **(usuario, fuente, código)**, no por match. Ya pueden existir para una CA sin match.
- `OportunidadMatch.alertas` y `OportunidadSeguida.alertas` tienen `ON DELETE CASCADE`. Borrar un
  match NO toca seguidas ni feedback.
- "Me sirve" = `MatchFeedback.valor='sirve'` (`app/matching/feedback.py::alternar_me_sirve`). No lo
  lee el matching ni el score. "Descartar" = `valor='descarte'`; el feed y el explorador
  (`app/explorador_ca.py:265`) ya excluyen las descartadas del usuario.
- "Activar alertas" = `OportunidadSeguida` (`app/matching/seguimiento.py`). Las alertas de seguidas
  (`app/alerts/detector.py`: `detectar_cambio_estado_seguidas`, `detectar_recordatorio_cierre_seguidas`)
  **no dependen del match**: hacen join directo con `licitaciones`/`compras_agiles`.

**Lo que hoy ata todo al match (y hay que cambiar):**
1. `check_oportunidad_access` (`app/api/query.py:887`) devuelve el match del usuario o None, y
   **todas** las rutas de acción (`/seguir`, `/me-sirve`, `/descartar`, `/deshacer-descarte`, en
   `app/api/routes/pages.py:890–987`) responden 404 sin match. Por eso una CA del explorador sin match
   no se puede seguir ni descartar (~97 % de las CA del explorador).
2. La ficha (`oportunidad_detalle`, `pages.py:738`) abre una CA sin match en **solo lectura**
   (`oportunidad.html:193–213`, `{% if match is none %}`); una **licitación** sin match da 404.
3. `_render_card_partial` (`pages.py`) usa `get_item_oportunidad`, que necesita match: sin match
   devuelve cuerpo vacío.
4. `lifecycle._rezagadas` (`app/ingest/lifecycle.py:183`) solo refresca licitaciones **con match**; y
   `_candidatas_detalles_match` (`app/ingest/orchestrator.py:216`) solo baja detalle de oportunidades
   **con match**. Una guardada sin match no se refrescaría al vencer ni tendría productos en la ficha.
5. `purgar_terminales` (`app/core/retencion.py`) protege solo oportunidades con alerta **pendiente**:
   una guardada terminal pierde `raw_json` e ítems/productos a los 90 días.

**Matches que nunca se borran [V]:** ningún código hace `DELETE` de `oportunidades_match`.
`match_perfil` hace upsert de hasta `_MAX_CANDIDATOS = 500` candidatos vigentes y nunca elimina los
que dejaron de calzar (ni al editar el perfil en `/perfiles`, ni al agregar una exclusión). Caso real
(01-oct, Paso 0 de F-acentos): el perfil 5 sigue mostrando CA con «desratización» aunque la excluye.
Ojo: `_rezagadas` y `detalles-match` usan "tiene match" como señal; si se borran matches, esa señal
se pierde → cubrirlo con "o está guardada" (puntos 4 y B.4).

**Otros hechos:**
- Texto libre del explorador (`explorador_ca.py:309–312`) busca solo en `compras_agiles.tsv`; los
  perfiles buscan además en `ca_productos` (`_FTS_CA_INCLUDE`).
- Etiquetas actuales: nav "Alertas activas" (`base.html:42`); botones "Activar alertas" / "Me sirve"
  en `_card_oportunidad.html:104–121` y `_ficha_acciones.html:16–19`; `_onboarding_modals.html`
  explica ambos por separado.

---

## Paso 0 (solo lectura, lo corre Boris contra producción ANTES de escribir código)
Script `data/paso0_guardar.py` (gitignored, mismo patrón que `data/paso0_keywords_tilde.py`:
normaliza el driver a `+psycopg`, imprime el host, solo `SELECT`). Salida en `data/logs/guardar.txt`.
Por usuario:
1. `sirve` totales; cuántas ya tienen `OportunidadSeguida` (activa / archivada); cuántas son de
   oportunidades vigentes.
2. Seguidas activas / archivadas; cuántas **sin ningún match** del usuario.
3. Recordatorios de cierre que generaría la migración: `sirve` sin seguida cuya oportunidad cierra
   en < 48 h desde ahora (comparar con el tope de 250 correos/día).
4. Por perfil activo: matches totales; matches de oportunidades **vigentes** que hoy NO pasan el
   criterio del perfil (incluye/excluye/rubro/organismo/región/monto) = lo que borraría la limpieza
   (sección B); de esos, cuántos son además guardados o descartados por el dueño.
5. `ca_productos`: filas totales y filas de CA vigentes (costo del Texto en productos, sección D).
**Si el punto 3 pasa de 50, o el punto 4 de 30 % de los matches de un perfil, detenerse y avisar.**
Pegar la salida al final de este archivo antes de seguir.

---

## Qué construir

### A. Guardar = seguir (modelo y rutas)
- **Guardada = `OportunidadSeguida` con `archivada = False`.** Sin tabla nueva. Guardar reusa
  `seguir_oportunidad`; quitar de guardadas = `dejar_de_seguir`. Archivar se mantiene (lo expone
  F-registro).
- `MatchFeedback` queda **solo para descarte**. `sirve` deja de escribirse; se mantiene en el enum para
  leer filas viejas (regla 6) con un comentario. `alternar_me_sirve` pasa a llamar a la lógica de
  guardar (alias deprecated).
- Exclusión mutua: guardar una descartada borra el descarte; descartar una guardada la quita de
  guardadas (`dejar_de_seguir`, no archivar). Nunca las dos a la vez.
- **Acceso nuevo** (`app/api/query.py`): `puede_actuar(session, user_id, fuente, codigo) -> bool` =
  la oportunidad existe **y** (el usuario tiene match **o** es `compras_agiles` **o** el usuario ya la
  tiene guardada/descartada). Las licitaciones sin match siguen sin poder abrirse si no están
  guardadas (no hay explorador de licitaciones). `check_oportunidad_access` se mantiene para lo que
  necesita el match (razones, banda), pero las rutas de acción usan `puede_actuar`. La ficha de una
  licitación guardada cuyo match se borró debe abrir (sin razones).
- Rutas: `POST /oportunidad/{f}/{c}/guardar` (toggle; HTMX y no-HTMX con `next`; CSRF; ownership;
  `fuente` validada contra `{"licitaciones","compras_agiles"}`; `estado_actual` leído de la
  oportunidad). `me-sirve`, `seguir` y `dejar-de-seguir` quedan como alias de lo mismo (deprecated en
  el código). `descartar`/`deshacer-descarte` pasan a `puede_actuar`.
- `_render_card_partial`: si no hay item con match (CA del explorador), re-renderizar la fila del
  explorador (`origen="explorador"`, parcial nuevo) en vez de devolver vacío.

### B. Limpieza de matches que ya no calzan
Función nueva `limpiar_matches_perfil(session, perfil, ahora) -> int` en `app/matching/engine.py`:
- Borra los `OportunidadMatch` **de ese perfil** cuya oportunidad está **vigente** (misma condición
  de vigencia que `_candidatos_*`: `PUBLICADA` y cierre futuro / nulo en CA) y que **no** pasa el
  criterio actual del perfil: inclusión (`_FTS_*_INCLUDE` OR rubro OR organismo; sin ninguno = todo
  pasa) AND NOT exclusión (`_FTS_*_EXCLUDE`) AND región (solo CA) AND monto (con la regla de
  "monto no informado pasa"). Una sola sentencia `DELETE … WHERE perfil_id = :p AND …` por fuente,
  con los **mismos fragmentos** SQL del recall (nada de listas en Python, sin el tope de 500).
- **No** toca matches de oportunidades terminales/vencidas (los usan competencia, datos abiertos y
  el historial), ni seguidas, ni feedback. Las alertas del match se van por cascade.
- Se llama: (1) al final de `match_perfil` (cada `ciclo-match`); (2) desde `excluir_palabras` (E);
  (3) al guardar un perfil en `/perfiles` cuando cambian keywords, exclusiones, rubros, organismos,
  regiones o montos. `match_perfil` devuelve `"borrados"` en su dict y `match_todos` lo suma al log.
- Efecto conocido: si una keyword se quita y luego se vuelve a poner, el match se recrea con
  `fecha_match` nueva y reaparece en el resumen. Aceptado (documentar en el docstring).

### B.4 Señales que dependían de "tiene match"
- `lifecycle._rezagadas`: entra la licitación si tiene match **o** está en `oportunidades_seguidas`
  (de cualquier usuario, no archivada).
- `_candidatas_detalles_match`: idem para licitaciones y CA guardadas sin detalle (mismo orden y
  espera por fallos). Las guardadas son pocas; no cambia el presupuesto de cuota.

### C. Migración de datos (Alembic, reversible, `down_revision = "b5d1f8a3c6e2"`)
Por cada `MatchFeedback` con `valor='sirve'`:
- sin `OportunidadSeguida` para (usuario, fuente, código) → crearla con `estado_visto` = **estado
  actual** de la oportunidad (si no existe la oportunidad, `''`) y `archivada=False`;
- con seguida archivada → dejarla archivada; borrar la fila `sirve`.
- Si el Paso 0 punto 3 > 0: para las creadas cuya oportunidad cierra en < 48 h, insertar la alerta
  `seguimiento_cierre` con `estado='enviada'` (no se manda el correo; el usuario ya la tenía marcada).
- Registro para el downgrade: tabla `_mig_guardar_creadas(seguida_id, usuario_id, fuente, codigo)`
  creada en el upgrade y borrada en el downgrade. `downgrade`: recrear `sirve` para todas las `sirve`
  migradas (guardar también las que ya tenían seguida) y borrar solo las seguidas de esa tabla.
- Idempotente: correr `upgrade` sobre una base ya migrada no duplica (sin `sirve`, no hace nada).
- La migración NO borra matches (eso lo hace el primer `ciclo-match` con el código nuevo).

### D. Explorador de Compras Ágiles
- Fila de cada CA: botón **Guardar / Guardada ✓** y **Descartar** (HTMX, CSRF, `aria-pressed`,
  anuncio en `#anuncios`). Al descartar, la fila desaparece (el filtro ya existe). Una query para el
  estado guardada de las ≤ 50 CA de la página (sin N+1).
- La ficha de CA sin match deja de ser solo lectura: muestra Guardar / Descartar (sin razones ni banda).
- Texto libre: `tsv @@ _tsq(:texto) OR EXISTS (ca_productos p … AND _FTS_CA_PRODUCTO @@ _tsq(:texto))`,
  la misma expresión que `_FTS_CA_INCLUDE` (importarla o compartirla, no copiarla a mano).

### E. Descartar con "Excluir palabra" (pedido de Boris, 27-sep)
**Modal "Descartar"** (Bootstrap, accesible; desde la tarjeta del feed y la ficha **con match**; en el
explorador sin match, Descartar es directo, sin modal):
- Qué lo trajo: perfil(es) y `razones["keywords_hit"]` ("Llegó por *salud* en el perfil *Hospitales*").
- Palabras sugeridas como chips: términos del nombre (sin stopwords ni las keywords del perfil); campo
  para escribir otra (validada como en `perfiles.py`, tope 5 por vez).
- En qué perfil excluir: por defecto el que generó el match; si hay varios, elegir.
- Vista previa: "Esto también saca N oportunidades más de tu feed" — endpoint HTMX solo con números,
  que cuenta con la MISMA condición que `limpiar_matches_perfil` aplicada al perfil con la palabra
  agregada (sin escribir nada).
- Botones "Descartar" y "Descartar y excluir". Tras excluir: anuncio con **Deshacer** = quitar las
  palabras agregadas y re-ejecutar `match_perfil` de ese perfil (recrea los matches; `fecha_match`
  nueva, aceptado).
**Backend:** `excluir_palabras(session, owner_id, perfil_id, palabras)` en `app/matching/perfiles.py`:
ownership del perfil (regla 17), CSRF (18), sin duplicar en `keywords_excluir`, luego
`limpiar_matches_perfil`. Guardadas intactas.

### F. UI y textos
- Tarjeta del feed y ficha: un solo botón **Guardar / Guardada ✓** (toggle, `aria-pressed`, anuncio en
  `#anuncios`); desaparecen "Me sirve" y "Activar alertas"; Descartar se mantiene (abre el modal E).
- Tarjeta guardada: chip "Guardada" (texto + icono, no solo color).
- Nav: "Alertas activas" → "Mi registro" (sigue en `/seguidas`; F-registro lo mueve a `/registro`).
- `_onboarding_modals.html`: Guardar = queda en tu registro y te avisa de cambios; Descartar = no la
  vuelves a ver (y puedes excluir la palabra que la trajo).
- Changelog: Guardar unificado; se puede guardar/descartar desde Explorar CA; el Texto del explorador
  busca también en los productos; al excluir una palabra, lo que ya no calza sale del feed. Fuente:
  Dirección ChileCompra.

### G. Retención
`purgar_terminales` no purga `raw_json` ni ítems/productos de oportunidades con una
`OportunidadSeguida` (archivada o no) de cualquier usuario, además de las con alerta pendiente.

---

## Tests (mínimo)
- **Acceso:** usuario B no guarda/descarta por A (404/ownership); CA sin match se puede guardar y
  descartar; licitación sin match ni guardada → 404; licitación guardada cuyo match se borró → ficha
  200 sin razones; `fuente` inválida → 404; CSRF en todas las rutas nuevas y alias.
- **Exclusión mutua** en ambas direcciones; alias `me-sirve`/`seguir`/`dejar-de-seguir` hacen lo mismo
  que `guardar`.
- **Limpieza (Postgres):** quitar una keyword borra los matches vigentes que ya no calzan y deja los
  que sí; agregar una exclusión con tilde («desratización») borra los que la contienen (en nombre y en
  producto); no toca matches de oportunidades terminales, ni de otro perfil u otro usuario; seguidas y
  feedback intactos; perfil sin keywords ni rubros ni organismos no borra nada; monto no informado no
  se borra; candidatos > 500 no se borran por el tope (sembrar 501 con `_MAX_CANDIDATOS` parcheado a un
  número chico); alertas del match borrado se van por cascade.
- **Vista previa = lo que realmente se borra** (mismo número).
- **Migración:** `sirve` sin seguida → seguida con `estado_visto` actual; `sirve` + archivada → sigue
  archivada; `sirve` borrado; cierre < 48 h → alerta `enviada`, no `pendiente`; downgrade restaura
  `sirve` y borra solo las creadas; upgrade dos veces no duplica; tras migrar,
  `detectar_cambio_estado_seguidas` no crea alertas para las migradas.
- **B.4:** licitación guardada sin match entra a `_rezagadas` y a la cola de `detalles-match`.
- **Explorador:** botones con `aria-pressed` correcto; descartar saca la fila; Texto encuentra una CA
  cuyo término está solo en `ca_productos` («reparación» con tilde, Postgres).
- **Retención** protege guardadas y archivadas.
- Adaptar (no borrar) los tests que usan `me-sirve` o el texto "Activar alertas"/"Me sirve"
  (`test_feedback_routes`, `test_feed_ui`, `test_ficha_routes`, `test_ui_accesibilidad`,
  `test_seguimiento_routes`, `test_ca_explorar*`).

## Fuera de alcance
F-registro (`/registro`, notas, archivar desde UI) · F11 (usar el feedback para el score) ·
explorador de licitaciones · borrar matches de oportunidades terminales · cambiar el tope de 500.

## Despliegue (para Boris, después de la auditoría)
1. Antes del push: limpiar dev si se va a migrar dev (tabla `rubro_vocabulario` suelta, cabeza
   `d7f2a4c8b6e1`) — los tests de migración corren contra dev.
2. Push justo después de un `ca` de los :05; confirmar en Render `Running upgrade b5d1f8a3c6e2 -> <nueva>`
   (nunca `alembic upgrade` a mano contra producción).
3. El primer `ciclo-match` con el código nuevo hace la limpieza: revisar en el log de Actions
   `match_todos: … borrados=N` y compararlo con el Paso 0 punto 4.
4. Al día siguiente, `/salud`: sin ráfaga de correos.

---

## Decisiones abiertas (Boris)
1. **Cuándo limpiar matches:** (a) en cada `ciclo-match` + al editar perfil/excluir **(por defecto)**;
   (b) solo al editar perfil/excluir y una vez al deploy.
2. **Guardar una CA sin match ¿baja su detalle?** (a) sí, entra a `detalles-match` **(por defecto)**;
   (b) no, se ve sin productos.
3. **Descartar en el explorador:** (a) directo, sin modal **(por defecto)**; (b) también con modal
   (sin perfil que lo trajo, solo "excluir en el perfil…").

*Fuente de los datos de dominio: Dirección ChileCompra.*

---

## Resultado del Paso 0 (producción, 01-oct, `data/logs/guardar.txt`) [V]
`data/paso0_guardar.py` terminó con **"OK, se puede seguir"**.
- **"Me sirve":** solo boris tiene: **43** (11 ya con seguida activa, 0 archivadas, **32 sin seguida**;
  solo 5 vigentes). La migración crea **32 seguidas**, casi todas de oportunidades ya cerradas: entran
  a "Mi registro" con `estado_visto` = estado actual, así que no disparan alertas.
- **Recordatorios de cierre < 48 h que generaría la migración: 0.** El marcado `enviada` de la
  sección C queda como resguardo, pero hoy no aplica.
- **Seguidas:** boris 17 activas (1 de CA), alejandra 1 archivada; **0 sin match** hoy. B.4 no cambia
  nada todavía, pero hace falta apenas la limpieza borre el primer match de una guardada.
- **Descartes:** boris **1.924**. Exclusión mutua y alias deben probarse con volumen (la consulta de
  estado del explorador ya filtra por usuario).
- **Limpieza (punto 4), vigentes que ya no calzan:** total **163 matches**, todos de boris:
  | Perfil | Licitaciones | Compras Ágiles |
  |---|---|---|
  | 5 «perfil vejez» | 13 de 136 (10 %; 13 ya descartados) | 76 de 456 (17 %; 37 ya descartados) |
  | 6 «Personal» | 7 de 86 (8 %; 7 ya descartados) | 67 de 390 (17 %; 38 ya descartados) |
  | 7, 8, 9 | 0 | 0 |
  **95 de los 163 ya estaban descartados a mano**: la limpieza hace lo que boris venía haciendo.
  **0 guardados** entre los que se borran. Primer `ciclo-match` tras el deploy: esperar
  `borrados` ≈ 163 (± lo que entre o salga de vigencia entre medio).
- **`ca_productos`:** 9.672 filas; 2.840 de CA vigentes (966 CA con productos). El EXISTS del Texto
  del explorador (sección D) es barato: no hace falta índice nuevo.
- **Ojo (fuera de alcance, anotar):** con la vigencia del matching (`publicada` y cierre futuro o
  nulo) hay **17.273 CA "vigentes"**, contra ~7.800 del explorador (que exige publicación ≤ 7 días si
  no hay cierre). El matching y la limpieza consideran CA sin cierre muy antiguas. Candidato a fase
  aparte (alinear `_candidatos_ca` con `condicion_ca_vigente`); F-guardar NO lo cambia.

**Decisiones abiertas con este resultado:** las tres opciones por defecto quedan respaldadas
(limpieza en cada `ciclo-match`: 163 filas una vez y luego pocas; detalle para CA guardadas sin match:
hoy 0 casos; descartar directo en el explorador).
