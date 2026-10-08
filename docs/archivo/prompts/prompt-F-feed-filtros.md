# Prompt F-feed-filtros — filtros, facetas y paginación del feed

> **Destino en el repo:** `docs/prompt-F-feed-filtros.md`
> **Fase:** F-feed-filtros (una fase, un commit) · **Migración:** ninguna · **Dependencias nuevas:** ninguna
> **Depende de:** F-ui-fixes auditada y en `main`.
> **Origen:** `docs/13-auditoria-ux.md` §4. El diseño del dashboard promete filtros que el backend
> no tiene; esta fase los construye **sin tocar una sola plantilla**.

---

## Contexto y regla de oro de la fase

`get_oportunidades_usuario` (`app/api/query.py`) hoy acepta `fuente`, `perfil_id`, `texto`,
`region`, `orden` ("score" | "cierre"), `min_score`, `limit` (50 fijo desde la ruta) y `offset`, y
devuelve `(items, total, total_sin_filtro_relevancia)`. Aplica los filtros en Python sobre el
conjunto ya cargado, después de la query.

**Todo el trabajo de esta fase se hace en Python sobre ese mismo conjunto ya cargado.** Ni una
query agregada nueva, ni un `SELECT COUNT` por faceta. Seis queries extra por carga de página
contra Neon free es exactamente el patrón que agotó las CU-horas antes (ver `docs/11` y las reglas
10-15 de `CLAUDE.md`). Si en algún punto parece que hace falta SQL nuevo, parar y anotarlo en vez de
escribirlo.

No se toca ninguna plantilla ni ninguna ruta HTML más allá de pasar los parámetros nuevos. La UI es
F-feed-ui.

Reglas del proyecto: parseo defensivo siempre; queries 100% parametrizadas; jobs y funciones
idempotentes; `ruff check .`, `python -m mypy app`, `python -m pytest` verdes antes de cerrar;
commit en español con prefijo de fase; nunca `git add -A`.

---

## Bloque 1 — Familias de estado: YA EXISTEN, solo se usan

`FamiliaEstado`, el mapa exhaustivo de los 16 valores de `EstadoOportunidad` y
`familia_de_estado()` los creó **F-feed-ui-1** en `app/models/enums.py`, junto con el test que
recorre el enum completo y falla si aparece un valor sin mapear.

**No crearlos de nuevo. No duplicar el mapa. No tocar `enums.py`.** Esta fase solo los importa y
los usa para el filtro por estado del Bloque 3.3.

Si al leer `app/models/enums.py` no estuvieran ahí, parar y avisar en vez de improvisarlos: sería
señal de que F-feed-ui-1 no está en la rama.

## Bloque 2 — El resultado del feed deja de ser una tupla

`get_oportunidades_usuario` devuelve hoy una 3-tupla y ahora necesita entregar además las facetas y
el conteo de nuevas del día. Una 5-tupla sería ilegible.

Definir en `app/api/query.py` un dataclass congelado:

```python
@dataclass(frozen=True)
class ResultadoFeed:
    items: list[dict[str, Any]]
    total: int                     # tras todos los filtros, antes de paginar
    total_sin_filtro_relevancia: int
    facetas: dict[str, dict[str, int]]
    nuevas_hoy: int
```

`get_oportunidades_usuario` pasa a devolver `ResultadoFeed`. **Hay que actualizar todos los
llamadores**, incluida la API REST `/api/oportunidades`, que debe conservar exactamente la misma
forma de JSON que expone hoy — los tests existentes lo cubren; si alguno cambia de forma, es una
regresión, no un ajuste.

---

## Bloque 3 — Filtros nuevos

Todos opcionales, todos aplicados en Python después de la query, todos con `None` = sin filtro.

### 3.1 Monto

Parámetros: `monto_min: float | None`, `monto_max: float | None`,
`incluir_monto_no_informado: bool = True`.

`_construir_item` ya normaliza `monto` (usa `monto_clp` en licitaciones y `monto_disponible_clp` en
Compra Ágil): el filtro se aplica sobre ese campo normalizado, no sobre las columnas crudas.

Un item con `monto is None` pasa el filtro solo si `incluir_monto_no_informado` es `True`. El
default es `True` a propósito: el monto falta seguido en la fuente oficial y excluirlo por omisión
escondería oportunidades reales.

