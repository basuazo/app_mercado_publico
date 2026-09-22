# Prompt F-feed-ui-2 — el panel lateral de filtros

> **Destino en el repo:** `docs/prompt-F-feed-ui-2.md`
> **Fase:** F-feed-ui-2 (una fase, un commit) · **Migración:** ninguna · **Dependencias nuevas:** ninguna
> **Depende de:** F-feed-filtros (`bc33280`) y F-coherencia (`f3a3015`), ya en la rama.
> **Origen:** `docs/13-auditoria-ux.md` y el lienzo de diseño del dashboard.

Última pieza del rediseño del feed. El backend ya existe entero: esta fase solo lo expone.

Reglas del proyecto: español de Chile sin voseo ni argentinismos; entrada en `app/changelog.py` en
el **mismo commit**; `ruff check .`, `python -m mypy app` y `python -m pytest` verdes antes de
cerrar; commit en español con prefijo de fase; **nunca `git add -A`**.

---

## Antes de escribir una línea: leer el contrato real

No inventar nombres. Leer primero y usar exactamente lo que existe:

- `app/api/query.py`: `FiltrosFeed` (qué campos acepta y con qué tipos), `ResultadoFeed` (`items`,
  `total`, `total_sin_filtro_relevancia`, `facetas`, `nuevas_hoy`), la forma exacta del dict
  `facetas`, `LIMITE_PAGINA_DEFAULT`, y los valores válidos de `agrupar_por` incluido `"ninguno"`.
- `app/api/presentacion.py`: `banda_relevancia`, `banda_urgencia`, `presentacion_estado`,
  `razones_tipificadas`, `texto_cierre`, `fecha_cierre_legible`, `formato_clp`, `formato_numero`.
- `app/models/enums.py`: `FamiliaEstado` y su mapa.
- `app/api/static/app.css`: los tokens ya definidos. **Usar las variables existentes**, no
  introducir hexadecimales nuevos en plantillas ni duplicar tokens.

Si algún campo que este prompt asume no existe con ese nombre, usar el real y anotarlo en el
resumen.

---

## Bloque 1 — El panel

Barra lateral de 288px a la izquierda del feed, con scroll propio, `position: sticky` bajo la
barra superior. Secciones en acordeón de Bootstrap, abiertas por defecto, en este orden:

1. **Perfil de búsqueda** — `<select>` con los perfiles del usuario más "Todos los perfiles".
2. **Fuente** — casillas para Licitaciones y Compra Ágil, cada una con su conteo de faceta.
3. **Región** — buscador con selección múltiple y chips. Debajo, en `text-muted small`, la
   advertencia que ya está en `/perfiles`: *"Aplica solo a Compra Ágil. Las licitaciones no traen
   región en la fuente oficial."* Es obligatoria: el filtro miente sin ella.
4. **Monto estimado (CLP)** — dos campos, desde y hasta, formateados con punto de miles. Más la
   casilla **"Incluir monto no informado", marcada por defecto**, que mapea al parámetro que ya
   existe en `FiltrosFeed`. Si se desmarca por descuido, el usuario pierde oportunidades reales.
5. **Fecha de cierre** — atajos "Cierra hoy", "3 días", "7 días", "Este mes", más rango
   personalizado. Y la casilla **"Incluir sin fecha de cierre informada", marcada por defecto**,
   por la misma razón: Compra Ágil deja ese campo nulo y sin la casilla desaparecen todas.
6. **Estado** — una casilla por `FamiliaEstado` (menos `DESCONOCIDO`, que va al final y aparte),
   cada una con el punto de color de su familia y su conteo.
7. **Relevancia del match** — grupo de tres: Alta (`_RELEVANCIA_ALTA`), Media
   (`feed_min_score_default`) y Todas. **Leer los cortes de donde ya viven**, no escribirlos como
   literales en la plantilla.

Al pie, fijos: "Aplicar filtros" (primario, ancho completo) y "Limpiar" (secundario).

