# Auditoría UX/UI y plan de rediseño del feed — sep 2026

> Diagnóstico hecho el 20-sep-2026 sobre las plantillas de `app/api/templates/`, la capa de
> presentación y `app/api/query.py`. La URL de producción estaba bloqueada por la política de
> egreso del entorno, así que la auditoría se hizo contra la **fuente primaria** (el código que
> Render sirve), no contra el sitio renderizado. Nada aquí se verificó en un navegador real.
>
> Contexto: F10 UX figura como completa desde julio, con la nota de que se verificó server-side
> y nunca con navegador. Esto es esa verificación pendiente, hecha sobre el código.

---

## 1. Hallazgos por severidad

### ALTA — La arquitectura de filtros está partida en dos lugares que no se hablan

El perfil (`/perfiles`) define los criterios persistentes: keywords, exclusiones, monto mín/máx,
regiones, rubros UNSPSC, organismos. El dashboard (`/`) solo ofrece fuente, perfil y texto libre.
Quien quiere "licitaciones de TI en Valparaíso sobre $10M que cierren esta semana" tiene que
editar un perfil, guardar y volver.

`get_oportunidades_usuario` **ya soporta filtro por región** (`query.py:150`) y la UI nunca lo
expone. No existe filtro por monto ni por fecha de cierre en el feed, que son las dos dimensiones
que más pesan al decidir si presentarse.

Los controles que sí existen son ocho enlaces disfrazados de botones en tres `btn-group`
consecutivos, cada grupo con un color distinto (`outline-primary`, `outline-secondary`,
`outline-dark`), sin etiqueta visible: las tres etiquetas viven solo en `aria-label`. Sin chips de
filtro activo, sin "limpiar", sin saber cuántos resultados quitó cada filtro.

*Heurísticas 6, 7, 8.*

### ALTA — La navegación se rompe en móvil

`base.html` declara `navbar-expand-lg` pero **no existe** el `navbar-toggler` ni el contenedor
`.collapse.navbar-collapse`. Bajo 992px los siete controles no colapsan: se desbordan. Es un bug
de layout, no una preferencia.

Sobre eso, la jerarquía de color del nav es arbitraria: Admin en `outline-warning`, Salud en
`outline-info`, tres links en `outline-light`, y **Salir en `outline-danger`** — el rojo de peligro
en la acción más inocua y reversible. Falta `<main>`, falta enlace de salto, falta `aria-current`.

*Heurísticas 4, 8. WCAG 1.3.1 (A), 2.4.1 (A).*

### ALTA — El feedback de las acciones HTMX es invisible y ambiguo

Las tres acciones de la tarjeta usan `hx-swap="outerHTML"`. Tres consecuencias:

No hay ninguna región `aria-live` en la app: tras el swap no se anuncia nada. Es el único criterio
AA que la app incumple de forma inequívoca. El foco se pierde, porque el nodo enfocado es
reemplazado y el foco vuelve a `<body>` — navegar el feed por teclado es inviable después de la
primera acción. Y el estado del toggle se comunica solo con color: "Me sirve" activo es
`btn-success` y no activo es `btn-outline-success`, misma etiqueta, sin `aria-pressed`.

Además "Descartar" no pide confirmación ni ofrece deshacer en el lugar, pero "Eliminar perfil" sí
usa `confirm()`. Dos acciones destructivas, dos criterios opuestos.

*Heurísticas 1, 3, 4. WCAG 1.4.1 (A), 4.1.2 (A), 4.1.3 (AA).*

### MEDIA-ALTA — Formato numérico incorrecto, sin zona horaria, información duplicada

`'{:,.0f}'.format(monto)` produce `$12,500,000` en una app chilena, donde se lee `$12.500.000`.
Aparece en tarjeta, ficha, competencia y Plan Anual. Sin `tabular-nums`, las columnas de montos no
alinean sus dígitos.

