# Prompt F-acentos — las palabras con tilde de los perfiles calzan como sin tilde

> **Destino en el repo:** `docs/prompt-F-acentos.md`
> **Fase:** F-acentos (una fase, un commit) · **Migración:** NO · **Dependencias nuevas:** ninguna
> **Orden:** después de F-ca-vocab (`2b5a810`, en producción, cabeza `b5d1f8a3c6e2`) y **antes** de
> F-guardar. Si `git log -1` no es `2b5a810` (o un commit de docs encima) o `alembic heads` no da
> `b5d1f8a3c6e2`, detenerse y avisar.
> **Modelo:** Sonnet. Sin llamadas a la API, sin cuota, sin lock nuevo, sin cambios de esquema.

Reglas del proyecto: CLAUDE.md completo, en especial las reglas de FTS y queries 100 %
parametrizadas (nada de interpolar keywords en SQL). Español de Chile; entrada en `app/changelog.py`;
`ruff check .`, `python -m mypy app`, `python -m pytest` verdes **con `DATABASE_URL` de dev exportada
con prefijo `postgresql+psycopg://`** (host `ep-dawn-sunset`; sin Postgres los tests nuevos se saltan
y no prueban nada). Commit "F-acentos: …" sin push; **nunca `git add -A`**; nada de `ruff format`
masivo. Si no ves la variable `DATABASE_URL`, no la inventes: deja todo listo, avisa, y Boris corre
`pytest`; tú solo commiteas.

---

## Hechos que condicionan [V]
**El bug (probado en Postgres 16 el 01-oct y en producción con `data/paso0_keywords_tilde.py`).**
`licitaciones.tsv` y `compras_agiles.tsv` se generan con
`to_tsvector('spanish', inmutable_unaccent(nombre) || ' ' || inmutable_unaccent(descripcion))`
(migración `fde568616494`); los ítems/productos y el PAC también se vectorizan con
`inmutable_unaccent`. Pero **la consulta no pasa por unaccent**: el stemmer español da otra raíz
cuando la palabra trae tilde o ñ en ciertos sufijos, y el keyword no calza con su propio texto.
- No calzan hoy: `reparación`→`repar` (doc `reparacion`), `ferretería`→`ferret` (doc `ferreteri`),
  `evaluación`→`evalu` (doc `evaluacion`), `desratización`→`desratiz`, `economía`→`econom` (doc
  `economi`), `señalética`, `computación`, `alimentación`, `papelería`.
- Calzan igual con o sin el arreglo: `construcción`, `camión`, `vehículos`, `actividad física`,
  `selección`, `radiológica`, `eléctrico`.

**Dónde está el bug (revisado contra el código en `2b5a810`):**
- `app/matching/engine.py`, 6 lugares con `websearch_to_tsquery('spanish', :q)` / `:qx` /
  `kw.keyword` sin unaccent:
  - `_FTS_LIC_INCLUDE` (líneas 114–121): 2 veces `:q` (tsv y `licitacion_items`).
  - `_FTS_LIC_EXCLUDE` (122–129): 2 veces `:qx`.
  - `_FTS_CA_INCLUDE` (135–142): 2 veces `:q` (tsv y `ca_productos`).
  - `_FTS_CA_EXCLUDE` (143–150): 2 veces `:qx`.
  - `_HITS_LIC_SQL` (248–281): 3 veces `kw.keyword` (nombre, descripción, ítems).
  - `_HITS_CA_SQL` (283–316): 3 veces `kw.keyword` (nombre, descripción, productos).
- `app/plan_busqueda.py` (líneas 36–38): `_FTS_INCLUDE`, `_FTS_EXCLUDE` y `_TS_RANK`, mismo bug.
  Afecta el Texto libre de la búsqueda inversa del PAC y "Para mis perfiles" (que arma la query con
  `build_tsquery` de los keywords del perfil en `app/api/routes/pages.py:1571`).
- `app/matching/text.py` no toca SQL: solo arma el string `"a OR b"`. **No normalizar en Python**:
  `keywords_hit` se devuelve desde SQL con la escritura original de la persona (`kw.keyword`), y eso
  es lo que se muestra en las razones del match; tiene que seguir así.
- El explorador ya está bien: `_tsquery_spanish` en `app/explorador_ca.py` (F-ca-vocab). Es el
  patrón a copiar.
