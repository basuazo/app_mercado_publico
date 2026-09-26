# Prompt F-vigencia — el dashboard muestra solo lo vigente

> **Destino en el repo:** `docs/prompt-F-vigencia.md`
> **Fase:** F-vigencia (una fase, un commit) · **Migración:** ninguna · **Dependencias nuevas:** ninguna
> **Orden:** 2.º (después de F-estados-vencidos, antes de F-guardar).
> **Decisión de Boris (24-sep):** el dashboard es para lo que todavía se puede postular. Lo guardado y
> lo vencido se revisa en otro lado (F-registro).

## Revisión 26-sep (leer antes que el resto)

Revisado contra `main` `8068b87` [V]: `get_oportunidades_usuario`, `_pasa_cierre`,
`_pasa_familia`, `calcular_facetas`, el comentario de F-coherencia, `familia_de_estado`,
`ESTADOS_TERMINALES`, `_matches_nuevos_usuario` y la sección 6 "Estado" de `_panel_filtros.html`
siguen donde dice el prompt. F-estados-vencidos (`2c9c402`) ya corrige estados atrasados; esta fase
igual decide por fecha antes que por estado.

- **Modelo:** Sonnet (`/model sonnet`). Fase chica, sin migración ni API.
- **Orden de ejecución:** después del commit de F-plan-busqueda, en la misma carpeta. Si
  F-plan-busqueda sigue sin commit, detenerse y avisar: las dos fases tocan `app/changelog.py` y
  no deben mezclarse en el árbol de trabajo.
- **Sin migración:** la cabeza de Alembic debe seguir igual al terminar (`alembic heads`).
- **Dónde va `es_vigente`:** `app/core/vigencia.py` (junto a `tiempo.py`), importando
  `familia_de_estado` de `app/models/enums.py`. No en `app/api/` ni en `app/matching/` (esa carpeta
  la reserva F-ca-rubro).
- **El prefiltro en SQL corre en SQLite y en Postgres:** los tests usan los dos. Nada específico
  de un dialecto sin la rama equivalente (ver `_sin_detalle` en `orchestrator.py`).
- **F-ficha-volver (`222cac3`):** "Volver" desde la ficha reconstruye la URL del feed con sus
  filtros, incluido `estado`. Test: una URL de Volver con `estado=...` sigue funcionando y el
  filtro se ignora.
- **Resumen:** si al filtrar lo vencido no queda nada, el usuario cuenta como `sin_nuevos` y no se
  mueve `ultimo_resumen_en` (mismo comportamiento de hoy cuando no hay matches nuevos).
- **Antes del deploy (Boris):** anotar el contador actual del dashboard ("N oportunidades") para
  compararlo después.
- **Cierre:** commit sin push. Se audita en Cowork antes del push. `git add` solo de los archivos
  de la fase.

Reglas del proyecto: CLAUDE.md completo. Español de Chile; entrada en `app/changelog.py` en el mismo
commit; `ruff check .`, `python -m mypy app`, `python -m pytest` verdes; commit "F-vigencia: …";
**nunca `git add -A`**.

---

## Leer antes
`app/api/query.py` (`get_oportunidades_usuario`, `_construir_item`, `_aplicar_filtros`,
`calcular_facetas`, el comentario de F-coherencia sobre por qué las facetas van en Python),
`app/models/enums.py` (`FamiliaEstado`, `familia_de_estado`, `ESTADOS_TERMINALES`),
`app/api/presentacion.py`, `app/core/tiempo.py`, `app/alerts/email.py` (`_matches_nuevos_usuario`),
`app/api/templates/_panel_filtros.html`.

## Qué construir