`fecha_cierre.strftime('%d/%m/%Y %H:%M')` no declara zona horaria, en el dato más crítico del
negocio, mientras la capa de ingesta ya trabaja con `ZoneInfo("America/Santiago")`.

`razones_legibles()` genera "Cierra pronto: 3 día(s)" y la tarjeta muestra **además** un badge
"cierra en 3d". El mismo dato dos veces. Las razones se renderizan como chips grises idénticos, sin
distinguir "coincide con tu keyword" (señal fuerte) de "monto no informado" (advertencia).

Ningún `<th>` del proyecto tiene `scope`. Las tablas no tienen encabezado pegajoso ni orden por
columna. La columna "Ganó" usa `✓`/`—` sin alternativa textual, y la fila propia se marca solo con
`table-success` — color como único canal.

*Heurísticas 2, 8. WCAG 1.3.1 (A).*

> Nota de honestidad: **el contraste de color pasa AA en lo grueso**, porque Bootstrap 5.3 viene
> calibrado. El problema de accesibilidad de esta app no es cromático, es semántico y de estado.

### MEDIA — `/perfiles` mezcla tres productos en 516 líneas

Conviven ajustes de cuenta (RUT, cadencia de resumen y **cambio de contraseña**), creación de
perfil y lista con edición inline. Cambiar la contraseña es una tarea de seguridad enterrada en el
medio de una pantalla de configuración de búsqueda.

El widget de rubros tiene un acordeón de checkboxes `name="categorias_unspsc"` **y debajo un input
de texto libre con el mismo `name`**: se fusionan sin avisar. El de organismos no muestra nada
hasta que se escribe, corta en 80 resultados sin decir que truncó, y está hecho de `<div>` con
`<button>` sueltos: sin semántica de combobox ni navegación por flechas.

Bug funcional con cara de bug de UX: `index.html` concatena `"texto=" ~ texto` **sin
`|urlencode`**. Una búsqueda con `&` corrompe los filtros al primer clic.

*Heurísticas 5, 6, 8.*

### Hallazgo tardío — Dos escalas de score en la misma pantalla

Los presets del feed son Alta = 60, Media = `feed_min_score_default` (40), Todas = 0. Las bandas de
color del badge son ≥80 verde, ≥50 ámbar, <50 gris. Una oportunidad con score 65 pasa el filtro
"Alta relevancia" y aparece con badge ámbar.

Se unifica hacia los presets (60/40), no hacia las bandas, porque `feed_min_score_default` es
ajustable por variable de entorno y está marcado como INFERIDO pendiente de recalibrar con datos de
producción. Amarrar el badge a un número que igual se va a mover sería peor.

### Lectura transversal

**Jerarquía visual.** El dashboard tiene un solo nivel de énfasis: todo es un botón chico con
borde. El score — el dato diferenciador del producto — es un número desnudo con la palabra "score"
en gris diminuto, sin escala ni referencia al perfil que lo generó.

**Arquitectura de información.** Siete destinos de nivel superior sin agrupación. "Descartadas" ni
siquiera está en el nav: se llega por un botón secundario que solo aparece si hay descartes.

**Agrupado.** Con `agrupar_por="motivo"` una misma oportunidad aparece en varios grupos, y el
encabezado dice "N oportunidad(es) · M aparición(es)" en la misma línea. El usuario ve el mismo
aviso tres veces sin saber si son tres o una.

**Ficha.** Scroll único de 211 líneas: badges → `<dl>` → descripción → tabla de ítems → tabla de
competencia → `<details>` → **y recién al final las acciones primarias**. En una licitación con 40
ítems, "Activar alertas" queda a dos pantallazos.

**Techo del feed.** `limit=50` fijo en `query.py:144`, sin paginación. La única forma de ver más es
"Ver más en este grupo", que recarga la página entera.

### Deuda relacionada, ya documentada

`Licitacion` no guarda región: el filtro de región del matching solo aplica a Compra Ágil y las
licitaciones pasan todas. Exponer ese filtro sin advertirlo sería prometer algo que el backend no
cumple.

