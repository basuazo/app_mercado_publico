# Prompt F-feed-ui-1 — tema visual y tarjeta nueva del feed

> **Destino en el repo:** `docs/prompt-F-feed-ui-1-tarjeta.md`
> **Fase:** F-feed-ui-1 (una fase, un commit) · **Migración:** ninguna · **Dependencias nuevas:** ninguna
> **Depende de:** F-ui-fixes (`82221c3`), ya en `main`.
> **Origen:** `docs/13-auditoria-ux.md` §3 (decisiones de diseño y tokens).

---

## Qué es esta fase y qué no

Es la **primera mitad visible** del rediseño del feed: el tema sobre Bootstrap, las familias de
estado, y la tarjeta de oportunidad reconstruida. Usa **solo los filtros que ya existen** (fuente,
perfil, texto, relevancia, orden). El panel lateral con monto, rango de cierre y estado es
F-feed-filtros más F-feed-ui-2, y no se toca aquí.

Se partió así a propósito: la tarjeta, el tema y los badges no dependen de ningún filtro nuevo, y
esperar dos fases de backend para ver el rediseño no tenía sentido.

Sigue siendo Bootstrap 5.3 + Jinja2 + HTMX. No se migra nada, no se agregan librerías.

Reglas del proyecto: español de Chile sin voseo ni argentinismos; no quitar "Fuente: Dirección
ChileCompra"; entrada en `app/changelog.py` en el **mismo commit**; `ruff check .`,
`python -m mypy app` y `python -m pytest` verdes antes de cerrar; commit en español con prefijo de
fase; **nunca `git add -A`** (arrastra `_to_delete/`, que tiene secretos y no está gitignoreado).

---

## Bloque 1 — El tema, en un archivo estático

Hoy todo el CSS es Bootstrap por CDN y hay un `<style>` inline en `base.html` con la clase `.num`
que dejó F-ui-fixes.

1. Crear `app/api/static/app.css` y montarlo con `StaticFiles` (viene en Starlette, no es
   dependencia nueva). Montarlo en `create_app` (`app/api/main.py`), en `/static`.
2. En `base.html`, después del `<link>` de Bootstrap, enlazarlo con un query de versión para
   cachear sin quedar pegado: `/static/app.css?v=<algo estable del deploy>`. Si no hay un valor de
   versión a mano, usar una constante en `app/core/settings.py` o el número de la fase; no inventar
   un hash en tiempo de request.
3. Mover ahí la clase `.num` y quitar el `<style>` inline.

El archivo define los tokens en `:root` y **pisa las variables de Bootstrap** en vez de pelear con
sus clases:

```css
:root {
  --bg-app: #F8FAFC;   --bg-surface: #FFFFFF;   --bg-subtle: #F1F5F9;
  --border: #E2E8F0;   --border-strong: #CBD5E1;
  --texto: #0F172A;    --texto-2: #475569;      --texto-3: #64748B;
  --marca: #1D4ED8;    --marca-hover: #1E40AF;  --marca-suave: #EFF6FF;  --marca-texto: #1E40AF;

  /* Familias de estado: fondo / texto / borde */
  --est-abierta-bg: #ECFDF5;   --est-abierta-fg: #065F46;   --est-abierta-bd: #6EE7B7;
  --est-eval-bg:    #EFF6FF;   --est-eval-fg:    #1E40AF;   --est-eval-bd:    #93C5FD;
  --est-adj-bg:     #F5F3FF;   --est-adj-fg:     #5B21B6;   --est-adj-bd:     #C4B5FD;
  --est-compl-bg:   #F0FDF4;   --est-compl-fg:   #166534;   --est-compl-bd:   #86EFAC;
  --est-sinef-bg:   #FEF2F2;   --est-sinef-fg:   #991B1B;   --est-sinef-bd:   #FCA5A5;
  --est-desc-bg:    #F8FAFC;   --est-desc-fg:    #475569;   --est-desc-bd:    #CBD5E1;

  /* Urgencia de cierre (borde izquierdo de la tarjeta) */
  --urg-critica: #DC2626;  --urg-alta: #EA580C;  --urg-media: #D97706;  --urg-baja: #CBD5E1;

  /* Bandas de match */
  --match-alta: #059669;  --match-media: #D97706;  --match-baja: #64748B;

  /* Pisa Bootstrap */
  --bs-body-bg: var(--bg-app);
  --bs-body-color: var(--texto);
  --bs-primary: var(--marca);
  --bs-border-color: var(--border);
  --bs-link-color: var(--marca);
  --bs-link-hover-color: var(--marca-hover);
}
```

Tipografía: el **stack del sistema**, que es lo que Bootstrap ya usa. No agregar Inter ni ninguna
fuente por CDN — la decisión está tomada y razonada en `13-auditoria-ux.md` §3.

