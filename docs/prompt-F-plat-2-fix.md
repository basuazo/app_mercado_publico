# Prompt — F-plat-2-fix (tope de páginas al leer PDF con texto) · 5-nov-2026

> Para Claude Code. **Modelo: Sonnet** (guarda acotada con test). Un commit, **sin push**, sobre `02e17f5`. Sin
> migración. Origen: auditoría de Cowork de F-plat-2. Lo marcado [V] está verificado en el código.

## Problema [V]
`_texto_pdf` (`app/mercado/anexos.py`) recorre **todas** las páginas del PDF con `find_tables()` y `extract_text()`, y
mantiene la lista `pdf.pages` viva hasta el final:
- solo después recorta el texto a `MAX_TEXTO` (50.000 caracteres);
- la subida corre en la **web** de Render (512 MB, regla 12) y admite hasta 10 MB.

Un anexo de texto de 200–300 páginas, como un consolidado de varias unidades, puede tardar minutos y hacer crecer la
memoria. pdfplumber conserva en caché los objetos de cada página: con eso, el proceso web puede quedarse sin memoria y
Render lo reinicia. Las guardas nuevas (ZIP y render a 2.000 px) no cubren este camino.

## Cambio
- `MAX_PAGINAS_TEXTO = 40`: no se leen más páginas que esas.
- El bucle corta en cuanto el texto acumulado alcanza `MAX_TEXTO`.
- Se libera cada página apenas se procesa: `pag.close()` en pdfplumber 0.11 (verificar el nombre en la versión del
  lock, 0.11.10; si no existe, `pag.flush_cache()`).
- No pedir `list(pdf.pages)` entero. Iterar con índice sobre `len(pdf.pages)`, o leer de a una con `pdf.pages[i]`
  liberando la anterior, y conservar `n` (el total) para `paginas` y `paginas_omitidas`.
- La heurística de "escaneado" (`chars / n >= MIN_CHARS_POR_PAGINA`) se calcula sobre las páginas **leídas**, no
  sobre `n`.
- Si se cortó por tope, el aviso al usuario dice cuántas páginas se leyeron de cuántas (mismo estilo que
  `_aviso_paginas`). Las páginas no leídas cuentan como `paginas_omitidas`.

## Tests
- PDF sintético de 60 páginas con texto: lee 40, `paginas` = 60, `paginas_omitidas` = 20, y el aviso lo dice.
- PDF con mucho texto por página: corta al llegar a `MAX_TEXTO` antes de las 40.
- PDF escaneado de 3 páginas: sigue yendo por imagen como hoy.
- La muestra real de `evaluar_extraccion.py` no cambia de resultado: anotar en el commit que ningún anexo de la muestra
  pasa de 40 páginas, o cuál sí.

`ruff check .`, `python -m mypy app`, `python -m pytest -rs` contra `mp-test-pg`. Commit:
`F-plat-2-fix: tope de páginas y memoria al leer PDF con texto`.

## Agregado (Cowork, auditoría de `02e17f5`) — conexión retenida durante la extracción
`anexo_subir` (`app/api/routes/catalogo.py`) y la lista de precios (`app/api/routes/mesa.py`) hacen SELECT antes de
`procesar_anexo_ca` (`_ca_o_404`, `regiones_activas`, proveedor): la sesión queda con la transacción abierta y la
conexión del pool tomada durante la extracción (hasta `llm_timeout_s` = 60 s, más reintentos de 429). El docstring
de `procesar_anexo_ca` dice lo contrario. Además, `OpenRouterClient` abre sus propias sesiones (caché y presupuesto),
así que cada subida usa dos conexiones (regla 15: `pool_size ≤ 5`).
- Cambio: liberar la conexión **antes** de llamar al modelo (p. ej. `session.rollback()` tras leer lo necesario y
  pasar a `procesar_anexo_ca` solo valores, o separar `extraer` de `guardar` y llamar al modelo entre ambos). Lo que
  se escribe después no debe depender de objetos expirados sin recargarlos.
- Test: con un extractor falso que comprueba `session.in_transaction() is False` (o que el pool no tiene la conexión
  de la sesión tomada) en el momento de la llamada.
- Corregir el docstring.
