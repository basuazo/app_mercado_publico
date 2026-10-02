# Prompt F-registro — "Mi registro": guardadas, cerradas, vencidas recientes, descartadas y archivadas

> **Destino en el repo:** `docs/prompt-F-registro.md` (revisado por Cowork el 02-oct contra `176f168`;
> la versión del 24-sep queda en git, `dc284c8`).
> **Fase:** F-registro (una fase, un commit) · **Migración:** ninguna (si hiciera falta un índice,
> justificarlo con el Paso 0 y avisar antes) · **Dependencias nuevas:** ninguna
> **Orden:** después de F-guardar (`176f168`, en producción, cabeza `c9e4b2f7a1d8`). Independiente de
> F-ajustes: puede ir antes o después. Si `alembic heads` no da `c9e4b2f7a1d8` (o la cabeza que dejó
> F-ajustes si ya entró), detenerse y avisar.
> **Modelo:** Sonnet. Sin API, sin cuota, sin jobs nuevos, no borra nada.
> **Decisiones de Boris (24-sep), vigentes:**
> - Lo vencido y lo guardado se revisan fuera del dashboard, en un registro personal.
> - Las vencidas **no guardadas** con match **≥ 30** siguen visibles **14 días** después de vencer;
>   después se **ocultan, no se borran**.
> - Las guardadas que vencen quedan en el registro para revisarlas después.
> **Decisiones abiertas:** al final; sin respuesta en este archivo, usar la opción **(por defecto)**.

Reglas del proyecto: CLAUDE.md completo, en especial 6 (no descartar por falta de dato), 12 (512 MB),
17 (ownership), 18 (CSRF) y queries parametrizadas. Español de Chile; entrada en `app/changelog.py`;
`ruff check .`, `python -m mypy app`, `python -m pytest -rs` verdes **con `DATABASE_URL` de dev**
(`postgresql+psycopg://`, host `ep-dawn-sunset`; 0 saltados); commit "F-registro: …" sin push;
**nunca `git add -A`**; nada de `ruff format` masivo.

---

## Hechos que condicionan [V, código en `176f168`]
- **Mi registro ya existe a medias** (F-guardar): nav "Mi registro" → `/seguidas` (`base.html:41`);
  `seguidas.html` lista guardadas con Archivar / Desarchivar / Quitar y un toggle `?archivadas=1`;
  `/descartadas` aparte (`descartadas.html`, botón "Ver descartadas (N)" en `index.html:134`).
  Fallbacks de rutas a `/seguidas` y `/descartadas` en `pages.py` (`_guardar` "quitar", archivar,
  desarchivar, deshacer-descarte).
- **Guardada = `OportunidadSeguida` no archivada**, y **puede no tener match** (CA guardada desde
  Explorar CA, o licitación cuyo match borró la limpieza). Hoy: boris ~49 guardadas.
  `listar_seguidas_detalle` (`app/api/query.py:695`) ya arma la lista desde las seguidas, sin match.
- **`get_oportunidades_usuario` no sirve tal cual para el registro:** (1) parte de
  `oportunidades_match`, así que pierde las guardadas sin match; (2) con `solo_vigentes=False` **no
  tiene prefiltro SQL** y carga TODOS los matches del usuario con sus oportunidades (boris: ~5.000
  matches) en cada request — contra la regla 12; (3) arma un item **por match**, no por oportunidad
  (una CA que calza con dos perfiles sale dos veces), y el registro necesita el **score máximo** del
  usuario por oportunidad.
- `_construir_item` (`query.py:65`) y `_card_oportunidad.html` asumen `item["match"]` (score,
  razones). Una guardada sin match necesita una variante sin match.
- Vigencia: `es_vigente(estado, fecha_cierre, fuente, ahora, fecha_publicacion)`
  (`app/core/vigencia.py`). Una CA **sin `fecha_cierre`** deja de ser vigente a los
  `CA_SIN_CIERRE_VIGENCIA_DIAS = 7` días de publicada (o antes si su estado deja de ser Abierta).
- La limpieza de F-guardar (`limpiar_matches_perfil`) borra solo matches de oportunidades
  **vigentes**: los matches de vencidas se conservan, así que "Vencidas recientes" tiene de dónde salir.
  La retención (90 días, solo terminales) no toca nada dentro de la ventana de 14 días.
