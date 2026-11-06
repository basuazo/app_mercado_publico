# Prompt — F-plat-impuestos (impuestos específicos en el catálogo; neto sin impuestos, bruto con todos) · 6-nov-2026

> Para Claude Code. **Modelo: Opus**: toca el catálogo común, los costos, la oferta y la cotización formal.
> Un commit, **sin push**, **con migración** (`down_revision` = la cabeza vigente). No toca el rediseño:
> `app.css` y `base.html` quedan igual, y los cambios de pantalla van en los parciales existentes.

## Decisión (Boris, 6-nov)
- Un impuesto específico, como el ILA de las bebidas analcohólicas, es parte del **costo bruto** y **no** del neto.
  Funciona igual que el IVA.
- Si una categoría de productos tiene un impuesto específico, ese impuesto se maneja en el **catálogo**. No se
  define por proveedor ni por planilla.
- Consecuencias:
  - `precio_neto` sigue siendo el precio sin IVA y sin impuestos específicos.
  - El margen, el precio sugerido, P(ganar) y las comparaciones con la competencia se calculan siempre sobre el
    neto. Las cotizaciones de `ca_cotizacion_productos` ya son netas.
  - El bruto es lo que se paga y lo que se cobra: caja, solicitudes a proveedores y cotización formal.

## 0. Verificar antes de sembrar (reglas 20 y 23)
- Las tasas y las condiciones de cada impuesto se toman de la **fuente primaria** (sii.cl o la ley vigente) y se
  citan en `docs/decisiones.md` con fecha y URL.
- Hay que confirmar como mínimo:
  - ILA de bebidas analcohólicas: tasa general y tasa alta por contenido de azúcar, y el umbral de azúcar.
  - Impuesto a bebidas alcohólicas, si alguna familia lo necesita.
  - Si estos impuestos usan débito y crédito como el IVA (recuperables para el revendedor). Anotar lo verificado,
    pero **no detenerse**: el modelo de §1 soporta los dos casos con la columna `recuperable`.
- Ninguna tasa queda en el código: viven en la tabla de §1, con su vigencia.

## 1. Modelo (solo `app/models`)
- Tabla `impuesto_especifico` con `codigo` (único, por ejemplo `ila_analcoholica_baja` / `ila_analcoholica_alta`),
  `nombre`, `tasa` (fracción), `recuperable` (bool, por defecto true), `vigente_desde`, `fuente_url` y `notas`.
  `recuperable = true` es la decisión de Boris: el impuesto va al bruto y no al neto. Con `false`, que es lo que
  corresponde si el contador dice que no se recupera, el impuesto **se suma al costo neto** (es un costo real) y la
  cotización no lo cobra aparte. Así la respuesta del contador se aplica cambiando un dato, sin otra fase. Cuando una tasa cambia, se agrega una fila
  nueva; nunca se edita la vieja. La tasa aplicable es la vigente en la fecha del costo.
- En el catálogo canónico:
  - `producto_canonico.impuesto_codigo`, nullable.
  - Una regla por `familia`: tabla `familia_impuesto` con `familia`, `impuesto_codigo` y `modo`. `modo` puede ser
    `aplica` (todos los productos de la familia lo llevan) o `clasificar` (lo llevan, pero la tasa depende de cada
    producto, como baja o alta según el azúcar).
  - La resolución va del producto a la familia y, si ninguno lo define, el producto no tiene impuesto.
  - En una familia `clasificar`, un producto sin código queda **«impuesto por clasificar»**: es una señal, no se
    supone ninguna tasa.
- En `proveedor_productos`: `impuesto_codigo`, nullable, **solo como pista mientras no hay vínculo** con el
  catálogo. Cuando hay vínculo manda el catálogo, y si la pista no coincide se muestra el aviso «el catálogo dice
  otra cosa».
- Migración con seed **vacía** de tasas: Boris las carga después de verificarlas en §0. Se puede incluir un script
  `scripts/mercado/sembrar_impuestos.py` con los valores verificados, pero que lo ejecute Boris.

## 2. Funciones (en `app/mercado`, sin HTML)
- `impuesto_de(session, pp_o_producto, fecha) -> Impuesto | None | PORCLASIFICAR`.
- `bruto(neto, impuesto) = neto × (1 + IVA + tasa)`. Con `recuperable = false`, el neto que entrega
  `neto_desde_bruto` ya incluye el impuesto (`bruto ÷ (1 + IVA)`). Los dos impuestos se calculan sobre el mismo neto: confirmarlo
  en §0. La constante IVA queda en un solo lugar, la de `cotizacion.IVA`, y no se duplica.
