# Prompt — F-plat-anexos-auto (descarga y extracción automática de adjuntos de CA) · 6-nov-2026

> **ACTIVADA (Boris, 6-nov tarde).** Se automatiza la descarga sin Cowork ni personas: job HTTP directo (vía A),
> elegido frente a un navegador automático (vía B, Playwright) porque carga menos a ChileCompra (1 request por
> archivo) y no cuesta a ninguna escala. Motivo: analizar TODAS las CA del territorio, no solo las que la empresa
> cotiza (~1.300 CA/mes en Ñuble [V, datos abiertos mayo 1.258, sep 1.345]); a mano serían ~3,5 h/mes por región.
> Sigue en paralelo el pedido formal a ChileCompra de un endpoint oficial; si lo dan, se cambia el cliente y se apaga
> esta vía. **Si ChileCompra bloquea (403/429 persistentes o bloqueo de IP), se para y se escala a ChileCompra:
> prohibido rotar IP, proxies, cuentas o headers para eludir un bloqueo.**

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

## Lo verificado el 6-nov en la consola de Boris (CA de prueba con un PNG de 152 kB) [V]
- La descarga es UN `GET https://adjunto.mercadopublico.cl/adjunto-compra-agil/v1/adjuntos-compra-agil/descargar/{UUID}`
  (preflight 204 + XHR 200, `content-type: image/png`, respuesta blob). Initiator: el bundle de la ficha
  (`Summary.actions.js`), no la extensión.
- Headers: `Authorization: Bearer <JWT>` y `user_key: <clave pública del frontend>`.
- El JWT es **anónimo**: emisor `https://heimdall.mercadopublico.cl/auth/realms/chilecomprarealm` (Keycloak), `azp`
  `mercadoPublicoClient`, `preferred_username` `anonymouser`, vigencia ~8 h. Se entrega a cualquier visitante sin login:
  no hace falta cuenta de empresa.
- [I] El `{UUID}` de `descargar` es el mismo `id` de `documentos[]` que la API v2 ya entrega (anexos-2). Si se confirma,
  no hacen falta `listar` ni `encrypt`.
- [I] Cómo obtiene la ficha el token anónimo (endpoint de heimdall y tipo de grant): por verificar en la sonda.

## 1. Sonda (la corre Boris, de día)
Paso 0 (Boris, navegador, sin código): en una ficha con adjuntos, pestaña Network → (a) anotar la request que emite
el token anónimo (URL de heimdall, método, `grant_type` y `client_id`; **sin pegar valores de token ni secretos**);
(b) comparar el UUID de `descargar/…` con `compras_agiles.documentos` de esa CA.
Luego `scripts/smoke_test.py --adjuntos <codigo>`: obtener token anónimo → descargar (o listar → encrypt → descargar si
el paso 0 muestra que el UUID no coincide) de UNA CA vigente; imprime status, forma del
JSON (claves, sin valores sensibles), tipo y tamaño del archivo. Headers desde variables de entorno:
`MP_ADJUNTOS_USER_KEY` y, si hace falta, `MP_ADJUNTOS_HEADERS_EXTRA` (JSON). **Ningún valor de clave en código, tests,
fixtures, logs ni commits**: se tratan como `MP_TICKET` (regla 1; el logger las enmascara). Boris obtiene los valores
desde el bundle en su navegador. Si la sonda da 403 con los headers, parar y reportar: no probar otras formas de entrar.

## 2. Cliente (`app/clients/mp_documentos.py`, ya existe apagado desde anexos-2 §2.7)
- Reemplazar la «URL pendiente» por el flujo de la sonda. Solo `app/clients` conoce httpx y esas URLs.
- Token anónimo: se pide al inicio de la corrida, vive solo en memoria y se renueva al vencer (nunca a log, disco ni
  Postgres). Un fallo al obtenerlo corta la corrida.
- Límites duros: ≤ 1 req/s con jitter (cuenta token, listar, encrypt y descargar), tope diario `ANEXOS_DESCARGA_MAX`
  (default 300; Ñuble necesita ~60/día hábil [I: ~60 % de las CA con adjuntos], contado y persistido en Postgres como la cuota), User-Agent genérico identificable
  (`Rastrillo/1.0`; sin correos personales). 403 o 429 → pausa persistida de 60 min y cortar la corrida (como el 429
  de la API); 504/timeout → enfriar 60 s. Nunca reintentar en bucle ni rotar headers, IP, proxies o cuentas.
- Bandera `ANEXOS_DESCARGA_DIRECTA` (existe) sigue siendo el interruptor.

## 3. Job `mercado-anexos` (Actions, no Render: regla 12)
- Workflow propio cada hora en horario hábil + una pasada nocturna; mismo advisory lock.
- Selección: **todas** las CA vigentes del territorio (no solo las familias de la empresa) con `n_documentos > 0` (o
  `documentos` no vacío) y sin anexo bajado de ese documento; orden: cierre más próximo, familias de las empresas primero.
  `empresa_id=None`: el anexo es dato público del territorio, no de una empresa (regla 26 aplica a lo comercial).
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
