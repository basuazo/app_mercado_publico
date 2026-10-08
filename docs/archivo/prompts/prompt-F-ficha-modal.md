# Prompt de implementación — F-ficha-modal (+ descarte en dos botones) · 07-oct-2026

> Reemplaza al prompt de diseño D-ficha-modal del 24-sep. Aquel describía artboards para un
> diseñador y era anterior a F-guardar, F-ajustes y F-registro. Este es para Claude Code:
> implementar directamente en el stack actual. Modelo: Sonnet. Una fase = un commit, sin push.

## 0. Antes de escribir código (lectura, sin cambios)
Lee `CLAUDE.md`, `docs/13-auditoria-ux.md` (§2, matriz de la ficha) y el código actual de:
- la página `/oportunidad/{fuente}/{codigo}` (ruta + plantilla),
- la tarjeta del feed y su uso en dashboard, `/registro` y explorador `/compras-agiles`,
- las rutas de Guardar / Descartar / Archivar / Deshacer y el parámetro de origen ("registro"),
- el flujo actual de descartar con exclusión de términos (sugeridas, vista previa,
  `exclusiones_que_chocan`, `excluir_palabras`) que dejó F-ajustes,
- `app/core/vigencia.py` (`es_vigente`, `fecha_vencimiento`, `condicion_*_vigente`).

Si algo de este prompt contradice el código (una ruta no existe, una acción se llama distinto,
el flujo de descarte no es como se describe en §3), **no inventes**: adapta al código real y
déjalo anotado en el reporte final como "Desvío del prompt: …". Si la contradicción cambia el
alcance, detente y repórtalo sin commitear.

## 1. Contexto
La decisión que sirve la ficha es una: **¿me presento o no, y antes de cuándo?** Pesa, en
orden: fecha y HORA de cierre (Chile), estado real, monto, organismo, qué piden, match y
competencia. Hoy la ficha es una página aparte con diseño viejo; abrirla saca a la persona
del feed. Decisión vigente de Boris: **ficha en modal sobre la lista, URL con `ficha=`; la
página `/oportunidad/...` se mantiene porque la enlazan los correos.**

## 2. Ficha en modal
### 2.1 Dónde se abre
Desde las tarjetas del dashboard, de `/registro` (todas las pestañas) y del explorador de CA.
Clic en el título o en "Ver ficha" abre el modal; Ctrl/Cmd-clic y clic medio siguen abriendo
la página `/oportunidad/...` en otra pestaña (el `href` real se mantiene).

### 2.2 Mecánica
- Bootstrap 5.3 `modal-dialog-scrollable`, `modal-xl` en escritorio, `modal-fullscreen-md-down`.
- Contenido por HTMX desde un endpoint parcial (p. ej. `GET /oportunidad/{fuente}/{codigo}/modal`)
  que **reutiliza la misma función de datos y el mismo parcial de contenido** que la página
  completa. La página `/oportunidad/...` pasa a renderizar ese parcial dentro del layout: un
  solo lugar para el contenido de la ficha.
- Mismas reglas de acceso que la página actual (ownership en servidor; CA sin match abrible en
  solo lectura como hoy). Sin `raw_json` nuevo: solo lo que ya se guarda.
- URL: al abrir, `history.pushState` agrega `ficha={fuente}:{codigo}` conservando los demás
  parámetros (filtros, pestaña, página). Al cerrar (×, Esc, clic fuera, botón Atrás) se quita.
  Cargar una URL con `ficha=` abre el modal sobre la lista. Atrás del navegador con el modal
  abierto lo cierra sin salir de la lista.
- Foco atrapado en el modal y devuelto a la tarjeta de origen al cerrar.

### 2.3 Contenido (de arriba a abajo)
1. **Cabecera fija:** tipo + código; título (máx. 2 líneas, `title` con el completo); fila con
   estado · cierre con urgencia · monto · Match. × arriba a la derecha.
2. **Barra de acciones fija bajo la cabecera:** primaria "Abrir en Mercado Público ↗" (o el
   texto de por qué no está disponible, como hoy); luego las **mismas acciones y estados que
   la tarjeta**: Guardar (unifica "Me sirve" + alertas desde F-guardar), los dos botones de
   descarte de §3, Archivar donde corresponda. Sin "Volver".
3. **Resumen siempre visible:** organismo, región, publicación, ofertas recibidas, razones del
   match (máx. 3 + "+N" desplegable). Sin match: en lugar de score y razones, la misma nota
   que la tarjeta ("Guardada desde Explorar CA" / "Ya no calza con tus perfiles" / solo lectura).
4. **Aviso de detalle faltante** si es CA sin descripción ni productos descargados:
   "La app todavía no descarga la descripción ni los productos de esta Compra Ágil."
5. **Pestañas:** Descripción · Ítems/Productos (N) · Competencia. Una pestaña sin datos no se
   muestra; si ninguna tiene datos, no se muestra la barra de pestañas.
6. "Fuente: Dirección ChileCompra" **una vez**, al pie del modal.