### 3.2 Rango de fecha de cierre

Parámetros: `cierre_desde: datetime | None`, `cierre_hasta: datetime | None`,
`incluir_sin_fecha_cierre: bool = True`.

**Trampa conocida, no la pases por alto:** Compra Ágil está dejando `fecha_cierre` en NULL en la BD
(deuda documentada en `docs/00-estado-actual.md`), incluidas las `publicada`, y el matching ya las
trata como abiertas. Un filtro por rango sin escape excluiría **todas** las Compras Ágiles. De ahí
`incluir_sin_fecha_cierre=True` por defecto.

Comparar siempre contra el mismo tipo de datetime que guarda la columna: si es naive, comparar
naive; si es aware, convertir el borde a esa zona. Verificar cuál es antes de escribir la
comparación (esto quedó anotado como pendiente en F-ui-fixes §2.5; si esa fase ya lo resolvió,
reutilizar el hallazgo en vez de repetirlo).

### 3.3 Familias de estado

Parámetro: `familias: set[FamiliaEstado] | None`. Un item pasa si la familia de su estado está en el
conjunto. `None` = todas.

### 3.4 Región, con su advertencia de alcance

El parámetro `region` ya existe en la firma y nunca se expuso. Esta fase no cambia su
comportamiento: se documenta en el docstring que **solo afecta a Compra Ágil**, porque `Licitacion`
no guarda región y las licitaciones pasan todas. La advertencia visible al usuario la pone
F-feed-ui; aquí basta con que el docstring lo diga para que nadie asuma lo contrario después.

No intentar arreglar la región de licitaciones en esta fase: es un cambio de modelo y de ingesta.

---

## Bloque 4 — Orden por monto

`orden` acepta hoy `"score"` y `"cierre"`. Agregar `"monto"`: descendente, con los `None` **al
final** en ambos sentidos (un monto no informado no es un monto cero).

Un valor de `orden` desconocido cae al default actual sin romper, igual que hoy.

---

## Bloque 5 — Conteos por faceta

`facetas` es un dict de dicts, calculado en Python sobre el conjunto ya filtrado:

```python
{
  "fuente":   {"licitaciones": 96, "compras_agiles": 28},
  "estado":   {"abierta": 118, "en_evaluacion": 6, "adjudicada": 41, ...},
  "region":   {"13": 74, "05": 31, "sin_region": 19},
}
```

**Regla de búsqueda facetada, y es la parte fácil de equivocar:** el conteo de cada faceta se
calcula con todos los demás filtros aplicados **menos el suyo propio**. Si el usuario ya filtró por
"Licitaciones", el conteo de "Compra Ágil" tiene que seguir mostrando cuántas habría si soltara ese
filtro — si no, el número baja a cero y el filtro se vuelve una trampa de la que no se puede salir.

Implementación sugerida: una función pura que reciba la lista completa y el conjunto de filtros, y
para cada faceta aplique los predicados de las otras. Mantenerla testeable sin BD.

Las claves de `region` son los códigos; los nombres los resuelve `presentacion.nombre_region` en la
capa de plantilla, no aquí.

---

## Bloque 6 — Nuevas del día

`nuevas_hoy`: cuántos de los matches del conjunto tienen `fecha_match` dentro del día calendario
actual en `America/Santiago`.

`fecha_match` es inmutable por diseño (F-notificaciones): es la primera vez que esa oportunidad
matcheó ese perfil, y no se re-toca al re-scorear. Eso hace el conteo confiable.

Usar `ZoneInfo("America/Santiago")` para el borde del día, nunca la hora del proceso: Render corre
en UTC (regla 5 de `CLAUDE.md`).

---

## Bloque 7 — Paginación, y su choque con el agrupado

F-feed-agrupado **eliminó a propósito** la paginación global y la reemplazó por un cap de 10 ítems
por grupo con "ver más en este grupo". Reponerla sin más la contradiría.

Regla adoptada:

- `agrupar_oportunidades` acepta un valor nuevo `"ninguno"`, que devuelve un solo grupo implícito
  con todos los ítems y sin cap.
- Cuando la agrupación es `"ninguno"`, manda la paginación: `limit` por defecto **20**, `offset`, y
  `total` para poder decir "quedan N".
