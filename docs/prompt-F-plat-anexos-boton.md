# Prompt — F-plat-anexos-boton (adjuntos a Rastrillo con un clic desde la ficha pública) · 6-nov-2026

> Para Claude Code. **Modelo: Sonnet** (extensión chica + priorización sobre piezas existentes). Un commit,
> **sin push**. Sin migración salvo que §2 lo pida (`down_revision` = cabeza vigente). Va en paralelo con
> F-plat-auto-revision (no tocar `catalogo.py` ni la confirmación de líneas). [V] verificado, [I] inferido.

## Por qué esta vía (decisión de Boris, 6-nov: «la opción menos riesgosa»)
- [V, Guía API Compra Ágil v2 3.0, mayo 2026] La API oficial solo da `documentos[].id` (UUID) y `nombre`; no hay
  endpoint para bajar el archivo. Ya lo guardamos en `compras_agiles.documentos` (anexos-2).
- [V, navegador 6-nov] La ficha pública baja los adjuntos desde `adjunto.mercadopublico.cl/…/adjuntos-compra-agil/
  {listar|encrypt|descargar}` con una clave del frontend; sin ella, 403. Usar esa clave desde nuestro servidor es la
  vía de mayor riesgo (queda en reserva: `prompt-F-plat-anexos-auto.md`).
- Esta fase: **la descarga la dispara una persona en su navegador** (como hoy, pero con un clic por CA), y todo lo
  demás (subida, extracción, revisión automática) lo hace Rastrillo. No hay bot ni lectura de HTML desde el servidor.

## 1. Extensión de Chrome (`extension/`, Manifest V3, sin dependencias ni build)
- `content_script` solo en `https://buscador.mercadopublico.cl/ficha*`. Agrega un botón **«Enviar adjuntos a
  Rastrillo»** junto a la sección «Adjuntos».
- Al pulsarlo: toma el código de la URL (`code=`), y para cada adjunto de la página dispara la descarga que ya hace la
  propia ficha (el mismo clic de una persona) y captura el archivo en memoria antes de que se guarde (la ficha arma un
  `Blob` y un enlace temporal [V, bundle]); NO llama por su cuenta al servicio de adjuntos ni usa su clave.
  Investigar la forma más simple y robusta (p. ej. script en el mundo `MAIN` que observa `URL.createObjectURL`, o
  `chrome.downloads` + releer el archivo) y anotarla; si ninguna es fiable, plan B: descarga normal y la extensión
  sube desde la carpeta de descargas con el código en el nombre.
- Sube cada archivo a `POST /api/anexos/{codigo}` (anexos-2 §2.3) con el token de la empresa guardado en las opciones
  de la extensión (`chrome.storage.local`; nunca en el código). Muestra el resultado por archivo (nuevo, ya existía,
  inválido) en la misma ficha.
- Un archivo a la vez, sin reintentos automáticos. Pie: «Fuente: Dirección ChileCompra».
- `extension/README.md`: instalar en modo desarrollador (cargar descomprimida), configurar URL de Rastrillo y token.

## 2. Priorización: solo las CA que necesitan anexo
- `necesita_anexo(ca)` (pura): `n_documentos > 0` y (sin líneas de la API, o líneas de la API pobres: caso D de
  anexos-2, o texto que remite al adjunto, p. ej. «detalle en archivo adjunto»), en familias de la empresa y vigente.
- `/catalogo/anexos/pendientes` (y bloque en «Hoy»): lista ordenada por cierre con botón **«Abrir fichas»** que abre las
  próximas N fichas en pestañas (N configurable, default 10) para pasar el botón de la extensión en cada una.
- `GET /api/anexos/pendientes` (ya existe) usa el mismo criterio.

## 3. Tests y cierre
`necesita_anexo` con casos; la lista y el botón; la extensión con un test mínimo de su lógica pura (armado del
request, lectura del código) si se puede sin navegador; si no, checklist manual en el README. `ruff`, `mypy app`,
`pytest -m "not postgres" -rs`. Entrada en changelog. Anotar en `decisiones.md` la vía elegida y por qué.
Commit: `F-plat-anexos-boton: adjuntos a Rastrillo con un clic desde la ficha`.

## 4. Pasos de Boris
1. Generar el token de anexos en `/admin/empresas/{id}` y cargarlo en la extensión.
2. Probar con 5 CA de «Abrir fichas»: medir segundos por CA (meta < 15 s) y que las líneas aparezcan solas.
3. Pedir a ChileCompra por `ayuda.mercadopublico.cl` (con Clave Única) un endpoint oficial para bajar
   `documentos[].id` de Compra Ágil. Si lo dan, se reemplaza la extensión por un job.
