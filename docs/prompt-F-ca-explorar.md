# Prompt F-ca-explorar — explorador de Compras Ágiles con filtros y rubros favoritos

> **Destino en el repo:** `docs/prompt-F-ca-explorar.md`
> **Fase:** F-ca-explorar (una fase, un commit) · **Migración:** SÍ (2 tablas chicas) ·
> **Dependencias nuevas:** ninguna
> **Orden:** después de F-plan-busqueda y F-vigencia (usa `es_vigente` y su regla SQL). Va
> **antes** de F-ca-rubro, que queda para después y reutiliza el vocabulario de esta fase. Si
> F-plan-busqueda o F-vigencia no tienen commit, detenerse y avisar.
> **Modelo:** Sonnet (`/model sonnet`). Sin llamadas a la API ni cambios de cuota.
> **Decisión de Boris (26-sep):** el match automático de CA por rubro choca con el cuello de
> botella de los detalles. En esta versión se resuelve con un **explorador**: el usuario filtra
> todas las CA vigentes (por rubro, región, monto, cierre, texto) y marca **rubros favoritos**.

Reglas del proyecto: CLAUDE.md completo, en especial 8 (fuente), 10, 11, 12 (512 MB), 17
(ownership), 18 (CSRF) y queries 100 % parametrizadas. Español de Chile; entrada en
`app/changelog.py`; `ruff check .`, `python -m mypy app`, `python -m pytest` verdes (con
`DATABASE_URL` de dev exportada, para que los tests de Postgres no queden skipped); commit
"F-ca-explorar: …" sin push; **nunca `git add -A`**; nada de `ruff format` masivo.

---

## Hechos que condicionan [V: código y Paso 0 del 26-sep]
- ~15.200 CA abiertas; solo ~460 tienen detalle (y productos). El rubro de una CA solo se conoce
  con el detalle: un filtro por rubro "exacto" dejaría la pantalla casi vacía. Por eso hay dos
  niveles: **confirmado** (productos con ese prefijo UNSPSC) y **posible** (el nombre calza con
  el vocabulario típico del rubro).
- `compras_agiles.tsv` = `to_tsvector('spanish', unaccent(nombre) || ' ' || unaccent(descripcion))`,
  GENERATED y con índice GIN `ix_compras_agiles_tsv`.
- Vocabulario por rubro (Paso 0, `data/paso0_ca_vocabulario.py`): aprendido de
  `licitacion_items` (114.191 filas con UNSPSC y nombre); 10 términos por prefijo y lift ≥ 10
  encontraron el 91 % de las CA que sí calzan, con 7 % de precisión. En una pantalla para
  recorrer, el ruido se tolera mejor que en una alerta, siempre que se rotule "posible".
- Catálogo de rubros: `app/catalogos/unspsc.py` (segmentos y familias, `data/unspsc_rubros.csv`)
  y el widget `rubros_widget` de `perfiles.html` (buscador + acordeón). Reusarlo.
- Cabeza de Alembic al escribir este prompt: `d7f2a4c8b6e1` (F-plan-busqueda). La migración nueva
  cuelga de la cabeza vigente al empezar; `alembic heads` = una sola antes del commit.
- 11 de los 30 prefijos del perfil de prueba no aparecen en ningún ítem (códigos UNSPSC nuevos, sin
  nombre en español): sin vocabulario, solo pueden salir como "confirmado".

## Qué construir

### 1. Modelo (una migración, reversible)
- `rubros_favoritos(id, owner_id → usuarios ON DELETE CASCADE, prefijo, creado_en)`,
  único `(owner_id, prefijo)`. Prefijo validado `^\d{2,8}$`.
- `rubro_vocabulario(prefijo, lexema, df_rubro, lift, actualizado_en)`, PK `(prefijo, lexema)`.
  Datos derivados: se regeneran enteros.

