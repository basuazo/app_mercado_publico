# Prompt de implementación — F-perfiles-1 (`/perfiles` liviano y legible, `/cuenta` aparte) · 09-oct-2026

> Para Claude Code. **Modelo: Sonnet.** Una fase = un commit, **sin push**, **sin migración**.
> Origen: `docs/14-auditoria-integral.md` §4.3 (U1–U4) y §4.4-1, y los pendientes de `/perfiles` de
> la auditoría UX del 22-sep (`docs/archivo/13-auditoria-ux.md`). El asistente de perfil con vista
> previa en vivo es **F-perfiles-2**, no esta fase.
> Lee `CLAUDE.md` antes de empezar (reglas 16–18: ownership en servidor, CSRF en mutaciones).

## 0. Antes de escribir código
Lee estos archivos:
- `app/api/templates/perfiles.html` (452 líneas), `_rubros_widget.html` y `app/api/static/rubros_widget.js`. Ojo: el widget de rubros **también lo usa el explorador de Compras Ágiles** y no se puede romper.
- `app/api/routes/pages.py`, desde `perfiles_get` (≈ línea 1738) hasta `perfil_editar` (≈ 2012):
  - `_parse_monto` (≈ 1690) frente a `_monto` (≈ 326);
  - `_parse_regiones`, `_parse_categorias`, `_parse_organismos`;
  - `/perfiles/rut-proveedor` y `/cuenta/*`.
- `app/matching/perfiles.py`: `listar_perfiles` devuelve **solo los activos**.
- `app/matching/engine.py`: `match_todos` filtra por `activo` y `criterio_perfil`.
- `app/core/vigencia.py`: `condicion_lic_vigente` y `condicion_ca_vigente`.
- `app/api/presentacion.py`: `nombre_region` y el filtro `clp`.
- `app/api/templates/base.html`: el nav.

## 1. Problemas que resuelve (medidos en la auditoría)
- **U1.** `/perfiles` pesa ~1,5 MB con 4 perfiles. El widget de rubros (510 checkboxes, ~277 KB) se dibuja una vez por perfil más una para "Nuevo perfil". Además se incrustan ~1.333 organismos en JSON y un `<select>` de ~510 opciones. Encima, el GET puede sincronizar catálogos por red.
- **U2.** Si hay un error al crear o editar, la página redirige con `?error=` y **se pierde todo lo escrito**.
- **U3.** El resumen de cada perfil es ilegible:
  - montos crudos ("5000000 – — CLP");
  - regiones y organismos como códigos;
  - no dice cuántas oportunidades trae;
  - el campo `activo` existe pero no hay forma de pausar un perfil.
- **U4.** La lista de perfiles queda al fondo, después de tres tarjetas. Los formularios de nuevo y de editar están duplicados (~60 líneas cada uno). Los montos usan `type=number`, que no acepta "5.000.000".
- **Abierto del 22-sep:**
  - dos inputs de rubros con el mismo `name` (`categorias_unspsc`);
  - los organismos no tienen combobox y se cortan en 80 resultados sin aviso;
  - labels sin `for`.

## 2. Cambios

### 2.1 Estructura de las pantallas
- **`/perfiles`** muestra, en este orden:
  1. encabezado con el botón "Nuevo perfil";
  2. **lista de perfiles como tarjetas**;
  3. "Rubros favoritos" en una tarjeta plegable al final.

  Los ajustes de cuenta salen de esta página.
- **`/cuenta` (nueva):** RUT de proveedor, frecuencia del resumen y cambio de contraseña. Las rutas POST existentes se mantienen (`/perfiles/rut-proveedor` y `/cuenta/*`): solo cambia dónde se muestran y a dónde redirigen después de guardar.
- **Nav:** agregar "Cuenta" junto a Perfiles. No reorganizar el resto del nav, porque eso va en F-bandeja.

