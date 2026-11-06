# Prompt — F-plat-lectura (juez corregido, OCR basura como imagen, volver a leer y elección del modelo) · 6-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit, **sin push**. Sin migración salvo que §3 la pida
> (`down_revision` = cabeza vigente). [V] verificado, [I] inferido. Va **antes o en paralelo** con
> `prompt-F-plat-anexos-auto.md`: esta fase deja lista la LECTURA; aquella, la DESCARGA. Las dos tocan
> `app/mercado/anexos.py`: si van en paralelo, la segunda rebasa sobre la primera.

## Por qué (medición del 6-nov, `claude/12-medicion-anexos-cowork-y-juez.md` del proyecto) [V]
- Claude leyó los 29 anexos únicos del set (555 líneas). Con el juez actual: 84,1 %. Sin contar la unidad (J1): 98,0 %.
  Solo cantidad (J2): 99,1 %. De 88 fallas, **1 era un error real de lectura**; el resto es el juez:
  58 de unidad por convención (botella/caja/tarro/bolsa/paquete vs «unidad»; «kg» vs «unidad» en «Azúcar 1 kg»),
  26 de texto («Manjar 1k» vs «Manjar 1 kg», «300cc» vs «300 cc», «1kg» vs «1 kg», plural/singular, orden) y
  4 de conversión (750 g vs 0,75 kg).
- `IMG_20260811_0003.pdf` e `IMG_20260831_0001.pdf` (93 líneas, 17 % del set) son escaneos con una capa de OCR basura
  («5 1 0 5 U U n n i i d d a a d d e e s s…»); `extraer_texto` los trata como PDF con texto y el modelo nunca ve la imagen.
- La caché del 5-nov se perdió (dev en `a7c3e9d1b4f2`, sin `llm_cache`; prod con la tabla vacía): la evaluación se re-corre.

## 1. Juez corregido (`scripts/mercado/evaluar_extraccion.py`)
- Normalizar antes de comparar textos: separar números pegados a unidades («1kg» → «1 kg», «300cc» → «300 ml», «1k» →
  «1 kg», «1lt» → «1 L»), singular/plural simple (quitar -s/-es finales de palabras ≥ 4 letras) y quitar «c/u».
- Cantidad: igual si coincide tal cual **o** tras convertir g↔kg y ml↔L cuando las unidades son de esas familias.
- Unidad: clase «envase/conteo» = {unidad, u, botella, caja, cajita, tarro, bolsa, bolsita, paquete, pack, frasco, sobre,
  manga, rollo, bandeja, set, pote, saco, lata}; dentro de la clase cuentan como iguales. kg/g y L/ml por conversión.
  Además: «kg» esperado vs «unidad» extraída cuenta como acierto si el texto trae el formato «1 kg» (y al revés).
- El informe muestra **juez estricto (el de hoy) y juez corregido**; el criterio (≥ 95 % total, ≥ 90 % escaneados, el más
  barato) se aplica al **corregido**. Agregar una fila de referencia fija «claude-cowork (6-nov, manual)» con 98,0 %.
- `emparejar_indices` la usa también `calibrar_auto_revision.py`: que use el juez corregido (anotarlo en el docstring).
- Commitear también `scripts/mercado/diagnosticar_extraccion.py`, `donde_esta_llm_cache.py` y `volumen_anexos.py`
  (ya existen, sin commit), con el juez corregido en el diagnóstico.

## 2. Capa de OCR basura → página como imagen (`app/mercado/anexos.py`, `extraer_texto`)
- Función pura `texto_es_basura(texto) -> bool`: por ejemplo, proporción de tokens de 1 letra > 0,25, o proporción de
  palabras ≥ 3 letras sin vocal o con símbolos mezclados (`]`, `!`, `¡`, `|` dentro de la palabra) > 0,2. Calibrarla con los
  PDF del set: los dos `IMG_*` deben dar basura y los 6 PDF con texto bueno (Cueca, Caja ×2, OMIL, MAIS, TT.RR) no.
- Si la capa es basura: tratar el PDF como escaneado (render a imagen, mismo tope de páginas) y marcar `escaneado=True`.
- Tests con textos sintéticos (sin los anexos reales en el repo: `data/` está fuera de git).

## 3. Volver a leer
- Hoy un archivo repetido sale «Ya existía» aunque su lectura haya quedado `sin_presupuesto` o `error`, y el binario no se
  guarda: no hay forma de releer. Cambio: si el anexo existente está en `sin_presupuesto` o `error`, **volver a subir el
  mismo archivo lo procesa de nuevo** (misma fila de `ca_anexos`, sin duplicar; líneas nuevas `dudosa` o auto según
  F-plat-auto-revision). Vale para la ficha, la carga masiva, `POST /api/anexos/{codigo}` y la descarga automática.
- En la ficha de la CA, junto a un anexo «Manual (sin lectura automática)» o «Error»: texto «Vuelve a subir el archivo
  para leerlo» (y, cuando exista anexos-auto, botón «Volver a bajar y leer» que encola la CA para el job).

## 4. Tests y cierre
Juez: casos de cada clase de falla de la medición (con textos sintéticos). `texto_es_basura`. Releer: duplicado `hecha`
→ «Ya existía»; duplicado `sin_presupuesto` → se procesa. `ruff check .`, `python -m mypy app`, `python -m pytest -rs`.
Entrada en `app/changelog.py`. Anotar en `docs/decisiones.md`: juez corregido y por qué; OCR basura; elección del modelo
pendiente de §5. Commit: `F-plat-lectura: juez corregido, OCR basura como imagen y volver a leer`.

## 5. Pasos de Boris (después del commit)
1. Poner dev al día:
   `.venv\Scripts\python -c "from dotenv import load_dotenv; load_dotenv(); from alembic.config import main; main(['upgrade','head'])"`
2. Evaluar (~US$0,10; queda en caché de dev):
   `.venv\Scripts\python scripts\mercado\evaluar_extraccion.py --modelos google/gemma-4-26b-a4b-it,anthropic/claude-haiku-5.5,z-ai/glm-5.3-flash,qwen/qwen3.5-9b`
   y el diagnóstico con los mismos modelos.
3. Elegir: el más barato que cumpla con el juez corregido como `LLM_MODELO_EXTRACCION`; el segundo como
   `LLM_MODELO_RESPALDO`. Si ninguno cumple, el que más se acerque como provisional (todo queda «dudosa» mientras la
   auto-revisión esté apagada) y revisar el diagnóstico antes de probar modelos más caros.
4. Cargar las dos variables en `.env`, Render (la subida manual lee en la web) y GitHub Secrets (anexos-auto lee en
   Actions). Anotar el resultado en `docs/decisiones.md`.
5. Volver a subir los 2 anexos de la prueba del 6-nov (notebook y CECOSF Valle Hondo) para verificar que se leen.

*Fuente de los datos de dominio: Dirección ChileCompra.*