---

## 2. Matriz de mejoras

| Componente / Pantalla | Problema detectado | Solución UX propuesta | Impacto en el usuario | Fase |
|---|---|---|---|---|
| Dashboard — filtros | 3 filtros volátiles; región soportada y no expuesta; sin monto ni fecha; sin chips activos | Panel lateral sticky de 288px con Perfil, Fuente, Región (con advertencia de alcance), Monto, Cierre, Estado y Relevancia; chips de filtro activo removibles y "Limpiar todo"; conteo por opción | Pasa de 3 a 7 dimensiones sin salir de la pantalla; el usuario ve qué está aplicado y lo deshace en un clic | F-feed-filtros + F-feed-ui |
| Dashboard — orden/umbral/agrupar | 8 enlaces-botón en 3 grupos sin etiqueta visible, estado solo por relleno | Select de orden, select de agrupación, toggle de relevancia etiquetado con `aria-pressed` y marcador `✓` | Desaparece el muro de botones; el estado deja de depender del color | F-feed-ui |
| Dashboard — enlaces de filtro | `texto` sin `urlencode`: un `&` rompe los filtros | `\| urlencode` en cada valor interpolado | El filtrado deja de fallar en silencio | F-ui-fixes |
| `base.html` — navegación | `navbar-expand-lg` sin toggler ni collapse; sin `<main>`, sin skip link, sin `aria-current` | Hamburguesa real bajo `lg`; `<main id="contenido">` + enlace de salto; `aria-current="page"` | La app deja de estar rota en móvil; teclado y lector de pantalla viables | F-ui-fixes |
| `base.html` — color del nav | "Salir" en `outline-danger` | Salir como ítem neutro; rojo solo para acciones destructivas de datos | El rojo recupera su significado | F-ui-fixes |
| Tarjeta — acciones HTMX | Sin `aria-live`, foco perdido, estado solo por color, descarte sin deshacer | Región `role="status"` global; foco devuelto al control equivalente; `aria-pressed` + `✓`; toast de deshacer de 8s | El feedback deja de ser solo visual; recuperarse de un descarte pasa a un clic | F-ui-fixes (a11y) + F-feed-ui (toast) |
| Tarjeta — badge de score | Número desnudo, jerga "score", sin escala ni origen | Anillo de progreso rotulado "Match", banda 60/40, tooltip con el perfil y las razones de más peso | El número deja de ser opaco | F-feed-ui |
| Tarjeta — razones | Chips grises indiferenciados; la razón de cierre duplica el badge | Quitar la razón de cierre; tipificar en `match` (azul), `oportunidad` (verde) y `advertencia` (ámbar); máximo 3 + "+N" | Menos ruido, señal jerarquizada | F-ui-fixes (quitar) + F-feed-ui (tipificar) |
| Formato de datos (global) | `$12,500,000`; sin `tabular-nums`; cierre sin zona horaria | Filtros Jinja `clp` y `numero` con punto de miles; `tabular-nums` en toda columna numérica; sufijo "hrs" previa verificación | El escaneo de montos deja de exigir traducción mental | F-ui-fixes |
| Ficha | Scroll único de 211 líneas con las acciones al final | Cabecera pegajosa con acciones primarias fijas; cuerpo en pestañas Resumen / Ítems / Cronograma / Competencia | Las acciones pasan de dos scrolls a un clic | Fase posterior |
| Tablas | Sin `scope`, sin sticky, sin orden; `✓` sin alternativa; fila propia solo por color | `scope="col"`, `thead` sticky, orden por columna; `✓` con texto para lector; chip "Tu empresa" además del fondo | Tablas de 40+ filas navegables | F-ui-fixes (semántica) + fase posterior (sticky/orden) |
| Feed | `limit=50` fijo, sin paginación; duplicados entre grupos | Paginación de 20 con "Cargar más" cuando no hay agrupación; motivos como chips en la tarjeta en vez de grupos repetidos | Desaparece el techo invisible y el conteo triple | F-feed-filtros + F-feed-ui |
| `/perfiles` | Tres productos en una página | Separar `/cuenta` de `/perfiles`; creación en panel lateral; edición en diálogo | Cada tarea tiene su lugar | Fase posterior |
| `/perfiles` — rubros | Dos inputs con el mismo `name` que se fusionan | Buscador con selección múltiple; prefijos finos en sección "Avanzado" con nombre propio | Desaparece la fusión silenciosa | Fase posterior |
| `/perfiles` — organismos | Sin semántica de combobox, corta en 80 sin avisar, chips de 9px | Combobox accesible; "Mostrando 80 de 1.333"; objetivos táctiles ≥24px | Un selector de 1.333 elementos deja de ser inutilizable por teclado | Fase posterior |
| Estados | 16 valores renderizados con el mismo badge gris | Mapa de 6 familias con color, icono y texto | Un vistazo distingue Publicada de Adjudicada o Revocada | F-feed-filtros (mapa) + F-feed-ui (badges) |

