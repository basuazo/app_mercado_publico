# 17 — Plan del pivote: plataforma de intermediación de Compra Ágil (Ñuble)

*3-nov-2026 · Boris con Claude (Cowork). Sucede y amplía a `16-pivote-compra-agil-nuble.md` (que sigue
siendo la fuente del diagnóstico de mercado). Convención: [V] verificado en la fuente · [I] inferido.
Nada de esto entra a `decisiones.md` hasta que Boris lo apruebe.*

## 0. En una página

**Qué se construye.** Una plataforma de administración para **intermediar y ganar Compras Ágiles (CA)**,
solo CA, con alimentos como giro principal y otros rubros como expansión. Cuatro capacidades, en este orden
de dependencia:

1. **Ver las oportunidades producto a producto.** Hoy una CA de alimentos suele ser "1 unidad, según anexo
   adjunto" ($300.000 de "abarrotes en documento adjunto"). Sin el desglose no hay precio ni margen. La
   plataforma guarda **líneas 1:1** (producto canónico × cantidad × formato) por CA, las obtiene de la API
   cuando vienen explícitas, del **anexo** cuando no, y deja **marcar y corregir** las CA históricas sin
   detalle desde la misma pantalla.
2. **Centralizar proveedores y costos.** Proveedor con local (comuna, dirección), contacto, productos que
   vende, precio por formato y **variación histórica**. Privado por empresa; se carga a mano, por planilla o
   desde una foto/PDF de lista de precios.
3. **Decidir con probabilidad de adjudicación.** Para cada CA vigente: precio ganador esperado por línea,
   costo, logística, margen y **P(ganar | precio ofertado)**, calibrada con las cotizaciones históricas de
   Ñuble (todas las ofertas, no solo la ganadora). Se predice **antes** del cierre y se juzga con la
   adjudicación real.
4. **Pronosticar demanda y recomendar abastecimiento.** Qué compra cada unidad compradora, con qué
   recurrencia y estacionalidad, para negociar precio firme con proveedores y preparar stock.

**Lo que ya existe y se reutiliza [V].** Login multiusuario, Postgres en Neon, cliente v2 con parser de
`proveedores_cotizando` / `productos_cotizados` (F-merc-0b), cuota y pausa de 429 persistidas,
`scripts/backfill_cot_nuble.py` (cotizaciones de ago-2026 ya bajadas: 2.577 CA, 49.693 filas de oferta
× producto), observatorio con 24 meses de `oc-da` y `COT_`, y los prompts `prompt-F-merc-1.md` y
`prompt-F-merc-2.md` ya revisados.

**Lo que ordena el plan.** El cuello de botella no es la ingesta: es **saber qué producto es** (error de
55 % en precio con UNSPSC; "producto heterogéneo" en 201 de 245 códigos con n ≥ 5 [V,
`bandas_precio.md`]). Por eso el catálogo canónico y las líneas 1:1 van antes que cualquier modelo.

**Decisiones de Boris (3-nov):** congelar la app actual y construir el módulo nuevo al lado (mismo login y
base); anexos: sonda a la API primero y, si no hay descarga, carga manual con señalización y corrección en
la plataforma; modelo de lenguaje vía **OpenRouter** con modelos baratos; no hay datos estructurados
propios todavía (se parte de cero en proveedores y costos); **la cuota de la API se dedica entera a Compra
Ágil** (la ingesta de licitaciones queda apagada; no se reactiva en ningún job del pivote); **la ingesta se
acota a los territorios priorizados por cada empresa** (regiones y comunas), y el admin crea empresas y les
agrega usuarios.

**Reglas nuevas del pivote (propuestas para `CLAUDE.md` cuando Boris apruebe):**
- R-p1. Cuota de la API = solo Compra Ágil. Ningún job del pivote pide licitaciones ni Plan Anual con ticket.
- R-p2. El territorio que se ingesta es la unión de las regiones priorizadas de las empresas activas (hoy:
  Ñuble, 16). Las comunas priorizadas ordenan la mesa y la logística, no la ingesta (la API filtra por región).
