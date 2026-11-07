# Prompt — F-plat-lectura · corrección de la auditoría (7-nov-2026)

> Para Claude Code. **Modelo: Sonnet.** Trabaja en el worktree `../mp-F-plat-lectura`, rama `F-plat-lectura`
> (commit 89105bd, aún sin PR). **Sin migración.** Al final: **enmendar el mismo commit** (`git commit --amend`,
> una fase = un commit) y `git push --force-with-lease origin F-plat-lectura`. No tocar `main`.
> [V] = verificado en la auditoría con casos sintéticos; [I] = inferido.

## 1. `texto_es_basura` da falsos positivos en tablas normales [V] — BLOQUEANTE
Hoy marca basura un PDF bueno con 25 filas como `Leche entera 1 L | 12 | unidades`, `Detergente líquido 3 L | 5 | UN`
o `Servilletas paquete x 100 | 3 | PQT` (la «L»/«x»/«u» suelta pasa el 15 %; siglas sin vocal PQT/KGS/LTS/RUT/SKU pasan
el 7 %). Consecuencia: se descarta el texto, van solo 5 páginas como imagen y queda `escaneado=True`.
- No contar como «letra suelta» un token de 1 letra que va pegado a un número (anterior o siguiente token numérico:
  `1 L`, `x 12`, `12 u`) ni las letras de unidad/conector {l, g, u, x, y, a, o, e}.
- Excluir del test «sin vocal» una lista de siglas comunes: kgs, kg, lts, lt, grs, gr, ml, cc, pqt, und, un, cjs, cj,
  mts, mt, rut, sku, nro, cod, srl, ltda, spa… (normalizar a minúsculas y sin punto).
- Agregar la señal propia del OCR malo: proporción de pares consecutivos de tokens de 1 letra iguales o casi iguales
  («U U n n i i d d»). Basura = esa señal alta **o** (letras sueltas alta **y** palabras raras alta). Recalibrar con el set
  real (`data/`, fuera de git): los 6 PDF buenos NO, los dos `IMG_*` SÍ; anotar los números nuevos en el docstring.
- Tests nuevos (sintéticos): las tres tablas de arriba con encabezado normal → **no** basura; el texto
  «5 1 0 5 U U n n i i d d a a d d e e s s…» → basura; los tests actuales siguen verdes.

## 2. El juez corregido acepta errores reales de cantidad [V] — BLOQUEANTE
`unidad_igual_corregida` da por buena «kg/L esperado vs unidad extraída» con CUALQUIER formato en el texto. Casos que hoy
pasan y no deben: «Arroz 5 kg» esperado 10 kg vs extraído 10 unidades (= 50 kg); «Jugo 200 ml» esperado 48 unidades vs
extraído 48 L (= 9,6 L reales).
- Aceptar el cruce medida↔envase solo si (a) el formato del texto es 1 (1 kg, 1 L, 1000 g, 1000 ml) y la cantidad
  coincide, o (b) cantidad × formato coincide con la cantidad esperada en la misma familia (tolerancia 1 %).
  Extraer el formato (número + unidad) con la regex existente, no solo detectar que exista.
- Tests: los dos casos de arriba → falla; «Azúcar 1 kg» 10 kg vs 10 unidades → acierto; «Arroz 5 kg» 50 kg vs
  10 unidades → acierto.
- Como `calibrar_auto_revision.py` usa este juez por defecto, anotar en `docs/decisiones.md` que la calibración de la
  auto-revisión debe re-correrse con el juez ya corregido.

## 3. `diagnosticar_extraccion.py`: clases con el juez viejo
`clasificar` empareja con el corregido pero clasifica las fallas con `ev.similitud` y `_cant_ok` (estrictos). Usar
`ev.similitud_corregida` y `ev.cantidad_igual_corregida` (con unidades normalizadas) para que las clases
texto/cantidad/unidad sean coherentes con el docstring. Renombrar la etiqueta «Juez actual» a «Juez corregido».

## 4. Relectura concurrente duplica líneas
En `crear_lineas_de_anexo`, el conjunto `mismo_documento` excluye `CaAnexo.id != anexo.id`: una segunda relectura del
mismo anexo no borra las líneas no confirmadas que dejó la primera. Quitar esa exclusión (el borrado ocurre antes de
insertar las nuevas, así que no toca las propias). Test: procesar dos veces seguidas el mismo anexo forzando
`sin_presupuesto` → leído (simular la carrera dejando líneas `dudosa` del mismo `anexo_id`) → sin líneas duplicadas;
las `ok` se conservan.

## 5. Detalles
- Ficha: no mostrar «Vuelve a subir el archivo para leerlo» en un anexo rechazado por archivo inválido (filas de
  `anexo_rechazado`: sin `paginas` y sin `texto_extraido`, o `mime == "desconocido"`); ahí subirlo de nuevo vuelve a
  fallar. Test de plantilla.
- Relectura: conservar `subido_por` original si la nueva subida viene sin usuario (`usuario_id is None`).
- Informe del evaluador: la fila «claude-cowork (6-nov, manual)» debe decir «98,0 % sin unidad (no es el juez
  corregido)».
- `docs/decisiones.md` (entrada de esta fase): corregir umbrales de §1 y la regla de §2; aclarar que la descarga
  automática no relee en la práctica (`cola_anexos` excluye CA con anexos).

## 6. Cierre
`ruff check .`, `python -m mypy app`, `python -m pytest -rs -m "not datos_locales"` **completo** contra `mp_test_b`
(0 fallos, 0 errores, 0 saltados) y además los tests `datos_locales` de anexos si `data/` está disponible.
Enmendar el commit (mismo mensaje, agregar al cuerpo «corrección de auditoría: basura sin falsos positivos en tablas,
juez sin aceptar formatos ≠ 1, diagnóstico coherente, relectura sin duplicar») y push con `--force-with-lease`.
Entregar el texto del PR actualizado: quitar «descarga automática» de la lista donde aplica la relectura (o aclararlo),
y aclarar la fila de referencia.

*Fuente de los datos de dominio: Dirección ChileCompra.*
