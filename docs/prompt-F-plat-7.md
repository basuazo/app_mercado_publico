# Prompt de implementación — F-plat-7 (operación del pivote: salud, informe mensual, cierre del ciclo) · 3-nov-2026

> Para Claude Code. **Modelo: Sonnet** (observabilidad, correo y retención sobre piezas ya construidas). Una
> fase = un commit, **sin push**. **Sin migración** salvo que falte algún índice (anotarlo). Va después de
> F-plat-5. Origen: `docs/17-plan-pivote-plataforma-ca.md` §2 C8, R-p1.

> **Revisado contra el código el 4-nov-2026 (HEAD `a9c0cfe`).** La sección "Revisión 4-nov" manda sobre el resto
> donde choquen.

## Revisión 4-nov (Cowork) — leer primero
1. **Requests por job: hoy no existen [V].**
   - `quota_log` (`app/clients/base.py`) guarda un total por día, sin el job.
   - `sync_state.requests_usadas_hoy` lo escriben solo `licitaciones` y el contador de correos.
   - `mercado-backfill` deja `requests` en su resumen; `mercado-ca` no.

   Cambio: cada job con API escribe `requests` en `job_runs.resultado_json` (delta del `QuotaTracker` entre el inicio
   y el fin). `/salud` suma por job las corridas del día en hora de Chile. La alerta "un job fuera de `mercado-*`
   consumió ticket" (R-p1) sale de esa suma. Sin migración.
2. **`/salud` ya tiene `_tablas_mercado_salud`** (F-merc-2 [V]): se extiende con las tablas nuevas, no se duplica.
3. **`ca_anexos.contenido` no existe:** F-plat-2 decidió no guardar el binario (regla 11). §1.4 queda sin esa regla.
4. **`evaluaciones_cache`** es una tabla (F-plat-5, revisión 3). La retención borra las filas de CA cerradas hace más
   de 90 días.
5. **`predicciones.estado`** ya viene de F-plat-5 (revisión 6): §1.3 no necesita migración.
6. **Purga:** las CA con `ofertas_propias` o `predicciones` quedan protegidas (F-plat-5, revisión 12). Test aquí
   también.
7. **Informe idempotente** (regla de jobs idempotentes): marcar cada envío en `sync_state` con fuente
   `mercado_informe:<empresa_id>:<AAAA-MM>`. Re-disparar el job el mismo mes no duplica correos. Sin migración.
8. **Hora del informe: 08:40, no 08:00.** `mercado-indices` empieza a las 07:45 con el mismo advisory lock y podría
   seguir corriendo a las 08:00; el informe saldría `omitido`. Con 08:40 y `esperar_lock_min` hay holgura.
9. **Destinatarios y tope:** `usuarios.dias_resumen` existe (default 3 [V]). El tope diario usa el contador
   `alerts_email` de `app/alerts/email.py` [V].
10. **Disparos reales** (`operacion-disparos.md` + `decisiones.md` 31-oct + F-plat-4) para la tabla de §1.5:
    - `mercado`: cada 15 min.
    - `mercado-nocturno` (job `mercado-backfill`): 22:30 y 03:40.
    - `retencion`: 05:40.
    - `mercado-oc`: 07:20.
    - `mercado-indices`: 07:45.
    - `mercado-informe`: día 1, 08:40.
    - Apagados: `ca`, `ciclo-ca`, `ciclo-match`, `ciclo-activas`, `nocturno`, `resumen` y `catalogos` (que incluye
      plan-anual).

    Boris confirma en cron-job.org que `GH retencion` volvió a estar activo tras F-plat-3-fix y que `GH nocturno`
    sigue en pausa [I].

## Revisión 6-nov (Cowork + código de `main` en `c4c09ec`) — manda sobre la del 4-nov donde choquen
Contraste punto por punto con el código antes de implementar. **Lo que ya existía** (no se duplica) y **lo que cambia**:

1. **§1.1 requests por job.** Sigue sin existir. Cambio respecto a la revisión del 4-nov: no se toca cada runner; se
   instrumenta el único camino por el que pasan scheduler, endpoint y CLI, `orchestrator._run_with_lock`. Lee
   `quota_log` del día de Chile antes y después del job (`clients.base.requests_hoy`) y graba el delta como `requests` en
   `job_runs.resultado_json` (`_con_requests`); el valor que devuelve el job no cambia, y un `requests` propio del runner
   (`mercado-backfill`) manda. Una corrida que falla o se omite a mitad también deja su `requests` si gastó (para que la
   alerta no pierda un job que cayó después de pedir). Sin tabla `quota_log` (tests) no agrega nada. Sin migración.