- `neto_desde_bruto(bruto, impuesto)`, para la planilla y las listas de precios.

## 3. Importación de precios (`importar.py` y la plantilla)
- Columnas nuevas opcionales en la hoja `precios`:
  - `precio_bruto`: precio final con IVA e impuestos.
  - `impuesto`: un código de la tabla, o vacío.
- Si viene `precio_bruto`, el neto se calcula con el impuesto del catálogo (vía el vínculo) o, si no hay vínculo,
  con la pista `impuesto` de la fila. Si vienen `precio_neto` y `precio_bruto` y no cuadran dentro de ±1 %, manda
  `precio_bruto` y la vista previa lo advierte fila por fila.
- Si viene `precio_bruto`, el producto está en una familia `clasificar` y la fila no trae código: la fila entra
  igual, con el neto calculado **solo sin IVA**, marcada «impuesto por clasificar», y se cuenta en la vista previa.
- La regla de identidad no cambia: el producto se reconoce por proveedor + producto + formato. Volver a subir la
  misma planilla con `precio_bruto` corrige el neto de esas filas (estado `cambiado`) sin duplicar nada.
- La plantilla descargable y la hoja «Instrucciones» explican las dos columnas nuevas.

## 4. Dónde se usa el bruto
- **Catálogo** (`/catalogo`, ficha del producto canónico; solo admin, porque el catálogo es común): asignar
  impuesto a una familia, con su modo, y a un producto. Más una lista **«Impuesto por clasificar»** que permite
  asignar en bloque, porque son decenas de bebidas.
- **Catálogo Proveedores**: ficha y lista muestran neto y bruto; filtro «Impuesto por clasificar».
- **Lote y solicitudes de precio**: el mensaje y la planilla al proveedor piden el precio **con impuestos**, que es
  como cotiza el proveedor. Al anotar la respuesta, se guarda el neto calculado.
- **Oferta propia y cotización formal**: las líneas guardan su impuesto. El DOCX muestra Neto, IVA 19 %, el
  impuesto específico por código (una fila por impuesto, solo si existe) y Total bruto. `total_neto` sigue siendo
  neto.
- **Caja y ventas**: lo comprometido con proveedores se calcula en bruto. Revisar que `ventas.caja` diga en cada
  monto si es neto o bruto, porque hoy dice «montos NETOS».
- **Margen y P(ganar)**: sin cambios de fórmula (neto). Con un impuesto «por clasificar», la mesa y el lote
  muestran la señal «impuesto por clasificar». El margen no se bloquea, porque el neto es correcto si la fila se
  cargó con `precio_neto`.

## 5. Tests y cierre
- Resolución producto → familia → nada, incluido el caso `clasificar` sin código.
- Vigencia: una tasa nueva no cambia los costos anteriores a su fecha.
- `bruto` y `neto_desde_bruto` son inversos, con redondeo a peso.
- Importación:
  - solo bruto, solo neto, ambos que cuadran y ambos que no cuadran;
  - pista sin vínculo y pista que contradice el catálogo;
  - una segunda subida corrige sin duplicar.
- DOCX con y sin impuesto específico.
- Caja en bruto.
- Ownership: el catálogo lo edita solo admin; la pista es por empresa.
- `ruff check .`, `python -m mypy app` y `python -m pytest -rs` contra Postgres (0 fallos, 0 errores, 0 saltados).
- Entrada en `app/changelog.py`.
- Commit: `F-plat-impuestos: impuestos específicos en el catálogo, neto sin impuestos y bruto con todos`.

## 6. Pasos de Boris
1. §0: verificar las tasas en el SII y preguntar al contador de Aralia si el impuesto de las bebidas se recupera
   como el IVA. Si no se recupera, se marca `recuperable = false` al sembrar; no hace falta esperar la respuesta
   para correr la fase.
2. Revisar el diff, aplicar la migración, deploy y correr `sembrar_impuestos.py`.
3. En el catálogo, asignar el impuesto a la familia de bebidas analcohólicas (modo `clasificar`) y clasificar los
   productos.
4. Volver a subir `aralia_precios_importar.xlsx`, que ya trae la columna `precio_bruto`. Los netos de las bebidas
   se corrigen solos.