- R-p3. Todo dato comercial (proveedores, costos, ofertas, predicciones, pronósticos) pertenece a una empresa;
  un usuario ve solo lo de su empresa. El admin crea empresas y asigna usuarios.

## 1. Diagnóstico de los datos reales de agosto 2026 (API, Ñuble) [V]

Fuente: `data/mercado/cot_api_2026-08.csv.gz` (F-merc-0b) y `data/mercado/informe/bandas_precio.md`.

| Dato | Valor | Qué implica |
|---|---|---|
| CA con ofertas | 2.577; 501 multiproducto (19 %) | La mayoría pide un código UNSPSC; el desglose real va en el anexo |
| Filas oferta × producto | 49.693; `PrecioUnitario × CantidadCotizada = MontoTotalProducto` en 100 % | El precio unitario de la API es confiable |
| CA de alimentos (seg. 50) | 182; 2.903 filas | ~6 CA de alimentos por día hábil en Ñuble |
| Texto típico de alimentos | "ALIMENTOS SEGÚN ANEXO ADJUNTO N°1", "LISTADO DE ABARROTES EN DOCUMENTO ADJUNTO", "Pack de alimentos según detalle en anexo" con cantidad 1 | **El problema 1:1 es la norma, no la excepción** |
| CA de un producto con ≥ 2 ofertas: gana el más barato | 59 % (ago-2026 API) vs. 78–81 % (`COT_`, meses previos) | El precio no lo es todo: el 41 % restante es lo que hay que modelar (comprador, plazo, local, "o similar") [I: parte puede ser diferencia de muestra] |
| Brecha mediana ganador – segundo | 2,7 % | Margen de maniobra chico; la logística define si conviene |
| Ruido intra-CA (mismo producto, cantidad, comuna) | 17,7 % de desviación mediana | Piso de cualquier banda de precio |
| Productos "usables" sin normalizar | 44 de 245 | Sin catálogo canónico no hay precio de referencia |

Nota: en el CSV de la API **no viene `unidad_medida`** ni el formato; un mismo código mezcla "unidad" con
"paquete de 20". El catálogo canónico resuelve eso con `formato` + `unidad_base`.

## 2. Componentes

### C1. Datos de mercado (en marcha)
- **F-merc-1** (`prompt-F-merc-1.md`, corregido 31-oct: una sola key `MP_TICKET`): cotizaciones de CA de
  Ñuble a Neon (`ca_cotizaciones`, `ca_cotizacion_productos`), job `mercado-ca` cada 15 min y backfill
  nocturno. **Ajuste al prompt** (sonda 31-oct [V]): no existe `seleccion`; el ganador es
  `proveedor_seleccionado` al nivel de la oferta y el estado es `estado`. El parser de F-merc-0b ya lo hace.
- **F-merc-2** (`prompt-F-merc-2.md`): líneas de OC de CA de Ñuble desde `oc-da` (`oc_lineas_ca`) y
  `precio_referencia` nacional por UNSPSC × base.
- Lo que aportan al resto: por cada CA cerrada, **todas** las ofertas con precio por producto y quién ganó.
  Es la base empírica de P(ganar).

### C2. Catálogo canónico y líneas 1:1 (nuevo, F-plat-1)
- `producto_canonico`: `id`, `nombre` ("Leche entera líquida"), `familia` (del observatorio), `unspsc_sugerido`,
  `unidad_base` (`kg|L|u`), `activo`.
- `producto_formato`: `producto_id`, `formato` ("caja 1 L", "saco 25 kg", "pack 6 u"), `factor_base`
  (1,0 L; 25 kg; 6 u), `ean` opcional.
- `producto_alias`: `texto_normalizado` → `producto_id` + `formato_id`, con `fuente` (`regla|llm|humano`),
  `confianza`, `revisado_por`, `revisado_en`. Es el diccionario que aprende: cada corrección humana es un
  alias nuevo.