### 2.2 Tarjeta de perfil (resumen legible)
- **Nombre y estado.** Interruptor **Activo/Pausado** (`POST /perfiles/{id}/activo` con CSRF y ownership). Un perfil pausado sigue apareciendo en `/perfiles` con la etiqueta "Pausado", pero no entra en el matching, el feed ni el resumen.
  - `listar_perfiles` devuelve solo activos y lo usan otras partes, así que agrega `listar_perfiles(..., incluir_pausados=True)` o una función nueva solo para esta página. No cambies lo que reciben los demás.
  - Reactivar un perfil lanza `_match_perfil_background`, igual que al editar.
- **Conteo:** "N oportunidades vigentes" (licitaciones · CA), más la fecha del último match nuevo.
  - Usa **una sola consulta agrupada** para todos los perfiles del usuario, no una por perfil: `oportunidades_match` con join a `licitaciones`/`compras_agiles` y las condiciones de vigencia de `vigencia.py`.
  - Un perfil pausado muestra el último conteo con la nota "(pausado)", o "—".
- **Criterio en lenguaje simple:**
  - palabras clave y exclusiones como chips;
  - fuentes con su nombre ("Licitaciones", "Compras Ágiles");
  - regiones con `nombre_region`;
  - montos con el filtro `clp` ("$5.000.000 – sin máximo");
  - rubros con su nombre (ya existe `rubros_por_perfil`);
  - **organismos con su razón social**, resuelta desde `instituciones_pac`; si no hay nombre, mostrar el código.
- **Acciones:** "Editar" y "Eliminar". Eliminar mantiene la confirmación pero con un modal de Bootstrap, no con `confirm()`.

### 2.3 Formulario único, cargado bajo demanda (lo que baja el peso)
- **Un solo parcial** `_perfil_form.html` para crear y editar, que reemplaza los dos bloques duplicados.
- Se carga con HTMX **solo al abrirlo**: `GET /perfiles/nuevo/form` y `GET /perfiles/{id}/form`, con ownership verificado (404 si es ajeno).
  - La página inicial **no** incluye ningún widget de rubros ni el catálogo de organismos.
  - Abrir un formulario lo trae dentro de la tarjeta, sin navegar.
- **Organismos:**
  - el catálogo se entrega como JSON en `GET /organismos/catalogo.json`, cacheable (`Cache-Control: private, max-age=86400`) y solo para usuarios logueados;
  - el formulario lo pide **una vez por página**;
  - un **único** widget de organismos (`static/organismos_widget.js`) con chips, búsqueda sin tildes y aviso "Mostrando 80 de N; afina la búsqueda" cuando se corta;
  - el código JS que hoy está duplicado entre `perfiles.html` y `rubros_widget.js` (`debounce`, `crearChip`, `quitarChip`) pasa a `static/util.js` y lo usan ambos.
- **Sincronización de catálogos:** `sync_instituciones_pac` y `sync_sectores_organismos` salen del GET de `/perfiles` y se ejecutan al pedir el catálogo JSON, con su TTL de siempre. Si fallan, el JSON devuelve lo que haya en caché, como hoy.
- **Rubros:**
  - el widget sigue igual en comportamiento, pero el input de "prefijos finos" pasa a llamarse `categorias_unspsc_extra` y va dentro de un `<details>` "Avanzado";
  - `_parse_categorias` une ambos campos;
  - **ajusta también el explorador de CA** si usa ese input, o deja un alias, y verifica que el explorador siga funcionando (hay tests).
- **Montos:**
  - inputs `type=text` con `inputmode="numeric"` y formato de miles al salir del campo;
  - el servidor usa **`_monto`** (que ya acepta "$5.000.000") en vez de `_parse_monto`;
  - quita `_parse_monto` si queda sin uso.
- **Accesibilidad:** todos los `<label>` llevan `for` y los ids son únicos por formulario (prefijo `nuevo_` o `p{id}_`).

### 2.4 Errores sin perder lo escrito
- Si hay un error de validación (`PerfilInvalido`, exclusiones que chocan, nombre vacío), `POST /perfiles/nuevo` y `POST /perfiles/{id}/editar` **responden 422 con el mismo parcial del formulario re-renderizado**:
  - con los valores enviados;
  - con el mensaje junto al campo que falla.