Los conteos salen de `ResultadoFeed.facetas`. **No recalcularlos en la plantilla ni con otra
consulta.** Si una faceta no viene en el dict, mostrar la opción sin número en vez de inventarlo.

Bajo 1024px el panel se convierte en un `offcanvas` de Bootstrap, disparado por un botón
"Filtros (N)" que muestra el conteo de filtros activos.

---

## Bloque 2 — Chips de filtro activo

Fila bajo el buscador, un chip por valor aplicado, con la dimensión como prefijo: "Región:
Valparaíso", "Monto: $5.000.000 – $50.000.000", "Cierra: próximos 7 días". Cada chip con su botón
de quitar de al menos 24×24px y su `aria-label` describiendo qué quita. Al final, "Limpiar todo (N)".

**Si no hay filtros activos, la fila no se renderiza.** No dejar un espacio vacío reservado.

---

## Bloque 3 — Cabecera de resultados y el cambio de default

- Línea de conteo: total, `nuevas_hoy` y las ocultas por relevancia con su enlace "ver todas".
  `nuevas_hoy` ya lo calcula `contar_nuevas_hoy`; usarlo.
- **Orden**: `<select>` etiquetado con "Mejor match", "Cierran antes" y **"Monto mayor"**, que ya
  existe en `_ordenar`.
- **Agrupar**: `<select>` etiquetado que ahora incluye **"Sin agrupar"**, y ese pasa a ser el
  **default de la ruta**, reemplazando a `"motivo"`.

Ese cambio de default es deliberado y cambia lo que el usuario ve hoy: con agrupación por motivo la
misma oportunidad aparece en varios grupos y el encabezado tiene que hablar de "apariciones". Sin
agrupar, cada oportunidad aparece una vez y los motivos viven como chips en su tarjeta, que es
donde informan sin duplicar. Los tres modos de agrupación siguen disponibles para quien los quiera.

- **Paginación**: cuando `agrupar_por` es `"ninguno"`, botón "Cargar N más (quedan M)" usando
  `LIMITE_PAGINA_DEFAULT` y el `offset` que F-feed-filtros dejó listo en la capa de query pero que
  la ruta HTML todavía no expone. Esta fase lo expone. Cuando el usuario agrupa, sigue mandando el
  cap por grupo y no hay paginación global.
- Toggle de densidad cómoda/compacta, persistido en `localStorage`.

---

## Bloque 4 — El querystring

Siete dimensiones filtrables significa un querystring largo, y es el punto donde esto se rompe.

- **Cada valor interpolado va con `| urlencode`.** Es la tercera vez que aparece esta regla; el bug
  original de F-ui-fixes fue exactamente este.
- El estado completo de filtros se serializa a query params, de modo que la URL sea compartible y
  el botón atrás del navegador funcione.
- Construir los enlaces con **un solo helper** —un macro de Jinja o una función en la ruta— que
  reciba los filtros actuales y el cambio a aplicar, y devuelva la URL. Nada de concatenar a mano
  en cada control: con siete dimensiones eso garantiza que uno quede sin codificar.
- Los valores que no están activos no se serializan, para que la URL no crezca con ruido.

---

## Bloque 5 — Accesibilidad

Lo mismo que ya rige, aplicado a controles nuevos:

- Cada control con `<label>` visible asociado. Los placeholders no sustituyen etiquetas.
- El grupo de relevancia y el de atajos de cierre: `role="group"` con `aria-labelledby` apuntando a
  un encabezado **visible**. En enlaces, `aria-current="true"`; en botones reales, `aria-pressed`.
  Nunca `aria-pressed` en un `<a>`.
- Estado activo con color **más** un marcador no cromático (el `✓` que ya usa el proyecto).
- El conteo de resultados se anuncia en la región `#anuncios` que ya existe, tras aplicar filtros.
- El `offcanvas` atrapa el foco mientras está abierto y lo devuelve al botón "Filtros" al cerrarse.
- Objetivos táctiles de al menos 24×24px en los botones de quitar chip.
- No tocar el anillo de foco.