- Los correos enlazan a la ficha (`/oportunidad/...`), no a `/seguidas`; igual se mantienen
  redirecciones por enlaces guardados.
- La sección "Estado" del panel de filtros se sacó en F-vigencia (`_panel_filtros.html:181`).

## Paso 0 (solo lectura, Boris contra producción, ANTES de escribir código)
`data/paso0_registro.py` (gitignored, mismo patrón que `paso0_guardar.py`), salida en
`data/logs/registro.txt`. Por usuario: guardadas vigentes / no vigentes / archivadas, y de esas cuántas
sin match; candidatas a "vencidas recientes" (oportunidad con match del usuario, score máx ≥ 30, vencida
en los últimos 14 días, no guardada ni descartada) por fuente; las mismas con piso 40; matches totales
del usuario (para dimensionar). **Si "vencidas recientes" pasa de 300 para alguien, avisar** (la
paginación y el conteo en el dashboard deben seguir siendo baratos). Pegar la salida al final.

---

## Qué construir

### 1. Página `/registro` con pestañas (`?tab=`)
| Pestaña | Fuente de datos | Qué muestra | Orden |
|---|---|---|---|
| `guardadas` (default) | seguidas no archivadas | las **vigentes** | cierre más próximo primero (sin fecha al final) |
| `cerradas` | seguidas no archivadas | las **no vigentes**: siguen avisando cambios (adjudicación, etc.) | vencimiento más reciente primero |
| `vencidas` | matches del usuario | **no guardadas (ni archivadas), no descartadas, no vigentes**, vencidas en los últimos `REGISTRO_DIAS_GRACIA` días, **score máximo del usuario ≥ `REGISTRO_MIN_SCORE_VENCIDAS`** | vencimiento más reciente primero |
| `descartadas` | `listar_descartadas_detalle` | lo de hoy en `/descartadas`, con Restaurar | más reciente primero |
| `archivadas` | seguidas archivadas | con Desarchivar y Quitar | más reciente primero |
- Cada pestaña con su conteo en la etiqueta ("Vencidas recientes (12)"); pestañas accesibles
  (`role="tablist"`/`aria-selected`, o enlaces con `aria-current="page"`; la URL manda).
- **Guardadas / Cerradas:** cada tarjeta con Archivar, Quitar de guardadas y chip de estado real.
  Las que no tienen match muestran "Guardada desde Explorar CA" (CA) o "Ya no calza con tus perfiles"
  (licitación) en vez de razones y score.
- **Vencidas:** cada tarjeta dice "Se oculta en N días" (N ≥ 1; el último día "Se oculta hoy") y
  ofrece **Guardar** (pasa a `cerradas`) y **Descartar** (directo, sin modal). Texto arriba:
  "Oportunidades con buen match que cerraron hace poco sin que las guardaras. Se ocultan a los 14 días;
  guárdalas si quieres conservarlas."
- Tarjeta: reusar `_card_oportunidad.html` con `item["match"]` opcional (si es None: sin score ni
  razones). `_construir_item` acepta `m=None` y un `score_max` explícito. Las acciones HTMX existentes
  (`/guardar`, `/descartar`) re-renderizan con un `origen="registro"` nuevo en `_ORIGENES`; si la
  tarjeta deja la pestaña (guardar una vencida, quitar una guardada), devuelve vacío + anuncio en
  `#anuncios`, como el feed.
- **Filtros:** solo **Fuente** y **Texto** (sobre nombre/organismo, en Python sobre la lista ya
  acotada). No se reusa el panel de facetas del feed (sus facetas suponen un match por item).
- Paginación 20 + "Ver 20 más" (mismo patrón que el feed) en `vencidas`, `descartadas` y `archivadas`;
  `guardadas`/`cerradas` también si pasan de 20.

### 2. Consultas (nuevas, en `app/api/query.py`; todas acotadas por `user_id`, regla 17)
- `listar_registro_guardadas(session, user_id, *, archivadas: bool) -> list[dict]`: parte de
  `oportunidades_seguidas` del usuario; carga en lote las oportunidades y los matches del usuario
  **solo para esos códigos** (score máx y razones del mejor match, si hay); separa vigentes / no
  vigentes con `es_vigente`. Reemplaza a `listar_seguidas_detalle` (o la envuelve).