- `ca_linea`: la unidad de trabajo de toda la plataforma. `ca_codigo`, `n`, `producto_id`, `formato_id`,
  `cantidad`, `texto_origen`, `origen` (`api|anexo|manual`), `estado` (`ok|dudosa|pendiente`),
  `revisado_por`. Una CA de la API con un producto explícito ("PLÁTANOS, 60 u") genera una línea `api|ok`;
  una CA "según anexo" genera una línea `pendiente` hasta que se cargue el anexo.
- **Bandera "CA sin detalle"** (pedido de Boris): en la ficha y en la mesa, con filtro y conteo; al corregir
  queda registrado quién y cuándo.
- Normalización: primero reglas (las de `scripts/mercado/mercado_observatorio.py: normalizar`, movidas a
  `app/catalogos/mercado.py` en F-merc-2), después el modelo de lenguaje para los textos que las reglas no
  resuelven (en lote, por texto único, con caché), y siempre revisión humana para los 200 productos de mayor
  monto.

### C3. Anexos → líneas (nuevo, F-plat-2)
- **Paso 0, sonda humana** (regla 20: no afirmar el negativo sin ir a la fuente): la guía v3.0 documenta
  `documentos[].{id, nombre}` en el detalle y **no documenta** un endpoint de descarga [V en la guía; que
  no exista en el servidor es [I]]. La sonda prueba, con una CA real y el ticket, las rutas plausibles
  (`/v2/compra-agil/{codigo}/documentos/{id}` y variantes que aparezcan en el bundle JS público del
  buscador, método de `docs/04-datos-abiertos.md` §0) y anota el resultado en
  `01-analisis-api-mercado-publico.md`. Si existe descarga, el job la usa; si no, flujo manual.
- **Flujo manual**: el usuario abre la ficha pública (`buscador.mercadopublico.cl/compra-agil`), baja el
  anexo y lo **sube** en la ficha de la CA (PDF, XLSX, DOCX, imagen; tope 10 MB). Sin scraping (regla 9).
- **Extracción**: el archivo se convierte a texto (pdfplumber / openpyxl / python-docx; imagen → modelo
  con visión) y un modelo vía **OpenRouter** devuelve JSON con `[{texto, cantidad, unidad, formato}]`.
  Cada línea pasa por el catálogo (C2) y queda `dudosa` hasta que un humano la confirme en una tabla
  editable (HTMX). El archivo se guarda en Neon como bytes solo si es < 2 MB; si no, solo el texto
  extraído y un hash (regla 11).
- **Presupuesto**: `LLM_PRESUPUESTO_MES_USD` (default 5), contador persistido en Postgres (regla 10),
  caché por `sha256(texto)`; sin presupuesto el flujo sigue en modo manual (el usuario tipea las líneas).
- **Dependencias nuevas** (se proponen y justifican en el prompt, regla del `CLAUDE.md`): `httpx` ya está
  (OpenRouter es REST; no hace falta SDK), `pdfplumber`, `openpyxl`, `python-docx`.
- **Secretos**: `OPENROUTER_API_KEY` solo en variable de entorno, enmascarada en el logger (regla 1).
- **Elección del modelo de extracción**: no se fija en el código. Variable `LLM_MODELO_EXTRACCION` y un
  **set de evaluación de 30 anexos reales** con sus líneas correctas (`tests/fixtures/anexos/`, sin datos
  personales) que mide exactitud por línea y costo. Candidatos baratos en OpenRouter a probar [I: precios y
  disponibilidad cambian; verificar en openrouter.ai/models el día de la fase]: una variante *flash/mini*
  de Google o OpenAI, Claude Haiku, y un modelo abierto (Qwen o DeepSeek). Regla: el más barato que pase
  **≥ 95 % de líneas correctas** (producto, cantidad y unidad) en el set.

