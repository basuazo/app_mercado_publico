# Prompt de implementación — F-equipo (trabajo en equipo sobre las CA y alarmas dentro de la app) · 5-nov-2026

> Para Claude Code. **Modelo: Opus** (esquema que hereda F-plat-5, ownership por empresa, concurrencia entre usuarios).
> Una fase = un commit, **sin push**. **Con migración**: `down_revision` = la cabeza que deje F-plat-comp (verificar
> con `alembic heads`; debe haber una sola). Empieza **después de que F-plat-comp esté commiteada**.
> Origen: pedido de Boris (5-nov): la plataforma es un espacio de trabajo **colaborativo**; Boris tiene que saber
> cuándo Mario o Gonzalo priorizan o avanzan una CA, con alarmas **dentro de la app** (sin correo en esta fase).
> Lo marcado [V] está verificado contra el código del 5-nov; si algo no calza, gana el código: anotarlo en el commit.

## 0. Antes de escribir código
- `CLAUDE.md` reglas 10, 11, 13, 14, 17, 18, 26. `docs/prompt-F-plat-5.md` §§1.1–1.4 (ofertas propias, descartes y
  cierre): **esta fase define el estado compartido de cada CA y F-plat-5 se monta encima** (ver §5).
- [V] `usuarios.empresa_id` (un usuario pertenece a una empresa); `deps.empresa_activa_id`, `deps.html_require_empresa`,
  `deps.check_csrf` (header `X-CSRF-Token` o campo `csrf_token`). `/mesa` (F-mesa-v0: `app/api/routes/mesa_dia.py`,
  `app/mercado/mesa_dia.py`, plantilla `mesa_dia.html`). Barra superior y lateral en `base.html`.
- [V] `oportunidades_seguidas` y `alertas` son de la app anterior (por usuario, no por empresa): **no reutilizarlas**.

## 1. Modelo de datos (solo `app/models` define esquema)
### 1.1 `ca_trabajo` — estado compartido de una CA para una empresa
`id`, `empresa_id` (FK), `ca_codigo` (FK a `compras_agiles.codigo`), `estado`, `responsable_id` (FK usuarios,
nullable), `motivo_descarte` (texto corto, nullable), `version` (int, para concurrencia), `creado_en`,
`actualizado_en`, `actualizado_por_id`. `UNIQUE (empresa_id, ca_codigo)`. Índices `(empresa_id, estado)` y
`(empresa_id, responsable_id)`.
- Estados (decisión de Boris, 5-nov): `nueva` (no hay fila = nueva), `priorizada`, `cotizando`, `lista`
  (lista para enviar), `enviada`, `descartada`. Resultado aparte (§1.3), no como estado.
- Transiciones libres entre estados (es un equipo chico; no bloquear), salvo: `descartada` exige motivo; volver de
  `descartada` a otro estado se permite y queda en el registro.
- **Concurrencia:** todo cambio manda la `version` que vio el usuario; si no coincide, no se aplica y la respuesta
  muestra quién lo cambió y cuándo («Gonzalo la pasó a Cotizando hace 2 min»), con el estado actual. Nunca pisar
  en silencio el cambio de otro.

### 1.2 `ca_eventos` — registro de actividad (solo se agrega, nunca se edita)
`id`, `empresa_id`, `ca_codigo`, `usuario_id` (nullable: `NULL` = sistema), `tipo`, `datos` (JSON chico: estado
anterior y nuevo, responsable, motivo, `comentario_id`), `creado_en`. Índice
`(empresa_id, creado_en DESC)` y `(empresa_id, ca_codigo)`.
- Tipos de usuario: `estado` (incluye priorizar, avanzar, enviar, descartar, reabrir), `asignada`, `comentario`,
  `mencion` (uno por persona mencionada).
- Tipos de sistema: `cierre_24h`, `cierre_4h`, `resultado`. **Idempotentes:** índice único parcial
  `(empresa_id, ca_codigo, tipo) WHERE usuario_id IS NULL`; generarlos dos veces no duplica.
- Cada cambio de `ca_trabajo` escribe su evento **en la misma transacción**.

### 1.2-bis `ca_comentarios` — conversación por CA (pedido de Boris, 5-nov)
Un hilo tipo chat por CA y empresa, para comentar y dejar registro: `id`, `empresa_id`, `ca_codigo`, `usuario_id`,
`texto` (≤ 2.000 caracteres, texto plano), `creado_en`, `editado_en` (nullable), `eliminado` (bool). Índice
`(empresa_id, ca_codigo, creado_en)`.
- Se puede comentar una CA aunque no tenga `ca_trabajo` (no la crea ni le cambia el estado).
- El autor puede editar o eliminar **su** comentario; eliminar es lógico y el hilo muestra «comentario eliminado»
  (el registro no desaparece). Editado se marca «(editado)».