### 2.4 Estado y cierre
El estado mostrado sale de `app/core/vigencia.py`, no de lógica nueva: si el estado sigue
abierto pero `es_vigente` dice que no (cierre pasado, o CA sin cierre publicada hace > 7 días),
mostrar "Cierre vencido" en la familia gris/roja, nunca "Abierta". Cierre con día, hora y
"hrs" en hora de Chile; urgencia ≤1d `#DC2626`, ≤3d `#EA580C`, ≤7d `#D97706`, >7d `#CBD5E1`.
Aplica igual en la tarjeta si hoy muestra "Abierta" con cierre pasado.

### 2.5 Anterior / Siguiente
"‹ Anterior" y "Siguiente ›" en la cabecera recorren **las tarjetas cargadas en el DOM** de la
lista actual, en su orden (incluidas las traídas con "Ver 20 más"). Sin consultas nuevas para
saber el orden. En los extremos, el botón queda deshabilitado. Atajos ← → solo si el foco no
está en un campo de texto. Cada navegación actualiza `ficha=` con `replaceState`.

### 2.6 Acciones desde el modal
Al guardar, descartar o archivar desde el modal: la tarjeta de detrás se actualiza o se quita
igual que si la acción se hubiera hecho en ella, el anuncio va a `#anuncios` (región
`role="status"` existente) y Deshacer funciona igual. Si la tarjeta desaparece de la lista,
el modal pasa a la siguiente (o se cierra si no hay más).

### 2.7 Presentación (reglas fijas)
Solo Jinja2 + HTMX 1.9 + Bootstrap 5.3 (CDN) + `app/api/static/app.css`. Nada de librerías
nuevas. Tokens existentes de `app.css` (superficies, bordes, texto, marca `#1D4ED8`, familias de
estado con icono + texto, Match ≥60 verde / 40–59 ámbar / <40 gris rotulado "Match"). Texto
terciario ≥14px, objetivos táctiles ≥24px, `tabular-nums` en números, montos `$12.500.000`,
ausente → "No informado". Ningún dato dos veces (cierre, estado, atribución). Estado nunca solo
por color; `aria-pressed` en toggles.

## 3. Descarte en dos botones (tarjeta, modal y página de ficha)
Hoy descartar está ligado al flujo de excluir términos. Pasa a ser opcional:
- **"Solo descartar"**: descarta en un clic, sin abrir panel y **sin tocar ningún perfil**.
  Anuncio + Deshacer como hoy.
- **"Descartar y excluir términos"**: abre el flujo actual de F-ajustes sin cambios (sugeridas,
  vista previa, validación `exclusiones_que_chocan` **antes** de descartar). Cancelar el panel
  no descarta nada.
- Si la oportunidad no tiene match (no hay perfil del cual excluir), solo se muestra
  "Solo descartar".
- Ambos con la misma jerarquía visual (secundarios), etiquetas exactas como arriba, en todos
  los lugares donde hoy existe Descartar (dashboard, registro, explorador si aplica, modal,
  página `/oportunidad/...`). En móvil pueden ir en un menú "Descartar ▾" con las dos opciones.
- Las rutas existentes se reutilizan; si hoy una sola ruta hace las dos cosas, separar por
  parámetro explícito sin romper la API JSON (`PerfilInvalido` → 400 se mantiene).

## 4. Fuera de alcance
- Organismo de licitaciones en NULL (F-organismo-lic): el modal muestra "No informado".
- `refresh_estados` / `sync_activas` que dejan licitaciones "publicada" para siempre: aquí solo
  se corrige lo que se **muestra** (§2.4), no la ingesta.
- Menores anotados: conteos de pestañas sin filtros, pestaña Descartadas cargando todo en
  Python, mensaje de choque que no nombra la keyword.
- Sin migraciones. Si parece necesaria una, detente y repórtalo.

## 5. Tests
- Endpoint parcial: 200 para dueño; 404/403 igual que la página para ajenos; CA sin match en
  solo lectura; la página completa sigue respondiendo y contiene el mismo contenido.
- Pestañas: no se renderiza la que no tiene datos.
- Estado: oportunidad con estado abierto y cierre pasado → "Cierre vencido" (modal y tarjeta).
- Descarte: "Solo descartar" no modifica perfiles ni exclusiones; "Descartar y excluir" mantiene
  la validación previa (choque → nada descartado); sin match → no aparece la opción de excluir.
- Acciones con origen "registro" desde el modal siguen devolviendo vacío + anuncio.
- Tests de red mockeados; Postgres de dev con `postgresql+psycopg://`.

## 6. Cierre
`ruff check .` · `python -m mypy app` · `python -m pytest -rs` completo con `DATABASE_URL`
de dev en `postgresql+psycopg://` → 0 fallos, 0 errores, 0 saltados. Entrada en
`app/changelog.py` (ficha en modal con anterior/siguiente; descarte con o sin excluir
términos). `git add` solo de los archivos tocados (nunca `-A`); commit
`F-ficha-modal: ficha en modal y descarte con exclusión opcional`. **Sin push.** Reporte final:
hash, archivos tocados, resultado de la suite y lista de "Desvío del prompt" si hubo.

*Fuente de los datos de dominio: Dirección ChileCompra.*