- Cuando el usuario agrupa por motivo, región o fuente, sigue mandando el cap por grupo y el
  `offset` se ignora. El comportamiento actual no cambia.

**El default de la ruta sigue siendo `"motivo"` en esta fase.** El cambio a `"ninguno"` es una
decisión visual y va en F-feed-ui, junto con la plantilla que lo aprovecha: cambiarlo antes dejaría
el dashboard actual mostrando una lista plana dentro de un acordeón de un solo ítem, que es raro sin
ser un arreglo.

Subir `limit` de 50 a 20 baja el trabajo por carga, pero ojo: `get_oportunidades_usuario` carga
todos los matches del usuario en memoria y recién después filtra y corta. Esta fase **no cambia ese
patrón** — Render free tiene 512 MB y con el volumen actual aguanta — pero dejarlo anotado en el
resumen como techo conocido, por si el volumen crece.

---

## Fuera de alcance (no tocar)

- Cualquier plantilla de `app/api/templates/`.
- `app/api/static/` y el montaje de `StaticFiles`: los crea F-feed-ui.
- Cambiar el default de `agrupar_por` en la ruta.
- Arreglar la región de licitaciones o la `fecha_cierre` NULL de Compra Ágil: son cambios de modelo
  e ingesta, cada uno con su fase.
- Recalibrar `feed_min_score_default`: necesita la distribución real de producción.
- Queries SQL nuevas de cualquier tipo.

---

## Entregables

1. (nada en `app/models/enums.py`: `FamiliaEstado` y su mapa ya existen desde F-feed-ui-1).
2. `app/api/query.py`: `ResultadoFeed`; `get_oportunidades_usuario` con los parámetros nuevos y el
   nuevo tipo de retorno; los predicados de filtro y el cálculo de facetas como funciones puras;
   `agrupar_oportunidades` con `"ninguno"`; orden por monto.
3. Llamadores actualizados, con la forma del JSON de `/api/oportunidades` intacta.
4. Tests, todos offline, sobre listas fabricadas cuando sea posible:
   - cada filtro nuevo, incluidos los casos `None` (monto no informado, cierre sin fecha);
   - orden por monto con `None` al final;
   - facetas: el conteo de una faceta no se ve afectado por su propio filtro;
   - `nuevas_hoy` con el borde del día en `America/Santiago`, no en UTC;
   - paginación con `"ninguno"` y cap por grupo con `"motivo"`, en el mismo test para dejar la regla
     documentada.
5. Entrada en `app/changelog.py` en lenguaje simple.
6. Sin migración.

---

## Checklist de auditoría

**Automática**

- [ ] `ruff check .`, `python -m mypy app`, `python -m pytest` los tres verdes. Anotar el conteo.
- [ ] Ningún test nuevo pega a la red ni requiere Postgres.

**Criterio — lo que hay que mirar a mano en el diff**

- [ ] No hay ni un `select(` nuevo, ni un `func.count`, ni un `group_by` agregado. Buscarlos
      explícitamente en el diff.
- [ ] El conteo de cada faceta excluye su propio filtro. Verificarlo en el test, no solo leyendo el
      código.
- [ ] El borde del día usa `ZoneInfo("America/Santiago")`, no `datetime.now()` pelado.
- [ ] `incluir_monto_no_informado` e `incluir_sin_fecha_cierre` están en `True` por defecto, y hay
      un test que falla si alguien los cambia.
- [ ] La forma del JSON de `/api/oportunidades` no cambió.
- [ ] El default de `agrupar_por` en la ruta sigue siendo `"motivo"`.

---

## Commit

```
F-feed-filtros: monto, cierre, estado, facetas y paginación del feed

- query: ResultadoFeed reemplaza la tupla de retorno; filtros por monto, rango de
  cierre y familia de estado, todos en Python sobre el conjunto ya cargado
- orden por monto con los no informados al final
- conteos por faceta excluyendo el filtro propio de cada una
- nuevas_hoy sobre fecha_match con el día calendario de America/Santiago
- agrupar_oportunidades acepta "ninguno"; con él manda la paginación (20 por página)
- documenta en el docstring que el filtro de región solo afecta a Compra Ágil
- changelog: entrada de la fase

Sin migración. Sin queries SQL nuevas.
```
