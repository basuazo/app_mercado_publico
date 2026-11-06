# Rediseño Rastrillo — reglas comunes de las fases F-dis (7-nov-2026)

Todas las fases `docs/prompt-F-dis-*.md` empiezan leyendo este archivo. Lo que dice aquí manda sobre lo que el
prompt no repita.

## Fuentes de diseño
- `docs/diseno/maquetas/*.dc.html`: copia de las maquetas del canvas «Rastrillo · Maquetas». Son **referencia
  visual y de comportamiento**, no código de producción: HTML con huecos `{{…}}` y un `<script type="text/x-dc">`
  con datos inventados. Lo que vale es la estructura, los textos, los estados y las interacciones.
  - `maquetas.css`: esqueleto (menú, filas, panel, explicativos, modal, densidad).
  - `tokens.css`: colores claro y oscuro (oscuro gris neutro), radios, sombras, espacios y duraciones.
  - El CSS de los átomos (`bundle.css` del sistema «Rastrillo») no está en el repo: F-dis-1 lo reescribe a partir de
    las clases que usan las maquetas (`r-btn`, `r-estado`, `r-senal`, `r-dato`, `r-seg`, `r-filtro`, `r-contador`,
    `r-tecla`, `r-brillo`, `r-etiqueta`, `r-codigo`, `r-icono`, `r-tarjeta`, `r-delta--sube/baja`).
- Decisiones y vocabulario: `docs/diseno/decisiones-diseno.md` (copia del handoff `09-handoff-diseno.md`).

## Mapa maqueta → pantalla
| Maqueta | Ruta | Templates actuales |
|---|---|---|
| `Main.dc.html` (Hoy) | `/` | `hoy.html` |
| `Mesa.dc.html` | `/mesa` | `mesa_dia.html`, `_mesa_*.html`, `_precio_oferta.html` |
| `Lote.dc.html` (En trabajo) | `/trabajo`, `/lotes/{id}` | `trabajo.html`, `lotes.html`, `lote.html`, `lote_aviso.html`, `static/lote.js` |
| `Enviar.dc.html` | paso 3 del lote | `lote.html` (sección envío), `_requisitos.html` |
| `Cotizacion.dc.html` | DOCX de `cotizacion.py` | (documento) |
| `Ventas.dc.html` | `/ventas`, `/ventas/{id}`, `/caja` | `ventas.html`, `venta.html`, `caja.html` |
| `Documentos.dc.html` | nueva `/documentos` | `requisitos_plantilla.html` (hoy solo textos) |
| `Campana.dc.html`, `Paleta.dc.html` | componentes de `base.html` | `_campana_*.html` |
| `Actividad.dc.html` | `/actividad` | `actividad.html` |
| `Competencia.dc.html` | `/competencia`, `/competencia/{rut}` | `competencia.html`, `competencia_perfil.html`, `_competencia_macros.html` |
| `Estrategia.dc.html` | `/estrategia` | `estrategia.html` |
| `Abastecimiento.dc.html` | `/abastecimiento` | `abastecimiento.html` |
| `Demanda.dc.html` (Demanda MP) | `/catalogo`, `/catalogo/revision`, `/catalogo/anexos/*` | `catalogo*.html`, `_anexo_*.html`, `_catalogo_*.html` |
| `Proveedores.dc.html` | `/proveedores`, `/proveedores/{id}` | `proveedores*.html`, `proveedor_*.html`, `_proveedor_form.html`, `_producto_*.html` |
| `Inventario.dc.html` | `/inventario` | `inventario*.html`, `_inventario_*.html` |
| `Logistica.dc.html` | `/proveedores/logistica` | `proveedores_logistica.html` |
| `Admin.dc.html` (Configuración) | `/cuenta`, `/admin/*`, `/salud` | `cuenta.html`, `admin_*.html`, `salud.html` |

## Reglas de todas las fases F-dis
1. **Antes de tocar nada:** `git status`, `git log --oneline -5` y `alembic heads`. Si algún archivo que la fase
   va a tocar tiene cambios sin commitear de otra sesión, **detenerse y avisar**. Si hay más de una cabeza de
   Alembic, detenerse.
