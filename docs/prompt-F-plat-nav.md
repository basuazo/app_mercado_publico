# Prompt de implementación — F-plat-nav (la plataforma pasa a ser la app; la antigua queda en «Archivo» solo para el admin) · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet** (navegación y permisos sobre rutas que ya existen). Una fase = un commit,
> **sin push**. **Sin migración.** Decisión de Boris (4-nov): la plataforma de Compra Ágil es la interfaz principal; la
> app antigua sale del menú y queda accesible solo para el admin, sin borrar nada. La estética nueva viene en otra fase
> (F-plat-ui), cuando Boris pase su referencia: **aquí no se rediseña nada.** Lo marcado [V] está verificado en el
> código (HEAD `a1cbcc2`).

## 0. Qué hay hoy [V]
- `app/api/templates/base.html` muestra a todos: Perfiles, Cuenta, Explorar CA, Mi registro y Plan Anual. Para quien
  tiene empresa activa, además, el menú «Mesa» con Proveedores, Logística y Catálogo. Para el admin: Admin, Empresas y
  Salud.
- `GET /` (`pages.py`) es el feed de la app antigua.
- Rutas de la app antigua en `pages.py`: `/`, `/descartadas`, `/seguidas`, `/registro`, `/perfiles*`,
  `/rubros-favoritos*`, `/organismos/catalogo.json`, `/plan-anual*`, `/compras-agiles*`, las acciones sobre el feed y
  la ficha (`/oportunidad/{fuente}/{codigo}/descartar-opciones`, `excluir-vista-previa`, guardar, descartar, etc.) y
  sus POST.
- **La plataforma usa la ficha de CA** (`/oportunidad/ca/{codigo}`, pestaña «Líneas» de F-plat-1) **y `/cuenta`**
  (`dias_resumen`, que leerá el informe mensual de F-plat-7). Esas dos siguen visibles para quien tenga empresa.

## 1. Cambios
### 1.1 Portada
- `GET /`: con empresa activa, va a la portada de la plataforma. Hasta que exista `/mesa` (F-plat-5), es una página
  **«Hoy»** simple, con un bloque por pieza que ya existe:
  - CA abiertas de las regiones activas (`compras_agiles`, `estado='publicada'`, cierre futuro);
  - líneas por revisar en el catálogo (`ca_lineas` `pendiente|dudosa`, con enlace a `/catalogo`);
  - proveedores y productos cargados de la empresa (enlace a `/proveedores`).

  Solo conteos y enlaces, una consulta por bloque. Pie: "Fuente: Dirección ChileCompra" (regla 8). F-plat-5 cambiará
  esta portada por la mesa.
- Sin empresa activa y no admin: el aviso de F-plat-3 (`proveedores_sin_empresa.html` o equivalente). Admin sin empresa:
  la misma portada «Hoy», con el aviso de que falta asignarse una empresa.
- El feed antiguo se mueve a **`GET /archivo`**, con la misma vista y los mismos filtros. Revisar las plantillas y el JS
  del feed (paneles HTMX, filtros, paginación, `hx-get` y formularios con `action="/"`, redirects a `/` después de
  guardar o descartar) y apuntarlos a `/archivo`. Listar en el commit qué se cambió.

### 1.2 Permisos de la app antigua
- Una dependencia `requiere_admin_archivo` en **todas** las rutas de la app antigua, GET y POST, salvo la ficha de CA
  y `/cuenta`. Si el usuario no es admin: un GET responde 303 a `/` y un POST, 403. El control es en el servidor,
  porque esconder el menú no alcanza (regla 17).
- **La ficha de `/oportunidad/{fuente}/{codigo}`:** con `fuente = ca`, sigue abierta para usuarios con empresa. Las
  **acciones** de la app antigua dentro de la ficha (guardar, descartar, excluir términos, seguimiento) quedan solo
  para el admin: no se muestran a los demás, y su POST devuelve 403. Con licitaciones (`fuente != ca`), solo el admin.
- No se borra ninguna ruta, plantilla, job ni tabla. El retiro definitivo es otra fase, que Boris decide (§4).

### 1.3 Menú (`base.html`)
- Con empresa activa, menú de primer nivel: **Inicio** (`/`), **Catálogo** (`/catalogo`), **Proveedores**
  (`/proveedores`) y **Logística** (`/proveedores/logistica`). Se quita el desplegable «Mesa»: son 3 o 4 entradas y no
  hace falta. F-plat-5 y F-plat-6 agregarán **Mesa** y **Abastecimiento** a este mismo nivel.
- A la derecha: Cuenta, Novedades y Salir.
- Solo admin: Empresas, Usuarios, Salud y **Archivo** (`/archivo`). Dentro de Archivo, el submenú de la app antigua:
  Feed, Perfiles, Mi registro, Explorar CA y Plan Anual.
- La marca sigue como está ("MP Oportunidades") hasta que Boris elija un nombre (F-plat-ui).
- Accesibilidad: mantener `aria-current`, el "Saltar al contenido" y el toggler móvil
  (`tests/test_ui_accesibilidad.py`).

### 1.4 Changelog
Una entrada visible: "La plataforma de Compra Ágil pasa a ser la portada; la app anterior queda en Archivo".

## 2. Tests
- Portada: con empresa → «Hoy» con los tres conteos; sin empresa → aviso; admin sin empresa → «Hoy» con aviso.
- `/archivo` para el admin → el feed de siempre (reusar los asserts de los tests del feed, cambiando la URL).
- Usuario no admin con empresa:
  - GET a cada ruta antigua (lista parametrizada) → 303 a `/`;
  - POST a cada acción antigua → 403;
  - la ficha de CA → 200, sin botones de guardar ni descartar;
  - la ficha de licitación → 303.
- Menú: lo que ve el usuario, lo que ve el admin y lo que ve alguien sin empresa.
- Los tests existentes del feed, el registro, los perfiles y el plan anual se ajustan para correr como admin y contra
  `/archivo`. Anotar en el commit cuántos se tocaron.

`ruff check .`, `python -m mypy app` y `python -m pytest -rs` contra `mp-test-pg` (`TEST_DATABASE_URL`).

## 3. Cierre
`00-estado.md` y `decisiones.md`, con la decisión del 4-nov (plataforma principal; la app antigua en Archivo, solo
admin; el retiro se decide aparte). Commit: `F-plat-nav: la plataforma es la portada y la app anterior pasa a Archivo`.

## 4. Fuera de esta fase (para que no se pierda)
- **F-plat-ui:** la estética nueva desde la referencia de Boris: tokens de color y tipografía, componentes base y la
  portada. Va antes de F-plat-5, para que la mesa nazca con el diseño nuevo. Si significa dejar Bootstrap, se propone y
  se justifica primero (stack de `CLAUDE.md`).
- **F-retiro (decisión de Boris, no antes de que la sombra de F-plat-5 esté andando):**
  - borrar las rutas, plantillas, jobs, workflows y tests de la app antigua;
  - borrar sus tablas, con export previo: `licitaciones`, `licitacion_items`, `ofertas_competencia`,
    `perfiles_busqueda`, `oportunidades_match`, `oportunidades_seguidas`, `match_feedback`, `alertas`,
    `plan_compra_*`, `instituciones_pac`, `rubros_*`.

  Eso libera espacio en Neon, que importa porque el backfill se detiene a los 350 MB, y deja un solo producto. Es
  irreversible: requiere el OK explícito de Boris.
