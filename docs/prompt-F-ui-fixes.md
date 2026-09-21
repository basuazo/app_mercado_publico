# Prompt F-ui-fixes — bugs de interfaz, formato de datos y accesibilidad

> **Destino en el repo:** `docs/prompt-F-ui-fixes.md`
> **Fase:** F-ui-fixes (una fase, un commit) · **Migración:** ninguna · **Dependencias nuevas:** ninguna
> **Origen:** `docs/13-auditoria-ux.md`
> **Versión:** podada el 21-sep-2026. Se sacó todo lo que `index.html` y `_card_oportunidad.html`
> van a perder igual en F-feed-ui; sería trabajo botado.

---

## Contexto

`mp-oportunidades` es FastAPI + Jinja2 + HTMX con Bootstrap 5.3 por CDN. La capa web vive en
`app/api/`: rutas en `app/api/routes/`, funciones puras en `app/api/presentacion.py`, consultas en
`app/api/query.py`, plantillas en `app/api/templates/`. No hay carpeta `static/` todavía (la crea
F-feed-ui).

Esta fase **no es un rediseño**. Corrige defectos verificados y deja escritos los helpers puros que
las dos fases siguientes van a consumir. El rediseño del feed es F-feed-ui; los filtros nuevos son
F-feed-filtros.

Reglas del proyecto que aplican:

- Español de Chile en todo texto de UI. Sin voseo, sin argentinismos.
- No quitar "Fuente: Dirección ChileCompra" del pie.
- Esta fase agrega una entrada a `app/changelog.py` en el **mismo commit**.
- Antes de cerrar: `ruff check .`, `python -m mypy app`, `python -m pytest`, los tres verdes.
- Commit en español con prefijo de fase. **Nunca `git add -A`**: arrastra `_to_delete/`, que
  contiene secretos y no está en `.gitignore`.

---

## Bloque 1 — Bugs funcionales

### 1.1 El navbar no colapsa en móvil

`base.html` declara `<nav class="navbar navbar-expand-lg navbar-dark bg-dark">` pero **no existe**
el `<button class="navbar-toggler">` ni el contenedor `<div class="collapse navbar-collapse">`. En
Bootstrap 5 eso significa que bajo 992px los siete controles se desbordan.

Agregar el patrón estándar: `navbar-toggler` con `data-bs-toggle="collapse"`, `data-bs-target`,
`aria-controls`, `aria-expanded="false"` y `aria-label="Abrir navegación"`; los enlaces y el
formulario de salir dentro del `div.collapse.navbar-collapse`.

El `hx-headers` con el token CSRF está en `<body>`: **no moverlo**, cualquier cambio ahí rompe todas
las acciones HTMX.

### 1.2 El texto de búsqueda no se codifica en los enlaces de filtro

`index.html` arma el querystring a mano y `texto` entra crudo:

```jinja
{% set filtros_qs = "fuente=" ~ fuente ~ "&texto=" ~ texto ~ (("&perfil_id=" ~ perfil_id) if perfil_id else "") %}
```

Buscar `aseo & mantención` parte el querystring y corrompe los filtros al primer clic en orden,
umbral o agrupación.

Aplicar `| urlencode` a **cada valor** interpolado, en `filtros_qs`, en `ver_todas_href` y en el
enlace de `grupo_expandido`. Es lo único que esta fase toca de `index.html`: la plantilla se
reescribe en F-feed-ui, pero el bug está vivo en producción ahora y el arreglo es de una línea.

Test de render: una búsqueda con `&` produce `%26` en los `href` de los controles de orden y
agrupación.

---

## Bloque 2 — Helpers puros y formato de datos

Estos tres helpers son la base que F-feed-filtros y F-feed-ui van a usar. Van en
`app/api/presentacion.py`, que es el módulo de funciones puras sin BD ni red.

### 2.1 Formato de montos y cantidades

Hoy las plantillas usan `'{:,.0f}'.format(monto)`, que produce `$12,500,000`. En Chile se lee
`$12.500.000`.

