# Prompt F-ca-vocab — rubro en el explorador: confirmados por defecto + palabras sugeridas

> **Destino en el repo:** `docs/prompt-F-ca-vocab.md`
> **Fase:** F-ca-vocab (una fase, un commit) · **Migración:** SÍ (una columna nullable en
> `rubro_vocabulario`) · **Dependencias nuevas:** ninguna
> **Orden:** después de F-ca-explorar (`8afc4af`, en producción, cabeza `a4c9e2b7d1f3`) y **antes**
> de F-guardar. Si `alembic heads` no da `a4c9e2b7d1f3` al empezar, detenerse y avisar.
> **Modelo:** Sonnet. Sin llamadas a la API, sin cuota, sin lock nuevo.
> **Decisión de Boris (01-oct):** opciones **A + B** de la medición del vocabulario. La opción C
> (más detalles de CA por rubro, F-ca-rubro) queda en el roadmap para la próxima versión.

Reglas del proyecto: CLAUDE.md completo, en especial 8 (fuente), 12 (512 MB), 17 (ownership),
18 (CSRF) y queries 100 % parametrizadas. Español de Chile; entrada en `app/changelog.py`;
`ruff check .`, `python -m mypy app`, `python -m pytest` verdes **con `DATABASE_URL` de dev
exportada con prefijo `postgresql+psycopg://`** (sin el prefijo, `test_matching.py` y
`test_models.py` fallan por `psycopg2`); commit "F-ca-vocab: …" sin push; **nunca `git add -A`**;
nada de `ruff format` masivo.

---

## Hechos que condicionan [V: medición en producción del 01-oct]
Scripts de solo lectura: `data/paso0_ca_vocab_real.py` y `data/paso0_ca_vocab_grilla.py`
(salidas en `data/logs/ca_vocab_real.txt` y `ca_vocab_grilla.txt`). Verdad de referencia: 2.948 CA
con detalle (4.704 pares familia–CA); 7.843 CA vigentes.
- El "posible" actual (lift vs nombres de CA de 30 días, lift ≥ 10, k = 10, `tsv`, 1 lexema):
  **recall 32 %, precisión 4 %** (mediana por familia 10 %); marca una mediana de **107 CA
  vigentes por familia** (p90 303). En pantalla, un rubro como 8612 muestra 0 confirmadas y 604
  posibles.
- Grilla de 48 combinaciones (base del lift CA/licitaciones × lift 3/5/10 × k 10/20 × campo
  tsv/nombre × calce 1/2 lexemas): ninguna sirve. Con 1 lexema la precisión no pasa de 7 %; con 2,
  sube a 27 % pero el recall cae a 3–15 %. Ajustar `RUBRO_VOCAB_K`/`RUBRO_VOCAB_LIFT` no lo arregla.
- Conclusión: el vocabulario no sirve para **clasificar** CA solo, pero sí como **sugerencia** para
  que una persona elija palabras. Los lexemas son raíces (`comput`, `constru`): para mostrarlos hace
  falta una palabra legible.
- Código actual (F-ca-explorar): `app/explorador_ca.py` (`FiltrosExplorador.solo_confirmados`,
  `_cond_confirmado`, `_cond_posible`, `_familias_posibles`, `vocabulario_de_prefijos`),
  `app/api/routes/pages.py` (`_EXPLORADOR_ORDEN_PARAMS`, `_armar_filtros_explorador`,
  `_chips_explorador`, `explorador_ca_get`, `explorador_ca_conteo`), plantillas
  `compras_agiles.html` y `_panel_explorador.html`, job `app/catalogos/vocabulario_rubro.py`.

## Qué construir

### A. "Solo confirmados" por defecto; los posibles, opcionales y rotulados
- Con rubros elegidos, por defecto solo pasan las CA **confirmadas** (productos con ese prefijo).
- Reemplazar el parámetro `solo_confirmados` por **`incluir_posibles=1`** (opt-in). En
  `FiltrosExplorador`, `solo_confirmados` pasa a `incluir_posibles: bool = False`. Una URL vieja con
  `solo_confirmados=1` sigue funcionando (es el comportamiento por defecto; el parámetro se ignora).
- Panel: la casilla pasa a **"Incluir posibles (por nombre, poco preciso)"**, desmarcada. Texto de
  ayuda con el dato honesto, por ejemplo: "Posible: el nombre se parece a lo que suele comprarse en
  el rubro. Acierta poco: de cada 10 posibles, 1 o menos es del rubro."
- Chip "Incluye posibles" cuando está activa. Etiqueta en la tarjeta: **"Posible (por nombre):
  <familia>"**.
- Con `incluir_posibles` apagado no se lee el vocabulario para filtrar ni se llama a
  `_familias_posibles`.
- Estado vacío con rubros elegidos y sin confirmadas: "Sin CA confirmadas para estos rubros." + una
  línea que sugiere usar las palabras del rubro (B) o incluir posibles.
- Aviso de vocabulario (nota 3 de la auditoría): "todavía no hay vocabulario… se actualiza cada
  lunes" **solo** si `rubro_vocabulario` está vacía entera. Si tiene filas pero no para los rubros
  elegidos: "Estos rubros no tienen palabras típicas aprendidas (códigos nuevos o sin nombre en
  español)." Y solo se muestra si `incluir_posibles` o las sugerencias (B) lo necesitan.