- Con HTMX (`hx-post`, `hx-target` en el contenedor del formulario) se reemplaza solo el formulario.
- Sin JS, la página completa se re-renderiza con el formulario abierto y los valores.
- Si todo sale bien:
  - redirige a `/perfiles` (o `HX-Redirect`) con el aviso "Perfil guardado. Buscando oportunidades…";
  - la tarjeta muestra "Actualizando…" hasta que el conteo cambie.
  - Basta un `hx-trigger="load delay:5s"` que recargue la tarjeta una o dos veces. Nada de websockets.

### 2.5 Peso objetivo
- `GET /perfiles` con 4 perfiles: **< 150 KB** de HTML, sin contar recursos estáticos cacheables.
- Debe quedar un test que lo verifique con una cota holgada (por ejemplo < 200 KB) sobre un fixture de 4 perfiles con rubros y organismos.

## 3. Fuera de alcance
- Asistente por pasos, vista previa en vivo, validación de keywords con conteo y sugerencias de exclusiones: **F-perfiles-2**.
- Score, feed, organismos seguidos en el matching: **F-match-1**.
- Reorganizar el nav completo, Bandeja y Cierres: **F-bandeja**.
- Partir `pages.py`. Si conviene, mueve las rutas de perfiles y cuenta a `app/api/routes/perfiles.py` bajo el **mismo router y las mismas URLs**. Es opcional; no partas el resto.
- La API JSON (`/api/perfiles`) no cambia.

## 4. Tests
- **`GET /perfiles`:**
  - responde 200;
  - no contiene el widget de rubros ni el JSON de organismos;
  - pesa menos de 200 KB con 4 perfiles;
  - muestra montos con `$` y miles, regiones y organismos por nombre, y el conteo de vigentes.
- **`GET /perfiles/{id}/form`:** 200 para el dueño y 404 para otro usuario. Lo mismo para `POST /perfiles/{id}/activo`.
- **Pausar:** el perfil queda `activo=False`, sigue en `/perfiles` como "Pausado" y no aparece en el feed ni en `match_todos`.
- **Reactivar:** dispara el match en background (mock).
- **Errores:**
  - crear con nombre vacío, o editar con una exclusión que choca, responde 422 con los valores enviados en el HTML (keywords, montos, regiones marcadas, rubros marcados);
  - no se crea ni se modifica nada en la base.
- **Montos:** "5.000.000", "$5.000.000" y "5000000" se guardan como 5000000.
- **Rubros:** el campo avanzado `categorias_unspsc_extra` se une con los checkboxes, y el explorador de CA sigue pasando sus tests.
- **Catálogo:** `GET /organismos/catalogo.json` exige login y, si la sincronización falla, devuelve la caché.
- **Conteo:** la consulta agrupada da lo mismo que contar perfil por perfil, sobre un fixture con vigentes y vencidas de ambas fuentes.

## 5. Cierre
- Correr `ruff check .`, `python -m mypy app` y `python -m pytest -rs` completo contra dev con `postgresql+psycopg://`: 0 fallos, 0 errores, 0 saltados.
- Entrada en `app/changelog.py`: "Mis perfiles carga más rápido y se lee mejor: cada perfil muestra cuántas oportunidades vigentes trae, sus montos y organismos con nombre, y se puede pausar sin borrarlo. Si algo falla al guardar, no pierdes lo escrito. Los ajustes de tu cuenta están ahora en 'Cuenta'".
- `git add` solo de los archivos tocados. Commit: `F-perfiles-1: perfiles livianos y legibles, pausar perfil, formulario único y Cuenta aparte`. **Sin push.**
- **Reporte:** hash, archivos, peso de `/perfiles` antes y después (bytes del HTML con el fixture), resultado de la suite y "Desvío del prompt".
- Sin migración: el push puede ir a cualquier hora.

*Fuente de los datos de dominio: Dirección ChileCompra.*
