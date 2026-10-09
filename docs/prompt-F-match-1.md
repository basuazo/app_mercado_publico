# Prompt de implementación — F-match-1 (relevancia, organismos, exclusión por título, feed y resumen) · 08-oct-2026, revisado 09-oct

> Para Claude Code. **Modelo: Sonnet.** Una fase = un commit, **sin push**. **Requisito:
> F-datos-1 en producción** (sin él las licitaciones no tienen organismo y la parte de
> organismos solo sirve para CA). Origen: `docs/14-auditoria-integral.md` §3 y §7-bis.
>
> **Revisión 09-oct** contra el código posterior a F-indices (`e8e064a`) y F-perfiles-1 (`08cabc8`):
> referencias de código actualizadas y se agrega §1.7 (exclusión solo en el título, decisión de Boris
> 09-oct, sin migración). F-datos-1 ya está en producción, así que el requisito está cumplido.

## 0. Antes de escribir código

### 0.a Lectura (sin cambios)
`CLAUDE.md`; `docs/14-auditoria-integral.md` §3 y §7-bis. Código:
- `app/matching/engine.py`: `score_*`, `_score_licitacion`, `_score_ca`, `_campo_hit`, `match_perfil`, `criterio_perfil`, `_criterio_lic`, `_criterio_ca`, `_FTS_*_EXCLUDE`, `_where_limpieza`, `contar_limpieza`, `limpiar_matches_perfil` y `_upsert_match` (desde F-indices es una sola sentencia `ON CONFLICT` con un `WHERE` que no reescribe filas sin cambios).
- `app/matching/text.py`.
- `app/matching/perfiles.py`: `verificar_exclusiones` y `palabras_sugeridas` (las palabras que ofrece "Descartar y excluir" salen del **nombre**).
- `app/api/query.py`: `get_oportunidades_usuario`, `_construir_item`, `_ordenar`, `_pasa_texto` y `_aplicar_filtros`.
- `app/alerts/email.py`: `_matches_nuevos_usuario`, `enviar_resumen` y `_ctx_resumen_item`.
- `app/api/routes/pages.py`: presets de relevancia (`_RELEVANCIA_ALTA`, ≈ línea 221) y la vista previa de "Descartar y excluir" (`contar_limpieza(criterio_perfil(perfil, lista))`, ≈ línea 1380).
- Organismos en la UI (F-perfiles-1): `static/organismos_widget.js`, `GET /organismos/catalogo.json` y `razones_sociales` en `query.py`. El perfil guarda `codigo_entidad`; no cambia.
- Plantillas: `_perfil_form.html` (campo Excluir), `_card_oportunidad.html` y `_ficha_acciones.html` (modal de descarte), `_ficha_contenido.html` (fecha de publicación).
- `app/models/tables.py`: `InstitucionPAC` y `OportunidadMatch`.
- `app/core/settings.py`: `feed_min_score_default`.

### 0.b Por qué
Paso 0 en producción, 08-oct:
- De lo que calza **por palabra clave**, el piso de 40 oculta el **56 % en licitaciones (2.625 de 4.716)** y el **81 % en CA (5.804 de 7.205)**.
- Al revés, 385 licitaciones y 43 CA aparecen con ≥ 40 sin ninguna palabra clave, solo por rubro y urgencia.

La causa es la fórmula. El texto vale `hits/keywords × 60`, así que un perfil con 7 palabras y un acierto suma 9 puntos. Encima se suman urgencia y competencia, que no dicen nada sobre relevancia.

Además:
- organismos seguidos da **0 matches**, porque se compara `codigo_entidad` con un RUT;
- el feed muestra 426 oportunidades duplicadas de boris;
- el resumen no aplica umbral y no excluye las descartadas.

## 1. Cambios

### 1.1 Relevancia separada de prioridad (`engine.py`)
El campo `OportunidadMatch.score` pasa a guardar **solo la relevancia**. Lo que se muestra como "Match" no cambia de nombre.

Función pura `relevancia(...)` con estos valores por defecto. Son decisión de Boris y pueden ajustarse tras el Paso 1.c; van como constantes nombradas al inicio del módulo:

| Señal | Puntos |
|---|---|
| ≥ 1 keyword con acierto en **nombre** o en **ítem/producto** | base 50 |
| ≥ 1 keyword con acierto **solo en la descripción** | base 35 |
| cada keyword **adicional distinta** con acierto | +8 (máx. +16) |
| rubro UNSPSC confirmado | +20 |
| organismo seguido | +15 |
| sin keyword, con rubro **o** con organismo | base 40 en vez de 0 (+15 si están ambos) |
| tope | 100 |

- `campo_hit` hoy prioriza nombre > descripcion > producto. Para la base de 50 basta con que haya acierto en nombre o en producto, así que ese dato se agrega a las razones (`hit_en_nombre_o_item: bool`).
- La urgencia y la competencia **salen del score**. Se siguen calculando para mostrar y ordenar: `razones` conserva `ofertas`.
- **Quitar `dias_al_cierre` de `razones`.** Sigue ahí (`_score_licitacion`/`_score_ca`). Como cambia en cada ciclo, hace que el `WHERE` de cambios del `ON CONFLICT` de F-indices nunca ahorre una escritura. Si la UI lo usa (`presentacion.py`: `banda_urgencia` y vecinas), que lo calcule al mostrar a partir de `fecha_cierre`. Revisar `_construir_item` y las plantillas.
- **Keywords válidas.** El denominador ya no se usa. `keywords_validas` pasa a quitar también un `-` inicial suelto: un `-algo` sin comillas convertiría la tsquery en "todo menos algo".

### 1.2 Organismos seguidos
- **Licitaciones:** `codigo_organismo`, poblado por F-datos-1, contra los códigos del perfil.
  - [I] El código del PAC (`codigo_entidad`) y `Comprador.CodigoOrganismo` serían el mismo identificador de Mercado Público. Verificarlo en dev con 3 casos reales; si no coinciden, detenerse y reportarlo.
- **CA:** al construir el criterio, traducir cada `codigo_entidad` del perfil a RUT con `instituciones_pac.rut`.
  - Comparar normalizando: sin puntos, guion ni espacios, `k` en minúscula.
  - Aplicarlo en el recall y en el score.
- **Sin migración:** el perfil sigue guardando códigos.

### 1.3 Candidatos: región y monto antes del tope de 500
- Pasar los filtros de región (solo CA) y de monto al SQL de candidatos, reutilizando las condiciones de `_where_limpieza`, para que el `LIMIT 500` se aplique después de filtrar.
- Hoy ningún perfil pierde resultados por esto. Es preventivo y alinea el recall con la limpieza.
- **Licitaciones con región** (columna nueva de F-datos-1):
  - aplicar el filtro de región también a ellas;
  - una licitación con `region` NULL **pasa** y lleva la razón `region_no_informada`, igual que hoy pasa un monto no informado;
  - quitar el comentario de `_pasa_region` en `query.py` que dice que las licitaciones pasan todas, y aplicar lo mismo en el feed.

### 1.4 Feed (`query.py`)
- **Un ítem por oportunidad:** agrupar por `(fuente, codigo)`, quedarse con el match de mayor relevancia y adjuntar `perfiles: list[str]` (nombres) para mostrarlos en la tarjeta como "Perfiles: A, B". Los conteos y facetas se calculan después de deduplicar.
- **Orden "Mejor match":** relevancia descendente, luego `fecha_cierre` ascendente (NULL al final).
- **`_pasa_texto`:** comparar sin tildes y sin mayúsculas (`unicodedata`), sobre el nombre **y** el organismo.

### 1.5 Correo-resumen (`email.py`)
- `_matches_nuevos_usuario`:
  - aplicar el piso `feed_min_score_default`;
  - excluir las oportunidades descartadas o ya guardadas por el usuario, con `NOT EXISTS` en el SQL;
  - deduplicar por oportunidad;
  - cargar licitaciones y CA **en lote**, no una por una.
- Orden: relevancia descendente y luego cierre más próximo. Top 5 como hoy.
- **Sección nueva "Cierra en ≤ 48 h":** hasta 5 oportunidades vigentes del usuario con relevancia ≥ 60, cierre en las próximas 48 h (hora de Chile), no descartadas, no guardadas y que no estén ya en el top.
  - Va **en el mismo correo**: no hay correos nuevos (regla 14).
  - Si la sección queda vacía, no se muestra.