```python
def formato_clp(valor: float | int | None) -> str:
    """Monto en pesos chilenos con punto como separador de miles.

    None -> "No informado": el organismo no lo publica con frecuencia y el
    hueco se lee como error de la aplicación.
    """
    if valor is None:
        return "No informado"
    return "$" + f"{valor:,.0f}".replace(",", ".")


def formato_numero(valor: float | int | None) -> str:
    """Cantidad sin símbolo de moneda, mismo separador. None -> '—'."""
    if valor is None:
        return "—"
    return f"{valor:,.0f}".replace(",", ".")
```

Localizar dónde se construye el `Jinja2Templates` (revisar `app/api/main.py` y `app/api/deps.py`) y
registrarlas como filtros `clp` y `numero`.

Reemplazar `'{:,.0f}'.format(...)` en **`oportunidad.html` y `plan_anual.html`**. `_card_oportunidad.html`
queda como está: se reescribe en F-feed-ui.

Tests puros: cero, negativo, `None`, y un monto de diez dígitos.

### 2.2 Banda de relevancia con una sola fuente de verdad

Los presets del feed son Alta = 60 y Media = `feed_min_score_default` (40). Las bandas de color del
badge son ≥80 verde, ≥50 ámbar. Una oportunidad con score 65 pasa "Alta relevancia" y sale ámbar:
dos definiciones de "alta" en la misma pantalla.

```python
def banda_relevancia(score: float, corte_alta: int, corte_media: int) -> str:
    """'alta' | 'media' | 'baja', con los mismos cortes que los presets del feed."""
```

Los cortes salen de donde ya salen `relevancia_alta` y `relevancia_media` para la plantilla
(localizar: probablemente `app/core/config.py` más una constante en `app/api/routes/pages.py`).
**No duplicar los valores**: si el 60 está hardcodeado en la ruta, dejarlo en un solo lugar y que
tanto el preset como la banda lo lean de ahí.

Aplicarlo en el badge grande de `oportunidad.html`, que hoy usa 80/50. La tarjeta del feed lo toma
en F-feed-ui.

Tests puros: los valores de corte exactos, justo debajo y justo encima, 0 y 100.

### 2.3 Quitar la razón de cierre duplicada

`razones_legibles()` genera "Cierra pronto: 3 día(s) para el cierre" y "Cierra hoy", mientras la
tarjeta y la ficha muestran **además** un badge de cierre. El mismo dato dos veces en el mismo
bloque visual.

Quitar el bloque que deriva frases de `dias_al_cierre`. El badge queda como único portador.

Buscar y actualizar los tests existentes de `razones_legibles`. Confirmar que el resto sigue
intacto: keywords, rubro, organismo seguido, ofertas, monto no informado.

### 2.4 Alineación de columnas numéricas

En `base.html`, dentro de un `<style>` inline mínimo (el archivo estático lo crea F-feed-ui):

```css
.num { font-variant-numeric: tabular-nums; }
```

Aplicarla a montos, cantidades y códigos en `oportunidad.html` y `plan_anual.html`.

### 2.5 Zona horaria del cierre — VERIFICAR ANTES DE TOCAR

`oportunidad.html` renderiza `fecha_cierre.strftime('%d/%m/%Y %H:%M')` sin sufijo, en el dato más
crítico del negocio.

**Antes de agregar cualquier etiqueta**, verificar cómo se almacena `fecha_cierre`: la columna en
`app/models/`, el parseo en `app/ingest/` y `app/clients/`, y si el datetime es naive o aware.

- Guardado como hora local de Chile: agregar `hrs` junto a la hora y, una vez por pantalla, la
  aclaración "Horas en hora de Chile continental".
- Guardado en UTC: **no convertir en esta fase** y **no poner la etiqueta**. Documentar el hallazgo
  en el resumen y en `docs/00-estado-actual.md` como pendiente; convertir toca la capa de datos y
  merece fase propia con tests.

Regla 20/23 del proyecto: no escribir como hecho algo que no se verificó en la fuente.

---

## Bloque 3 — Accesibilidad estructural

El contraste de color pasa AA en lo grueso porque Bootstrap viene calibrado. Lo que falla es
semántica y estado.

### 3.1 Landmark y enlace de salto

