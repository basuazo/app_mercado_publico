# Prompt F-fecha-cierre — la hora de cierre real, y el bug que pierde licitaciones

> **Destino en el repo:** `docs/prompt-F-fecha-cierre.md`
> **Fase:** F-fecha-cierre (una fase, un commit) · **Migración:** ninguna · **Dependencias nuevas:** ninguna
> **Va ANTES de F-feed-filtros**, que construye el filtro por rango de cierre sobre este campo.
> **Origen:** hallazgo verificado el 21-sep-2026 durante la auditoría de F-ui-fixes.

---

## El problema, verificado en el código

Tres piezas que juntas producen un bug de negocio, no cosmético:

**1. El parser tira la hora.** `app/clients/types.py::parse_fecha_v1` hace
`date.fromisoformat(s[:10])` y devuelve un `date`. Su propio docstring dice que el listado de
licitaciones activas trae `FechaCierre` en ISO-8601, así que la hora **llega en la respuesta y el
slice de 10 caracteres la descarta**.

**2. La ingesta fabrica medianoche.** `app/ingest/licitaciones.py::_fecha_a_dt` hace
`datetime(d.year, d.month, d.day)`. El `00:00` que la app mostraba en la ficha no venía de
ChileCompra: lo inventaba el código.

**3. El filtro de candidatos compara contra UTC naive.** `app/matching/engine.py:174` filtra
`Licitacion.fecha_cierre > ahora`, y `ahora` viene de `datetime.now(UTC).replace(tzinfo=None)`.

**La consecuencia.** Una licitación que cierra el 24 a las 15:00 queda guardada como `24/09 00:00`.
Ese instante, leído como UTC, son las **21:00 del día 23 en Chile**. Desde esa hora la licitación
deja de ser candidata: no aparece en el feed ni genera alertas durante todo su último día, que es
justo cuando importa. Para una herramienta cuyo trabajo es avisar antes de que cierren, es la falla
que no puede tener.

Compra Ágil tiene una variante del mismo problema: `parse_fecha_iso` hace `s.strip().rstrip("Z")` y
prueba formatos sin `%z`, así que **descarta el offset** y guarda como naive algo que venía marcado
como UTC.

Confianza: el comportamiento del código es **[V]**, leído en esas cuatro funciones. Lo que queda
**[I]** es a qué huso se refieren los valores que manda la API, porque los fixtures son sintéticos
y `docs/01-analisis-api-mercado-publico.md:163` dice "fechas a ISO/UTC" como recomendación de
diseño, no como observación de una respuesta real.

---

## Paso 0 — Verificación contra la fuente primaria (la corre Boris, no los tests)

Regla 20/23 del proyecto: no escribir como hecho lo que no se verificó en la fuente. Y regla del
stack: las llamadas reales viven solo en `scripts/smoke_test.py` y las ejecuta el humano.

Agregar a `scripts/smoke_test.py` una comprobación que pida **una** página del listado de
licitaciones activas y del listado de Compra Ágil, y que imprima, sin exponer el ticket:

- el valor crudo de `FechaCierre` tal como llega, para dos o tres registros;
- si trae componente de hora distinto de `00:00:00`;
- si trae offset explícito (`Z`, `+00:00`, `-03:00`) o ninguno.

Con eso se decide el resto. **No avanzar a los bloques siguientes sin ese resultado**: si la API
no manda hora, la mitad de esta fase no aplica y hay que replantearla.

Cuando el resultado esté, escribirlo en `docs/00-estado-actual.md` marcando [V] lo observado.

---

## Bloque 1 — El parser conserva lo que la fuente manda

`parse_fecha_v1` hoy devuelve `date | None` y se usa en varios lugares. Cambiar su tipo de retorno
rompe llamadores, así que:

- Dejar `parse_fecha_v1` como está, para los campos que de verdad son fechas (`ddmmaaaa`).
- Agregar `parse_fecha_v1_dt(s) -> datetime | None`, que para un valor ISO devuelve el datetime
  completo **con** su hora y su offset si lo trae, y para un `ddmmaaaa` devuelve medianoche
  **marcada como tal** (ver Bloque 3).