### C4. Proveedores y costos (nuevo, F-plat-3)
- `empresa` (la que intermedia; 1 por ahora, preparada para varias): `nombre`, `rut`, `comuna_base`,
  `regiones_prioritarias` (lista de códigos; default `[16]`), `comunas_prioritarias` (lista), `activa`.
  `usuarios.empresa_id` (nullable: los usuarios de la app antigua siguen sin empresa). Admin: `/admin/empresas`
  (crear, editar territorio, asignar/quitar usuarios). Ownership verificado en servidor (regla 17).
  **La ingesta lee el territorio desde acá**: `mercado_regiones` deja de ser un setting fijo y pasa a ser
  `SELECT DISTINCT unnest(regiones_prioritarias) FROM empresas WHERE activa` (con fallback `[16]` si está
  vacío). Esto entra en F-plat-3, que se adelanta en el orden (ver §4).
- `proveedor`: `empresa_id`, `nombre`, `rut`, `comuna`, `direccion`, `lat/lon` opcional, `contacto_nombre`,
  `telefono`, `email`, `rubros`, `condiciones` (plazo de pago, mínimo de compra, despacha sí/no), `notas`.
- `proveedor_producto`: `proveedor_id`, `producto_id`, `formato_id`, `precio_neto_actual`, `vigente_desde`,
  `fuente` (`manual|planilla|lista_llm`), `disponible`.
- `costo_historial` (append-only): cada cambio de precio con fecha y fuente → **variación histórica** por
  proveedor × producto, gráfico en la ficha del proveedor y "mejor costo hoy" por producto.
- Carga: formulario, importación CSV/XLSX con plantilla descargable, y **lista de precios por foto/PDF**
  reutilizando el extractor de C3.
- `tarifa_logistica`: `empresa_id`, `comuna_destino`, `costo_fijo`, `costo_por_kg`, `dias`. Versión 1:
  tarifa por provincia (Diguillín, Itata, Punilla) desde Chillán, editable.

### C5. Precio esperado y probabilidad de adjudicación (nuevo, F-plat-4)
- `precio_indice`: `producto_id` × `ambito` (comuna / provincia / región / país) × `mes`: `n`, `p25`,
  `mediana`, `p75` del **precio ganador por unidad base**, y lo mismo para **todas las ofertas**. Caída
  jerárquica cuando `n < 5`.
- `modelo_adjudicacion` (v1, interpretable): regresión logística sobre las cotizaciones históricas de Ñuble,
  una fila por oferta, objetivo `ganó`. Variables: `ratio = precio_ofertado / mediana_ofertas_de_la_misma_CA`
  (lo que sí es comparable sin normalizar: todos ofertan lo mismo), `n_ofertas`, `es_local`, `es_emt`,
  `unidad_compradora` (efecto fijo para las 30 que compran ≥ 6 meses al año), `plazo_entrega_ofrecido`,
  `familia`. Para una CA vigente, `n_ofertas` y la mediana se estiman desde el histórico de esa unidad
  compradora y familia. Sin scikit-learn: `statsmodels` o una logística a mano con numpy (dependencia a
  proponer; numpy ya viene con pandas si se aprueba pandas en F-merc-2).
- Salida por CA vigente y por línea: `precio_sugerido` (cuantil objetivo, no promedio), `intervalo_80`,
  `p_ganar(precio)` como curva (3 puntos: agresivo / sugerido / cómodo), `costo` (mejor proveedor con
  disponibilidad), `logistica`, `margen_esperado = (precio − costo − logística − financiero 30 días) ×
  p_ganar`.
- **Juez**: walk-forward mensual. Entrenar hasta el mes M−1, predecir M, comparar con la adjudicación
  real. Métricas: calibración (Brier, curva de confiabilidad por decil), error mediano del precio ganador
  por línea normalizada, y "ganables con margen ≥ 8 %" como % de las CA en alcance. Se guarda en
  `evaluacion_modelo` (mes, métrica, valor) y se muestra en `/salud`. **Meta del peldaño 2
  (`16-pivote` §2)**: intervalo 80 % acierta ≥ 70 %; ganables ≥ 10 %.