`base.html` mete el contenido en un `<div class="container py-4">` pelado: sin `<main>`, sin enlace
de salto (WCAG 2.4.1, A).

Convertirlo en `<main id="contenido" class="container py-4">`, y como primer hijo de `<body>`, antes
del `<nav>`, un `<a class="visually-hidden-focusable …" href="#contenido">Saltar al contenido</a>`
que se haga visible al recibir foco.

### 3.2 Página actual en la navegación

`Jinja2Templates` deja `request` en el contexto: comparar `request.url.path` y agregar
`aria-current="page"` más la clase `active` al enlace correspondiente (`/`, `/perfiles`,
`/seguidas`, `/plan-anual`, `/admin/usuarios`, `/salud`).

### 3.3 Región de anuncios y foco tras los swaps de HTMX

Ninguna región `aria-live` existe en la app. Tras un `hx-swap="outerHTML"` no se anuncia nada
(WCAG 4.1.3, AA) y el foco vuelve a `<body>`, lo que hace inviable navegar el feed por teclado.

En `base.html`, que es donde vive lo común a todas las fases:

1. Un único `<div id="anuncios" role="status" aria-live="polite" class="visually-hidden"></div>`.
2. Un script inline que escuche `htmx:afterSwap`, lea un atributo `data-anuncio` del nodo nuevo y
   lo copie a `#anuncios.textContent`.
3. En `htmx:beforeRequest`, guardar un identificador del control enfocado (un `data-accion` en cada
   botón de acción); en `htmx:afterSwap`, buscar en el nodo nuevo el botón con ese mismo
   `data-accion` y hacerle `.focus()`.

Los `data-anuncio` y `data-accion` de la tarjeta del feed los pone F-feed-ui. Esta fase deja el
mecanismo montado y lo aplica en `_ficha_acciones.html`, que no se rediseña ahora.

Sin librerías nuevas. Si algún caso no sale limpio, dejarlo anotado en el resumen en vez de
forzarlo.

### 3.4 Encabezados de tabla sin `scope`

Ninguna tabla del proyecto declara `scope` (WCAG 1.3.1, A). Agregar `scope="col"` a todos los `<th>`
de encabezado de columna en `oportunidad.html` (ítems, resumen de competencia, detalle por ítem),
`plan_anual.html`, `admin_usuarios.html` y `salud.html`. Revisar si hay otras.

### 3.5 Iconos y filas marcadas solo con color

En el detalle de competencia, la columna "Ganó" usa `✓` y `—` sin texto: envolver en
`<span aria-hidden="true">✓</span><span class="visually-hidden">Adjudicado</span>`, y el caso
negativo con "No adjudicado".

La fila del propio proveedor se marca solo con `class="table-success"`. Agregar, además del fondo,
`border-start border-4 border-primary` y un `<span class="badge text-bg-primary">Tu empresa</span>`
junto al nombre. El color deja de ser el único canal.

### 3.6 Estado de los toggles en la ficha

En `_ficha_acciones.html` y en los botones de acción de `oportunidad.html`, los toggles cambian solo
de relleno. Agregar `aria-pressed="true|false"` y, cuando está activo, anteponer un `✓` con
`aria-hidden="true"` más `<span class="visually-hidden">seleccionado</span>`.

---

## Bloque 4 — Textos y jerarquía

### 4.1 El rojo del nav

"Salir" usa `btn-outline-danger`, el rojo de peligro en la acción más inocua y reversible de la app,
mientras Admin va en `outline-warning` y Salud en `outline-info`. Dejar "Salir" como botón neutro y
reservar el rojo para acciones destructivas de datos.

### 4.2 El filtro de región promete más de lo que cumple

Deuda conocida: `Licitacion` no guarda región, así que el filtro de región del matching solo aplica
a Compra Ágil y las licitaciones pasan todas.

En `perfiles.html`, junto a "Regiones (vacío = todas)" de ambos formularios, una nota en
`text-muted small`:

> El filtro de región aplica solo a Compra Ágil. Las licitaciones no traen región en la fuente
> oficial, así que no se filtran por este criterio.

Texto solamente. El comportamiento del matching no cambia en esta fase.

---

## Fuera de alcance (no tocar)

