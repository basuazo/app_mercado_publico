# Prompt de implementación — F-plat-ui (estética nueva, estilo dashboard tipo Stripe) · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet** (CSS y plantillas; sin lógica nueva). Una fase = un commit, **sin push**.
> **Sin migración.** Va **después de F-plat-nav**, porque usa su menú nuevo, y **antes de F-plat-5**, para que la mesa
> nazca con este diseño. Decisión de Boris (4-nov): estética "tipo Stripe" (referencia: stripe.com/es). Lo marcado [V]
> está verificado en el código (HEAD `a1cbcc2`).

## 0. Qué se toma de la referencia y qué no
Se toma el **lenguaje visual de un dashboard financiero sobrio**, al estilo del Dashboard de Stripe más que de su sitio
de marketing:
- fondo claro y superficies blancas con bordes finos en vez de sombras pesadas;
- un solo color de acento;
- tipografía sans limpia, con números tabulares;
- tablas densas y legibles;
- badges suaves;
- mucho aire alrededor de los bloques, poco dentro de las tablas.

**No** se copia nada de la marca Stripe: ni logo, ni ilustraciones, ni textos, ni su degradado característico, ni su
color de marca exacto (`#635BFF`). La app tiene su propia identidad; la referencia es de estilo.

Estado actual [V]:
- Bootstrap 5.3.3 y htmx 1.9.12 desde CDN (`base.html`), más `app/api/static/app.css` (328 líneas);
- navbar oscura arriba (`navbar-dark bg-dark`) con botones `btn-outline-*`;
- sin CSP en `security_headers` (`app/api/main.py`).

## 1. Decisiones técnicas
- **Se queda Bootstrap 5.3.** Se sobreescriben sus variables CSS (`--bs-*`) y algunos componentes en `app.css`, sin
  framework nuevo (stack de `CLAUDE.md`). Si algo de lo pedido no se puede lograr así, se propone en el commit, no se
  agrega nada.
- **Tipografía: Inter, autoalojada** (licencia OFL). Van en `app/api/static/fonts/` los woff2 de 400, 500 y 600,
  latin, con `font-display: swap`. Así no depende de otro CDN y la app se ve igual sin Google Fonts. Pila de respaldo:
  `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
- **Íconos:** sin librería nueva. Unos pocos SVG inline (inicio, catálogo, proveedores, logística, mesa, salud,
  archivo), de trazo de 1,5 px, en una macro Jinja (`_iconos.html`).

## 2. Tokens (`:root` en `app.css`)
| Token | Valor | Uso |
|---|---|---|
| `--c-fondo` | `#F6F8FA` | fondo de la app |
| `--c-superficie` | `#FFFFFF` | tarjetas, tablas, barra lateral |
| `--c-borde` | `#E3E8EE` | bordes de 1 px y separadores |
| `--c-texto` | `#1A2333` | texto principal (azul noche, no negro) |
| `--c-texto-2` | `#5E6B7A` | secundario, encabezados de tabla |
| `--c-acento` | `#4F46E5` | botón primario, enlaces, foco, ítem activo |
| `--c-acento-suave` | `#EEF0FF` | fondo del ítem activo y de los badges del acento |
| `--c-ok` / `--c-ok-suave` | `#0E7C4A` / `#E6F6EE` | completo, ganada |
| `--c-aviso` / `--c-aviso-suave` | `#9A5B00` / `#FFF4E0` | por revisar, cierra pronto |
| `--c-error` / `--c-error-suave` | `#C0262D` / `#FDECEC` | error, perdida |
| `--radio` | `8px` (tarjetas), `6px` (controles) | |
| `--sombra` | `0 1px 2px rgba(16,24,40,.05)` | solo tarjetas; nada más |

Mapear a Bootstrap: `--bs-body-bg`, `--bs-body-color`, `--bs-primary` (y `-rgb`), `--bs-border-color`,
`--bs-border-radius`, `--bs-link-color`, `--bs-font-sans-serif` y los `--bs-*-bg-subtle` de los badges. **Contraste AA**
de todos los pares texto/fondo (≥ 4,5:1 en texto normal): verificarlo y anotar los ratios en el commit.

Escala de texto: base 14 px; títulos de página 22 px / 600; títulos de tarjeta 15 px / 600; encabezados de tabla
12 px / 500 en mayúsculas con `letter-spacing: .04em` y color `--c-texto-2`. Montos y cantidades con
`font-variant-numeric: tabular-nums`, alineados a la derecha.