### C6. Mesa del día y registro comercial (nuevo, F-plat-5)
- `/mesa`: CA de Ñuble vigentes en alcance, ordenadas por `margen_esperado × p_ganar`, con filtros por
  familia, comuna, cierre, "sin detalle", "con anexo pendiente". Tarjeta: comprador, cierre, líneas, precio
  sugerido, costo, margen, P(ganar), competidores probables (top 3 por unidad compradora y familia).
- Ficha de CA ampliada: líneas 1:1 editables, carga de anexo, histórico del comprador (qué compró, a quién,
  a qué precio), proveedores con costo por línea.
- `oferta_propia`: `ca_codigo`, `empresa_id`, `usuario_id`, `lineas` (precio por línea), `total`,
  `proveedor_por_linea`, `enviada_en`, `resultado` (`ganada|perdida|desierta|no_enviada`), `precio_ganador_real`,
  `margen_realizado`. **Modo sombra**: la predicción se guarda en `prediccion` antes del cierre aunque no se
  oferte; el job `mercado-ca` la cierra sola cuando llega la adjudicación.
- Exportación de la cotización (CSV/XLSX con líneas y precios) para pegarla en el portal; la oferta se
  envía en Mercado Público a mano (no hay API de oferta [I: verificar en la guía antes de prometer más]).

### C7. Demanda y abastecimiento (nuevo, F-plat-6)
- `demanda_serie`: `unidad_compradora` × `producto_id` (o familia) × `mes`: `n_ca`, `cantidad_base`,
  `monto`, desde `oc_lineas_ca` + `ca_linea`. 24 meses de historia (oct-24 → hoy).
- Pronóstico v1, sin caja negra: recurrencia (meses con compra / meses observados), estacionalidad
  (promedio por mes calendario, p. ej. colaciones marzo–diciembre, Fiestas Patrias, Navidad) y tendencia
  (promedio móvil 3 meses). Salida por producto: cantidad esperada próximos 1–3 meses con intervalo, y
  **recomendaciones**: "pedir precio firme a proveedor X por 500 kg de arroz para marzo", "stock sugerido",
  "unidad compradora Y compra galletas cada 2 meses: la próxima se espera en abril".
- **Juez**: backtest de recurrencia (¿la unidad compró el mes predicho? precisión/recall) y error de
  cantidad (MAPE) sobre los últimos 6 meses. Solo se muestran recomendaciones con `n ≥ 4` compras.
- Cruce con Plan Anual de Compras cuando vuelva (D12: PAC 2027), como señal adicional, no como base.

### C8. Operación y cierre del ciclo (F-plat-7)
- Jobs: `mercado-ca` (en vivo, F-merc-1), `mercado-oc` (diario, F-merc-2), `mercado-indices` (nocturno:
  índices, modelo, pronóstico, evaluación), `mercado-anexos` (si la sonda encontró descarga).
- `/salud`: tamaño de las tablas nuevas, gasto LLM del mes, última evaluación del modelo, CA sin detalle.
- Informe mensual automático (correo, Brevo) por empresa: adjudicaciones, margen realizado, calibración.
- Retención: las tablas del pivote no se purgan (excepción anotada en F-merc-1 §1.7); `raw_json` nunca.

## 3. Arquitectura (cambios mínimos)

```
app/
  clients/     mp_v2.py (sin cambios de contrato), datos_abiertos.py (+oc-da), openrouter.py (NUEVO; único
               que conoce la URL y el formato de OpenRouter: capa anti-corrupción)
  catalogos/   mercado.py (GRUPOS, familias; F-merc-2), canonico.py (reglas de normalización)
  mercado/     NUEVO, sin httpx: lineas.py (C2), anexos.py (C3), proveedores.py (C4), indice.py,
               adjudicacion.py (C5), demanda.py (C7), evaluacion.py
  api/routes/  mesa.py (NUEVO: /mesa, /ca/{codigo}, /proveedores, /catalogo, /abastecimiento)
  models/      tables.py (todas las tablas nuevas; solo acá se define esquema)
```