- **Menciones:** `@` + nombre de un usuario activo de la misma empresa (autocompletar en el cuadro de texto) genera un
  evento `mencion` para esa persona. Resolver la mención en servidor contra los usuarios de la empresa; un `@` que no
  calza queda como texto.
- Cada comentario nuevo escribe un evento `comentario` (con `comentario_id`) en la misma transacción. Editar o
  eliminar no genera alarma.
- Los usuarios necesitan un nombre visible: si `usuarios` no tiene nombre [verificar], agregar `nombre` (nullable;
  si falta, la parte del correo antes de la `@`) y dejarlo editable en `/admin`.

### 1.3 Resultado de una CA enviada
Cuando una CA en `enviada` ya cerró y sus ofertas están leídas (`cotizaciones_obtenidas_en` no nulo): comparar
`empresas.rut` con los RUT de `ca_cotizaciones` (usar `comprador.rut_competidor` de F-plat-comp, el mismo
normalizador para los dos lados [V cuando F-plat-comp esté commiteada]):
`ganada` (nuestra oferta elegida tiene `seleccionado = true`), `perdida` (está y no fue seleccionada, o fue
seleccionada otra), `no_registrada` (no aparece nuestro RUT: oferta no subida o RUT mal escrito), `sin_dato`
(`seleccionado` nulo en todas), `desierta` (CA desierta/cancelada). Empresa sin RUT → no se calcula y la pantalla
avisa con enlace a `/admin/empresas`. Regla de elección: la de F-merc-1c, función única (F-plat-comp §6.2).
Guardar `resultado` y `resultado_en` en `ca_trabajo` (columnas nullable) y emitir un evento `resultado`.

### 1.4 Lectura y preferencias por usuario
- `usuarios.actividad_leida_hasta` (DateTime nullable): todo evento posterior es «no leído» para ese usuario.
- `preferencias_alarma`: `usuario_id`, `tipo` (`actividad_equipo`, `asignada_a_mi`, `cierre_cercano`, `resultado`),
  `activa`. Sin fila = activa (las cuatro alarmas que eligió Boris vienen prendidas).
- **Sin filas por usuario por evento** (nada de fan-out): los no leídos se calculan con una consulta sobre
  `ca_eventos` de la empresa, posteriores a `actividad_leida_hasta`, **excluyendo los que hizo el propio usuario**,
  filtrados por sus preferencias:
  - `actividad_equipo`: eventos de usuario hechos por otros (`estado`, `comentario`, y `asignada` a otra persona);
  - `asignada_a_mi`: `asignada` con `datos.responsable_id = yo` (aunque tenga apagada la actividad del equipo);
  - **`mencion` a mí: siempre llega** (no se puede apagar);
  - `cierre_cercano`: `cierre_24h` / `cierre_4h` de CA en `priorizada|cotizando|lista`;
  - `resultado`: `resultado`.

## 2. Alarmas de sistema (sin API, sin estado en memoria; regla 10)
`generar_alarmas(session, empresa_id, ahora)` en `app/mercado/equipo.py`:
- `cierre_24h` / `cierre_4h` para CA de la empresa en `priorizada|cotizando|lista` cuyo `fecha_cierre` cae dentro
  de 24 h / 4 h (las fechas están en UTC naive [V]; mostrar en hora de Chile);
- `resultado` para las `enviada` que ya se pueden resolver (§1.3).
Se llama (a) al final de `mercado-ca` y de `mercado-nocturno` para todas las empresas activas (consulta por lotes,
sin cargar CA completas; regla 12), y (b) en el sondeo de la campana, a lo más una vez por minuto por empresa
(marca en Postgres, p. ej. `sync_state`, no en memoria). Idempotente por el índice único parcial.

## 3. UI (solo usuarios con empresa; mutaciones con CSRF; regla 18)
- **Campana** en la barra (`base.html`, en las dos barras): contador de no leídos, sondeo HTMX cada 60 s
  (`hx-trigger="load, every 60s"`) contra `GET /actividad/contador` (solo un número). Al abrirla, los últimos 20 no
  leídos con texto humano: «Mario priorizó *Abarrotes Cesfam San Carlos* · cierra mañana 15:00», «Gonzalo te asignó
  …», «Mario comentó en …: "el flete a Coihueco sale caro"», «Gonzalo te mencionó en …», «Cierra en 4 h y sigue en
  Cotizando: …», «Ganamos …». Botón «Marcar todo como leído» (POST). El título de la
  pestaña muestra el contador: «(3) Rastrillo».
- **`/actividad`:** el registro completo de la empresa, paginado, con filtros por persona, tipo y CA. Cada fila enlaza
  a la CA.