### 2. Vocabulario (`app/catalogos/vocabulario_rubro.py` + job semanal)
- Por familia UNSPSC (4 dígitos; un segmento = unión de sus familias): lexemas de
  `to_tsvector('spanish', inmutable_unaccent(li.nombre))` vía `unnest(tsvector)` + `GROUP BY`
  (sin `ts_stat`, que no admite parámetros). `df_rubro ≥ 3`; lift contra la frecuencia en
  **nombres de CA de los últimos 30 días**; top `RUBRO_VOCAB_K` (default 10) con
  lift ≥ `RUBRO_VOCAB_LIFT` (default 10). Lexemas validados `^[a-zñ]+$`.
- Job `vocabulario-rubros` en `_JOBS`, con el lock único, agregado a `catalogos.yml` (lunes 06:35)
  como un job más, sin cron nuevo. Reemplazo en una transacción (sin ventana vacía). Idempotente.
- Settings nuevos opcionales en `_job.yml`, con default en `settings.py`.
- `/salud`: filas del vocabulario, familias con vocabulario, fecha de actualización.

### 3. Pantalla `/compras-agiles` ("Explorar Compras Ágiles", en la barra de navegación)
- **Universo:** CA vigentes según la regla de F-vigencia (incluida la de 7 días para CA sin
  cierre), menos las que el usuario descartó. Todo desde la base: la pantalla **nunca** llama a la
  API.
- **Filtros** (panel lateral con el mismo estilo de `_panel_filtros.html`, chips removibles):
  - **Rubro:** selector con `rubros_widget`; al entrar, preseleccionados los **favoritos** del
    usuario. Casilla "Solo confirmados" (por defecto apagada). Con rubros elegidos, una CA pasa si
    tiene productos con el prefijo (**confirmado**) **o** su `tsv` calza con
    `to_tsquery('simple', :q)` armada con los lexemas del vocabulario (**posible**).
  - Región (múltiple), monto mín./máx. (incluir "sin monto" por defecto), cierre (hoy / 3 días /
    semana), texto libre (misma semántica que los perfiles, `websearch_to_tsquery('spanish', …)`),
    organismo (texto sobre `organismo_nombre`).
- **Orden:** cierre más próximo (default), monto mayor, publicación más reciente.
- **Resultado:** tarjeta compacta (nombre, organismo, región, monto, cierre, etiqueta
  **Confirmado** / **Posible: <familia>**), enlace a la ficha existente (las acciones de guardar
  o descartar viven en la ficha; no duplicarlas aquí). Conteo total arriba ("1.234 CA").
- **Paginación en SQL** (`LIMIT/OFFSET` + `count(*)` aparte, 50 por página): prohibido cargar el
  universo en Python (regla 12).
- **Favoritos:** estrella junto a cada rubro elegido y en `/perfiles` (sección nueva y chica,
  "Rubros favoritos", fuera del formulario de perfiles para no chocar con su rediseño). POST con
  CSRF; solo el dueño (regla 17).
- **Conteo en vivo** al cambiar filtros (HTMX, igual que `/plan-anual/conteo`), solo números.
- "Fuente: Dirección ChileCompra" una vez. Estado vacío honesto: "Sin CA vigentes con estos
  filtros."

## Tests (mínimo)
- Vocabulario: con ítems fixture, términos comunes en nombres de CA quedan fuera por lift; la
  familia `8512` no captura ítems `5812…`; reemplazo sin duplicar; lexema raro descartado; lock.
- Filtros: confirmado vs posible; "Solo confirmados"; combinación rubro + región + monto + cierre;
  CA vencida y CA sin cierre publicada hace 8 días no aparecen; descartadas del usuario no
  aparecen, las de otro usuario sí; texto con comillas y `%` parametrizado.
- Paginación: `count` coincide con la suma de páginas; página fuera de rango vacía sin error.
- Favoritos: agregar/quitar, único por usuario, sin CSRF → 403, otro usuario no los ve ni edita;
  al entrar a la pantalla vienen preseleccionados.
- Ninguna ruta del explorador llama a la red (respx sin rutas: cualquier request falla el test).

## Fuera de alcance
Detalles por rubro en la ingesta (queda en `docs/prompt-F-ca-rubro.md`: cuando se retome, sus
favoritos priorizan la cola nocturna y lee `rubro_vocabulario` en vez de calcularlo); alertas por
rubros favoritos; licitaciones en el explorador.

*Fuente de los datos de dominio: Dirección ChileCompra.*
