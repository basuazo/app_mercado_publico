# Prompt de implementación — F-merc-3 (detalle de las CA vigentes en `mercado-ca`) · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet** (cola nueva sobre funciones que ya existen; contrato claro). Una fase = un
> commit, **sin push**. **Con migración** (dos columnas). Va **después de F-plat-1**, para no chocar con su
> migración, y **antes de F-plat-2 y F-plat-5**, que la necesitan. Origen: revisión de F-plat-5 del 4-nov-2026
> (`prompt-F-plat-5.md`, "Revisión 4-nov", punto 1). Lo marcado [V] está verificado en el código (HEAD `a9c0cfe`).

## 0. El problema [V]
- `mercado-ca` pide el detalle solo de las CA **cerradas**. `cola_mercado_ca` (`app/ingest/mercado.py`) toma las
  terminales (`ESTADOS_TERMINALES_CA`) y las `publicada` con `fecha_cierre` ya pasada.
- El job `ca`, que bajaba el detalle de las vigentes para el matching, está apagado desde el 31-oct
  (`decisiones.md`, pivote).
- El listado v2 no trae `productos_solicitados` ni `documentos`: vienen solo en el detalle (`app/clients/mp_v2.py`).
- Consecuencia: una CA abierta de Ñuble no tiene `ca_productos`, y por lo tanto F-plat-1 no le crea `ca_lineas`.
  Tampoco se sabe si trae anexos: `n_documentos` se parsea, pero no se guarda en ninguna columna. La mesa de
  F-plat-5 y la descarga de anexos de F-plat-2 §1.6 dependen justo de esas CA.

## 1. Cambios
### 1.1 Esquema (solo `app/models` + migración)
`compras_agiles.detalle_vigente_en` (DateTime, nullable) y `compras_agiles.n_documentos` (Integer, nullable). La ruta
de detalle de cerradas (`guardar_detalle_mercado`) también guarda `n_documentos`.

### 1.2 Cola de vigentes en `mercado-ca`
- `cola_vigentes(session, regiones, ahora, limite)`: CA de `regiones_activas` con `estado = 'publicada'` y
  `fecha_cierre > ahora`, sin `detalle_vigente_en` o con `detalle_vigente_en < fecha_ultimo_cambio` (la CA cambió).
  Orden: primero las que nunca tuvieron detalle, después por `fecha_cierre` asc. Respeta la espera por fallos, como
  `cola_mercado_ca`.
- **Va antes de la cola de cerradas**, porque es lo que corre contra el reloj (cierre). Tiene tope propio:
  `MERCADO_VIGENTES_MAX` (default 60) y `MERCADO_VIGENTES_MINUTOS` (default 4), dentro de los 10 min de
  `mercado_detalles_minutos`. Las cerradas usan el tiempo que quede.
- **Solo `upsert_ca_detalle`** (campos y `ca_productos`) más `n_documentos` y `detalle_vigente_en = ahora`.
  **No** toca `ca_cotizaciones` ni `cotizaciones_obtenidas_en`. Si los tocara, la cola de cerradas, que depende de
  `cotizaciones_obtenidas_en < fecha_ultimo_cambio`, podría no volver a pedir la CA tras el cierre y se perdería el
  ganador. Test obligatorio.
- Sin `raw_json` (regla 11). Mismo cliente, cuota, rate limit y pausa de 429 que el resto (reglas 3, 4 y 24): un 429
  corta la corrida guardando el avance.
- El resumen del job suma `vigentes_cola`, `vigentes_detalles` y `vigentes_pendientes`.

### 1.3 Encaje con F-plat-1
`ca_productos` se reinserta en cada detalle. `ca_lineas` ya maneja el cambio de `texto_origen` por `(ca_codigo, n)`:
sin revisión humana se recalcula; revisada pasa a `dudosa`. El cursor de `mercado-catalogo` [V, `f235686`] lee
`actualizado_en` y `cotizaciones_obtenidas_en`. Sumar `detalle_vigente_en` a esa condición, porque reemplazar
`ca_productos` no siempre mueve `actualizado_en`. Test: una vigente cuyo detalle cambió vuelve a procesarse.

### 1.4 Volumen [I → medir]
Antes de fijar los defaults, contar en `compras_agiles` las CA de región 16 publicadas por día en los últimos 30 días
(mediana y p90). Poner esos números en el commit. Es probable que sean decenas por día [I], muy por debajo de la cuota
(9.000), pero el tope por corrida tiene que cubrir el p90 en 4 corridas por hora.

## 2. Tests
- La cola: entran las vigentes sin detalle y las cambiadas; no entran las cerradas ni las de otra región.
- El orden de la cola.
- Que no toca `cotizaciones_obtenidas_en` ni `ca_cotizaciones`.
- Que `n_documentos` se guarda en las dos rutas.
- Los topes de cantidad y de minutos.
- Que un 429 en la cola de vigentes corta la corrida sin pedir las cerradas.
- La idempotencia: correr dos veces sin cambios no pide nada.

`ruff check .`, `python -m mypy app`, `python -m pytest -rs` contra Postgres local (`TEST_DATABASE_URL`).

## 3. Cierre
Sin changelog: no hay cambios visibles. `00-estado.md`. Commit: `F-merc-3: detalle de las CA vigentes en mercado-ca`.

## 4. Pasos de Boris
1. Migración de día, con `GH mercado` en pausa → push → `Running upgrade` → reactivar.
2. Al día siguiente, pegar el resumen de 2 corridas de `mercado` (`vigentes_*`) y la cuenta de CA abiertas de Ñuble
   con y sin `ca_productos`.