- En `app/ingest/licitaciones.py`, usar la nueva función para `fecha_cierre` y
  `fecha_publicacion`, y dejar de pasar por `_fecha_a_dt` cuando ya hay un datetime real.

`parse_fecha_iso` (Compra Ágil): dejar de hacer `rstrip("Z")`. Parsear con `datetime.fromisoformat`,
que en Python 3.11+ entiende `Z` y los offsets, y caer a los formatos actuales solo si eso falla.
Conservar el offset.

Parseo defensivo siempre (regla 6): cualquier valor que no calce devuelve `None` y se loguea, nunca
rompe la ingesta.

---

## Bloque 2 — Un solo criterio de zona horaria, en un solo lugar

Hoy conviven tres precisiones: licitaciones a medianoche fabricada, Compra Ágil con hora pero sin
offset, y un `ahora` en UTC naive. Eso hay que unificar, y la decisión debe quedar **escrita**.

Criterio a implementar, salvo que el Paso 0 diga otra cosa:

- Las columnas siguen siendo `DateTime` sin `timezone=True` (no hay migración en esta fase), así
  que en la base se guarda **naive en UTC**, que es lo que ya asume `_ahora()`.
- Un valor con offset se convierte a UTC y se guarda naive.
- Un valor **sin** offset se interpreta como **hora de Chile continental**
  (`ZoneInfo("America/Santiago")`) y se convierte a UTC. Es una API del Estado de Chile publicando
  plazos chilenos; la suposición es razonable, pero va marcada como **[I]** en el docstring y en
  `docs/00-estado-actual.md` hasta que el Paso 0 la confirme o la corrija.
- Toda esa lógica vive en **una** función en `app/clients/types.py` (por ejemplo
  `a_utc_naive(valor)`), y nadie más convierte husos a mano. Si aparece un `ZoneInfo` suelto en
  `app/ingest/` o en `app/matching/`, es un error.

`_ahora()` en `app/ingest/licitaciones.py` y el `ahora` de `app/matching/engine.py` deben venir del
mismo helper, no cada uno del suyo. Verificar si hay más definiciones de "ahora" en el proyecto y
dejar una sola.

---

## Bloque 3 — Cuando la fuente solo da la fecha

Un `ddmmaaaa` no tiene hora, y para esos casos seguir guardando medianoche es inevitable. Lo que no
se puede es tratarla como si fuera el instante de cierre.

Regla: cuando solo hay fecha, el instante que se guarda es el **fin del día** en hora de Chile
convertido a UTC, no el comienzo. Una licitación que cierra "el 24" está abierta durante el 24; con
medianoche al inicio la damos por cerrada un día antes.

Esto arregla el bug de candidatos incluso para los registros que no traigan hora, y es la parte de
esta fase con más impacto: `Licitacion.fecha_cierre > ahora` deja de descartar oportunidades en su
último día.

Documentar el criterio en el docstring de la función de conversión.

---

## Bloque 4 — Los datos ya guardados

Las filas existentes tienen medianoche de inicio de día. `upsert_basica`
(`app/ingest/licitaciones.py`) ya sobrescribe `fecha_cierre` cuando el item entrante **sí** trae el
dato —solo protege el caso `None`— así que las licitaciones activas se curan solas en la próxima
corrida de `activas`.

Verificarlo leyendo `upsert_basica` y confirmarlo en el resumen de la fase. **No escribir un script
de backfill**: si el upsert cura, no hace falta; y un UPDATE masivo sobre `production` no es parte
de una fase de código.

Las terminales viejas quedan con el dato viejo. No importa: no son candidatas y la retención las
purga a los 90 días.

---

## Bloque 5 — La UI, con cuidado

`app/api/presentacion.py::texto_cierre` hoy muestra la hora solo en Compra Ágil, y en licitaciones
solo la fecha, justamente porque la hora era fabricada (F-feed-ui-1).

**No devolver la hora a licitaciones en esta fase.** Después del arreglo, las filas recién
sincronizadas tendrán hora real, pero las que no se hayan re-sincronizado seguirán con el valor
derivado de la fecha. Mostrar la hora entonces volvería a publicar un dato que no vino de la
fuente, para un subconjunto de las filas, sin manera de distinguirlas en la UI.

