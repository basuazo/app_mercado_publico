# Prompt F-coherencia — la ficha, el smoke test y la faceta de fuente

> **Destino en el repo:** `docs/prompt-F-coherencia.md`
> **Fase:** F-coherencia (una fase, un commit) · **Migración:** ninguna · **Dependencias nuevas:** ninguna
> **Depende de:** F-feed-filtros (`bc33280`), ya en la rama.
> **Va ANTES de F-feed-ui-2.** Son tres cabos sueltos de las fases anteriores; dejarlos para
> después significa construir el panel lateral encima de ellos.

Fase de limpieza: nada nuevo para el usuario, tres incoherencias cerradas.

Reglas del proyecto: español de Chile sin voseo ni argentinismos; entrada en `app/changelog.py` en
el **mismo commit**; `ruff check .`, `python -m mypy app` y `python -m pytest` verdes antes de
cerrar; commit en español con prefijo de fase; **nunca `git add -A`**.

---

## Bloque 1 — La ficha muestra la medianoche que la tarjeta ya evita

`app/api/templates/oportunidad.html:32-38` sigue con la lógica que F-ui-fixes dejó ahí: bandas
propias (`dias_al_cierre <= 3` / `<= 7`) y `oportunidad.fecha_cierre.strftime('%d/%m/%Y %H:%M')`
crudo. Resultado visible en producción: *"cierra en 0d — 21/09/2026 00:00"*.

La tarjeta del feed ya resuelve esto con `texto_cierre` y `banda_urgencia`
(`app/api/presentacion.py`), que deciden el texto y no muestran hora cuando el dato no la tiene de
verdad. La ficha quedó con una copia divergente, así que la misma licitación se lee distinto en la
tarjeta y en su propia ficha.

- La ruta de la ficha (`app/api/routes/pages.py::oportunidad_detalle`) pasa `texto_cierre` y
  `banda_urgencia` ya calculados, igual que hace la del dashboard.
- `oportunidad.html` los consume y **borra** su lógica propia de bandas y su `strftime` del cierre.
  El badge queda con la misma forma que en la tarjeta: color por urgencia, icono, texto.
- `fecha_publicacion` de la línea 64 se queda como está: es una fecha, no un instante, y ahí el
  `strftime('%d/%m/%Y')` es correcto.

Buscar si hay una tercera copia de esa lógica en otras plantillas (`seguidas.html`,
`descartadas.html`, las de correo en `app/alerts/templates/`) y unificarlas también. Si alguna no
puede usar el helper por falta de contexto, dejarlo anotado en el resumen en vez de duplicar.

**Nota:** ahora que la ingesta guarda la hora real, la hora **sí** puede volver al badge de
licitaciones. Pero solo para las filas ya re-sincronizadas; las viejas siguen con el instante
derivado de la fecha. Así que en esta fase **`texto_cierre` no cambia su criterio** — sigue sin
mostrar hora en licitaciones. Devolverla es una decisión aparte, cuando una corrida completa de
`activas` haya pasado sobre el grueso de las filas abiertas.

---

## Bloque 2 — El smoke test miente en su reporte y no normaliza la URL

Dos defectos en `scripts/smoke_test.py`, los dos verificados el 21-sep:

**2.1** `create_engine(settings.database_url)` no pasa por `normalizar_url_driver`, a diferencia de
`make_engine` (`app/api/main.py`) y `alembic/env.py`. Con un `DATABASE_URL` sin driver explícito
—que es como Neon entrega la connstring— el script falla con
`ModuleNotFoundError: No module named 'psycopg2'`, un error que no dice nada sobre la causa real.
Usar el mismo helper que el resto del proyecto.

**2.2** El detector de hora del bloque `--fechas` imprime **"SIN componente de hora"** para
`'2026-09-22 18:00'`, que sí tiene hora. Busca el separador `T` y no reconoce el formato con
espacio que usa Compra Ágil. Es el defecto más serio de los tres de esta fase: una herramienta de
verificación que afirma lo contrario de lo que muestra induce a cerrar mal una pregunta.

Corregirlo para que reconozca los tres formatos observados: ISO con `T` y segundos, ISO con `T` y
milisegundos más `Z`, y `YYYY-MM-DD HH:MM` con espacio y sin segundos. Y que distinga las tres
cosas por separado: si hay hora, si hay segundos, y si hay offset.

Tests offline para el detector, con los valores reales observados.

---

## Bloque 3 — La faceta de fuente, sin pagar memoria por ella

En F-feed-filtros, `fuente` salió del `WHERE` (`app/api/query.py:443`) porque la regla
leave-one-out de `calcular_facetas` necesita contar la fuente descartada, y mi prompt prohibía
cualquier SQL nuevo. La decisión fue correcta dadas esas dos reglas, pero el costo es real: cada
carga del dashboard trae los matches de ambas fuentes aunque el usuario haya filtrado a una, sobre
una instancia de 512 MB que ya carga todos los matches antes de filtrar.

