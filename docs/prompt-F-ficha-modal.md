# Prompt F-ficha-modal — la ficha se abre en un modal sobre el feed

> **Destino en el repo:** `docs/prompt-F-ficha-modal.md`
> **Fase:** F-ficha-modal (una fase, un commit) · **Migración:** ninguna · **Dependencias nuevas:** ninguna
> **Depende de:** F-ficha-volver (commit aparte), F-vigencia, F-guardar y F-registro. Es la 5.ª y última
> fase de la serie del 24-sep: se ajustó a "Guardar" y a "Mi registro" después de diseñarla.
> **Origen:** `docs/prompt-D-ficha-modal.md` (brief de diseño), lienzo Design "Ficha en modal —
> MP Oportunidades" (artboards A licitación con ítems, B CA sin detalle, C adjudicada con
> competencia, D móvil) y `docs/13-auditoria-ux.md`.

Pedido de Boris (24-sep): al cliquear una licitación o CA, abrir un popup y no una página nueva,
para no perder los filtros ni el lugar en la lista.

Reglas del proyecto: español de Chile sin voseo ni argentinismos; entrada en `app/changelog.py` en
el **mismo commit**; `ruff check .`, `python -m mypy app` y `python -m pytest` verdes antes de
cerrar; commit en español con prefijo de fase; **nunca `git add -A`** (`_to_delete/` tiene un `.env`).

---

## Antes de escribir una línea: leer el contrato real

- `app/api/routes/pages.py`: `oportunidad_detalle`, `_url_volver_feed`, `_render_card_partial`
  (`origen="dashboard"|"ficha"`), la ruta `guardar` de F-guardar, `archivar`/`desarchivar` y
  `descartar`/`deshacer-descarte`, y la página `/registro` de F-registro.
- `app/api/templates/oportunidad.html`, `_ficha_acciones.html`, `_card_oportunidad.html`,
  `base.html` (región `#anuncios`, `#toasts`, script de foco y toast de deshacer).
- `app/api/presentacion.py`: `banda_relevancia`, `banda_urgencia`, `presentacion_estado`,
  `razones_tipificadas`, `texto_cierre`, `formato_clp`. `app/models/enums.py`: `FamiliaEstado`.
- `app/api/static/app.css`: tokens. **Usar variables existentes**; no hexadecimales nuevos en
  plantillas.

Si algo que este prompt asume no existe con ese nombre, usar el real y anotarlo en el resumen.

---

## Qué construir

### 1. Contenido de la ficha como parcial reutilizable
Extraer el cuerpo de `oportunidad.html` a `_ficha_contenido.html`. La página completa
`/oportunidad/{fuente}/{codigo}` **se mantiene** (los correos de alerta enlazan ahí) y lo incluye.
Con header `HX-Request`, la misma ruta devuelve solo el parcial (sin `base.html`). Un solo
contexto, una sola plantilla de contenido: nada duplicado.

### 2. Modal en el feed y en Mi registro
- Un solo `<div class="modal" id="modal-ficha">`, en un parcial incluido por `index.html` **y**
  `registro.html` (misma conducta en ambas: la pestaña y los filtros del registro tampoco se pierden), `modal-xl
  modal-dialog-scrollable`, `modal-fullscreen-md-down`. `aria-labelledby` al título.
- Título de la tarjeta y "Ver ficha" conservan su `href` real (clic medio / nueva pestaña /
  sin JS siguen funcionando) y agregan `hx-get` al mismo path con `hx-target="#modal-ficha
  .modal-content"`. Clic normal → modal; clic con modificador → navegación normal.
- **URL**: al abrir, `history.pushState` agrega `ficha=<fuente>:<codigo>` a la query ACTUAL (sin
  tocar los filtros). Atrás (`popstate`) cierra el modal. Cerrar con × o Esc hace
  `history.back()` si el modal lo abrió el push, para no dejar entradas huérfanas. Cargar `/`
  con `ficha=` abre el modal al entrar (enlace compartible). `url_feed`/`_url_feed` **no**
  deben propagar `ficha` a los enlaces de filtros ni a "Ver 20 más".
- **Anterior / Siguiente**: recorren las tarjetas presentes en el DOM del feed
  (`[data-oportunidad-key]`, en orden). Sin estado en servidor. En el borde, deshabilitado.
  Muestra "N de M" con M = tarjetas cargadas.
- Foco: al abrir, al título; al cerrar, de vuelta al enlace de la tarjeta que lo abrió.
  Anuncio en `#anuncios`: "Ficha abierta: <nombre>".

### 3. Layout del modal (según lienzo)
- **Cabecera**: tipo · código; título; chip de estado por familia (icono + texto); chip de
  cierre con urgencia y hora ("hrs"); monto a la derecha ("No informado" si falta); anillo
  "Match" con la banda 60/40. Navegación y × arriba a la derecha.
- **Barra de acciones inmediatamente bajo la cabecera** (no al final): "Abrir en Mercado
  Público ↗" o el texto actual de por qué no está; **Guardar / Guardada ✓** y Descartar
  (los artboards del lienzo aún muestran "Activar alertas" y "Me sirve": se diseñaron antes de
  F-guardar; donde dicen eso va un solo botón Guardar).
  **Sin "Volver"** en el modal (sí en la página completa).
- **Cuerpo**: pestañas Descripción · Ítems/Productos (N) · Competencia (N). Una pestaña sin datos
  no se renderiza. Pestaña inicial: Ítems si hay; si no, Descripción. Pestañas con
  `role="tablist"`/`tab`/`tabpanel` (Bootstrap `nav-tabs`).