2. **§1.1 tablas.** `_tablas_mercado_salud` ya existía (F-merc-2) y ya traía `demanda_series`, `pronosticos` y
   `recomendaciones` (F-plat-6): se **extendió**, no se duplicó. Suma las que nacieron después: `competencia_mes`,
   `competencia_comprador_mes`, `demanda_nombres`, `ca_lineas`, `ca_anexos`, `producto_canonico|formato|alias`,
   `proveedores`, `proveedor_productos`, `predicciones`, `ofertas_propias`, `evaluaciones_cache`, `ca_trabajo`,
   `ca_eventos`, `ca_comentarios`, `empresa_productos`, `empresa_producto_movimientos` y `llm_cache`. La alerta del 70 % de
   0,5 GB mira la suma de esas tablas **y** la base entera.
3. **§1.1 «Modelo».** La tarjeta «Precio esperado y P(ganar)» (F-plat-4: modelo activo, Brier, intervalo 80 %, ganables, metas
   en verde/rojo) ya estaba en `/salud`: queda. Se suma el backtest de `demanda` (`evaluacion_modelo`, modelo `demanda`).
3b. **§1.1 «Sombra».** Predicciones abiertas, cerradas y sin adjudicación y acierto acumulado del intervalo 80 %, por
   empresa. «Acierto» = ganador dentro del intervalo; la calibración de P(ganar) sigue en `/mesa/resultados`.