### 1.6 UI mínima
- La tarjeta y la ficha muestran "Perfiles: …" cuando hay más de uno.
- Las razones de match muestran "en el título", "en un ítem" o "en la descripción" según dónde hubo acierto.
- No hay rediseño: eso va en F-bandeja.
- **Fechas de publicación en hora de Chile** (detectado en F-datos-1): la ficha y la tarjeta formatean
  `fecha_publicacion`, que está en UTC naive, sin convertirla. Desde las 21:00 de Chile muestran el
  día siguiente. Convertir a `America/Santiago` antes de formatear, igual que el cierre; test con
  reloj a las 22:00 de Chile.

### 1.7 Exclusión solo en el título (decisión de Boris, 09-oct)
**Problema.** Hoy una palabra excluida saca la oportunidad si aparece en el nombre, en la
descripción (`tsv` = nombre + descripción) **o en cualquier ítem/producto** (`_FTS_LIC_EXCLUDE`,
`_FTS_CA_EXCLUDE`). Ejemplo real de Boris: excluye "agua" porque no repara sistemas de agua, pero
eso también le saca una licitación de programas de adulto mayor que trae un ítem de agua
embotellada. La exclusión tiene que expresar "no es de lo mío", y eso lo dice el **título**:
"Reparación sistema de aguas centro adulto mayor" sí se excluye; un ítem o la descripción no.
(Auditoría M6: la descripción completa ya generaba falsos negativos, como "no incluye arriendo".)

**Cambio.** Para ambas fuentes, la exclusión se evalúa **solo sobre el nombre**:
- `_FTS_LIC_EXCLUDE` → `NOT (to_tsvector('spanish', inmutable_unaccent(coalesce(licitaciones.nombre, ''))) @@ {_tsq(':qx')})`.
- `_FTS_CA_EXCLUDE` → lo mismo con `compras_agiles.nombre`.
- Sin `EXISTS` sobre ítems ni productos, y sin usar `tsv`.
- La inclusión **no cambia**: las keywords siguen buscando en nombre, descripción e ítems.
- **Sin migración ni índice nuevo.** Es un filtro negativo que se evalúa sobre filas que ya pasaron vigencia e inclusión, así que un índice no ayudaría. Confirmar con `EXPLAIN ANALYZE` en dev, con el perfil de más keywords, que el recall no empeora de forma visible (reportar los ms antes y después).
- Como `_criterio_*` lo comparten el recall, `_where_limpieza`, `contar_limpieza` y la vista previa de "Descartar y excluir", todos quedan coherentes con un solo cambio. **No dupliques la expresión**: una constante por fuente.
- `exclusiones_que_chocan` no cambia (compara exclusiones contra keywords, no contra oportunidades).
- **UI:**
  - en `_perfil_form.html`, bajo "Excluir", texto de ayuda: "Saca una oportunidad solo si la palabra está en su título";
  - el modal de "Descartar y excluir" ya ofrece palabras del nombre (`palabras_sugeridas`), así que queda coherente. Agrega la misma aclaración si el modal explica qué hace excluir.
- **Efecto esperado:** en el primer `ciclo-match` vuelven a entrar como matches nuevos las oportunidades vigentes que solo se excluían por un ítem o por la descripción. Aparecerán como nuevas en el siguiente resumen. Se acepta, porque para la persona son nuevas. La simulación §1.c las cuenta antes.
- Actualizar el docstring de `limpiar_matches_perfil` y los comentarios de los fragmentos FTS.

## 1.c Simulación antes del commit (la corre Boris)
Escribir `data/paso0_score_nuevo.py`: solo lectura, mismo patrón que `data/paso0_auditoria.py`. Recalcula la relevancia nueva **desde las `razones` guardadas** en producción (`keywords_hit`, `campo_hit`, `categorias_hit`, `organismo_seguido`).

Para cada usuario y fuente imprime, **solo con oportunidades vigentes**:
- cuántas pasan hoy el piso de 40 y cuántas lo pasarían con la fórmula nueva;
- cuántas superan 60, antes y después;
- 5 títulos de ejemplo de las que **entran** y 5 de las que **salen**.