- **Resumen lateral de 300px** (arriba en `md-down`): organismo, región, publicación, ofertas
  (CA), razones tipificadas máx. 3 + "+N".
- **Sin detalle** (sin descripción y sin ítems): aviso "Todavía sin descripción ni productos — La
  app aún no descarga el detalle de esta Compra Ágil. Mientras tanto, revísalo en la ficha
  oficial." (artboard B).
- **Cierre vencido**: función pura nueva en `presentacion.py` → si la familia es Abierta y
  `fecha_cierre < ahora` (hora de Chile), chip ámbar "Publicada · cierre vencido" + aviso
  "Figura como publicada, pero su fecha de cierre ya pasó. Revisa la ficha oficial antes de
  preparar una oferta." Con F-vigencia estas ya no salen en el dashboard; el chip sigue sirviendo
  en Mi registro y en la página completa (enlaces de correo).
- "Fuente: Dirección ChileCompra" una sola vez en el modal (pie).
- Móvil (artboard D): pantalla completa, acciones fijas abajo, objetivos de 44px.

### 4. Acciones dentro del modal
- `guardar`/`descartar`/`deshacer-descarte` con `origen=modal`: la respuesta trae la barra de
  acciones del modal **y** la tarjeta del feed por `hx-swap-oob` (seleccionar por
  `data-oportunidad-key`, no por `id`: el id lleva índice). Descartar desde el modal mantiene el
  modal abierto con "Descartada · Restaurar" y saca la tarjeta del feed con el toast de
  deshacer existente.
- `archivar`/`desarchivar` (visibles en el modal solo si la oportunidad está guardada): si todavía
  recargan la página con `next`, agregarles rama HTMX (`HX-Request`) que devuelva la barra del
  modal y la tarjeta OOB. Sin JS siguen funcionando igual. CSRF igual que hoy.
- Guardar desde el modal abierto en la pestaña Vencidas del registro saca la tarjeta de esa
  pestaña (pasa a Cerradas) y actualiza los conteos de las pestañas.
- Ownership (regla 17): el parcial pasa por `check_oportunidad_access` igual que la página; un
  código ajeno devuelve 404 también por HTMX.

---

## Tests (mínimo)
1. GET ficha con `HX-Request` devuelve el parcial sin `<nav class="navbar` ni `<html`.
2. GET ficha sin `HX-Request` sigue devolviendo la página completa con "Volver".
3. Usuario sin acceso → 404 en ambos modos.
4. Pestañas: sin ítems no aparece la pestaña Ítems; sin competencia no aparece Competencia.
5. CA sin descripción ni productos muestra el aviso de detalle faltante.
6. Cierre vencido: función pura con `fecha_cierre` pasada y estado publicada → "vencida";
   tarjeta y ficha muestran "cierre vencido", no "Abierta".
7. `guardar` con `origen=modal` devuelve la barra + un elemento con `hx-swap-oob` que apunta a la
   tarjeta.
8. `archivar` con `HX-Request` no redirige (200 con parcial); sin HTMX sigue redirigiendo a `next`.
11. El modal abre desde `/registro?tab=vencidas` y al cerrarlo la pestaña y los filtros siguen iguales.
9. `_url_feed` no arrastra el parámetro `ficha`.
10. "Fuente: Dirección ChileCompra" aparece una sola vez en el parcial.

## Validación del diseño contra la auditoría (hecha 24-sep, fila por fila)

| Fila de `13-auditoria-ux.md` §2 | Estado en el diseño |
|---|---|
| Ficha — scroll único con acciones al final | **Cumple**: barra de acciones bajo la cabecera, visible sin scroll en A, B y C |
| Ficha — pestañas Resumen/Ítems/Cronograma/Competencia | **Cumple parcial**: Resumen pasa a columna lateral siempre visible. **Cronograma queda fuera**: la tabla `licitaciones` solo guarda publicación y cierre [V, `app/models/tables.py`]; no se diseña algo sin datos |
| Estados — 16 valores con el mismo badge gris | **Cumple** en la ficha (hoy muestra el slug "publicada") + familia nueva "cierre vencido" |
| Tarjeta — badge de score opaco | **Cumple**: anillo "Match" con banda 60/40 |
| Tarjeta — razones indiferenciadas | **Cumple**: chips tipificados, máx. 3 + "+N" |
| Formato — `$12,500,000`, sin zona horaria | **Cumple**: CLP con punto y "hrs" |
| Tablas — `scope`, fila propia solo por color | **Cumple**: `scope="col"`, chip "Tu empresa" + borde interior |
| Tarjeta — acciones HTMX sin feedback | **Cumple**: `aria-pressed`, anuncio, foco devuelto, toast existente |
| Feed — filtros se pierden al entrar a la ficha | **Resuelto**: el feed nunca se descarga |

**Puntos ciegos que el diseño NO resuelve (no son de UI):**
- Licitaciones sin organismo ni región: `_parse_licitacion_detalle` lee `CodigoOrganismo` en el
  primer nivel y en el detalle v1 viene bajo `Comprador` [V: `docs/10-enlace-ficha.md` §2.a]. →
  fase propia **F-organismo-lic**.
- Estados que nunca se actualizan: `refresh_estados` solo mira cierres en −7/+3 días y
  `sync_activas` no cierra lo que sale del listado [V, código]. → fase propia
  **F-estados-vencidos**. El chip "cierre vencido" es la mitigación visible mientras tanto.

## Fuera de alcance
Separar `/perfiles` y `/cuenta`; revisión en vivo de `/perfiles`, seguidas, Plan Anual y móvil.

*Fuente de los datos de dominio: Dirección ChileCompra.*