- `listar_vencidas_recientes(session, user_id, ahora, *, dias, min_score) -> list[dict]`:
  **prefiltro en SQL** por ventana de vencimiento (ver §4) y `score >= min_score`, agrupado por
  (fuente, código) con `max(score)` sobre los perfiles activos del usuario; excluye en SQL las que el
  usuario tiene en `oportunidades_seguidas` (archivadas incluidas) o descartadas; confirma "no
  vigente" con `es_vigente` en Python. Nunca carga el histórico entero.
- `contar_vencidas_recientes(...)`: la misma condición en un `count`, para el dashboard y la pestaña.

### 3. Parámetros
`REGISTRO_DIAS_GRACIA` (default **14**) y `REGISTRO_MIN_SCORE_VENCIDAS` (default **30**) en
`app/core/settings.py`, por variable de entorno, **no** requeridos en `_job.yml` (los usa la web).
Comentario: el piso 30 queda bajo el del feed (40) a propósito (decisión 24-sep): en Vencidas aparecen
oportunidades con match 30–39 que nunca se vieron en el dashboard.

### 4. Fecha de vencimiento (una sola función, `fecha_vencimiento(op, fuente)`)
- Con `fecha_cierre`: `fecha_cierre`.
- **CA sin `fecha_cierre` (por defecto):** `fecha_publicacion + CA_SIN_CIERRE_VIGENCIA_DIAS` — es el
  momento en que la app dejó de mostrarla como vigente (misma regla que `es_vigente`). Si tampoco
  hay `fecha_publicacion`: `actualizado_en` (regla 6: no descartar por falta de dato).
- En SQL, la ventana de §2 usa la misma definición (para CA sin cierre:
  `fecha_publicacion BETWEEN ahora - (dias + 7) AND ahora - 7`). Test que compare la función Python
  con el filtro SQL sobre una matriz de casos (como `test_ca_explorar.py` con `condicion_ca_vigente`).
- Una oportunidad que dejó de ser vigente **antes** de su vencimiento (p. ej. CA cancelada con cierre
  futuro) usa igualmente su `fecha_cierre`: aparece en Vencidas cuando el cierre ya pasó. Documentar.

### 5. "Ocultar, no borrar"
Pasados los 14 días la vencida no cumple el filtro: **ningún job borra matches** por esto.

### 6. Navegación y rutas viejas
- Nav: "Mi registro" → `/registro` (`aria-current` en cualquier pestaña).
- `GET /seguidas` → 303 a `/registro?tab=guardadas` (`archivadas=1` → `tab=archivadas`);
  `GET /descartadas` → 303 a `/registro?tab=descartadas`.
- Fallbacks de `pages.py` que hoy van a `/seguidas` o `/descartadas` → a la pestaña que corresponda.
  `next` de los formularios del registro → la pestaña actual.
- Dashboard: "Ver descartadas (N)" → `/registro?tab=descartadas`; y bajo el contador, enlace discreto
  "N vencidas recientes en tu registro" cuando N > 0 (con `contar_vencidas_recientes`, una query).
- `seguidas.html` y `descartadas.html` se reemplazan por `registro.html` (+ parciales); borrar las
  viejas solo si ningún test ni ruta las usa.
- Onboarding (`_onboarding_modals.html`): una línea sobre Mi registro y las vencidas recientes.

### 7. Changelog
"Mi registro reúne tus guardadas (vigentes y cerradas), las vencidas recientes que tuvieron buen match
y no guardaste (se ocultan a los 14 días), tus descartadas y tus archivadas. Fuente: Dirección
ChileCompra."

---

## Tests (mínimo)
- **Vencidas:** score máx 35 vencida hace 3 días → aparece con "Se oculta en 11 días"; score 25 → no;
  vencida hace 15 días → no, **y la fila de match sigue existiendo**; descartada → solo en
  `descartadas`; guardada o archivada → nunca en `vencidas`; dos perfiles con scores 25 y 35 → aparece
  una vez, con 35; vigente → no.