Espaciado en múltiplos de 4. Radio 12px en tarjetas, 8px en controles. Sombra solo en hover y en
elementos flotantes. Nada de gradientes.

El anillo de foco no se toca: Bootstrap ya lo dibuja y nunca debe desaparecer.

Topbar: bajar el `bg-dark` del navbar a `#0F172A` vía la variable, sin cambiar la estructura que
dejó F-ui-fixes (el `navbar-toggler` y el `collapse` se quedan tal cual).

---

## Bloque 2 — Familias de estado

`EstadoOportunidad` tiene 16 valores y hoy todos se renderizan con el mismo `badge bg-light`. La
tarjeta nueva necesita agruparlos, y ese mapa es una decisión de dominio: va en
`app/models/enums.py`, junto al enum que agrupa.

```python
class FamiliaEstado(enum.StrEnum):
    ABIERTA = "abierta"
    EN_EVALUACION = "en_evaluacion"
    ADJUDICADA = "adjudicada"
    COMPLETADA = "completada"
    SIN_EFECTO = "sin_efecto"
    DESCONOCIDO = "desconocido"
```

| Familia | Valores |
|---|---|
| `ABIERTA` | `publicada` |
| `EN_EVALUACION` | `cerrada`, `en_proceso`, `enviada_proveedor`, `pendiente_recepcion` |
| `ADJUDICADA` | `adjudicada`, `proveedor_seleccionado`, `aceptada` |
| `COMPLETADA` | `recepcion_conforme`, `recepcion_parcial`, `recepcion_conforme_incompleta` |
| `SIN_EFECTO` | `desierta`, `revocada`, `cancelada`, `suspendida` |
| `DESCONOCIDO` | `desconocido` |

Más `familia_de_estado(estado) -> FamiliaEstado`, que ante un valor no mapeado loguea y devuelve
`DESCONOCIDO`, igual que hacen `estado_licitacion` y `estado_oc`.

Y en `app/api/presentacion.py`, una función pura que dé la etiqueta legible y la clase CSS de cada
familia (`familia_abierta`, `familia_eval`, etc.), para que la plantilla no lleve lógica.

**Test obligatorio:** recorrer `EstadoOportunidad` completo y afirmar que cada valor está mapeado
explícitamente. Cuando ChileCompra agregue un estado, el test falla en vez de dejarlo caer en
silencio a `DESCONOCIDO`.

> `suspendida` quedó en `SIN_EFECTO` y es la única asignación discutible: un proceso suspendido
> puede reanudarse, pero para quien decide si presentarse significa "no actúes ahora". Si hay que
> moverla a `EN_EVALUACION`, es una línea del mapa y su test.

F-feed-filtros va a reusar este enum y este mapa para el filtro por estado. No duplicarlos allá.

---

## Bloque 3 — La tarjeta nueva

Reescribir `app/api/templates/_card_oportunidad.html` completo. Estructura horizontal, tres
columnas arriba y dos filas abajo:

```
┌─[borde izq. 3px = urgencia]──────────────────────────────────────────┐
│  ⟠ 87      Título de la licitación (máx 2 líneas)      $48.750.000   │
│  Match     Organismo · Región · Código · Tipo             estimado   │
│                                                                      │
│  [Abierta] [Cierra en 3 días] [chip razón] [chip razón] [+2]         │
│                                                                      │
│  [Ver ficha] [Alertas] [Me sirve]                      [Descartar]   │
└──────────────────────────────────────────────────────────────────────┘
```

Detalles que importan:

**El score.** Anillo circular de 46px con el número al centro y la palabra "Match" debajo en 11px
—no "score", que es jerga. El color del anillo sale de `banda_relevancia`, que ya existe desde
F-ui-fixes: la ruta del dashboard debe calcular la banda por item y pasarla, igual que ya hace la
ruta de la ficha. **Esto elimina los últimos `>= 80` / `>= 50` del proyecto** — hoy son el único
lugar donde quedan.

**El borde de urgencia.** 3px a la izquierda, con el color según `dias_al_cierre`: ≤1 día crítica,
≤3 alta, ≤7 media, más grises. Permite escanear la columna en vertical sin leer nada.

**El monto.** A la derecha, en `.num` y con el filtro `| clp` de F-ui-fixes. Cuando es `None`, el
filtro ya devuelve "No informado": mostrarlo en itálica y `--texto-3`, nunca dejar el hueco vacío.
Debajo, la palabra "estimado" en licitaciones y "disponible" en Compra Ágil.

**El badge de estado.** Color + icono + texto, siempre los tres. Iconos como SVG inline de trazo,
nunca emoji ni una librería de iconos nueva.

