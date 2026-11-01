# Prompt de implementación — F-merc-0b (puente: cotizaciones de Compra Ágil de Ñuble con la key actual, a CSV) · 31-oct-2026

> Para Claude Code. **Modelo: Opus** (usa la cuota y la pausa de 429 de la key de producción).
> Una fase = un commit, **sin push**. **Sin migración y sin tablas nuevas.** Es un puente hasta tener la
> segunda key: lo que se escribe acá (parser) se reutiliza tal cual en `prompt-F-merc-1.md` §1.4.


> **Corrección 31-oct (Boris): la ingesta actual de la app se pausa** (se apagan sus disparos en
> cron-job.org). El script ya no compite con producción: `--rps` por defecto **0.5** (el mismo ritmo que
> usaba producción) y `reserva=0`. Sigue corriendo solo de 22:00 a 07:00 (regla 5) y sigue persistiendo la
> pausa de 429. El paso 3 de §2 se simplifica: ya no hay que comparar con los 429 de `ca`.

## 0. Por qué y límites
- Para decidir la canasta de alimentos hace falta el **precio unitario por producto de cada oferta** de
  las Compras Ágiles (CA) de Ñuble, y reconstruir los 5 meses que faltan en `COT_` (mar, abr, jun, jul y
  ago 2026). Ver `docs/16-pivote-compra-agil-nuble.md` §4-bis y `prompt-F-merc-1.md` §0.
- Se usa **`MP_TICKET` (la key de producción)**. Restricción real [V, `15-diagnostico-ca-429.md`]: el
  429 `codigo 429` llega tras ~110–190 requests seguidas, o antes si hubo ráfaga en la hora previa; no
  se sabe si el límite es por ticket o por IP (H1). Por eso:
  - corre **solo desde el PC de Boris** (no en Actions) y **solo de 22:00 a 07:00 de Chile** (regla 5);
  - usa el `QuotaTracker` de producción (`principal`) con **reserva** para `ca` (parámetro `reserva`
    de `QuotaTracker`, F-datos-1; usar el mismo valor que los jobs de detalle) y **respeta y persiste la pausa** de `api_pausa`: si este script recibe un 429, la
    ingesta de producción también pausa 20 min. Es lo correcto si el límite es por ticket;
  - ritmo propio más lento que producción: `--rps 0.2` por defecto (una request cada ~5 s con jitter).
- No escribe en la base salvo `quota_log` y la pausa (que ya escriben los jobs). Los datos van a CSV.

## 1. Cambios
1. **Parser** en `app/clients/mp_v2.py` + `types.py`: exactamente `prompt-F-merc-1.md` §1.4
   (`CotizacionCA`, `CotizacionCAProducto`, campos nuevos de `CompraAgilDetalle`, parseo defensivo,
   `seleccionado=None` si no viene `seleccion`). Con sus tests de fixtures sintéticas. `_parse_ca_detalle`
   sigue devolviendo lo mismo de antes para los consumidores actuales (los campos nuevos son aditivos).
2. **Script** `scripts/backfill_cot_nuble.py` (lo ejecuta solo el humano, como `smoke_test.py`):
   - `--meses 2026-08,2026-07,2026-06,2026-04,2026-03` (orden por defecto), `--region 16`,
     `--estados proveedor_seleccionado,desierta,cancelada,cerrada`, `--rps 0.2`, `--max-requests 2500`.
   - Valida la ventana 22–07 con `ZoneInfo("America/Santiago")`; fuera de ella sale con mensaje (flag
     `--fuera-de-ventana` solo para la corrida de prueba de §2.1, con `--max-requests 30`).
   - Por mes, ventanas de **un día** de `publicado_desde/hasta`, `ordenar_por=FechaPublicacion`,
     `tamano_pagina=50`, con `MercadoPublicoV2Client.iterar_compra_agil`; luego `detalle_compra_agil` de
     cada CA. Primero las `proveedor_seleccionado` (son las que tienen ganador), después el resto.
   - Avance en `data/mercado/_backfill_estado.json` (mes, día, códigos ya hechos); reanudable; nunca pide
     dos veces un detalle ya guardado. El archivo local está bien acá: es una herramienta del PC, no un
     proceso de Render (regla 10 aplica a la app).
   - Salida: `data/mercado/cot_api_{AAAA-MM}.csv.gz` con las columnas que lee
     `data/mercado_observatorio.py` (`CodigoCotizacion`, `Region`, `RUTUnidaddeCompra`,
     `NombreUnidaddeCompra`, `MontoTotalDisponble`, `CodigoProducto`, `NombreProductoGenerico`,
     `ProductoCotizado`, `CantidadSolicitada`, `Estado`, `RazonSocialProveedor`, `RUTProveedor`, `Tamano`
     (de `es_emt`), `ProveedorSeleccionado`, `MontoTotal`, `archivo`) **más** `PrecioUnitario`,
     `MontoTotalProducto`, `CantidadCotizada`, `DireccionEntrega`, `PlazoEntrega`. Una fila por
     (CA × proveedor × producto cotizado); CA sin ofertas → una fila con proveedor vacío.
   - Corta limpio ante pausa, 429, cuota o `--max-requests`, guardando avance. Al final imprime: requests,
     CA listadas, detalles, filas, 429 recibidos y a qué hora, y minutos de pausa.
   - Nunca imprime el ticket ni lo escribe en el CSV o el estado (regla 1).
3. **Observatorio** (`data/mercado_observatorio.py`): `analizar_cot` lee también `cot_api_*` y, para un
   mismo mes, **prefiere la API** sobre `COT_`. Con `PrecioUnitario`, agregar al resumen de alimentos:
   por producto de la canasta, mediana del precio unitario ganador y de los perdedores, y distancia
   ganador–segundo por producto.

## 2. Pasos de Boris (en este orden)
1. **Prueba corta, de día** (`--fuera-de-ventana --meses 2026-08 --max-requests 30`): confirma que la API
   entrega CA de agosto y que `productos_cotizados` trae `precio_unitario`. Pegar la salida.
2. **Prueba de antigüedad**: lo mismo con `--meses 2026-03`. Si la API no entrega marzo, anotarlo [V] y
   quitar ese mes.
3. **Primera noche**: sin flags. Revisar al otro día los 429 del resumen del script **y** los de `ca` en
   Actions esa noche. Si `ca` no recibió 429 mientras el script sí → el límite es por IP (H1 confirmada)
   y se puede subir `--rps`; si ambos los recibieron → es por ticket y conviene no pasar de ~1.500
   requests por noche.

## 3. Tests y cierre
Parser (fixtures sintéticas de `prompt-F-merc-1.md` §1.4); script con respx: ventana horaria con reloj
inyectado, reanudación desde el JSON, corte ante `MPTasaError` con pausa persistida, CSV con las
columnas esperadas, el ticket no aparece en ningún archivo de salida. `ruff check .`,
`python -m mypy app`, `python -m pytest -rs` sin saltados. Sin entrada de changelog (no hay cambio
visible). `00-estado.md` actualizado. Commit:
`F-merc-0b: cotizaciones de Compra Ágil de Ñuble con precio por producto (puente con la key actual)`.