- **CA sin cierre:** publicada hace 10 días → vencida hace 3 (aparece); publicada hace 22 → no;
  sin publicación → usa `actualizado_en`. Matriz Python vs SQL de §4.
- **Guardadas / cerradas:** guardada vigente → `guardadas` (y en el dashboard con chip "Guardada");
  guardada que vence → sale del dashboard y entra a `cerradas`; **guardada sin match** (CA del
  explorador, y licitación cuyo match se borró) → aparece con su texto, sin score; archivada → solo en
  `archivadas`.
- **Acciones desde el registro:** Guardar una vencida la pasa a `cerradas`; Descartar una vencida la
  pasa a `descartadas`; Archivar / Desarchivar / Quitar vuelven a la pestaña correcta; CSRF.
- **Ownership:** otro usuario no ve nada ajeno en ninguna pestaña ni en los conteos.
- **Conteos** de cada pestaña = filas listadas; conteo del dashboard = pestaña `vencidas`.
- **Redirecciones** de `/seguidas`, `/seguidas?archivadas=1` y `/descartadas` (303).
- **Memoria:** `listar_vencidas_recientes` no carga matches fuera de la ventana (test que siembra
  matches viejos y verifica que no se instancian, p. ej. contando filas devueltas por la query).
- Adaptar (no borrar) los tests de `/seguidas` y `/descartadas` existentes.

## Fuera de alcance
Notas por oportunidad (salvo decisión 1) · exportar el registro · cambiar la vigencia (eso es
F-ajustes) · facetas por perfil/keyword en el registro · borrar matches.

## Despliegue (para Boris, después de la auditoría)
Sin migración: push a cualquier hora. Verificar `/registro` en las 5 pestañas, que `/seguidas` y
`/descartadas` redirijan, y el enlace "N vencidas recientes" del dashboard.

---

## Decisiones abiertas (Boris)
1. **Notas:** `OportunidadSeguida.notas` existe sin UI. (a) fuera de alcance **(por defecto)**;
   (b) campo corto (≤ 500 caracteres) en las tarjetas de Guardadas/Cerradas, guardado por HTMX.
2. **Vencimiento de una CA sin cierre:** (a) publicación + 7 días, igual que la vigencia
   **(por defecto)**; (b) `fecha_ultimo_cambio` (lo del prompt del 24-sep).
3. **Piso de score de Vencidas:** (a) 30, como se decidió **(por defecto)**; (b) 40, igual que el
   feed (ver el Paso 0 para comparar volúmenes).

*Fuente de los datos de dominio: Dirección ChileCompra.*

---

## Resultado del Paso 0 (producción, 02-oct, `data/logs/registro.txt`) [V]
| Usuario | Matches | Guardadas vigentes / no vigentes / archivadas / sin match | Vencidas recientes piso 30 / 40 |
|---|---|---|---|
| boris | 5.140 | 6 / 43 / 0 / 0 | 16 / 6 (CA 10/0, lic 6/6) |
| alejandra.oehninger.m | 2.343 | 0 / 0 / 1 / 0 | **324** / 136 (CA 294/112, lic 30/24) |
| Vgrivas | 955 | 0 / 0 / 0 / 0 | 142 / 0 (CA 141/0, lic 1/0) |
| otros 8 usuarios | 0 | 0 | 0 |
- Confirma la migración de F-guardar: boris tiene 49 guardadas (6 vigentes + 43 cerradas).
- Confirma que **no se puede** reusar el feed sin prefiltro: hasta 5.140 matches por usuario.
- Alejandra pasa el umbral de aviso (300) con piso 30. **Decisión de Boris (02-oct): piso 30 igual**
  (lo decidido el 24-sep). Consecuencias para la implementación: paginación obligatoria en
  `vencidas` (20 + "Ver 20 más"), conteo de pestaña y del dashboard con una sola query `count`, y nada
  de cargar la lista completa para contar.
- Decisiones abiertas cerradas: **1 = (a)** notas fuera de alcance; **2 = (a)** vencimiento de CA sin
  cierre = publicación + 7 días; **3 = (a)** piso 30 (ajustable con `REGISTRO_MIN_SCORE_VENCIDAS`).