**El badge de cierre — y acá va con cuidado.** `fecha_cierre` de licitaciones es **medianoche
fabricada por el parser** (`parse_fecha_v1` corta el ISO a 10 caracteres y `_fecha_a_dt` expande a
medianoche; ver `docs/00-estado-actual.md`). Así que:

- En **licitaciones**: mostrar solo la fecha (`24/09/2026`), nunca la hora. Mostrar `00:00` sería
  publicar un dato inventado como si fuera de la fuente.
- En **Compra Ágil**: fecha y hora, sin afirmar zona horaria (el huso de la fuente no está
  verificado).
- El texto del badge: "Cierra hoy", "Cierra mañana", "Cierra en N días", "Cerró el DD/MM".

La hora en licitaciones vuelve cuando se ejecute F-fecha-cierre. No adelantarla acá.

**Los chips de razón.** `razones_legibles` ya no incluye la de cierre (F-ui-fixes). Tipificarlas en
tres variantes visuales: `match` en azul de marca (keyword, rubro, organismo seguido),
`oportunidad` en verde (poca competencia, sin ofertas), `advertencia` en ámbar (monto no
informado). La clasificación se hace en `presentacion.py` como función pura, no en la plantilla.
Máximo 3 chips visibles y un chip "+N" que abra un `popover` de Bootstrap con el resto.

**Las acciones.** "Ver ficha" primario. "Activar alertas" / "Alertas activas" y "Me sirve" como
toggles con `aria-pressed` y marcador `✓` cuando están activos —el patrón que F-ui-fixes ya dejó en
`_ficha_acciones.html`. "Descartar" a la derecha, discreto, sin rojo de relleno.

**Los ganchos de accesibilidad, con el arreglo del bug que detecté auditando F-ui-fixes.** El
script de `base.html` busca `[data-accion="..."]` sobre el nodo swapeado, sin distinguir de qué
tarjeta vino. Con una sola instancia por página funcionaba; con veinte tarjetas va a devolver la
primera del DOM y el foco se irá a la tarjeta equivocada.

Corregirlo: cada botón lleva `data-accion` **y** la tarjeta lleva ya su `data-oportunidad-key`
(existe hoy). El script debe combinar los dos —guardar la clave de la tarjeta además de la acción
en `htmx:beforeRequest`, y en `htmx:afterSwap` buscar
`[data-oportunidad-key="<clave>"] [data-accion="<accion>"]`, **desde `document`**, no desde
`evt.detail.target`, porque con `hx-swap="outerHTML"` el nodo original queda fuera del árbol.
Mismo criterio para el `data-anuncio`.

Cada tarjeta lleva su `data-anuncio` describiendo el estado resultante en español: "Siguiendo:
<nombre>", "Marcada como útil: <nombre>", "Oportunidad descartada: <nombre>".

---

## Bloque 4 — Cabecera de resultados

En `index.html`, reemplazar los tres `btn-group` de ocho enlaces por controles etiquetados. **Sin
agregar filtros nuevos**: los que hay son los que hay.

- Línea de conteo: "N oportunidades · M ocultas por baja relevancia — ver todas". Si
  `total_apariciones > total_unico`, decirlo en palabras claras, no con dos números pegados.
- Orden: un `<select>` con etiqueta visible ("Orden"), opciones "Mejor match" y "Cierran antes".
  Nada de orden por monto: eso no existe en el backend todavía.
- Agrupar: un `<select>` con etiqueta visible, con las tres opciones actuales (motivo, región,
  fuente). **No agregar "sin agrupar"**: requiere soporte en `agrupar_oportunidades` y es
  F-feed-filtros.
- Relevancia: un grupo de botones con `role="group"` y `aria-labelledby` apuntando a un encabezado
  **visible** ("Relevancia"), con `aria-current="true"` y `✓` en el activo. Son enlaces, así que
  `aria-current`, no `aria-pressed`.
- Chips de filtro activo bajo el buscador, uno por filtro aplicado de los que existen (fuente,
  perfil, texto, relevancia), cada uno con su botón de quitar de al menos 24×24px, más "Limpiar
  todo (N)". Si no hay ninguno, la fila no se renderiza: no dejar un espacio vacío.

El buscador se queda donde está, con su `<label>` asociado —hoy solo tiene `placeholder`, que no
sustituye a la etiqueta (WCAG 3.3.2).

Todos los enlaces que se construyan a mano deben llevar `| urlencode` en cada valor, como dejó
F-ui-fixes. No reintroducir el bug.

---

## Bloque 5 — Deshacer el descarte

Hoy "Descartar" borra la tarjeta del DOM sin confirmación ni vuelta atrás; recuperarla exige
descubrir `/descartadas`. La ruta `POST /oportunidad/{fuente}/{codigo}/deshacer-descarte` ya
existe.

