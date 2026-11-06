# Prompt — F-dis-lote-datos (lo que En trabajo necesita y hoy no existe) · 7-nov-2026

> Para Claude Code. **Modelo: Opus**: concurrencia entre lotes, menciones y aprendizaje. Un commit, **sin push**,
> **con migración**. Leer `docs/diseno/README.md`, `docs/diseno/decisiones-diseno.md` (En trabajo / Lote, Equipo) y la
> maqueta `Lote.dc.html`. Requiere F-dis-1 en `main`. Puede correr en paralelo con F-dis-hoy-mesa (worktree propio).
> **No toca templates**: los viste F-dis-lote.

## 1. Conversación del lote completo
- Hoy `ca_comentarios` es solo por CA. Agregar `ca_comentarios.lote_id` (nullable, FK a `lotes` con CASCADE) y hacer
  `ca_codigo` nullable con un CHECK «exactamente uno de los dos». Índice por `(lote_id, creado_en)`.
- En `app/mercado/equipo.py`: `comentar_lote`, `hilo_lote`, con las mismas reglas que el hilo de CA (menciones
  `@nombre` → evento `mencion` con `lote_id` en `datos`, editar/eliminar propios, no leídos sin fan-out).
- Rutas `GET /equipo/lote/{id}/hilo` y `POST /equipo/lote/{id}/comentarios` (mismo patrón y CSRF que las de CA).

## 2. Mover y quitar CA entre lotes
- `lote.mover_ca(session, empresa_id, codigo, desde_id, hacia_id, version_desde, version_hacia, usuario, ahora)`:
  mueve la CA con sus líneas elegidas y reservas de stock, en una transacción, con control de `version` en ambos
  lotes (409 si cambió). Evento en los dos lotes. Devuelve lo necesario para **deshacer** (mover de vuelta).
- `quitar_ca` ya existe (`/lotes/{id}/cas/{codigo}/quitar`): devolver también el dato para deshacer y aceptar
  `POST /lotes/{id}/cas/{codigo}/restaurar`.
- Ruta `POST /lotes/{id}/cas/{codigo}/mover` con `hacia` (o `nuevo` para crear el lote destino con nombre).
- Renombrar lote: `POST /lotes/{id}/nombre` (la columna `lotes.nombre` ya existe), con `version`.
- «Recibidas de la mesa»: `lote.cas_sin_lote(session, empresa_id)` (CA en trabajo `COTIZANDO` sin lote activo).

## 3. Recordar para líneas parecidas
- Cuando el usuario elige un producto propio (inventario) o de proveedor para una línea y marca «Recordar para líneas
  parecidas», guardar la asociación **texto normalizado de la línea → producto** por empresa. Revisar primero si
  `producto_alias` / `vinculo.py` ya cubren esto para productos canónicos; si sirve, reutilizarlo con una columna
  `empresa_id`; si no, tabla `empresa_eleccion_recordada` (`empresa_id`, `clave_texto`, `empresa_producto_id` o
  `proveedor_producto_id`, `creado_por_id`, `creado_en`, `usos`).
- `lote._elegir_costo` (o la función que elige hoy) consulta primero lo recordado y marca la elección como
  «elegido por historial». Debe poder deshacerse por línea y olvidarse desde la línea («Olvidar»).
- Definir «línea parecida» de forma simple y documentada: mismo producto canónico vinculado, o mismo texto
  normalizado (minúsculas, sin tildes, sin cantidades). Nada de similitud difusa en esta fase.

## 4. Presencia liviana
- `presencia` en memoria del proceso no sirve (varios workers): tabla `presencia` (`usuario_id`, `recurso` String(40)
  como `lote:14` o `ca:CODIGO`, `detalle` String(80), `visto_en`), único por usuario + recurso, UPSERT.
- `POST /presencia` (cada 30 s desde la pantalla abierta, HTMX `every 30s`) y `GET /presencia?recurso=` → quiénes
  están (visto en los últimos 60 s, sin el usuario actual). Limpieza: filas de más de 1 día se borran en el job diario.
- Costo: una escritura cada 30 s por usuario con la pantalla abierta (3–10 usuarios): aceptable en Neon; anotarlo.

## 5. Tests y cierre
Hilo de lote (menciones, empresa ajena, CHECK); mover con versiones en conflicto (409), reservas que se mueven,
deshacer; quitar/restaurar; recordar → próxima línea igual se llena sola, olvidar; presencia (expira, empresa ajena
no ve). Cierre según `docs/diseno/README.md` §13 (sin changelog: no es visible hasta F-dis-lote).
Commit: `F-dis-lote-datos: hilo por lote, mover CA entre lotes, elecciones recordadas y presencia`.
