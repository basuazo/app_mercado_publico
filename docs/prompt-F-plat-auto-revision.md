# Prompt — F-plat-auto-revision (líneas de anexo confirmadas solas; humano solo en excepciones) · 6-nov-2026

> Para Claude Code. **Modelo: Opus** (juez y calibración; toca la regla de fuentes de las líneas). Un commit,
> **sin push**. Con migración si agrega columnas (`down_revision` = cabeza vigente). Va en paralelo con
> F-plat-anexos-auto (no tocar `app/clients/` ni el job de descarga).

## Objetivo
Hoy toda línea extraída de un anexo queda `dudosa` y una persona la confirma. Meta: que la gran mayoría pase a `ok`
sola con **precisión ≥ 98 %** en lo auto-confirmado, y que el equipo revise solo una cola corta de excepciones.

## 1. Señales por línea (función pura `puntaje_linea`, sin red)
1. Calce con el catálogo canónico: sugerencia única y de confianza alta (`canonico`), unidad base coherente.
2. Coherencia con la API: si la CA trae productos de la API, la línea calza con alguno (UNSPSC/familia, cantidad
   del mismo orden); si el anexo suma un monto, cuadra con el presupuesto estimado (± tolerancia).
3. Cantidad y unidad parseables y plausibles para el producto (rango histórico de `oc_lineas_ca` / índice).
4. Acuerdo entre dos lecturas: el modelo principal y el de respaldo coinciden (producto normalizado + cantidad +
   unidad). Solo se llama al segundo modelo cuando 1–3 no bastan (controla el costo; caché ya existe).
5. Tipo de documento (texto/escaneado/docx/xlsx) y calidad de la extracción (páginas omitidas, texto vacío).

## 2. Calibración con la verdad que ya existe
`scripts/mercado/calibrar_auto_revision.py` sobre `data/mercado/anexos_eval/` (30 anexos, 602 líneas revisadas):
precisión y cobertura por umbral y por tipo de documento; elige el umbral más bajo que cumple ≥ 98 % y lo imprime.
El umbral vive en settings (`AUTO_CONFIRMAR_UMBRAL`), no en código. Sin modelo elegido todavía, el script corre con
lo que haya en caché y dice qué falta.

## 3. Aplicación
- Al extraer (manual, masiva, API externa o descarga automática): línea con puntaje ≥ umbral → `ok` con
  `confirmada_por = 'auto'` y su puntaje guardado; el resto queda `dudosa` en la cola.
- Si todas las líneas de un anexo quedan `ok` → la CA queda «anexo resuelto» sola (regla anexo > API de anexos-2:
  las de la API pasan a `reemplazada` igual que con confirmación humana).
- Cola de excepciones (`/catalogo/revision`): solo `dudosa`, ordenada por cierre de la CA; con «Confirmar todas» por CA.
- Muestreo de control: el 5 % de lo auto-confirmado de la semana entra a la cola marcado «control»; lo corregido ahí
  cuenta como error del auto y se muestra en `/salud` (precisión observada). Si baja de 95 % dos semanas seguidas,
  el job sube el umbral solo y avisa.
- Nada de esto toca líneas ya `ok` por humano ni las de la API.

## 4. Tests y cierre
Puntaje con casos de cada señal; umbral desde settings; anexo totalmente auto → resuelto; muestreo determinista
(semilla); precisión observada; regresión de anexos-2 (reemplazada). `ruff`, `mypy app`, `pytest -m "not postgres" -rs`.
Entrada en changelog. Commit: `F-plat-auto-revision: confirmación automática por excepción`.

## 5. Pasos de Boris
1. OpenRouter: key y `evaluar_extraccion.py` para fijar modelo principal y de respaldo (prerrequisito).
2. Correr `calibrar_auto_revision.py` y pegar la curva; fijar `AUTO_CONFIRMAR_UMBRAL`.
3. Primera semana: revisar la cola de control (≈ 10–20 líneas) y mirar la precisión en `/salud`.