Implementar un toast de Bootstrap que aparezca al descartar, con el nombre de la oportunidad y un
botón "Deshacer" que llame a esa ruta vía HTMX, con vida de unos 8 segundos. Sin `window.confirm`:
el diálogo nativo bloquea y no aporta.

El toast vive en `base.html` (un contenedor único) y se dispara desde el mismo script que ya
remueve las apariciones duplicadas en `index.html`.

---

## Fuera de alcance (no tocar)

- Panel lateral de filtros y los filtros nuevos (monto, rango de cierre, estado): F-feed-filtros
  y F-feed-ui-2.
- `agrupar_por = "ninguno"`, paginación, conteos por faceta, "nuevas hoy": F-feed-filtros.
- La ficha (`oportunidad.html`) más allá de lo que herede del tema: fase propia.
- `/perfiles`, su separación de `/cuenta` y el widget de organismos.
- Mostrar la hora de cierre en licitaciones: F-fecha-cierre.
- Limpieza de argentinismos: fase aparte ya comprometida.

---

## Entregables

1. `app/api/static/app.css` nuevo, montado con `StaticFiles` en `app/api/main.py`, enlazado desde
   `base.html` con versión; `<style>` inline retirado.
2. `app/models/enums.py`: `FamiliaEstado`, mapa exhaustivo, `familia_de_estado`.
3. `app/api/presentacion.py`: etiqueta y clase por familia, y clasificación de razones en
   `match` / `oportunidad` / `advertencia`. Todo puro.
4. `app/api/routes/pages.py`: la ruta del dashboard calcula y pasa `banda` y la familia por item.
5. `_card_oportunidad.html` reescrito; `index.html` con la cabecera y los chips; `base.html` con el
   contenedor de toasts y el script de foco/anuncio corregido.
6. Tests offline: mapa de familias exhaustivo; clasificación de razones; render de la tarjeta con
   los seis estados, con monto `None`, y con las cuatro bandas de urgencia; render que confirme que
   ya no quedan `>= 80` ni `>= 50` en plantillas.
7. Entrada en `app/changelog.py`.
8. Sin migración.

---

## Checklist de auditoría

**Automática**

- [ ] `ruff check .`, `python -m mypy app`, `python -m pytest` los tres verdes. Anotar el conteo;
      base 603 passed / 20 skipped.
- [ ] Ningún test nuevo pega a la red ni requiere Postgres.
- [ ] `grep -rn ">= 80\|>= 50" app/api/templates/` no devuelve nada.
- [ ] `grep -rn "'{:,.0f}'" app/api/templates/` no devuelve nada.
- [ ] Ninguna fuente ni librería de iconos nueva por CDN en `base.html`.

**Manual en navegador (producción, que es donde hay datos)**

- [ ] A 375px la tarjeta no se desborda y las acciones se apilan legibles.
- [ ] El borde izquierdo distingue de un vistazo lo que cierra pronto.
- [ ] Una licitación muestra fecha de cierre **sin hora**. Una Compra Ágil muestra hora.
- [ ] Un monto ausente dice "No informado", no un hueco.
- [ ] Descartar la **cuarta** tarjeta de la lista: el toast nombra esa oportunidad, "Deshacer" la
      devuelve, y el foco queda en esa tarjeta —no en la primera. Este es el punto que más importa:
      es el bug que venía heredado.
- [ ] Un lector de pantalla anuncia cada acción con el nombre correcto de la oportunidad.

**Criterio**

- [ ] Los colores salen de variables CSS, no hay hexadecimales sueltos en las plantillas.
- [ ] Ningún estado se comunica solo con color: siempre color más icono o texto.
- [ ] Ningún texto nuevo usa voseo ni argentinismos.

---

## Commit

```
F-feed-ui-1: tema sobre Bootstrap, familias de estado y tarjeta nueva del feed

- static/app.css montado con StaticFiles: tokens en :root y variables de Bootstrap pisadas
- enums: FamiliaEstado + mapa exhaustivo de los 16 EstadoOportunidad, con test
  que falla si aparece un estado sin mapear
- presentacion: etiqueta y clase por familia, y razones tipificadas en
  match / oportunidad / advertencia
- tarjeta reconstruida: anillo de match con banda_relevancia, borde izquierdo de
  urgencia, badge de estado con icono, monto con | clp, chips de razón
- licitaciones muestran fecha de cierre sin hora: la hora actual es fabricada por
  el parser (pendiente F-fecha-cierre)
- toast de deshacer al descartar, en vez del borrado sin vuelta atrás
- corrige el foco y el anuncio tras los swaps: ahora combinan data-oportunidad-key
  con data-accion y buscan desde document, no desde el nodo desprendido
- cabecera de resultados con selects etiquetados y chips de filtro activo
- changelog: entrada de la fase

Sin migración. Sin filtros nuevos.
```

`git add` explícito por archivo o carpeta. Nunca `git add -A`.