---

## 3. Decisiones de diseño

El lienzo del dashboard rediseñado vive como Artifact de tipo Design, un artboard a 1440×1640.

**Sin sistema de diseño.** Los dos que la cuenta tiene son de Caminatas; mp-oportunidades es otro
producto. Se diseñó sobre Bootstrap 5.3 (lo que la app ya usa) más los tokens de estado y urgencia
definidos abajo.

**Tipografía: el stack del sistema, no Inter.** La auditoría recomendaba Inter y se descartó:
mantener la referencia visual actual y meter una tipografía nueva eran incompatibles, y de las dos
la tipografía era la prescindible.

**El agrupado por defecto pasa a "sin agrupar".** Hoy agrupa por motivo, y eso hace que la misma
licitación aparezca en tres grupos. Los motivos pasan a ser chips dentro de la tarjeta.

**La urgencia de cierre es un borde izquierdo de 3px**, no solo un badge: permite escanear la
columna en vertical sin leer nada.

**"No informado" se escribe.** El monto ausente no deja un hueco, que se lee como error de la app.

**La advertencia del filtro de región va pegada al control**, no en una nota aparte.

**El feed va a ancho completo (1096px)**, no a los 880 de la auditoría: con el panel lateral
comiéndose 288px, capar la tarjeta dejaba una franja muerta.

### Tokens

```
Superficies   --bg-app #F8FAFC   --bg-surface #FFFFFF   --bg-subtle #F1F5F9
Bordes        --border #E2E8F0   --border-strong #CBD5E1   --focus-ring #2563EB
Texto         --text-primary #0F172A   --text-secondary #475569   --text-tertiary #64748B
Marca         --brand #1D4ED8   --brand-hover #1E40AF   --brand-subtle #EFF6FF   --brand-text #1E40AF
```

`--text-tertiary` nunca bajo 14px. Todos los pares texto/fondo de estado superan 7:1.

| Familia de estado | Valores de `EstadoOportunidad` | Fondo | Texto | Borde | Icono |
|---|---|---|---|---|---|
| Abierta | `publicada` | `#ECFDF5` | `#065F46` | `#6EE7B7` | círculo lleno |
| En evaluación | `cerrada`, `en_proceso`, `enviada_proveedor`, `pendiente_recepcion` | `#EFF6FF` | `#1E40AF` | `#93C5FD` | reloj |
| Adjudicada | `adjudicada`, `proveedor_seleccionado`, `aceptada` | `#F5F3FF` | `#5B21B6` | `#C4B5FD` | check |
| Completada | `recepcion_conforme`, `recepcion_parcial`, `recepcion_conforme_incompleta` | `#F0FDF4` | `#166534` | `#86EFAC` | doble check |
| Sin efecto | `desierta`, `revocada`, `cancelada`, `suspendida` | `#FEF2F2` | `#991B1B` | `#FCA5A5` | prohibido |
| Desconocido | `desconocido` | `#F8FAFC` | `#475569` | `#CBD5E1` | interrogación |

