# Prompt F-guardar — "Me sirve" y "Activar alertas" pasan a ser una sola acción: Guardar

> **Destino en el repo:** `docs/prompt-F-guardar.md`
> **Fase:** F-guardar (una fase, un commit) · **Migración:** SÍ (Alembic, solo datos) · **Dependencias nuevas:** ninguna
> **Orden:** 3.º (después de F-vigencia, antes de F-registro).
> **Decisión de Boris (24-sep):** unificar. Guardar = queda en tu registro personal **y** te avisa
> de sus cambios. Las guardadas vigentes siguen en el dashboard con la etiqueta "Guardada".

Reglas del proyecto: CLAUDE.md completo, en especial 11 (retención), 14 (≤250 correos/día), 17
(ownership), 18 (CSRF). Español de Chile; entrada en `app/changelog.py` en el mismo commit
explicando el cambio a los usuarios; `ruff check .`, `python -m mypy app`, `python -m pytest`
verdes; commit "F-guardar: …"; **nunca `git add -A`**.

---

## Hechos que condicionan [V, código, 24-sep]
- "Me sirve" es `MatchFeedback.valor = sirve` (`app/matching/feedback.py`). **No lo lee el
  matching ni el score**: es solo una marca. Unificarla no cambia resultados.
- "Activar alertas" es `OportunidadSeguida` (`app/matching/seguimiento.py`), con `archivada`,
  `estado_visto` y `notas` (esta última sin UI). Dispara `detectar_cambio_estado_seguidas`
  (compara estado vs `estado_visto`) y `detectar_recordatorio_cierre_seguidas` (cierre < 48 h).
- `purgar_terminales` (`app/core/retencion.py`) protege solo matches con alerta **pendiente**: una
  seguida terminal pierde `raw_json` e ítems a los 90 días. Con "Guardar" eso es perder el registro.
- `_onboarding_modals.html` explica "Me sirve" y "Activar alertas" por separado.

## Qué construir

### 1. Modelo de dominio
- **Guardada = `OportunidadSeguida` con `archivada = False`.** No se crea tabla nueva.
- Guardar reusa `seguir_oportunidad`. Quitar de guardadas = `dejar_de_seguir`. Archivar se mantiene
  (lo expone F-registro).
- `MatchFeedback` queda **solo para descarte**. El valor `sirve` deja de escribirse; se mantiene en
  el enum para leer filas viejas sin romper (regla 6), y un comentario explica por qué.
- Guardar una descartada la saca de descartadas (borra el descarte). Descartar una guardada la quita
  de guardadas. Nunca las dos a la vez; test de ambas direcciones.

### 2. Migración de datos (Alembic, reversible)
Por cada `MatchFeedback` con `valor='sirve'`:
- si no existe `OportunidadSeguida` para (usuario, fuente, código) → crearla con
  `estado_visto` = **estado actual** de la oportunidad (si no, `detectar_cambio_estado_seguidas`
  mandaría un correo por cada una al día siguiente) y `archivada=False`;
- si existe archivada → dejarla archivada (el usuario ya decidió);
- borrar la fila `sirve`.
`downgrade`: recrear `sirve` para las seguidas que creó esta migración (marcarlas: guardar los ids
en una tabla temporal o reconstruir por `creado_en` de la migración; elegir y documentar).
**Paso 0 (solo lectura, pegar al final):** cuántas `sirve`, cuántas ya tienen seguida, cuántas
seguidas archivadas, por usuario. Estimar cuántos recordatorios de cierre < 48 h generaría la
migración y confirmar que caben en el tope de 250 correos/día; si no, la migración deja
`seguimiento_cierre` ya marcado como enviado para las que cierran en < 48 h.

### 3. UI
- Tarjeta del feed y ficha: un solo botón **Guardar** / **Guardada ✓** (toggle, `aria-pressed`,
  anuncio en `#anuncios`). Desaparecen "Me sirve" y "Activar alertas". Descartar se mantiene.
- Tarjeta guardada: chip "Guardada" (texto + icono, no solo color).
- `_onboarding_modals.html`: reescribir el párrafo (Guardar = queda en tu registro y te avisa de
  cambios; Descartar = no la vuelves a ver).
- Nav: "Alertas activas" pasa a "Mi registro" apuntando a `/seguidas` por ahora (F-registro lo
  mueve a `/registro`).
- Rutas: `POST /oportunidad/{f}/{c}/guardar` (toggle, HTMX y no-HTMX con `next`, CSRF, ownership).
  `me-sirve`, `seguir` y `dejar-de-seguir` quedan como alias que llaman a lo mismo, para no romper
  formularios cacheados; marcarlas deprecated en el código.

### 4. Retención
`purgar_terminales` no purga `raw_json` ni ítems/productos de oportunidades con una
`OportunidadSeguida` (archivada o no) de cualquier usuario. Test.

## Tests (mínimo)
- Migración: `sirve` sin seguida → seguida con `estado_visto` actual; `sirve` + seguida archivada →
  sigue archivada; `sirve` borrado; downgrade restaura; correr upgrade dos veces no duplica.
- Tras migrar, `detectar_cambio_estado_seguidas` no crea alertas para las migradas.
- Guardar/quitar/descartar: exclusión mutua, ownership (usuario B no guarda por A → 404), CSRF.
- Tarjeta y ficha: un botón Guardar con `aria-pressed` correcto; ya no aparecen "Me sirve" ni
  "Activar alertas".
- Retención protege guardadas y archivadas.
- Adaptar los tests existentes que usan `me-sirve` (`test_feedback_routes`, `test_feed_ui`,
  `test_ficha_routes`, `test_ui_accesibilidad`): cambiar la expectativa, no borrar cobertura.

## Después del deploy (Boris)
La migración la aplica Render al arrancar (`startCommand` corre `alembic upgrade head`; nunca
correrla a mano contra producción, `docs/despliegue.md`). Es solo de datos, así que los workflows de
Actions que corran con el código nuevo antes del deploy no chocan con el esquema. Al día siguiente,
revisar en `/salud` que no salió una ráfaga de correos.

*Fuente de los datos de dominio: Dirección ChileCompra.*