Cuando el Paso 0 confirme que la API manda hora y una corrida de `activas` haya pasado, se puede
hacer una fase de una línea que devuelva la hora al badge. Dejarlo anotado, no adelantarlo.

Lo que sí cambia en la UI: nada. Esta fase es datos y matching.

---

## Fuera de alcance (no tocar)

- Migración a columnas `timezone=True`: es un cambio de esquema y merece su propia fase con dry-run
  en una branch de Neon.
- Mostrar la hora de cierre en licitaciones (Bloque 5).
- La `fecha_cierre` NULL de Compra Ágil, que es una deuda distinta ya documentada: aquí se arregla
  el huso y el offset, no la captura del dato ausente.
- Cualquier filtro nuevo del feed: eso es F-feed-filtros.
- Script de backfill o UPDATE masivo (Bloque 4).

---

## Entregables

1. `scripts/smoke_test.py`: la comprobación del Paso 0, sin exponer el ticket.
2. `app/clients/types.py`: `parse_fecha_v1_dt`, `parse_fecha_iso` corregida, y la única función de
   conversión a UTC naive con su criterio documentado y marcado [V]/[I].
3. `app/ingest/licitaciones.py`: usa las nuevas funciones; `_fecha_a_dt` eliminada o reducida al
   caso "solo fecha" con fin de día.
4. `app/matching/engine.py` y la ingesta comparten un único "ahora".
5. Tests offline, con `respx` para cualquier cosa de red: ISO con hora y con offset, ISO con hora
   sin offset, `ddmmaaaa`, valores basura, y un test que afirme que una licitación que cierra hoy a
   las 15:00 hora de Chile **sigue siendo candidata** a las 10:00 de ese mismo día. Ese test es el
   que prueba que el bug murió.
6. Entrada en `app/changelog.py` en lenguaje simple.
7. Sin migración.

---

## Checklist de auditoría

**Automática**

- [ ] `ruff check .`, `python -m mypy app`, `python -m pytest` los tres verdes. Anotar el conteo.
- [ ] Ningún test nuevo pega a la red (`respx` para todo).
- [ ] `git grep -n "ZoneInfo" -- app/` solo muestra el helper de conversión, el borde del día donde
      ya existía, y nada más disperso.
- [ ] `git grep -n "rstrip(\"Z\")" -- app/` no devuelve nada.

**Criterio**

- [ ] Existe el test de "cierra hoy a las 15:00 y a las 10:00 sigue siendo candidata".
- [ ] El criterio de huso está escrito en un docstring, con [V] lo verificado y [I] lo supuesto.
- [ ] Hay una sola definición de "ahora" en el proyecto.
- [ ] `texto_cierre` no cambió: licitaciones siguen sin hora.
- [ ] No se agregó ningún script de backfill.

**Manual (la corre Boris)**

- [ ] El Paso 0 se ejecutó y su resultado quedó escrito en `docs/00-estado-actual.md`.
- [ ] Tras un `POST /api/jobs/run?job=activas` en producción, revisar en `/salud` o en el feed que
      una licitación que cierra hoy siga apareciendo.

---

## Commit

```
F-fecha-cierre: conservar la hora real de cierre y dejar de perder licitaciones su último día

- parse_fecha_v1_dt conserva el datetime del ISO que manda el listado de activas,
  que antes se cortaba con un slice de 10 caracteres
- parse_fecha_iso deja de hacer rstrip("Z") y conserva el offset
- una sola función convierte a UTC naive, con el criterio de huso documentado
- cuando la fuente solo da fecha, se guarda el FIN del día en hora de Chile:
  con medianoche de inicio dábamos por cerrada la licitación un día antes
- ingesta y matching comparten un único "ahora"
- test que prueba que una licitación que cierra hoy a las 15:00 sigue siendo
  candidata a las 10:00
- smoke_test: comprobación del formato real de FechaCierre (la corre el humano)
- changelog: entrada de la fase

Sin migración. La UI no cambia: la hora vuelve al badge en una fase posterior.
```

`git add` explícito por archivo o carpeta. Nunca `git add -A`.