> `suspendida` en "Sin efecto" es la única asignación discutible: un proceso suspendido puede
> reanudarse. Se agrupó ahí porque, para quien decide si presentarse, significa "no actúes ahora".
> Pendiente de confirmar con Boris.

```
Urgencia (borde izq.)  ≤1 día #DC2626 · ≤3 días #EA580C · ≤7 días #D97706 · >7 días #CBD5E1
Bandas de match        ≥60 #059669 · 40–59 #D97706 · <40 #64748B   (mismos cortes que los presets)
Chips de razón         match → azul marca · oportunidad → verde · advertencia → ámbar
```

Espaciado en múltiplos de 4. Panel lateral 288px. Card: radio 12px, padding 16/18. Densidad
cómoda 44px / compacta 36px en tablas.

---

## 4. Plan de implementación — estado real al 22-sep-2026

El plan original eran tres fases. Terminaron siendo seis, porque auditar F-ui-fixes destapó dos
problemas de datos que pesaban más que el rediseño y que había que arreglar antes de construir
encima.

| Fase | Commit | Estado | Qué dejó |
|---|---|---|---|
| F-ui-fixes | `82221c3` | hecha, en prod | Bugs y accesibilidad sobre lo que no se rediseñaba, más los helpers puros de `presentacion.py` |
| F-feed-ui-1 | `bbe3476` | hecha | Tema en `app/api/static/app.css`, `FamiliaEstado`, tarjeta nueva, toast de deshacer |
| F-fecha-cierre | `1687013` | hecha | **No estaba en el plan.** La hora real de cierre; la app perdía licitaciones su último día |
| F-feed-filtros | `bc33280` | hecha, sin pushear | `FiltrosFeed`, `ResultadoFeed`, facetas, orden por monto, paginación |
| F-coherencia | `f3a3015` | hecha, sin pushear | **No estaba en el plan.** Un solo criterio de fecha para ficha, correos y seguidas |
| F-feed-ui-2 | — | en vuelo, sin commitear | El panel lateral de filtros. Prompt en `docs/prompt-F-feed-ui-2.md` |

Después del feed, en este orden: la ficha (`oportunidad.html`) con cabecera pegajosa y pestañas; la
limpieza de argentinismos; y `/perfiles` separada de `/cuenta` con el widget de organismos
accesible.

### Por qué aparecieron dos fases que no estaban

**F-fecha-cierre.** `parse_fecha_v1` cortaba el ISO con un slice de 10 caracteres y `_fecha_a_dt`
expandía a medianoche, así que el filtro de candidatos `fecha_cierre > ahora` daba por cerrada una
licitación desde las 21:00 del día anterior. La herramienta se callaba justo el día que importaba.
Salió de tirar del hilo de una etiqueta de zona horaria que no se podía escribir.

**F-coherencia.** El badge de cierre estaba duplicado en cuatro lugares y solo uno se había
arreglado. El peor era `app/alerts/email.py`, que mandaba la medianoche fabricada al inbox.

### Restricciones que condicionaron las fases

Los conteos por faceta se calculan en Python sobre el conjunto ya cargado. Se evaluó bajarlos a una
consulta agregada y **no se puede**: cinco de los siete filtros del feed dependen de las filas de
`Licitacion`/`CompraAgil`, no de `oportunidades_match`. El razonamiento quedó en `query.py:443`.

La paginación aplica cuando `agrupar_por` es `"ninguno"`; agrupando manda el cap por grupo.

Compra Ágil dejaba `fecha_cierre` NULL por un formato que el parser no reconocía. Esa deuda murió
con F-fecha-cierre, verificado.

---

*Fuente de los datos de dominio: Dirección ChileCompra.*