- `index.html` más allá del `urlencode` de 1.2, y `_card_oportunidad.html` completo: se reescriben
  en F-feed-ui.
- Los filtros nuevos del panel (monto, cierre, estado, región expuesta), los conteos por faceta y la
  paginación: son F-feed-filtros.
- `app/api/static/` y el montaje de `StaticFiles`: los crea F-feed-ui.
- Toast de deshacer en "Descartar", panel lateral, ficha con pestañas, separación de `/perfiles` y
  `/cuenta`, rediseño del widget de organismos.
- Conversión de zona horaria si `fecha_cierre` resulta estar en UTC (ver 2.5).
- Limpieza de argentinismos: fase aparte ya comprometida. Si aparece una expresión argentina en un
  texto que esta fase toca igual, corregirla y anotarlo; no salir a buscarlas por el resto del código.

---

## Entregables

1. `app/api/presentacion.py`: `formato_clp`, `formato_numero`, `banda_relevancia`, y
   `razones_legibles` sin la razón de cierre. Registro de los dos primeros como filtros Jinja.
2. Plantillas: `base.html`, `index.html` (solo el `urlencode`), `oportunidad.html`,
   `_ficha_acciones.html`, `perfiles.html`, `plan_anual.html`, `admin_usuarios.html`, `salud.html`.
3. Tests nuevos, todos offline: formato CLP y número, banda de relevancia, render con `&` en la
   búsqueda, y render que confirme `<main>`, enlace de salto, `navbar-toggler` y la región
   `aria-live`. Más los tests actualizados de `razones_legibles`.
4. Entrada en `app/changelog.py` con la fecha del commit, en lenguaje simple: "Arreglamos la
   navegación en celulares, los montos ahora se muestran en formato chileno y mejoramos el uso con
   lector de pantalla."
5. Sin migración.

---

## Checklist de auditoría

**Automática**

- [ ] `ruff check .` limpio.
- [ ] `python -m mypy app` limpio.
- [ ] `python -m pytest` verde. Anotar el conteo; no debe bajar de 578 passed / 20 skipped salvo por
      tests reemplazados.
- [ ] Ningún test nuevo pega a la red.

**Manual en navegador (la hace Boris)**

- [ ] A 375px: el navbar colapsa a hamburguesa, abre, y los enlaces son alcanzables.
- [ ] Buscar `aseo & mantención` y hacer clic en "Cierran pronto": el texto sobrevive y los
      resultados siguen filtrados.
- [ ] Los montos se leen `$12.500.000` en ficha y Plan Anual, con los dígitos alineados.
- [ ] Tab desde el inicio revela "Saltar al contenido" y funciona.
- [ ] En la ficha, activar alertas: el foco queda en el mismo botón, aparece el `✓`, y un lector de
      pantalla anuncia el cambio.
- [ ] Una licitación con score entre 60 y 79 muestra badge verde en la ficha.
- [ ] En una adjudicada con RUT de proveedor cargado, la fila propia muestra el chip "Tu empresa".

**Criterio**

- [ ] Los cortes de relevancia viven en un solo lugar. Buscar `60` y `80` sueltos en rutas y
      plantillas para confirmar que no quedó duplicado.
- [ ] El hallazgo sobre la zona horaria quedó escrito, sea cual sea el resultado, marcado como
      verificado o inferido.
- [ ] Ningún texto nuevo usa voseo ni argentinismos.

---

## Commit

```
F-ui-fixes: navegación móvil, formato CLP chileno y accesibilidad AA

- base.html: navbar-toggler + collapse, <main>, enlace de salto, región aria-live
  y restitución de foco tras los swaps de HTMX
- index.html: urlencode de los valores del querystring en los enlaces de filtro
- presentacion.py: formato_clp, formato_numero y banda_relevancia (puras, con tests)
- unifica el badge de score de la ficha con los presets del feed (60/40)
- quita de razones_legibles la frase de cierre, ya cubierta por el badge
- aria-pressed / aria-current / scope / alternativas textuales en iconos
- nota de alcance del filtro de región en /perfiles
- changelog: entrada de la fase

Sin migración.
```

`git add` explícito por archivo o carpeta. Nunca `git add -A`.
