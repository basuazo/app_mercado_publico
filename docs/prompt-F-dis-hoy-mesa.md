# Prompt — F-dis-hoy-mesa (vestir Hoy y la Mesa con el sistema Rastrillo) · 7-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit, **sin push**, **sin migración**.
> Leer `docs/diseno/README.md`, `docs/diseno/decisiones-diseno.md` (secciones Hoy y Mesa) y las maquetas `Main.dc.html`
> y `Mesa.dc.html`. Requiere en `main`: F-dis-1, F-dis-datos y el cierre de F-plat-impuestos.
> Puede correr en paralelo con F-dis-lote-datos (worktree propio).

## 1. Hoy (`hoy.html`, ruta `/` en `pages.portada`)
Reemplazar el template completo con la maqueta `Main.dc.html`, usando solo datos de `app/mercado/hoy.py`:
- Cabecera: «Hoy», fecha y «actualizado HH:MM»; segmentado de período (`hoy|semana|mes`, querystring, recordado con
  `usuario_vista('hoy')`); primario «Ir a la mesa».
- Franja **«Desde tu última visita»** (`hoy.novedades`), con un botón por novedad y «Marcar como visto» (`POST
  /hoy/visto`, HTMX, la franja desaparece). No se muestra si no hay novedades.
- Cifras (`hoy.cifras`), la primera en grande (`dato(..., xl=True)`), con delta y nota.
- **«Te necesita»** (`hoy.te_necesita`) con filtros por tipo (Todo, Cierres, Ventas, Precios, Líneas, Anexos) con
  conteo, ícono por tipo, señal con explicativo, título, detalle y un botón con la acción. Ordenado como entrega la
  función. Estado vacío: «Nada urgente. Buen momento para revisar la Mesa.»
- Embudo (`hoy.embudo`) con enlaces por etapa como en la maqueta; Actividad (últimas 5) con «Ver todo» → `/actividad`.
- `pages.portada` deja de calcular los conteos viejos que ya no se muestran (sin borrar funciones de `app/mercado`).

## 2. Mesa (`mesa_dia.html` y parciales)
- Lista densa con la maqueta `Mesa.dc.html`: **Cierre** primera columna (urgente si cierra hoy), CA (nombre +
  comprador · comuna), monto, oferta sugerida, margen, P(ganar), señales (`hoy.senales_por_ca`, con explicativo
  informativo «Qué pasa» + «Se resuelve en En trabajo»), responsable (iniciales) y n.º de comentarios.
- **Encabezados ordenables** (Cierre, Monto, Margen, P(ganar)) por querystring; por defecto cierre ascendente.
  Segmentado de cierre (hoy, mañana, esta semana) y los filtros que ya existen. Filtros y orden recordados en
  `usuario_vista('mesa')`.
- **Panel lateral** con `GET /mesa/ca/{codigo}/panel` de F-dis-datos, cargado con `hx-get` al hacer clic en cualquier
  parte de la fila (`m-fila__abrir` estirado) y por `J`/`K`/`↑`/`↓` (`data-fila`, `data-abrir` de `rastrillo.js`);
  `Esc` cierra; «anterior / siguiente · n de N». Con el panel abierto se ocultan columnas secundarias. La URL lleva
  `?ca={codigo}` (`hx-push-url`) y se recuerda la última CA abierta (`usuario_vista('mesa').abierta`).
- Panel: cabecera, cifras, líneas con costo (neto; bruto en el explicativo; «Precio desactualizado · N días»;
  «Impuesto por clasificar»), trabajo (estado, **responsable con selector**, lote), **conversación del equipo**
  (reusar `/equipo/ca/{codigo}/hilo` y `_equipo_hilo.html` vestido: menciones como pastilla, Enter envía),
  fragmentos existentes de comprador, competidores y precio (`hx-get` diferido), enlaces a la ficha.
- **Selección múltiple**: casillas por fila y barra «N CA · Cotizar en lote» que crea o agrega a un lote con el
  endpoint que ya existe (`POST /lotes`), y lleva a `/lotes/{id}`.
- Fuente: Dirección ChileCompra al pie.

## 3. Tests y cierre
Render de Hoy con y sin datos, con y sin novedades, en los tres períodos; Mesa con orden, filtros, `?ca=`, panel,
selección múltiple; `aria-current`, `aria-expanded` en filas; teclado (smoke de atributos `data-fila`/`data-abrir`).
Cierre según `docs/diseno/README.md` §13. Commit: `F-dis-hoy-mesa: Hoy como bandeja y Mesa con panel lateral`.
Changelog visible: «Hoy muestra lo que te necesita y lo nuevo desde tu última visita; la Mesa abre el detalle al
costado».
