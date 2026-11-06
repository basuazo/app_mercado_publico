# Prompt — F-plat-anexos-auto (descarga y extracción automática de adjuntos de CA) · 6-nov-2026

> **EN RESERVA — NO EJECUTAR (decisión de Boris, 6-nov).** Se prefiere la vía de menor riesgo:
> `prompt-F-plat-anexos-boton.md` (extensión del navegador, la descarga la dispara una persona) + pedido formal a
> ChileCompra. Esta fase solo se activa si el volumen lo exige y ChileCompra no da una vía oficial.

> Para Claude Code. **Modelo: Opus** (reglas 1, 3, 4, 9, 10, 12; cliente nuevo de un servicio de ChileCompra).
> Un commit, **sin push**. Migración solo si hace falta (`down_revision` = cabeza vigente, una sola). [V] verificado,
> [I] inferido. Va en paralelo con F-plat-auto-revision (no tocar `app/mercado/catalogo.py` ni la confirmación de líneas).

## Decisión de Boris (6-nov) — manda sobre la regla 9 vigente
«Sin automatizar los anexos la app es inviable; si hay que mezclar API con lectura de la web, se hace.» Alcance
de esta fase: **descargar automáticamente el adjunto público** de cada CA en alcance. NO se lee el HTML de las fichas
ni se listan procesos fuera de la API: eso sigue prohibido. Reescribir la regla 9 de `CLAUDE.md` con esta decisión
(fecha, quién, límites de abajo) y anotarla en `docs/decisiones.md`.

## Lo verificado en el navegador (6-nov, ficha pública de `1057510-1996-COT26`) [V]
- La ficha (`buscador.mercadopublico.cl/ficha?code=…`, SPA) pide la lista de adjuntos a
  `https://adjunto.mercadopublico.cl/adjunto-compra-agil/v1/adjuntos-compra-agil/listar/{codigo}`.
- El bundle JS público usa además `…/v1/adjuntos-compra-agil/encrypt/{id}`, `…/descargar/{n}` (GET, respuesta blob)
  y `…/cotizacion/listar/{codigo}`.
- Sin credenciales responde **403 «Authentication parameters missing»**. El frontend manda un header `user_key`
  (clave pública incrustada en el bundle) y, al menos en `listar`, otro header `x-…` que no se alcanzó a leer.
- [I] Flujo probable: `listar` → `encrypt/{id}` → `descargar/{id_cifrado}`. Confirmarlo con la sonda (§1).
- La API oficial v2 no tiene ruta de documentos (403, R8). `compras_agiles.documentos` (anexos-2) ya guarda `{id, nombre}`
  desde el detalle de la API: comparar ese `id` con el de `listar`.

## 1. Sonda (la corre Boris, de día)
`scripts/smoke_test.py --adjuntos <codigo>`: listar → encrypt → descargar de UNA CA vigente; imprime status, forma del
JSON (claves, sin valores sensibles), tipo y tamaño del archivo. Headers desde variables de entorno:
`MP_ADJUNTOS_USER_KEY` y, si hace falta, `MP_ADJUNTOS_HEADERS_EXTRA` (JSON). **Ningún valor de clave en código, tests,
fixtures, logs ni commits**: se tratan como `MP_TICKET` (regla 1; el logger las enmascara). Boris obtiene los valores
desde el bundle en su navegador. Si la sonda da 403 con los headers, parar y reportar: no probar otras formas de entrar.

## 2. Cliente (`app/clients/mp_documentos.py`, ya existe apagado desde anexos-2 §2.7)
- Reemplazar la «URL pendiente» por el flujo de la sonda. Solo `app/clients` conoce httpx y esas URLs.
- Límites duros: ≤ 1 req/s con jitter (cuenta listar, encrypt y descargar), tope diario `ANEXOS_DESCARGA_MAX`
  (default 300, contado y persistido en Postgres como la cuota), User-Agent genérico identificable
  (`Rastrillo/1.0`; sin correos personales). 403 o 429 → pausa persistida de 60 min y cortar la corrida (como el 429
  de la API); 504/timeout → enfriar 60 s. Nunca reintentar en bucle ni rotar headers.
- Bandera `ANEXOS_DESCARGA_DIRECTA` (existe) sigue siendo el interruptor.

## 3. Job `mercado-anexos` (Actions, no Render: regla 12)
- Workflow propio cada hora en horario hábil + una pasada nocturna; mismo advisory lock.
- Selección: CA vigentes del territorio con `n_documentos > 0` (o `documentos` no vacío) y sin anexo bajado de ese
  documento; orden: cierre más próximo, familias de alimentos primero.
- Por archivo: descargar en memoria → `carga.procesar_archivo(fuente="descarga_directa", empresa_id=None)` (dedupe por
  sha256 ya existe) → extracción LLM con el presupuesto de `llm_uso`. Nada se guarda en disco.
- Resultado en `job_runs`: listados, descargados, ya existentes, inválidos, extraídos, gasto LLM, pausas.
- `/salud` → Pivote: descargas del día vs. tope, pausa vigente, CA vigentes con adjunto pendiente.

## 4. Tests (respx, sin red) y cierre
Flujo feliz, 403 → pausa persistida y corte, tope diario, dedupe, CA sin documentos, clave nunca en logs
(test de enmascarado), job idempotente. `ruff`, `mypy app`, `pytest -m "not postgres" -rs`. Entrada en changelog.
Commit: `F-plat-anexos-auto: descarga y extracción automática de adjuntos`.

## 5. Pasos de Boris
1. Poner `MP_ADJUNTOS_USER_KEY` (y extras) en `.env`, Render no (la web no descarga) y GitHub Secrets.
2. Correr la sonda §1 y pegar la salida antes de que Code termine el cliente.
3. Deploy; crear `GH mercado-anexos` en cron-job.org; encender `ANEXOS_DESCARGA_DIRECTA` en Secrets.
4. En paralelo (recomendado): pedir a ChileCompra (mesa de ayuda / API Mercado Público) acceso oficial a documentos
   de Compra Ágil; si lo dan, se cambia el cliente y se apaga esta vía.
