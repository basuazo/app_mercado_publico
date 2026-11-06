# Prompt — F-dis-1 (fundamentos del rediseño: tokens, átomos, macros, menú nuevo y preferencias) · 7-nov-2026

> Para Claude Code. **Modelo: Opus**: cambia `base.html` para todas las pantallas, crea el sistema de macros que
> usarán las demás fases y lleva una migración. Un commit, **sin push**, **con migración**.
> Leer primero `docs/diseno/README.md` (reglas comunes) y las maquetas `Main.dc.html`, `Admin.dc.html`,
> `Campana.dc.html`, `Paleta.dc.html`, `maquetas.css` y `tokens.css` de `docs/diseno/maquetas/`.
> **Va sola**: es la base de todo lo demás. Puede correr en paralelo con `F-plat-purga-legado` (no se tocan).

## Objetivo
Dejar el sistema visual «Rastrillo» disponible para todas las pantallas sin romper las que aún no se migran:
tokens claro/oscuro, átomos en CSS, macros Jinja, íconos, fuente Geist, menú lateral nuevo con campana y botón de
paleta, atajos de teclado y preferencias del usuario (tema, densidad, pantalla de inicio, filtros recordados).
**No se rediseña ninguna pantalla en esta fase**, salvo lo que vive en `base.html`.

## 1. Estáticos (`app/api/static/rastrillo/`)
- `tokens.css`: copiar `docs/diseno/maquetas/tokens.css`. Claro por defecto; oscuro con `[data-tema="oscuro"]` y
  también con `@media (prefers-color-scheme: dark)` cuando el usuario eligió «Según el sistema»
  (`[data-tema="sistema"]`).
- `atomos.css`: los átomos que usan las maquetas, escritos desde cero con los tokens (sin valores sueltos):
  `r-raiz`, `r-tarjeta`, `r-btn` (`--primario`, `--secundario`, `--terciario`, `--peligro`, `--icono`, `--sm`),
  `r-tecla`, `r-estado` (`--curso`, `--ok`, `--sin-punto`), `r-senal` (`--urgente`, `--atencion`, `--nuevo`),
  `r-seg`, `r-filtro`, `r-contador` (`--atencion`), `r-brillo` (texto con brillo mientras la IA trabaja; respetar
  `prefers-reduced-motion`), `r-dato` (`--xl`, `--fila`, `__valor`, `__nota`), `r-delta--sube`/`--baja`,
  `r-etiqueta`, `r-codigo`, `r-icono`. Ver cómo se ven en las maquetas abriendo los `.dc.html` en el canvas o
  leyendo sus estilos en línea.
- `estacion.css`: el esqueleto de `maquetas.css` renombrado sin el prefijo de maqueta cuando corresponda
  (`m-app`, `m-nav`, `m-item`, `m-fila`, `m-fila__abrir`, `m-ayuda`, `m-velo`, `m-desliza`, `m-aviso`,
  `m-presencia`, densidad compacta, etc.). Se puede conservar el prefijo `m-`.
- Fuente **Geist** y **Geist Mono** autoalojadas en `static/fonts/` (licencia OFL; dejar el archivo de licencia,
  como hoy con Inter). Solo los pesos usados (400, 500, 600 y mono 400/500), `woff2`, subconjunto latino,
  `font-display: swap`, `preload` del 400. Inter se queda mientras haya pantallas viejas.
- `rastrillo.js` (sin dependencias, `defer`):
  - `⌘K`/`Ctrl+K` abre la paleta (en esta fase, un diálogo con «Ir a» a las pantallas del menú y un campo de
    búsqueda que filtra esa lista; la búsqueda real llega en F-dis-paleta-campana).
  - `G` seguido de `H`/`M`/`T`/`V`/`D` navega a Hoy/Mesa/En trabajo/Ventas/Demanda MP. `/` enfoca el buscador de la
    pantalla (`[data-buscar]`). `Esc` cierra el panel o el modal abierto (`[data-cerrable]`). `J`/`K` y `↑`/`↓`
    recorren filas `[data-fila]` y abren la elegida con su botón `[data-abrir]`.
  - Ningún atajo se dispara con el foco en `input`, `textarea`, `select` o `[contenteditable]`.
  - Avisos con «Deshacer»: utilidad para mostrar un `m-aviso` que llega por `HX-Trigger` (`{"aviso": {"texto":
    "…", "deshacer": "/url"}}`) y hace `hx-post` a la URL de deshacer.