### 1. Una sola definición de "vigente"
Función pura `es_vigente(estado, fecha_cierre, fuente, ahora, fecha_publicacion=None) -> bool` en un módulo de dominio
(no en `api/`, porque la usan también `alerts/`). Regla:
- familia `ABIERTA` o `DESCONOCIDO` **y** `fecha_cierre > ahora`, **o**
- Compra Ágil con `fecha_cierre` NULL, familia `ABIERTA` **y `fecha_publicacion` de hace 7 días o
  menos** (constante `CA_SIN_CIERRE_VIGENCIA_DIAS = 7`). Decisión de Boris (26-sep) [V, Paso 0 de
  F-ca-rubro]: 8.567 de las CA abiertas (58 %) no tienen `fecha_cierre`, y el 90 % de las CA
  cierra antes de ~5 días desde su publicación (p50 47 h, p90 119 h). Sin este tope quedarían
  "vigentes" para siempre. CA sin cierre y sin `fecha_publicacion` → no vigente.
- Todo lo demás (cierre pasado, `cerrada`, terminales, suspendida) → no vigente.
Se decide **por fecha antes que por estado**: el estado puede venir atrasado aunque
F-estados-vencidos lo mejore. `fecha_cierre` es naive en UTC; `ahora` sale de `ahora_utc()`.
Documentar la regla en el docstring con este párrafo.

### 2. El feed carga solo vigentes
- En `get_oportunidades_usuario`, prefiltrar en SQL los matches cuyo `Licitacion`/`CompraAgil`
  puede ser vigente (`fecha_cierre > ahora` o, para CA, `fecha_cierre IS NULL`) y confirmar con
  `es_vigente` en Python. La vigencia NO es una faceta del usuario, así que filtrarla en SQL no
  choca con el razonamiento de F-coherencia; dejarlo explicado en el comentario. Beneficio
  lateral: el feed deja de crecer con el histórico de matches (techo de 512 MB).
- En el prefiltro SQL, la rama de CA sin cierre también exige `fecha_publicacion >= ahora - 7 días`.
- Parámetro nuevo `solo_vigentes: bool = True`; el registro (F-registro) lo usará en `False`.
- Facetas y conteos ("N oportunidades · N nuevas hoy · N ocultas por baja relevancia") se
  recalculan sobre el conjunto vigente.

### 3. Panel de filtros
- La sección **Estado** del panel pierde sentido (solo quedaría "Abierta"): sacarla del feed.
  El parámetro `estado` en la URL se sigue aceptando y se ignora (no romper enlaces compartidos).
- El atajo "Fecha de cierre" no debe ofrecer rangos en el pasado; `cierre_desde` anterior a hoy
  se acota a hoy.

### 4. Correo de resumen
`_matches_nuevos_usuario` excluye lo no vigente al momento de enviar (una oportunidad que cerró
entre el match y el correo no se anuncia).

### 5. Sin tocar todavía
Descartadas, seguidas y "Me sirve" siguen como están (F-guardar y F-registro).

## Tests (mínimo)
- `es_vigente`: tabla de casos — publicada con cierre futuro; publicada con cierre pasado (el caso
  1417913-96-L126); cerrada con cierre futuro; adjudicada; CA con cierre NULL abierta; CA con cierre
  NULL cerrada; CA con cierre NULL abierta publicada hace 6 días (vigente) y hace 8 días (no
  vigente); CA con cierre NULL sin fecha de publicación; desconocido con cierre futuro; borde exacto `fecha_cierre == ahora`.
- Feed: un match vencido no aparece ni cuenta en facetas ni en "nuevas hoy"; con
  `solo_vigentes=False` sí aparece.
- Panel: sin sección Estado; `?estado=adjudicada` no rompe ni filtra.
- Resumen: un match nuevo pero ya vencido no entra al correo.
- Los tests existentes de feed que usan fechas pasadas deben moverse a fechas relativas a `ahora`
  (no fijas), no "arreglarse" desactivando la vigencia.

## Después del deploy (Boris)
El contador del dashboard debe bajar. Anotar el antes/después en `docs/00-estado-actual.md`.

*Fuente de los datos de dominio: Dirección ChileCompra.*
