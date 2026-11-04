# Prompt — Sonda: ofertas repetidas del mismo RUT en el detalle de CA · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Sin commit de fase: se commitea solo el script y la anotación en docs
> (`docs: sonda de ofertas repetidas por RUT`). No toca `app/` ni la base. Origen: `prompt-F-merc-1c.md` §3.

## 0. Qué se quiere saber
El nocturno del 4-nov dio 45 avisos `oferta repetida del mismo RUT` en 131 detalles. Hoy `_sin_repetir`
(`app/ingest/mercado.py`) guarda la primera y descarta el resto. Hay que ver qué distingue a las repetidas:
¿anuladas o reemplazadas, sucursales distintas o duplicados exactos?

Reglas: **1** (ticket solo de `.env`/entorno, nunca en la salida), **3** (ante 429 no reintentar: imprimir y salir),
**4** (1 req/s), **9** (solo API oficial), **23** (marcar [V]/[I]).

## 1. Script: modo `--detalle-ca CODIGO [CODIGO ...]` en `scripts/smoke_test.py`
- Usa `MercadoPublicoV2Client` como el resto de `smoke_test.py`, pero pide el **JSON crudo** del detalle (sin pasar
  por `_parse_cotizacion`, que es justamente lo que se evalúa). Si el cliente no expone el crudo, usar el método
  interno que ya devuelve el `payload` y dejar un comentario.
- Antes de pedir nada: si hay pausa vigente (`leer_pausa`), imprimirla y salir.
- Por cada código: agrupar `proveedores_cotizando[]` por `rut_proveedor` y, **solo para los RUT repetidos**,
  imprimir una tabla con: RUT, razón social (30 chars), `id_cotizacion`, `estado` (id y glosa, o el texto),
  `proveedor_seleccionado`, `codigo_sucursal_empresa`, `monto_total` (u otro monto de la oferta que venga),
  n.º de productos, suma de `monto_total_producto` y fecha de la oferta si viene alguna.
- Además, por CA: total de ofertas, RUT distintos y la lista de **todas las claves** del primer elemento de
  `proveedores_cotizando` (por si hay un campo que no conocemos: `fecha_envio`, `vigente`, `version`...).
- Marcar en la salida si dos ofertas del mismo RUT son **idénticas** (mismo dict salvo `id_cotizacion`).
- Escribe la salida también en `data/logs/sonda_ofertas_<AAAA-MM-DD>.txt` y verifica que el valor del ticket no
  aparece en el archivo (abortar con error si aparece).
- Sin escribir en Neon, sin reintentos, sin paralelismo. `ruff check scripts/smoke_test.py`.

## 2. Lo corre Boris
```
python scripts/smoke_test.py --detalle-ca 2728-9-COT26 3881-33-COT26 4158-26-COT26
```
(3 requests). Pega la salida en el chat.

## 3. Dónde se anota (después de ver la salida, con Boris)
`docs/01-analisis-api-mercado-publico.md` §10: qué distingue a las ofertas repetidas, marcado [V], con los 3 códigos
de ejemplo. La regla de elección final queda para F-merc-1c §3.2.
