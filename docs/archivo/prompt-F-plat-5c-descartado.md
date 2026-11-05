# Prompt de implementación — F-plat-5c (P(ganar) oculta mientras el modo sombra no valide) · 5-nov-2026

> Para Claude Code. **Modelo: Sonnet** (cambio chico de UI y una regla de visibilidad; sin esquema nuevo).
> Una fase = un commit, **sin push**. **Con migración mínima** (una columna en `empresas`, ver §1.1) o, si Boris
> prefiere, sin migración usando `sync_state` (anotar cuál). Va sobre F-plat-5 y F-equipo ya commiteadas.
> Origen: decisión del equipo (Revisión CBS, 5-nov): «el modelo predictivo funciona en segundo plano 3–4 semanas
> para validar su precisión **antes de mostrar porcentajes de adjudicación** a los usuarios»
> (`docs/18-revision-cbs-y-flujos-operativos.md` §2.6).

## 0. Antes de escribir código
`CLAUDE.md` (10, 17), `docs/prompt-F-plat-5.md` (tarjeta `_mesa_precio.html`, `_oferta_simulacion.html`,
`_precio_oferta.html`, `/mesa/resultados`), `app/mercado/sombra.py` (`resultados`, `semana_de`),
`app/mercado/evaluacion.py` y `query.resumen_evaluacion_modelo` (Brier, intervalo 80 %), `deps.es_admin`.
[V] Hoy la tarjeta de `/mesa` muestra P(ganar) en 3 puntos y «margen esperado (× P(ganar))» con la etiqueta
«Con predicción (modo sombra)». La simulación de la ficha muestra «P(ganar) a este precio». Eso es lo que se oculta.

## 1. Cambios

### 1.1 Estado del modo sombra por empresa
- `empresas.sombra_publicada_en` (timestamptz, nullable). NULL = en sombra: los usuarios no admin **no ven**
  P(ganar), curva de P(ganar), «margen esperado (× P(ganar))» ni la etiqueta «Con predicción». Sí ven precio
  sugerido, intervalo, costo, logística y margen simple (precio − costo): esos no dependen del modelo de
  adjudicación.
- El admin ve todo siempre, con la etiqueta «Modo sombra: semana n de 4» (`semana_de`, desde la primera
  predicción de la empresa [V, `/mesa/resultados` ya lo calcula]).
- Una sola función `sombra.visible_para(session, empresa_id, user) -> bool` que usan las tres plantillas y la
  simulación; nunca una condición repetida en Jinja.

### 1.2 Criterio de salida (automático) y liberación manual
- En `mercado-mesa` (ya existe, sin API), al final de cada corrida y por empresa: si `sombra_publicada_en` es
  NULL y se cumple el criterio, se fija a `ahora` y se escribe un evento de sistema `sombra_publicada` en
  `ca_eventos` (idempotente por el índice único parcial de F-equipo) para que la campana lo avise.
- **Criterio** (constantes en `app/mercado/sombra.py`, documentadas): ≥ 30 predicciones cerradas con precio
  ganador real de la empresa en las últimas 4 semanas **y** acierto del intervalo 80 % ≥ 70 % **y** Brier del
  último mes cerrado ≤ 0,20 (`evaluacion_modelo`, modelo activo). Si falta cualquiera, sigue en sombra. Los
  números son las metas de `17-plan` C5; Boris puede cambiarlos (están en un solo lugar).
- **Liberación manual** (admin): botón «Publicar P(ganar) a la empresa» en `/mesa/resultados` con CSRF, y
  «Volver a sombra». Ambos escriben el evento.
- `/salud` (sección Pivote si F-plat-7 ya existe; si no, una fila en el bloque de F-plat-4): estado del sombra por
  empresa y los tres números del criterio.

### 1.3 Orden de la mesa en sombra
`/mesa` ordena por `margen_esperado` (que incluye P(ganar)) [V]. En sombra, para no admin, ordenar por margen
simple (precio sugerido − costo − logística) y cierre; el admin conserva el orden actual. Documentarlo en la
pantalla («En modo sombra la mesa se ordena por margen simple»).

## 2. Tests
`visible_para` en los cuatro cruces (admin/no admin × sombra/publicada); plantillas sin «P(ganar)» en el HTML
para no admin en sombra (test de render); criterio automático con fixtures (cumple / falla por cada condición);
liberación manual con ownership y CSRF; evento idempotente; orden de la mesa. `ruff`, `mypy app`, `pytest -rs`
sin saltados.

## 3. Cierre
Changelog («P(ganar) se publica cuando el modo sombra la valida»). `00-estado.md`. Commit:
`F-plat-5c: P(ganar) oculta hasta que el modo sombra valide`.

## 4. Pasos de Boris
1. Deploy con migración (según `despliegue.md`). Comprobar con la cuenta de Mario que la mesa no muestra %.
2. Anotar en `decisiones.md` el criterio de salida o que se liberará a mano.