2. **Fases en paralelo:** cada sesión trabaja en su propio `git worktree` y su rama `F-dis-<nombre>` desde `main`.
   Usa su propia base de tests (`TEST_DATABASE_URL` con otro nombre de base) para no pisar a la otra sesión.
   El conflicto en `app/changelog.py` es esperado y lo resuelve Boris al fusionar (las dos entradas quedan).
3. **Una sola fase con migración a la vez.** `down_revision` = la cabeza que muestra `alembic heads` al empezar.
4. **El diseño es presentación.** Las fases de vestir no cambian reglas de negocio de `app/mercado`: llaman a las
   funciones que existen. Si falta un dato, se agrega como función pura en `app/mercado` (sin HTML ni `Request`),
   con tests, y el prompt lo dice explícitamente.
5. **Estrangulamiento:** desde F-dis-1, `base.html` es el nuevo y Bootstrap sigue cargado para las páginas no
   migradas. Cada fase reemplaza **por completo** los templates de sus pantallas (sin versión doble ni flag), usa
   las macros de `_r.html` y no agrega clases de Bootstrap. F-dis-cierre saca Bootstrap.
6. **Precios (F-plat-impuestos):** margen, sugerido, P(ganar) y comparaciones con la competencia se calculan
   sobre el **neto**. Donde se paga o se cobra se muestra el **bruto** (solicitudes, cotización, caja, ventas). Si
   la maqueta muestra solo «bruto», mostrar el neto en el explicativo o la ficha. Usar siempre
   `app.mercado.impuestos` (`bruto`, `neto_desde_bruto`, `impuesto_de`); nunca recalcular el IVA en la vista. La
   señal «Impuesto por clasificar» es una `r-senal` de atención.
7. **Vocabulario:** «Precio desactualizado · N días» (más de 15 días; reemplaza «Precio vencido»), «Peak» (no
   «pico»), «En trabajo», «Hoy», «Demanda MP», «Configuración». Montos con `$` y punto de miles; «bruto» rotulado
   donde se ingresa o se copia un monto.
8. **Tres gramáticas:** lo que se aprieta (botones, con hover), lo que se lee (`r-estado`, tenue) y lo que te
   necesita (`r-senal` con ícono y frase). El índigo `accion` es solo para lo accionable. **Un primario por zona.**
   Cada señal tiene explicativo al pasar el mouse o con foco: informativo, sin botones («Qué pasa» + «Se resuelve…»).
9. **Patrón lista + panel:** clic en cualquier parte de la fila abre el panel lateral (HTMX); `J`/`K` o `↑`/`↓`
   recorren; `Esc` cierra; con el panel abierto se ocultan columnas secundarias. Estados vacíos con «Limpiar
   filtros». Acciones que cambian datos muestran un aviso con «Deshacer» cuando la acción es reversible.
10. **Texto en elementos `inline-flex` con gap** (`r-estado`, `r-senal`, `r-btn`, `r-dato__nota`): la frase va en
    un solo nodo (`<span>`), o el gap la parte.
11. **Accesibilidad:** foco visible (`--foco`), `aria-current`, `aria-pressed`, `aria-expanded`, `role="status"`
    para avisos, contraste AA en claro y oscuro, todo usable con teclado. Los atajos no se disparan dentro de inputs.
12. **Reglas del repo que siguen vigentes:** regla 9 (nada de leer el HTML de mercadopublico.cl; las ofertas se
    ingresan a mano en el portal), regla 8 («Fuente: Dirección ChileCompra» donde hay datos públicos), reglas
    17/26 (todo filtrado por `empresa_id`), regla 12 (consultas en lote, sin N+1), secretos nunca en el HTML.
13. **Cierre de cada fase:** `ruff check .`, `python -m mypy app`, `python -m pytest -rs` (0 fallos, 0 errores,
    0 saltados), smoke de render de cada template tocado en claro y oscuro, entrada en `app/changelog.py` si es
    visible, `git add` solo de los archivos de la fase, un commit, **sin push**.