## 2. Macros (`app/api/templates/_r.html`)
Una macro por átomo y por patrón, con parámetros explícitos y texto siempre dentro de un `<span>` (regla 10):
`boton(texto, tipo='secundario', icono=None, href=None, tam=None, attrs={})`, `estado(texto, tipo=None)`,
`senal(texto, nivel, que_pasa, se_resuelve=None)` (con explicativo accesible: `tabindex="0"`, `role="tooltip"`,
`aria-describedby`), `dato(etiqueta, valor, nota=None, xl=False)`, `segmentado(opciones, actual, nombre)`,
`contador(n, atencion=False)`, `tecla(t)`, `vacio(texto, url_limpiar=None)`, `aviso(texto, url_deshacer=None)`,
`panel_lateral(titulo, etiqueta)` (con `caller()`), `modal(titulo)` (con `caller()`), `cabecera_lista(columnas)`.
`_iconos.html`: pasar a trazo 1,75 y agregar los íconos Lucide que usan las maquetas (licencia ISC; dejar la
licencia en un comentario). La macro `icono(nombre, tam=16)` se mantiene compatible.
Un template de muestra, solo admin, `GET /admin/diseno` (`diseno_muestra.html`): todos los átomos y patrones en claro y
oscuro, para revisar el sistema con datos reales de la app. No va en el menú.

## 3. `base.html` nuevo
- Carga `rastrillo/tokens.css`, `rastrillo/atomos.css`, `rastrillo/estacion.css` **después** de Bootstrap y
  `app.css`, que se quedan para las pantallas no migradas. Verificar que el menú nuevo no herede estilos de
  Bootstrap que lo rompan (aislarlo con sus clases).
- `<html data-tema="{{ tema }}" data-densidad="{{ densidad }}">` desde las preferencias del usuario.
- Menú lateral como `Menu` en las maquetas (ver el `<nav>` de cualquier `.dc.html`): marca «Rastrillo» + campana;
  botón de paleta con `⌘K` visible; Hoy (contador de «Te necesita» cuando exista; por ahora el conteo de pendientes
  que ya calcula la portada), Mesa, En trabajo (contador: CA recibidas sin lote), Ventas, Actividad, Competencia,
  Estrategia, Abastecimiento; grupo **Catálogo**: Demanda MP, Proveedores, Inventario, Logística, Documentos (este
  enlace apunta a `/cuenta` hasta F-dis-docs); grupo **Administración**: Configuración (`/cuenta`; admin ve además
  Empresas, Usuarios y Salud dentro de Configuración cuando se migre). El «Archivo» de la app antigua sigue
  accesible como hoy (un enlace discreto al final del grupo Administración, solo si `en_archivo` o admin).
  Pie: avatar + nombre + rol, enlace a Configuración.
- «Inicio» pasa a llamarse **«Hoy»** en el menú y en el `<title>`; la ruta `/` no cambia.
- La campana conserva su comportamiento (sondeo único, `/actividad/campana`) pero sin `dropdown` de Bootstrap: un
  `<button aria-expanded>` y un panel propio (`Campana.dc.html`). El menú móvil (offcanvas de Bootstrap) se
  reemplaza por un `<dialog>` con el mismo menú.
- Los toasts y modales de Bootstrap que usa `base.html` se quedan mientras alguna pantalla los use; documentar en
  un comentario cuáles faltan migrar.

## 4. Preferencias del usuario (migración)
- `usuarios.tema` (`claro|oscuro|sistema`, por defecto `claro`), `usuarios.densidad` (`comoda|compacta`, por
  defecto `comoda`), `usuarios.inicio` (`hoy|mesa|trabajo|ventas`, por defecto `hoy`).
- Tabla `usuario_vista` (`usuario_id`, `pantalla` String(40), `datos` JSONB ≤ 2 KB, `actualizado_en`, único por
  usuario + pantalla): filtros recordados por pantalla. Funciones en `app/mercado/preferencias.py`:
  `vista_de(session, usuario_id, pantalla) -> dict` y `guardar_vista(...)` con lista blanca de claves por pantalla
  (lo que no está en la lista se descarta). Las fases de pantalla la usan; aquí solo existe y tiene tests.
- `POST /cuenta/apariencia` (tema, densidad, inicio) con CSRF; responde `HX-Refresh`. En `cuenta.html`, un bloque
  mínimo «Apariencia» con esos tres campos (la pantalla Configuración completa es F-dis-config).
- `GET /` redirige a la pantalla de inicio elegida si no es `hoy`, salvo que venga de un enlace interno a Hoy
  (`?hoy=1`; el menú usa ese enlace).

## 5. Tests y cierre
- Render de `base.html` en claro, oscuro y sistema, con y sin empresa, admin y no admin; el menú marca
  `aria-current` en cada ruta; «Hoy» en lugar de «Inicio».
- Preferencias: valores inválidos rechazados; lista blanca de `guardar_vista`; tamaño máximo; otro usuario no lee
  las vistas del primero.
- `/admin/diseno`: 403 o redirección para no admin (patrón de `/salud`).
- Smoke de todas las rutas HTML existentes (las 95 plantillas siguen renderizando con el `base.html` nuevo).
- Lo de `docs/diseno/README.md` §13. Commit: `F-dis-1: sistema visual Rastrillo (tokens, átomos, macros), menú
  nuevo y preferencias de apariencia`. Changelog: «Nuevo menú y modo oscuro».

## 6. Pasos de Boris
1. Revisar el diff y `/admin/diseno` en local (claro y oscuro). Push y deploy.
2. Probar ⌘K, `G` `M`, el modo oscuro y la densidad compacta desde Cuenta.