## 3. Layout y componentes
- **Barra lateral izquierda** (232 px, blanca, borde derecho):
  - arriba, la marca en texto, sin logo, hasta que Boris elija nombre;
  - las entradas del menú de F-plat-nav con ícono;
  - el ítem activo con fondo `--c-acento-suave` y texto `--c-acento`;
  - al pie: Cuenta, Novedades y Salir;
  - para el admin, una sección "Administración" (Empresas, Usuarios, Salud, Archivo).

  En pantallas < 992 px se vuelve un offcanvas de Bootstrap con botón hamburguesa. La barra oscura de arriba desaparece.
- **Encabezado de página:** título, una línea de descripción en `--c-texto-2` y las acciones a la derecha. Macro Jinja
  `encabezado(titulo, descripcion, acciones)`.
- **Tarjeta:** superficie blanca, borde de 1 px, `--radio`, `--sombra`, padding de 20 px. Las "tarjetas de número"
  (portada «Hoy») llevan etiqueta chica arriba, número grande (28 px / 600, tabular) y un enlace "Ver →".
- **Tablas:**
  - sin bordes verticales, filas de 44 px y separador `--c-borde`;
  - hover con `#FAFBFC`;
  - encabezado como en §2;
  - primera columna en `--c-texto` y el resto en `--c-texto-2` salvo los montos;
  - en móvil, `table-responsive`.
- **Botones:**
  - primario: acento sólido con texto blanco;
  - secundario: blanco con borde `--c-borde` y texto `--c-texto`;
  - de peligro: solo texto rojo, salvo en el modal de confirmación;
  - altura 34 px, radio de 6 px, peso 500;
  - foco: anillo de 2 px de `--c-acento` al 35 %.
- **Formularios:**
  - etiquetas de 13 px / 500 encima del campo;
  - campos de 36 px con borde `--c-borde`;
  - foco con borde acento y anillo suave;
  - errores en `--c-error` debajo del campo.
- **Badges:** pastilla (radio 999 px), 12 px / 500, fondo `*-suave` y texto del color fuerte. Se usan para la bandera
  del catálogo (completo / por revisar / sin detalle), el estado de la CA y, después, el de las ofertas.
- **Vacíos:** un estado vacío con texto corto y la acción siguiente ("No hay líneas por revisar. Ver catálogo →"), nunca
  una tabla vacía.
- **Login:** tarjeta centrada de 380 px sobre `--c-fondo`, con la marca arriba. Se permite un degradado suave propio (del
  acento a un violeta más claro) como franja superior de 4 px, pero no el de Stripe.

## 4. Alcance
Se rediseñan las pantallas de la plataforma, que heredan de `base.html`: login, portada «Hoy», `/catalogo` (tres
pestañas, ficha de producto, `/catalogo/ca/{codigo}`), `/proveedores` (lista, ficha, importar, logística), `/cuenta`,
`/admin/empresas`, `/admin/usuarios` y `/salud`. La pestaña «Líneas» de la ficha de CA también.

Las pantallas de **Archivo** (app antigua) solo tienen que **verse bien con el layout nuevo** (que no se rompan), sin
rediseño: se van a retirar.

Fuera de alcance: modo oscuro, animaciones y cambiar textos (salvo los títulos y descripciones de encabezado).

## 5. Tests y verificación
- Los tests actuales siguen en verde. Si alguno buscaba clases o textos de la navbar vieja, se ajusta y se anota en el
  commit.
- `tests/test_ui_accesibilidad.py`: "Saltar al contenido", `aria-current` en la entrada activa de la barra lateral,
  offcanvas con `aria-label`, contraste mínimo de los tokens (test que calcula los ratios de §2).
- Test de que `base.html` no referencia dominios de fuentes externos y de que los woff2 existen.
- `ruff check .`, `python -m mypy app` y `python -m pytest -rs` contra `mp-test-pg`.

## 6. Cierre
Changelog ("Nuevo diseño de la plataforma"). `00-estado.md` y `decisiones.md` (tokens y regla de "referencia de
estilo, no de marca"). Commit: `F-plat-ui: diseño nuevo de la plataforma (tokens, barra lateral y componentes)`.

## 7. Pasos de Boris
1. Revisar en el navegador, a 1366 px y en el celular: portada, catálogo, proveedores y login. Anotar en
   `decisiones.md` lo que cambie (colores, densidad).
2. Elegir un nombre para la app si se quiere reemplazar "MP Oportunidades" (es un cambio de una línea).