**Detente** y pídele a Boris que lo corra contra producción. **Sigue solo con su visto bueno.**
- Si el número de visibles de algún usuario se multiplica por más de 3, propón ajustar las constantes y repórtalo antes de seguir.
- Limitación conocida: `razones` no distingue acierto en producto cuando también lo hubo en nombre. La simulación usa `campo_hit` tal como está; es una aproximación suficiente.
- **Exclusión por título (§1.7):** para cada perfil activo con exclusiones y para cada fuente, cuenta las oportunidades **vigentes** que hoy quedan fuera por exclusión y que con la regla nueva entrarían. Es decir, las que pasan vigencia e inclusión, calzan con la exclusión vieja y no con la del nombre. Imprime el conteo y hasta 5 títulos con la palabra excluida que las sacaba. Solo `SELECT`, con los mismos fragmentos SQL del motor (importarlos, no copiarlos).

## 2. Fuera de alcance
- Sinónimos, sugerencias de exclusión y de keywords, y el bonus "parecida a tus guardadas": eso es F-match-2.
- Índices y `ON CONFLICT`: ya están en producción (F-indices). No reintroducir un `SELECT` previo en `_upsert_match`.
- Rediseño del feed (F-bandeja) y del asistente de perfiles (F-perfiles-2; `/perfiles` ya se rehízo en F-perfiles-1).
- Exclusión configurable por perfil (flag o alcance): no se hace. La regla es una sola, solo título (§1.7).

## 3. Tests
- `relevancia()`:
  - cada fila de la tabla de §1.1;
  - el tope de 100;
  - un perfil de 20 keywords con 1 acierto en el nombre da 50, no 3.
- Urgencia y competencia no cambian el score: misma oportunidad con distinto cierre, mismo score.
- Organismos:
  - una CA de un organismo seguido por `codigo_entidad` calza vía `instituciones_pac.rut`, aunque el RUT venga con o sin puntos;
  - una licitación calza por `codigo_organismo`.
- El tope de candidatos se aplica después de región y monto (fixture con más de 500 fuera de la región).
- Feed:
  - dos perfiles que calzan la misma oportunidad dan 1 ítem con 2 perfiles;
  - el orden por defecto;
  - el texto sin tildes calza ("reparacion" encuentra "Reparación").
- Resumen:
  - respeta el piso;
  - excluye descartadas y guardadas;
  - deduplica;
  - arma la sección "Cierra en ≤ 48 h" con reloj inyectado;
  - no hace una query por match (contar queries o mockear la sesión).
- `keywords_validas` limpia el `-` inicial suelto.
- **Exclusión por título** (tests PG, en `test_ajustes_pg.py` o en un archivo `_pg` nuevo):
  - licitación con "agua" solo en un ítem → **entra** con la exclusión "agua";
  - licitación con "agua" solo en la descripción → **entra**;
  - licitación titulada "Reparación sistema de aguas centro adulto mayor" → **sale** con la exclusión "agua" (raíz común agua/aguas);
  - lo mismo para CA con un producto que contiene la palabra;
  - `contar_limpieza` y `limpiar_matches_perfil` siguen el mismo criterio: no borran un match que solo tiene la palabra en un ítem;
  - con tildes: excluir "reparacion" saca "Reparación …".
- Actualizar los tests que asumen la fórmula vieja, explicando en el reporte cuáles y por qué.

## 4. Cierre
- Correr `ruff check .`, `python -m mypy app` y `python -m pytest -rs` completo contra dev con `postgresql+psycopg://`: 0 fallos, 0 errores, 0 saltados.
- Entrada en `app/changelog.py`: "El match ahora mide solo qué tan bien calza la oportunidad con tus palabras, rubros y organismos; lo urgente se ve en el orden y en la fecha. Las palabras excluidas solo sacan una oportunidad si están en su título, no por un ítem suelto. Una oportunidad que calza con dos perfiles aparece una sola vez, y el resumen diario trae una sección 'Cierra en 48 horas'".
- `git add` solo de los archivos tocados. Commit: `F-match-1: relevancia separada de urgencia, organismos seguidos, exclusión por título, feed y resumen sin duplicados`. **Sin push.**
- **Reporte:** hash, archivos, salida de la simulación de §1.c, resultado de la suite y "Desvío del prompt".
- **Sin migración**, así que el push puede ir a cualquier hora. El primer `ciclo-match` recalcula todos los scores.
- **Después del deploy:** revisar el feed de los 3 usuarios y el correo del día siguiente.

*Fuente de los datos de dominio: Dirección ChileCompra.*
