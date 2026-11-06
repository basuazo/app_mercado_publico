# Extensión de Chrome: adjuntos a Rastrillo con un clic

Manifest V3, sin dependencias ni build. Agrega el botón **«Enviar adjuntos a Rastrillo»** junto a la sección
«Adjuntos» de la ficha pública de una Compra Ágil (`https://buscador.mercadopublico.cl/ficha?code=…`).

**Qué hace y qué no:** la descarga la dispara una persona en su navegador, con el mismo clic que ya hace la ficha.
La extensión recoge el archivo que la propia ficha arma (un `Blob` y un enlace temporal) **antes de que se guarde**
y lo sube a `POST /api/anexos/{codigo}` de Rastrillo con el token de tu empresa. **No** llama por su cuenta al servicio
de adjuntos de Mercado Público, **no** usa la clave del frontend y **no** lee HTML desde ningún servidor. Un archivo a
la vez, sin reintentos automáticos. Fuente: Dirección ChileCompra.

## Instalar (modo desarrollador)

1. En Chrome (111 o superior): `chrome://extensions` → activa **Modo de desarrollador**.
2. **Cargar descomprimida** → elige la carpeta `extension/` de este repositorio.
3. Abre las **opciones** de la extensión (clic derecho sobre su ícono → Opciones) y completa:
   - **URL de Rastrillo**: p. ej. `https://tu-rastrillo.onrender.com` (https; `http://localhost` solo para pruebas).
     Al guardar, Chrome pide permiso para ese sitio y solo para ese.
   - **Token de anexos**: lo genera un administrador en `/admin/empresas/{id}` → «Generar token de anexos»
     (se muestra una sola vez). Se guarda en `chrome.storage.local`, solo en tu navegador; nunca va en el código.

## Usar

1. En Rastrillo: **Catálogo → «CA que necesitan anexo»** (`/catalogo/anexos/pendientes`) → **«Abrir fichas»**
   (abre las próximas N fichas en pestañas; si el navegador bloquea, permite las ventanas emergentes).
2. En cada ficha, pulsa **«Enviar adjuntos a Rastrillo»**. Debajo del botón aparece, por archivo:
   `Nuevo` (subido), `Ya existía` (mismo archivo, no se repite) o `Inválido` (con el motivo).
3. Las líneas leídas quedan «por revisar» en Rastrillo.

## Cómo captura el archivo (y su límite)

`hook_main.js` corre en el mundo `MAIN` de la ficha y envuelve `URL.createObjectURL` y el clic de los enlaces
`<a>`. Mientras el botón tiene la captura «armada», el clic sobre el enlace temporal de un `Blob` se intercepta, el
archivo se pasa al content script y **no** se guarda en disco. Esta forma es **[I]**: se infirió del bundle de la
ficha ([V] que arma un `Blob` y un enlace temporal) y **no se probó en un navegador**. Lo mismo vale para cómo
`content.js` ubica la sección «Adjuntos» y sus botones (`ubicarAdjuntos`: controles cuyo texto o fila parece un
nombre de archivo). Si algo no calza, se ajusta ahí (un solo lugar) y se anota aquí.

**Plan B (no implementado):** si la captura no resulta fiable, descarga normal y subir desde la carpeta de descargas
con el código en el nombre. Una extensión no puede leer esa carpeta sin que cada persona active «Permitir acceso a
URL de archivos», así que solo se hace si el Plan A falla en la prueba.

## Pruebas

- Lógica pura (lectura del código, armado del request, nombre del archivo, resultados): `node --test extension/lib.test.js`
  (Node 20+, sin dependencias).
- Checklist manual (necesita el navegador):
  1. Con la URL o el token vacíos, el botón avisa que falta configurar.
  2. En una ficha con adjuntos, el botón aparece bajo «Adjuntos» y lista cada archivo con su resultado.
  3. Un archivo repetido sale `Ya existía`; un `.exe` renombrado o > 10 MB sale `Inválido`.
  4. El archivo **no** queda en la carpeta de descargas.
  5. En la pestaña Network de la ficha no hay llamadas de la extensión a `adjunto.mercadopublico.cl`.
  6. Con un token revocado, avisa «Token rechazado».
