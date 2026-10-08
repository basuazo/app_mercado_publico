# Prompt de implementación — F-match-1 (relevancia, organismos, feed y resumen) · 08-oct-2026

> Para Claude Code. **Modelo: Sonnet.** Una fase = un commit, **sin push**. **Requisito:
> F-datos-1 en producción** (sin él las licitaciones no tienen organismo y la parte de
> organismos solo sirve para CA). Origen: `docs/14-auditoria-integral.md` §3 y §7-bis.

## 0. Antes de escribir código

### 0.a Lectura (sin cambios)
`CLAUDE.md`; `docs/14-auditoria-integral.md` §3 y §7-bis. Código:
- `app/matching/engine.py`: `score_*`, `_score_licitacion`, `_score_ca`, `_campo_hit`, `match_perfil`, `criterio_perfil`, `_where_limpieza` y `limpiar_matches_perfil`.
- `app/matching/text.py`.
- `app/matching/perfiles.py`.
- `app/api/query.py`: `get_oportunidades_usuario`, `_construir_item`, `_ordenar`, `_pasa_texto` y `_aplicar_filtros`.
- `app/alerts/email.py`: `_matches_nuevos_usuario`, `enviar_resumen` y `_ctx_resumen_item`.
- `app/api/routes/pages.py`: presets de relevancia (≈ líneas 190–220) y widget de organismos.
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
- **Quitar `dias_al_cierre` de `razones`.** Ese valor obliga a reescribir todos los matches en cada ciclo (auditoría §3.4). Si la UI lo usa, que lo calcule al mostrar a partir de `fecha_cierre`. Revisar `_construir_item` y las plantillas.
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

## 1.c Simulación antes del commit (la corre Boris)
Escribir `data/paso0_score_nuevo.py`: solo lectura, mismo patrón que `data/paso0_auditoria.py`. Recalcula la relevancia nueva **desde las `razones` guardadas** en producción (`keywords_hit`, `campo_hit`, `categorias_hit`, `organismo_seguido`).

Para cada usuario y fuente imprime, **solo con oportunidades vigentes**:
- cuántas pasan hoy el piso de 40 y cuántas lo pasarían con la fórmula nueva;
- cuántas superan 60, antes y después;
- 5 títulos de ejemplo de las que **entran** y 5 de las que **salen**.

**Detente** y pídele a Boris que lo corra contra producción. **Sigue solo con su visto bueno.**
- Si el número de visibles de algún usuario se multiplica por más de 3, propón ajustar las constantes y repórtalo antes de seguir.
- Limitación conocida: `razones` no distingue acierto en producto cuando también lo hubo en nombre. La simulación usa `campo_hit` tal como está; es una aproximación suficiente.

## 2. Fuera de alcance
- Sinónimos, sugerencias de exclusión y de keywords, y el bonus "parecida a tus guardadas": eso es F-match-2.
- Índices y `ON CONFLICT`: F-indices. Ojo: no empeorar el N+1 de `_upsert_match`.
- Rediseño del feed (F-bandeja) y de `/perfiles` (F-perfiles-1/2).
- Exclusión solo sobre nombre e ítems: va con migración, en F-match-2.

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
- Actualizar los tests que asumen la fórmula vieja, explicando en el reporte cuáles y por qué.

## 4. Cierre
- Correr `ruff check .`, `python -m mypy app` y `python -m pytest -rs` completo contra dev con `postgresql+psycopg://`: 0 fallos, 0 errores, 0 saltados.
- Entrada en `app/changelog.py`: "El match ahora mide solo qué tan bien calza la oportunidad con tus palabras, rubros y organismos; lo urgente se ve en el orden y en la fecha. Una oportunidad que calza con dos perfiles aparece una sola vez, y el resumen diario trae una sección 'Cierra en 48 horas'".
- `git add` solo de los archivos tocados. Commit: `F-match-1: relevancia separada de urgencia, organismos seguidos, feed y resumen sin duplicados`. **Sin push.**
- **Reporte:** hash, archivos, salida de la simulación de §1.c, resultado de la suite y "Desvío del prompt".
- **Sin migración**, así que el push puede ir a cualquier hora. El primer `ciclo-match` recalcula todos los scores.
- **Después del deploy:** revisar el feed de los 3 usuarios y el correo del día siguiente.

*Fuente de los datos de dominio: Dirección ChileCompra.*