- Misma base, mismo login, misma cuota de API. Rutas nuevas bajo el `base.html` actual con una entrada de
  menú "Mesa" visible para la empresa; las rutas antiguas quedan tal cual.
- Histórico nacional (país completo) **no** entra a Neon: solo Ñuble línea a línea y agregados nacionales
  (`precio_referencia`). DuckDB/Parquet queda como herramienta de PC (`scripts/mercado/`), no de la app.
- Tamaño estimado en Neon para 24 meses de Ñuble [I]: cotizaciones ~60–120 MB, `oc_lineas_ca` ~20 MB,
  catálogo + líneas + proveedores < 10 MB. Se mide en cada fase y se anota.
- Seguridad: subida de archivos validada por tipo y tamaño, nunca ejecutados; texto extraído se trata como
  dato, no como instrucción (el prompt al modelo va con el anexo entre delimitadores y pide solo JSON).
  Ownership por `empresa_id` en cada query de proveedores, costos, ofertas y predicciones.

## 4. Fases, orden y modelo de Claude Code

Convención del repo: Opus para fases que tocan reglas duras (cuota, 429, secretos), esquema central o
lógica estadística con juez; Sonnet para CRUD, UI, importadores y scripts con contrato claro. Haiku no se
recomienda para fases completas (tiende a dejar tests y mypy a medias); sirve para tareas mecánicas sueltas
(renombrar, fixtures, docstrings) dentro de una sesión. Una fase = un commit, sin push hasta auditar.

| # | Fase | Prompt | Modelo | Qué deja | Juez |
|---|---|---|---|---|---|
| 0 | F-merc-1 | `prompt-F-merc-1.md` (ya escrito; aplicar la nota de C1) | Opus | Cotizaciones de CA de Ñuble en Neon, `mercado-ca` en vivo, backfill | Conciliación con `cot_api_2026-08.csv.gz`: mismas CA y filas ±0,5 % |
| 1 | F-merc-2 | `prompt-F-merc-2.md` (ya escrito) | Sonnet | `oc_lineas_ca`, `precio_referencia`, `app/catalogos/mercado.py` | 12 meses de alimentos = $682 M ±0,5 % |
| 2 | F-plat-3 | `prompt-F-plat-3.md` | Sonnet | Empresa con territorio priorizado, admin de empresas y usuarios, proveedores, costos con historial, tarifa logística, importadores; `mercado_regiones` sale de las empresas | Boris crea la empresa, agrega 2 usuarios, carga 3 proveedores y 40 productos en < 30 min; un usuario de otra empresa no ve nada de eso |
| 3 | F-plat-1 | `prompt-F-plat-1.md` | **Opus** | Catálogo canónico, alias, `ca_linea`, bandera "sin detalle", `/catalogo` | ≥ 70 % del monto de alimentos de 12 meses con línea `ok`; walk-forward de precio por producto canónico ≤ 25 % (desde 33 % en alimentos con UNSPSC) |
| 4 | F-plat-2 | `prompt-F-plat-2.md` | **Opus** | Sonda de documentos, carga de anexos, extractor OpenRouter, revisión, presupuesto | Set de 30 anexos: ≥ 95 % de líneas correctas con el modelo elegido; costo < US$0,01 por anexo |
| 5 | F-plat-4 | `prompt-F-plat-4.md` | **Opus** | `precio_indice`, modelo de adjudicación, evaluación walk-forward, job `mercado-indices` | Calibración: Brier ≤ 0,20 y intervalo 80 % acierta ≥ 70 % en el último mes cerrado |
| 6 | F-plat-5 | `prompt-F-plat-5.md` | Sonnet | `/mesa`, ficha ampliada, ofertas propias, modo sombra, exportación | 4 semanas en sombra (peldaño 2 de `16-pivote` §2) |
| 7 | F-plat-6 | `prompt-F-plat-6.md` | **Opus** | Series de demanda, pronóstico, recomendaciones de abastecimiento | Backtest 6 meses: recurrencia ≥ 70 % de precisión; MAPE de cantidad reportado |
| 8 | F-plat-7 | `prompt-F-plat-7.md` | Sonnet | `/salud` del pivote, informe mensual, cierre automático de predicciones, retención | Primer informe mensual con margen realizado real |