**Reviso mi propia regla.** Lo que había que evitar eran seis agregados por carga contra Neon free,
no toda consulta agregada. **Una** sí cabe.

- Devolver `fuente` al `WHERE` de la query de matches.
- Calcular la faceta de fuente con **una sola** consulta agregada:
  `SELECT fuente, COUNT(*) ... WHERE perfil_id IN (...) GROUP BY fuente`, con los mismos filtros de
  perfil que la query principal y sin el de fuente. Una query, no una por opción.
- Las demás facetas (estado, región) siguen calculándose en Python sobre el conjunto cargado: para
  esas, el filtro no cambia qué filas se traen de la base.
- El resultado de la faceta de fuente tiene que ser idéntico al que devolvía el cálculo en Python.
  **Test que lo compare**: montar un conjunto en SQLite, calcular la faceta por las dos vías y
  afirmar que coinciden. Ese test es el que justifica el cambio.

Si al implementarlo aparece que la faceta agregada no puede respetar algún filtro que sí aplica el
camino en Python (por ejemplo `texto`, que pasa por FTS), **parar y decirlo** en vez de devolver un
número que no corresponde: es preferible dejar `fuente` en Python que mostrar un conteo falso.

---

## Bloque 4 — El foco al descartar

Pendiente de F-feed-ui-1. El script de `base.html` devuelve el foco al botón de la tarjeta, pero al
descartar esa tarjeta se va del DOM un instante después (el script de `index.html` la remueve), así
que el foco termina en `<body>`.

Cuando la acción sea `descartar` y la tarjeta desaparezca, mover el foco a la **tarjeta siguiente**
del feed —a su enlace "Ver ficha"— y si no hay siguiente, al botón "Deshacer" del toast, que es el
control relevante en ese momento. Sin librerías nuevas.

---

## Fuera de alcance

- El panel lateral de filtros: es F-feed-ui-2.
- Devolver la hora al badge de cierre de licitaciones (ver la nota del Bloque 1).
- El techo de cargar todos los matches en memoria antes de filtrar: sigue anotado como deuda; esta
  fase lo alivia para `fuente`, no lo resuelve.
- Argentinismos, `/perfiles`, `python -m app.admin crear-usuario`.

---

## Entregables

1. `oportunidad.html` y la ruta de la ficha consumiendo `texto_cierre` y `banda_urgencia`; lógica
   duplicada eliminada ahí y en las demás plantillas donde aparezca.
2. `scripts/smoke_test.py` con `normalizar_url_driver` y el detector de formatos corregido.
3. `app/api/query.py`: `fuente` de vuelta en el `WHERE`, faceta de fuente por una sola consulta
   agregada.
4. `base.html`: el fallback de foco al descartar.
5. Tests offline: el detector de formatos; la equivalencia de la faceta de fuente por las dos vías;
   render de la ficha mostrando el mismo texto de cierre que la tarjeta para la misma oportunidad.
6. Entrada en `app/changelog.py`.
7. Sin migración.

---

## Checklist de auditoría

**Automática**

- [ ] `ruff check .`, `python -m mypy app`, `python -m pytest` verdes. Base: 762 passed / 20 skipped.
- [ ] `git grep -n "strftime" -- app/api/templates/` solo devuelve fechas (publicación, PAC), ningún
      instante de cierre.
- [ ] En el diff hay **exactamente una** consulta agregada nueva. Si hay dos, algo se fue de alcance.
- [ ] Ningún test nuevo pega a la red.

**Criterio**

- [ ] La misma licitación muestra el mismo texto de cierre en la tarjeta y en la ficha. Hay un test
      que lo afirma.
- [ ] La faceta de fuente da lo mismo por SQL que por Python, con un test que lo compara.
- [ ] El detector del smoke test acierta en los tres formatos reales, y hay test.
- [ ] Ningún texto nuevo usa voseo ni argentinismos.

**Manual (producción)**

- [ ] Abrir la ficha de la licitación que en el feed dice "Cierra hoy": tiene que decir lo mismo, y
      sin `00:00`.
- [ ] Descartar una tarjeta del medio de la lista: el foco queda en la siguiente, no en `<body>`.

---

## Commit

```
F-coherencia: la ficha usa el mismo texto de cierre que la tarjeta

- oportunidad.html consume texto_cierre y banda_urgencia; se elimina la lógica
  de bandas y el strftime del cierre que duplicaba (mostraba la medianoche)
- smoke_test: normaliza la URL como el resto del proyecto, y su detector deja de
  informar "sin hora" para el formato con espacio de Compra Ágil
- query: fuente vuelve al WHERE; su faceta se calcula con UNA consulta agregada
  en vez de cargar ambas fuentes en memoria, con test de equivalencia
- base.html: al descartar, el foco pasa a la tarjeta siguiente
- changelog: entrada de la fase

Sin migración.
```

`git add` explícito por archivo o carpeta. Nunca `git add -A`.