- `app/catalogos/vocabulario_rubro.py` y `_cond_posible` del explorador usan
  `to_tsquery('simple', …)` sobre lexemas ya stemizados: **no se tocan**.
- Invariante recall/score (F9c): el recall (`_candidatos_*`) y `keywords_hit` (`_HITS_*`) deben usar
  **la misma expresión de tsquery**. Si se arregla uno y no el otro, aparecen matches con
  `keywords_hit = []` y score de texto 0.

**Medición en producción (Paso 0, 01-oct; últimos 30 días; solo `tsv`, sin ítems/productos):**
5 perfiles activos, 53 términos, 8 con tilde/ñ, **3 cambian de raíz**, en 2 perfiles (ambos de boris):
| Perfil | Término | CA actual → corregida |
|---|---|---|
| 6 «Personal» | incluye `Evaluación` | 159 → 187 |
| 6 «Personal» | incluye `Economía` | **92 → 14** (hoy calza por accidente con `económico/a`, raíz `econom`) |
| 5 «perfil vejez» | excluye `desratización` | 2 → 65 (63 CA con desratización que hoy no se filtran) |
Licitaciones dieron 0/0 en todos los términos: es un artefacto del script (filtra por
`fecha_publicacion`, que suele venir NULL en licitaciones) [INFERIDO], no una medición. El arreglo
es el mismo para ambas fuentes.

**Consecuencia esperada y deseada:** cada keyword calza con su propia familia de palabras, con o sin
tilde. Efecto secundario conocido: `Economía` deja de traer `económico/a`. No se compensa en código;
si Boris lo quiere, agrega `económico` como keyword al perfil 6 (calza bien con el arreglo).

---

## Qué construir

### A. Una sola expresión de tsquery en el matching
En `app/matching/engine.py`, definir arriba de los fragmentos FTS una función privada que devuelva el
**fragmento SQL** (string fijo, sin datos):
```python
def _tsq(param: str) -> str:
    """tsquery 'spanish' con unaccent, igual que el tsv (si no, 'reparación'→repar vs reparacion).
    `param` es un nombre de bindparam o columna fijos del código (':q', ':qx', 'kw.keyword'),
    nunca un dato de la persona."""
    return f"websearch_to_tsquery('spanish', inmutable_unaccent({param}))"
```
y reescribir los 6 fragmentos con ella (`_tsq(':q')`, `_tsq(':qx')`, `_tsq('kw.keyword')`), de modo
que **no quede ningún** `websearch_to_tsquery('spanish', :q)`, `…, :qx)` ni `…, kw.keyword)` sin
unaccent en el archivo. Los keywords siguen yendo solo como bindparams (`:q`, `:qx`, `:keywords`).
Actualizar el docstring del módulo (párrafo del invariante F9c: "misma tsquery con unaccent").

### B. Plan Anual
En `app/plan_busqueda.py`, lo mismo para `_FTS_INCLUDE`, `_FTS_EXCLUDE` y `_TS_RANK` (una función
local igual o importar la de engine si no crea un ciclo; preferir una local de 2 líneas). **No tocar**
`_TSV_DESCRIPCION` (tiene que calzar con el índice GIN de `d7f2a4c8b6e1`). Actualizar el docstring
del módulo: ya no es cierto que `app/matching/*` "no se toca" ni que use "el mismo motor" sin
matices; dejar una línea que diga que ambos usan `websearch_to_tsquery('spanish', inmutable_unaccent(:q))`.

### C. Docstrings de `app/matching/text.py`
Donde dice `websearch_to_tsquery('spanish', :q)`, decir `websearch_to_tsquery('spanish',
inmutable_unaccent(:q))` y agregar: "no se normalizan tildes acá: lo hace Postgres, para que
`keywords_hit` conserve la escritura de la persona". Sin cambios de lógica en este archivo.

### D. Changelog
Entrada nueva al inicio de `CHANGELOG` en `app/changelog.py`, fecha del día del commit:
- **Título:** "Tus perfiles ahora encuentran bien las palabras con tilde"
- **Descripción (aprox.):** "Algunas palabras con tilde de los perfiles, como 'reparación',
  'ferretería' o 'evaluación', no encontraban las publicaciones que las usan. Ya está corregido, tanto
  en lo que buscas como en lo que excluyes, y también en la búsqueda del Plan Anual. Puede que veas
  oportunidades nuevas en el próximo resumen. Cada palabra busca su propia familia: 'economía' trae
  'economía' pero no 'económico'; si quieres las dos, agrega ambas a tu perfil. Fuente: Dirección
  ChileCompra."