4. **§1.2 informe.** Sin cambios de fondo; lo nuevo: el informe también se ve en la web (`/mesa/informe/{AAAA-MM}`, empresa
   de la sesión; mes mal formado o futuro → 404); es idempotente **por usuario** (`sync_state`, fuente
   `mercado_informe:<empresa_id>:<AAAA-MM>`, `cursor` = ids ya atendidos: un fallo a mitad se completa al re-correr y un
   usuario nuevo recibe solo el suyo); si no hay `BREVO_API_KEY` ni SMTP el job **no falla**: no envía, no marca nada y lo
   dice en `avisos` (`::warning::` en Actions). Mes informado: el mes que acaba de terminar. Hora 08:40 (revisión 4-nov #8).
5. **§1.3 cierre del ciclo.** **Ya existe** en `app/mercado/sombra.py` (`cerrar_predicciones`, `cerrar_ofertas`, `DIAS_ESPERA`
   = 30) y es *más estricto* que el prompt: desierta, cancelada o revocada cierran de inmediato como `sin_adjudicacion`, y
   una CA cerrada sin ofertas leídas o sin seleccionada se cierra a los 30 días (no a los 7). No se cambia ni se duplica.
   `n_desiertas` ya lo alimenta F-plat-6 (`mercado-competencia`). **Lo único que faltaba:** el aviso del segundo punto →
   `sombra.ofertas_sin_resultado` y un bloque en `/mesa/resultados`. Definición: `ca_trabajo.estado = enviada` y
   `resultado` vacío, con la CA cerrada hace más de 14 días, de la empresa de la sesión. **Desvío:** no existe un formulario
   de «resultado manual»; el aviso manda a revisar el portal y anotarlo en los comentarios de la CA.
6. **§1.4 retención.** `ca_eventos` ya se purga (`equipo.purgar_eventos`, 12 meses): no se repite. Se creó la lista
   `retencion.PURGAS` (`Purga(nombre, fn)`, `registrar_purga`): `purgar_terminales` corre cada regla y deja
   `<nombre>_borrados` en el resultado del job. Hoy: `eventos_equipo` (la existente, movida a la lista), `llm_cache` > 180 días
   y `evaluaciones_cache` de CA cerradas hace > 90 días. Las CA con `ofertas_propias`, `predicciones` o `ca_trabajo` ya estaban
   protegidas (F-plat-5 §12, F-equipo): solo se agregó el test. **F-plat-lote** (lotes `cerrado` > 24 meses) y
   **F-plat-seguimiento** (`ca_requisitos` de CA `descartada` > 12 meses) suman su tabla con **una** llamada
   `registrar_purga(...)` (o una entrada en `PURGAS`) y una línea en `_tablas_mercado_salud`, sin tocar `purgar_terminales`.
7. **§1.5 documentación.** Tabla única de disparos y lista de apagados en `operacion-disparos.md`; qué mirar cada semana en
   `operacion.md` §16.
8. **Sin migración.** No hizo falta ningún índice: `job_runs` ya tiene `(job, iniciado_en)` y el filtro del día usa
   `iniciado_en` sobre pocas filas (≈ 100 por día).
9. **No tocado** (otras ramas en paralelo): `app/mercado/anexos.py`, `oferta.py`, `mesa_dia.py` y las tablas de
   F-plat-anexos-2 y F-plat-lote.

## 0. Antes de escribir código
Leer `CLAUDE.md` (1, 3, 10, 11, 14), `17-plan` §2 C8 y §5 (riesgos), `app/api/salud_data.py` y
`templates/salud.html`, `app/alerts/email.py` (Brevo, tope 250/día), `app/core/retencion.py`,
`app/ingest/orchestrator.py` (`job_runs`, `quota_log`), `docs/operacion-disparos.md`.

## 1. Cambios

### 1.1 `/salud` — sección "Pivote" (solo admin)
- Cuota: requests del día **por job** (`mercado-ca`, `mercado-backfill`, resto); alerta roja si cualquier
  job no-mercado consumió ticket (R-p1: la cuota es solo para CA). Pausa de 429 vigente en hora de Chile
  (backlog de `00-estado.md`).
- Territorio activo (regiones por empresa) y última corrida de `mercado-ca` (CA listadas, detalles,
  cotizaciones guardadas, anexos descargados si §1.6 de F-plat-2 está activo).
- Tablas del pivote: filas y tamaño (`pg_total_relation_size`) de `ca_cotizaciones`, `ca_cotizacion_productos`,
  `oc_lineas_ca`, `ca_lineas`, `ca_anexos`, `producto_*`, `proveedor_*`, `predicciones`, `demanda_series`;
  umbral 70 % de 0,5 GB con alerta.
- Catálogo: CA `sin_detalle` / `incompleto` / `completo` (últimos 30 días), líneas pendientes, alias
  aprendidos, correcciones por usuario (últimos 30 días).
- LLM: gasto del mes vs. presupuesto, requests, % desde caché, modelo en uso.
- Modelo: última evaluación (Brier, intervalo 80 %, ganables, cobertura; `demanda_*`), metas en verde/rojo,
  modelo activo (logístico o línea base).
- Sombra: predicciones abiertas/cerradas, acierto acumulado por empresa.

### 1.2 Informe mensual por empresa (correo, Brevo)
- Job `mercado-informe` (sin API), día 1 a las 08:40 Chile (documentar el disparo, no crearlo). Un correo
  por empresa a sus usuarios activos con `dias_resumen > 0` (campo existente de `/cuenta`), dentro del tope diario
  (regla 14).
- Contenido: CA en alcance del mes (n, monto), cotizadas por la empresa, ganadas/perdidas, margen realizado
  total y por familia, acierto de las predicciones (intervalo 80 %, p_ganar calibrada), top 5 compradores del
  mes, top 5 recomendaciones de abastecimiento vigentes, catálogo (líneas confirmadas, CA sin detalle), pie
  "Fuente: Dirección ChileCompra". HTML + texto plano, mismo estilo que el resumen diario.
- Vista web del mismo informe: `/mesa/informe/{AAAA-MM}` (ownership por empresa).

### 1.3 Cierre del ciclo
- Predicciones sin cerrar 7 días después del cierre de la CA (CA cancelada, desierta o sin detalle) se
  cierran con `resultado = sin_adjudicacion` y no cuentan en la calibración; CA `desierta` alimenta
  `n_desiertas` (F-plat-6).
- Ofertas propias `enviada` cuya CA cerró hace > 14 días sin resultado → aviso en `/mesa/resultados` para
  que el usuario lo registre a mano.

### 1.4 Retención y limpieza
- `purgar_terminales` ya exceptúa las CA de regiones activas (F-merc-1 §1.7). Agregar: `llm_cache` > 180
  días se purga; `evaluaciones_cache` de CA cerradas hace > 90 días se borra; las CA con `ofertas_propias` o
  `predicciones` no se purgan. Todo en el job
  `retencion` existente, con conteos en `job_runs`. Tests por regla.
- F-plat-lote: los `lotes` en estado `cerrado` sin cambios hace más de 24 meses se purgan con sus hijas
  (`lote_cas`, `lote_lineas`, `solicitudes_precio`, `solicitud_lineas`). La función ya existe y tiene test
  (`app.mercado.lote.purgar_cerrados`); falta llamarla desde el job `retencion` y contar en `job_runs`.

### 1.5 Documentación operativa
`docs/operacion-disparos.md`: tabla única de disparos del pivote (`mercado` 15 min, `mercado-nocturno`
22:30 y 03:40, `retencion` 05:40, `mercado-oc` 07:20, `mercado-indices` 07:45, `mercado-informe` día 1 08:40) y la lista de los
disparos **apagados** mientras dure el pivote (`ca`, `ciclo-*`, `nocturno`, `resumen`, `catalogos`
con plan-anual). `docs/operacion.md`: qué mirar cada semana en `/salud` → Pivote.

## 2. Tests
Secciones de `/salud` con datos sintéticos (incluida la alerta de cuota no-mercado); informe mensual
(render, tope diario, un correo por empresa, ownership de la vista web); cierre de predicciones viejas;
reglas de retención. `ruff`, `mypy app`, `pytest -rs` sin saltados.

## 3. Cierre
Changelog ("Salud e informe mensual del pivote"). `00-estado.md`. Commit:
`F-plat-7: salud, informe mensual y cierre del ciclo del pivote`.

## 4. Pasos de Boris
1. Crear en cron-job.org el disparo de `mercado-informe`; verificar en `/salud` → Pivote que ningún job
   fuera de `mercado-*` consume ticket.
2. Leer el primer informe mensual con Mario y anotar en `decisiones.md` si el pivote pasa al peldaño 3
   (`16-pivote` §2): ≥ 20 % de adjudicación con margen realizado ≥ 10 %.