### B. Palabras del rubro: sugerencias que la persona elige
1. **Palabra legible en el vocabulario.** Migración (reversible, encadena de `a4c9e2b7d1f3`):
   `rubro_vocabulario.palabra` `String(60)` **nullable**. El job `vocabulario-rubros` la llena en la
   misma transacción del reemplazo: para cada `(prefijo, lexema)` elegido, la palabra original
   (minúsculas, CON tildes) más frecuente en los `licitacion_items.nombre` de esa familia cuyo
   lexema sea ese (el mismo que da `to_tsvector('spanish', inmutable_unaccent(palabra))`).
   Desempate: más corta y luego alfabética. Validar `^[a-záéíóúüñ]{3,60}$`; si no hay o no valida,
   NULL (en pantalla se muestra el lexema). Todo en SQL agregado; a Python solo vuelven las filas del
   vocabulario (regla 12). Sin `ts_stat`.
2. **Sugerencias en el panel.** Con rubros elegidos, en la sección de rubro: "Palabras típicas de
   estos rubros", agrupadas por familia (nombre de la familia), hasta 10 por familia y hasta 8
   familias (orden de `df_rubro`). Cada palabra es un botón (`type="button"`) que la agrega a
   **"Tus palabras del rubro"**; las elegidas se ven como chips removibles. Además un campo para
   escribir palabras propias (separadas por coma). Todo en cliente, sin red; HTMX actualiza el conteo.
3. **Nuevo filtro `palabras_rubro`** (lista; máx. 20 palabras; cada una validada
   `^[a-záéíóúüñ]{3,60}$` sin distinguir mayúsculas; el resto se descarta). **Amplía** el rubro, no
   lo restringe:
   `rubro = confirmado OR (tsv calza con alguna palabra elegida) OR (incluir_posibles AND posible)`.
   La condición de palabras va parametrizada:
   `compras_agiles.tsv @@ websearch_to_tsquery('spanish', :q)` con `:q = " or ".join(palabras)`
   (misma semántica que los perfiles, `build_tsquery`). El filtro **Texto** existente no cambia:
   sigue siendo un AND global.
   - Funciona también sin rubros elegidos (solo palabras): entonces el rubro = las palabras.
   - Etiqueta en la tarjeta: **"Por tus palabras"** (gana "Confirmado" si aplica). No hace falta
     decir cuál palabra calzó.
   - Viaja en `_url_explorador` (orden canónico), en chips ("Palabra: x", quitar una) y en el conteo
     en vivo. "Limpiar" las borra.
4. Las palabras elegidas **no se guardan** en esta fase (ni en favoritos ni en perfiles).

### C. Arreglos chicos incluidos
- **Bug de logging** (`app/core/logging.py`, `_SecretFilter.filter`): hoy hace `tuple(record.args)`;
  cuando el único argumento es un dict, `logging` lo guarda como mapping y queda la tupla de sus
  claves → `--- Logging error --- TypeError: not all arguments converted` (pasó el 01-oct con
  `_log.info("vocabulario-rubros: %s", resultado)`). Si `record.args` es dict (mapping), enmascarar
  sus valores string y dejarlo como dict; si es tupla, como hoy.
- Docstring de `app/explorador_ca.py`: reemplazar el "91 % / 7 %" por lo medido el 01-oct (32 % /
  4 %) y la decisión A + B. Docstring de `condicion_ca_vigente`: corregir la referencia al test
  (`tests/test_ca_explorar.py`, no `test_vigencia.py`).
- Filtro de cierre: usar el `ahora` inyectado (convertido a hora de Chile) en vez de
  `datetime.now(TZ_CHILE)`.
- `explorador_ca_get`: no leer el vocabulario dos veces por request.

## Tests (mínimo)
- Rubro elegido sin `incluir_posibles`: solo confirmadas; una CA que solo calza por vocabulario no
  aparece. Con `incluir_posibles=1`: aparece con etiqueta "Posible (por nombre)". URL vieja con
  `solo_confirmados=1` → 200 y mismo resultado que el default.
- `palabras_rubro`: amplía (CA sin productos que calza con la palabra aparece, etiqueta "Por tus
  palabras"); confirmada que además calza muestra "Confirmado"; palabras inválidas (`a&b`, `x`,
  `'; drop`, `%`) se descartan sin error; tope de 20; funciona sin rubros; el Texto sigue siendo AND.
- Conteo en vivo = total de la pantalla con los mismos parámetros (incl. `palabras_rubro`,
  `incluir_posibles`).
- Job: `palabra` con tilde se elige por frecuencia (fixture con "construcción" ×3 y "construccion"
  ×1 → "construcción"); lexema sin palabra válida → NULL; reemplazo sin duplicar; migración
  upgrade/downgrade.
- Avisos: tabla vacía → "todavía…"; tabla con filas pero no del rubro → "no tienen palabras
  típicas…".
- Logging: `_log.info("x: %s", {"a": 1})` no produce "Logging error" y enmascara un secreto que
  esté dentro de un valor del dict.
- Ninguna ruta del explorador llama a la red (respx sin rutas).
- Los tests de Postgres usan tablas temporales para `rubro_vocabulario` como en
  `tests/test_ca_explorar_pg.py` (con la columna nueva), sin dejar nada permanente en dev.

## Fuera de alcance
Detalles de CA por rubro (F-ca-rubro, próxima versión: ver `docs/03-roadmap.md`); guardar las
palabras elegidas; alertas por rubro o por palabras; cambiar `RUBRO_VOCAB_K`/`RUBRO_VOCAB_LIFT`.

## Despliegue (para Boris, después de la auditoría)
Lleva migración: push justo después de un `ca` de los :05 y confirmar en Render
`Running upgrade a4c9e2b7d1f3 -> <nueva>`. Luego correr una vez
`python -m app.ingest run-once --job vocabulario-rubros` contra producción (para llenar `palabra`)
o esperar al lunes (`catalogos.yml`).

*Fuente de los datos de dominio: Dirección ChileCompra.*