- **Tarjeta de `/mesa`:** chip de estado y responsable (iniciales), y acciones en línea con HTMX: Priorizar, Tomar
  (me hago responsable), Asignar a…, cambiar estado (Cotizando / Lista / Enviada) y Descartar (con motivo). Ícono de
  comentarios con su número (y un punto si hay comentarios no leídos); al abrirlo, el hilo de la CA en un panel
  lateral, sin salir de la mesa.
  Filtro nuevo «Equipo»: todas · sin trabajar · priorizadas · mías · en curso · enviadas; las `descartada` se
  ocultan por defecto (con opción de verlas). Mantener el conteo HTMX que ya tiene la mesa.
- **Ficha de la CA:** bloque «Equipo» con estado y responsable, y el **hilo de comentarios** (más antiguo arriba,
  cuadro para escribir abajo, Enter envía y Shift+Enter hace salto de línea) intercalado con los cambios de estado
  como líneas de sistema («Mario pasó a Cotizando · 10:42»). Se refresca con el mismo sondeo de 60 s mientras está
  abierta. Es el mismo componente que el panel lateral de la mesa.
- **«Hoy»:** bloque «Equipo»: mis CA (responsable = yo) por cierre, priorizadas que cierran en < 24 h sin enviar, y las
  últimas 5 actividades.
- **Preferencias:** en la página de cuenta del usuario, cuatro interruptores (§1.4).
- Sin correo, WhatsApp ni push en esta fase (regla 14: el cupo de Brevo es para los resúmenes). Dejar
  `generar_alarmas` y la consulta de no leídos como funciones reutilizables para un resumen por correo futuro.

## 4. Seguridad y datos
- Ownership en servidor (reglas 17 y 26): toda consulta y mutación filtra por la `empresa_id` del usuario; una CA de
  otra región o un `ca_trabajo` de otra empresa → 404. Asignar solo a usuarios **activos de la misma empresa**.
- Regla 11: medir bytes por evento; retención: `retencion` purga `ca_eventos` de más de 12 meses (no los
  `ca_trabajo`). Una CA con `ca_trabajo` no se purga (igual que F-plat-5 §12).
- Los comentarios son texto del equipo: escapados en plantilla, sin `|safe`; los saltos de línea se muestran con CSS
  (`white-space: pre-wrap`), no convirtiendo a HTML. Los enlaces se muestran como texto.

## 5. Ajustes a F-plat-5 (dejarlos escritos en `docs/prompt-F-plat-5.md`, sección «Revisión F-equipo»)
- `ca_descartes_mesa` **no se crea**: el descarte es `ca_trabajo.estado = 'descartada'` con motivo.
- «Marcar enviada» de F-plat-5 cambia `ca_trabajo` a `enviada` (con su evento); `ofertas_propias` guarda el
  contenido de la oferta (líneas, precios, total), no el estado del flujo.
- El cierre de F-plat-5 §1.4 reutiliza §1.3 de esta fase; no se define dos veces.
- La mesa de F-plat-5 respeta el filtro «Equipo» y oculta las descartadas por defecto.

## 6. Tests
Transiciones y eventos en la misma transacción; descartar sin motivo → 422; conflicto de `version` (dos usuarios)
→ no se aplica y se informa quién cambió; asignar a usuario de otra empresa o inactivo → rechazo; ownership
cruzado en GET y POST (→ 404); CSRF en cada mutación; no leídos: excluye los propios, respeta preferencias,
`asignada_a_mi` llega aunque la actividad del equipo esté apagada; comentarios: crear, editar y eliminar solo el
autor (otro → 403/404), eliminado se ve como tal, mención a usuario de la misma empresa sí y de otra empresa o
inexistente no, la mención llega con preferencias apagadas, texto con `<script>` escapado, límite de 2.000; `generar_alarmas` idempotente (dos llamadas =
mismos eventos) y con los bordes de 24 h / 4 h; resultado: ganada, perdida, no_registrada, sin_dato, desierta,
empresa sin RUT; sondeo limitado a una vez por minuto; retención de eventos; pie de fuente donde se muestren datos
de CA. `ruff check .`, `python -m mypy app`, `python -m pytest -rs` completo contra Postgres local, sin saltados.

## 7. Cierre
Changelog («Trabajo en equipo, comentarios y alarmas»). `00-estado.md` y `decisiones.md` (estados del flujo, alarmas dentro de la
app, sin correo por ahora). Commit: `F-equipo: estado compartido de las CA, comentarios, actividad del equipo y alarmas`.

## 8. Pasos de Boris
1. Deploy de día con `GH mercado` pausado y Actions vacío (migración); en Render, un `Running upgrade … ->` de esta
   fase; reactivar `GH mercado`.
2. Verificar que Mario y Gonzalo tengan usuario activo **en la misma empresa** (`/admin`).
3. Prueba con dos navegadores (o con Mario): uno prioriza, asigna y comenta mencionando al otro; el otro ve la
   campana subir en ≤ 1 min y el hilo actualizado.
