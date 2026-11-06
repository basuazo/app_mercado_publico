# Prompt — F-dis-cierre (sacar Bootstrap y el CSS viejo; revisión de oscuro y accesibilidad) · 7-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit, **sin push**, **sin migración**. Va **sola y al final**, cuando todas
> las fases F-dis estén en `main`. Leer `docs/diseno/README.md`.

## 1. Inventario de lo que queda viejo
Listar con `grep` las plantillas que todavía usan clases de Bootstrap (`btn`, `card`, `row`, `col-`, `form-control`,
`dropdown`, `modal`, `toast`, `table`, `badge`, `d-…`, `text-muted`…) o clases de `app.css`. Para cada una: migrarla a
las macros de `_r.html` si es de la app nueva; si es del **Archivo** (app antigua: perfiles, registro, explorar CA,
plan anual, ficha de licitación), aplicarle solo el esqueleto mínimo para que se lea bien sin Bootstrap, sin rediseñarla.
## 2. Quitar
Bootstrap CSS y JS del CDN, `app.css` y la fuente Inter (si ya no se usa). Reemplazar los usos restantes de
`window.bootstrap` (modales, toasts, offcanvas) por `<dialog>` y los avisos de `rastrillo.js`.
## 3. Revisión
- **Modo oscuro:** recorrer cada pantalla en oscuro; nada de colores fijos fuera de los tokens (`grep` de `#` y `rgb(`
  en templates y CSS, salvo la cotización impresa).
- **Accesibilidad:** contraste AA (calcular con un script sobre los tokens), foco visible, `aria-*` de filas, paneles y
  modales, navegación completa con teclado, `prefers-reduced-motion`.
- **Rendimiento:** peso total de CSS/JS/fuentes por página (meta: < 120 KB comprimido sin contar HTMX), sin CDN salvo
  HTMX (o autoalojarlo también y anotarlo).
## 4. Cierre
Smoke de render de **todas** las rutas HTML en claro, oscuro y compacta. Cierre según `docs/diseno/README.md` §13.
Commit: `F-dis-cierre: sin Bootstrap; revisión de modo oscuro y accesibilidad`. Changelog visible.