---

## Fuera de alcance

- La ficha (`oportunidad.html`) más allá de lo que herede del tema.
- `/perfiles` y su separación de `/cuenta`.
- Devolver la hora al badge de cierre de licitaciones.
- La faceta de fuente por consulta agregada: quedó evaluada y descartada por ahora en F-coherencia.
- Argentinismos.

---

## Entregables

1. `index.html` reescrito con el panel, los chips, la cabecera y la paginación.
2. `base.html` si el layout de dos columnas lo exige; `app/api/static/app.css` con lo que el panel
   necesite, usando los tokens existentes.
3. `app/api/routes/pages.py`: la ruta arma `FiltrosFeed` desde los query params, expone `offset` y
   `orden=monto`, y cambia el default de `agrupar_por` a `"ninguno"`.
4. Tests offline: la ruta arma los filtros correctamente desde el querystring; una búsqueda con `&`
   sobrevive a aplicar un filtro; los chips reflejan exactamente los filtros activos; la paginación
   aparece sin agrupar y no aparece agrupando; el default de `agrupar_por` es `"ninguno"`.
5. Entrada en `app/changelog.py`, esta vez sí como función visible para el usuario.
6. Sin migración.

---

## Checklist de auditoría

**Automática**

- [ ] `ruff check .`, `python -m mypy app`, `python -m pytest` verdes. Base: 778 passed / 20 skipped.
- [ ] `git grep -n "urlencode" -- app/api/templates/index.html` muestra el helper único, y no hay
      concatenación manual de query params fuera de él.
- [ ] `git grep -n "#[0-9a-fA-F]\{6\}" -- app/api/templates/` no devuelve nada: los colores salen de
      variables CSS.
- [ ] Ninguna consulta nueva a la base en la ruta ni en la plantilla.

**Manual en navegador (producción)**

- [ ] Aplicar región + monto + cierre a la vez: los tres chips aparecen, la URL es compartible, y
      pegarla en otra pestaña reproduce el mismo estado.
- [ ] Buscar `aseo & mantención` y aplicar un filtro del panel: el texto sobrevive.
- [ ] Desmarcar "Incluir monto no informado": el conteo baja. Volver a marcarlo: vuelve.
- [ ] Filtrar por Compra Ágil y mirar el conteo de Licitaciones: sigue mostrando cuántas habría si
      soltara el filtro, no cero.
- [ ] A 375px: el botón "Filtros (N)" abre el offcanvas, se puede filtrar y cerrar con teclado.
- [ ] Tab por el panel completo sin quedar atrapado ni perder el anillo de foco.

**Criterio**

- [ ] Los cortes de relevancia siguen viniendo de un solo lugar; no hay `60` ni `40` literales en la
      plantilla.
- [ ] La advertencia del filtro de región está visible junto al control.
- [ ] Las dos casillas "incluir…" están marcadas por defecto.
- [ ] Ningún texto nuevo usa voseo ni argentinismos.

---

## Commit

```
F-feed-ui-2: panel lateral de filtros y feed sin agrupar por defecto

- panel de 288px con perfil, fuente, región, monto, cierre, estado y relevancia,
  con los conteos por faceta que ya devolvía ResultadoFeed
- offcanvas bajo 1024px con contador de filtros activos
- chips de filtro activo removibles y "limpiar todo"
- orden por monto y paginación "cargar más" cuando no se agrupa
- el default de agrupación pasa a "ninguno": los motivos viven como chips en la
  tarjeta en vez de repetir la misma oportunidad en varios grupos
- un solo helper arma el querystring, con urlencode en cada valor
- changelog: entrada de la fase

Sin migración.
```

`git add` explícito por archivo o carpeta. Nunca `git add -A`.