Dependencias: F-plat-3 (empresa) solo necesita F-merc-1 (usa su `mercado_regiones`) y puede ir en paralelo
con F-merc-2. F-plat-1 necesita F-merc-1, F-merc-2 y la `empresa` (para quién corrige). F-plat-2 necesita
F-plat-1. F-plat-4 necesita F-merc-1, F-merc-2, F-plat-1. F-plat-5 necesita todo lo anterior. F-plat-6
necesita F-merc-2 y F-plat-1. F-plat-7 necesita F-plat-5.

Qué queda apagado durante todo el pivote: `ca` (la ingesta genérica de CA de todas las regiones),
`ciclo-match`, `ciclo-activas`, `nocturno` (licitaciones), `resumen`, `catalogos`, `plan-anual`. El único
job con ticket es `mercado-ca` (y `mercado-backfill` de noche). Esto se verifica en `/salud`: requests del
día por job.

Estimación de calendario [I]: una fase por 2–4 días de trabajo con auditoría; las ocho en 6–8 semanas, con
el modo sombra de F-plat-5 corriendo 4 semanas en paralelo a 7 y 8.

## 5. Riesgos y cómo se miden

| Riesgo | Señal | Mitigación |
|---|---|---|
| La API no entrega anexos | Sonda de F-plat-2 §0 | Flujo manual desde el día 1; el volumen es ~6 CA de alimentos/día |
| El catálogo no converge (textos demasiado libres) | < 70 % del monto con línea `ok` tras F-plat-1 | Acotar la canasta a los 80 productos de mayor monto; el resto queda `dudosa` y se cotiza a mano |
| P(ganar) mal calibrada (59 % "gana el más barato" en agosto) | Brier > 0,25 o curva de confiabilidad torcida | Reportar el número; volver a un modelo más simple (ranking por ratio) antes que uno más complejo |
| 429 por IP (H1 abierta) al subir `mercado-ca` a cada 15 min | 429 en `job_runs` | Ya cubierto por pausa persistida; bajar frecuencia a 30 min |
| Neon 0,5 GB | `/salud` > 70 % | Guardar `productos_cotizados` solo de CA en alcance (previsto en F-merc-1 §1.5) |
| Gasto LLM | Contador mensual | Tope duro en Postgres; caché por texto; modo manual |
| Datos personales (RUT de proveedores, contactos) | — | Son datos de negocio del usuario; sin registro abierto, ownership por empresa, sin exportar a terceros |

## 6. Lo que falta de Boris

| Necesito | Para |
|---|---|
| Correr F-merc-1 y F-merc-2 (ya listos) y pegar las salidas | Datos en Neon para F-plat-1 |
| 30 anexos reales bajados de la ficha pública, con sus líneas correctas en una planilla | Set de evaluación de F-plat-2 |
| Cuenta de OpenRouter y `OPENROUTER_API_KEY` en `.env` y en GitHub Secrets | F-plat-2 |
| Lista inicial de proveedores (aunque sea 3) y tarifa de despacho por provincia | F-plat-3 y margen |
| Decisión de canasta: los 80 productos de alimentos de mayor monto (sale del observatorio) ± los que Boris quiera | F-plat-1 |
| Confirmar que no hay API de oferta en la guía (o que sí la hay) | Alcance de F-plat-5 |

*Fuente de los datos de dominio: Dirección ChileCompra.*