---

## Tests (mínimo)
Archivo nuevo **`tests/test_matching_acentos.py`**. No agregar filas a
`tests/fixtures/dataset_matching.py` (otros tests cuentan matches y el orden de ese dataset).

**Sin Postgres (siempre corren):**
1. Guardia de regresión: ninguno de `_FTS_LIC_INCLUDE`, `_FTS_LIC_EXCLUDE`, `_FTS_CA_INCLUDE`,
   `_FTS_CA_EXCLUDE`, `_HITS_LIC_SQL.text`, `_HITS_CA_SQL.text`, ni `_FTS_INCLUDE`, `_FTS_EXCLUDE`,
   `_TS_RANK` de `app.plan_busqueda` contiene `websearch_to_tsquery('spanish', :` ni
   `websearch_to_tsquery('spanish', kw.` (con espacios normalizados), y todos contienen
   `inmutable_unaccent(`.

**Con Postgres (`@needs_postgres`, mismo patrón que `tests/test_matching.py`):** fixture propia que
crea un usuario, perfiles y oportunidades con códigos prefijados `ACENTOS-` (licitación
`PUBLICADA` con `fecha_cierre` futura respecto de `AHORA`, CA `PUBLICADA`), y limpia **antes y
después** por código/email (autorreparable, como `ds` en `test_matching.py`).
2. Incluye con tilde, en nombre: perfil `["reparación"]` matchea la licitación "Servicio de reparación
   de bombas" y la CA "Reparación de techumbre"; en ambos `razones["keywords_hit"] == ["reparación"]`
   (escritura original) y `campo_hit == "nombre"`.
3. Incluye con tilde, solo en producto: perfil `["ferretería"]` matchea una licitación cuyo nombre no
   la menciona pero con un ítem "Artículos de ferretería", y una CA con un `ca_productos` "Insumos de
   ferretería"; `campo_hit == "producto"`.
4. Excluye con tilde: perfil `["materiales"]` excluir `["ferretería"]`: "Materiales de ferretería" NO
   queda; "Materiales de oficina" sí. Lo mismo para CA.
5. Sin tilde ↔ con tilde: perfil `["reparacion"]` (sin tilde) también matchea "Servicio de reparación
   de bombas", y perfil `["reparación"]` matchea un texto escrito "reparacion".
6. Regresión: perfil `["construcción"]` matchea "Construcción de sede social" (calzaba antes y debe
   seguir calzando).
7. Invariante F9c: para el perfil del test 2, todo match creado tiene `keywords_hit` no vacío.
8. Plan Anual: si `tests/test_plan_busqueda.py` ya tiene fixture Postgres con líneas del PAC,
   agregar ahí un caso `q_include="reparación"` que encuentre una línea "Reparación de vehículos" y
   uno `q_exclude="ferretería"` que la saque. Si armar la fixture cuesta más de 30 líneas, dejar solo
   el test 1 para el PAC y avisarlo en el resumen.

Los tests existentes (`test_matching.py`, `test_detalles_match.py`, `test_plan_busqueda*.py`,
`test_ca_explorar_pg.py`) deben seguir verdes sin cambios.

---

## Fuera de alcance
- Borrar matches viejos que ya no calzan (p. ej. las CA con "desratización" que ya matchearon el
  perfil 5): el matching no borra matches; lo arregla **F-guardar**.
- Normalizar o reescribir los keywords guardados en `perfiles_busqueda`.
- Compensar `economía`/`económico` en código, sinónimos o prefijos.
- Tocar `tsv`, índices, migraciones, `vocabulario_rubro.py` o el "posible" del explorador.
- Corregir el `0/0` de licitaciones del script de Paso 0.

## Despliegue (para Boris, después de la auditoría)
Sin migración: el push se puede hacer a cualquier hora (no hace falta esperar un `ca`). Render
redeploya la web (afecta el Plan Anual); los jobs de Actions usan el código nuevo desde el siguiente
disparo. El matching recorre todas las oportunidades vigentes en cada corrida, así que el próximo
`ciclo-match` (o uno disparado a mano) re-matchea los perfiles afectados sin pasos extra. Las
oportunidades que entren por primera vez tendrán `fecha_match` nueva y saldrán en el resumen de las
08:30 del día siguiente.
