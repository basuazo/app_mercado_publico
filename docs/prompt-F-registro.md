# Prompt F-registro — "Mi registro": guardadas, vencidas recientes, descartadas y archivadas

> **Destino en el repo:** `docs/prompt-F-registro.md`
> **Fase:** F-registro (una fase, un commit) · **Migración:** ninguna (verificar; si hace falta un
> índice, justificarlo) · **Dependencias nuevas:** ninguna
> **Orden:** 4.º (después de F-vigencia y F-guardar; antes de F-ficha-modal).
> **Decisiones de Boris (24-sep):**
> - Lo vencido y lo guardado se revisan fuera del dashboard, en un registro personal.
> - Las vencidas **no guardadas** con match **≥ 30** siguen visibles **14 días** después del cierre;
>   después se **ocultan, no se borran**.
> - Las guardadas que vencen quedan en el registro para revisarlas después.

Reglas del proyecto: CLAUDE.md completo, en especial 17 (ownership). Español de Chile; entrada en
`app/changelog.py` en el mismo commit; `ruff check .`, `python -m mypy app`, `python -m pytest`
verdes; commit "F-registro: …"; **nunca `git add -A`**.

---

## Leer antes
`es_vigente` y `solo_vigentes` (F-vigencia), el modelo Guardar (F-guardar),
`app/api/query.py` (`get_oportunidades_usuario`, `listar_seguidas_detalle`,
`listar_descartadas_detalle`, `_construir_item`), `app/api/routes/pages.py` (`/seguidas`,
`/descartadas`), plantillas `seguidas.html`, `descartadas.html`, `_card_oportunidad.html`,
`_panel_filtros.html`, `base.html`.

## Qué construir

### 1. Página `/registro` con pestañas (`?tab=`)
| Pestaña | Qué muestra | Orden |
|---|---|---|
| `guardadas` (default) | Guardadas **vigentes** | cierre más próximo primero |
| `cerradas` | Guardadas **no vigentes**: siguen avisando cambios (adjudicación, etc.) | cierre más reciente primero |
| `vencidas` | **No guardadas, no descartadas**, no vigentes, con `fecha_cierre` en los últimos `REGISTRO_DIAS_GRACIA` días y **score máximo del usuario ≥ `REGISTRO_MIN_SCORE_VENCIDAS`** | cierre más reciente primero |
| `descartadas` | Lo que hoy muestra `/descartadas`, con Restaurar | más reciente primero |
| `archivadas` | Seguidas archivadas, con Desarchivar | más reciente primero |

- Cada pestaña muestra su conteo en la etiqueta ("Vencidas recientes (12)").
- **Vencidas**: cada tarjeta dice "Desaparece en N días" y ofrece **Guardar** y **Descartar**.
  Guardar la pasa a `cerradas`. Texto de ayuda arriba: "Oportunidades con buen match que cerraron
  hace poco sin que las guardaras. Se ocultan a los 14 días; guárdalas si quieres conservarlas."
- Reusar `_card_oportunidad.html` (misma tarjeta que el feed, con chip de estado real) y el panel de
  filtros con los filtros que apliquen (texto, fuente, perfil, monto). En `cerradas` y `vencidas`
  la sección **Estado** del panel sí vuelve (ahí distingue cerrada / adjudicada / desierta).
- Paginación igual que el feed (20 + "Ver 20 más").

### 2. Parámetros
`REGISTRO_DIAS_GRACIA` (default **14**) y `REGISTRO_MIN_SCORE_VENCIDAS` (default **30**) en
`settings.py`, ajustables por variable de entorno, **no** requeridos en `_job.yml`.
Nota en el código: el piso 30 queda bajo el del feed (40); es intencional (decisión 24-sep) y hace
que en Vencidas aparezcan oportunidades con match 30–39 que nunca se vieron en el dashboard.

### 3. "Ocultar, no borrar"
Pasados los 14 días la vencida simplemente no cumple el filtro: **ningún job borra matches**. La
purga de 90 días sigue igual (y desde F-guardar respeta las guardadas).

### 4. Qué se considera "fecha de vencimiento"
`fecha_cierre` si existe. CA con `fecha_cierre` NULL que dejó de estar Abierta: usar
`fecha_ultimo_cambio` (hora de Chile, ignorar la Z falsa, regla 6) y, si también es NULL,
`actualizado_en`, como referencia para la ventana de 14 días. Documentarlo en el docstring y
testearlo.

### 5. Navegación y rutas viejas
- Nav: "Mi registro" → `/registro`. Botón "Ver descartadas (N)" del feed → `/registro?tab=descartadas`.
- `/seguidas` → 303 a `/registro?tab=guardadas` (con `archivadas=1` → `tab=archivadas`);
  `/descartadas` → 303 a `/registro?tab=descartadas`. Los correos antiguos siguen funcionando.
- Dashboard: bajo el contador, un enlace discreto "N vencidas recientes en tu registro" cuando N > 0
  (así el usuario sabe que no desaparecieron).

### 6. Consultas
Reusar `get_oportunidades_usuario(solo_vigentes=False)` + filtros propios de cada pestaña, sin
duplicar el pipeline de facetas. Todo acotado por ownership (regla 17). Para `vencidas` prefiltrar
en SQL por ventana de fecha (no cargar el histórico entero).

## Tests (mínimo)
- Vencida no guardada con score 35 cerrada hace 3 días → en `vencidas` con "Desaparece en 11 días".
- Score 25 → no aparece. Cerrada hace 15 días → no aparece, **y la fila de match sigue existiendo**.
- Guardada vigente → en `guardadas` y también en el dashboard con chip "Guardada".
- Guardada que vence → sale del dashboard, entra a `cerradas`.
- Descartada vencida → solo en `descartadas`, nunca en `vencidas`.
- Score = máximo entre los perfiles del usuario; otro usuario no ve nada ajeno (404/vacío).
- CA con cierre NULL no abierta → usa `fecha_ultimo_cambio` (y `actualizado_en` si falta) para la ventana.
- Redirecciones de `/seguidas` y `/descartadas`.
- Conteos por pestaña coinciden con lo listado.

*Fuente de los datos de dominio: Dirección ChileCompra.*
