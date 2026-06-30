# Spike — HTTP 500 en `GET /v2/compra-agil` (job `ca`)

> Estado: **VERIFICADO** contra la API real (`api2.mercadopublico.cl`) el 2026-06-30.
> Alcance: solo diagnóstico. No se modificó código de la app.
> MP_TICKET: nunca impreso ni pegado en este documento (regla 1).

---

## 1. Resumen ejecutivo (veredicto)

**Causa: contrato de la API, no caída del servicio ni problema de nuestra cuenta.**

`GET /v2/compra-agil` devuelve **500 `ERROR_INTERNO`** de forma **persistente y reproducible
(5/5 intentos)** cuando la consulta no incluye **ningún filtro real** (solo paginación, o
paginación + `ordenar_por`). En cuanto se agrega cualquier filtro real (`cambio_desde`,
`publicado_desde`/`publicado_hasta`, `estado`, `region` o `q`), el endpoint responde 200
de forma consistente.

El bug en nuestro lado: `sync_incremental` (`app/ingest/compra_agil.py`) solo envía
`cambio_desde` cuando ya existe un cursor guardado. Si el cursor es `NULL` —primera
corrida, o cualquier corrida anterior que nunca llegó a "éxito total"— la llamada sale
con **únicamente** `tamano_pagina` y `numero_pagina`, que es exactamente la combinación
que dispara el 500. Como el cursor solo avanza en éxito total (`finally` en
`sync_incremental`), esto es un **bucle de fallo permanente**: cada corrida falla por el
mismo motivo que impide que el cursor avance, así que la siguiente corrida vuelve a
fallar igual.

En la BD dev (Neon, branch dev — la que usa `DATABASE_URL` en `.env`) el cursor de
`sync_state` para `compra_agil` es:

```
fuente='compra_agil'  cursor=NULL  ultima_ejecucion=2026-06-28 17:50:54  ultimo_ok=NULL  notas=NULL
```

`ultimo_ok` es `NULL` → esta fuente **nunca ha completado una sincronización exitosa**
en este branch. Esto es consistente al 100% con la causa encontrada: cada corrida cae en
el caso "sin filtros" y se rompe.

**Caveat:** este cursor es de la branch **dev** de Neon (la única accesible desde el
entorno local; `DATABASE_URL_PROD` es solo referencia, la usa Render). El síntoma reportado
en prod ("cron horario falla con params que incluyen `cambio_desde:<ISO>`") es una
descripción genérica basada en leer el código, no un valor de `cambio_desde` realmente
capturado — porque hoy `base.py` descarta el cuerpo y los params en el error (ver §4).
Es muy probable que el cursor en prod esté en el mismo estado (`NULL`/nunca exitoso) por
el mismo motivo, pero **no se verificó directamente la BD de prod** en este spike.

---

## 2. Reproducción — cuerpo crudo de la API

Todas las pruebas se hicieron contra `https://api2.mercadopublico.cl/v2/compra-agil` con
el `MP_TICKET` real de `.env`, respetando ~1.2 req/s. El ticket nunca se imprimió.

### 2.a Caso que dispara el 500 (reproducido 5/5 veces, en 2 rondas separadas)

```
GET /v2/compra-agil?tamano_pagina=50&numero_pagina=1
→ 500
{"success": "ERROR", "trace": null, "payload": null,
 "errors": [{"codigo": "ERROR_INTERNO",
             "mensaje": "Servicio no disponible, intente nuevamente más tarde",
             "detalle": ""}]}
```

Repetido como `a)`, `e1)`, `e2)`, `e3)` (ronda 1) y `f7)` (ronda 2, control) — **siempre
500, siempre el mismo cuerpo**. No es intermitente.

También dispara 500 con `ordenar_por` solo (sin filtro real):
```
GET /v2/compra-agil?tamano_pagina=50&numero_pagina=1&ordenar_por=fecha_publicacion
→ 500  (mismo cuerpo ERROR_INTERNO)
```
Y con `tamano_pagina` solo (sin `numero_pagina`):
```
GET /v2/compra-agil?tamano_pagina=50
→ 500  (mismo cuerpo ERROR_INTERNO)
```

### 2.b Caso sin NINGÚN parámetro — distinto al anterior

```
GET /v2/compra-agil
→ 400
{"success": "NOK", "trace": null, "payload": null,
 "errors": [{"codigo": "PARAMETROS_INVALIDOS",
             "mensaje": "Parámetros de consulta inválidos",
             "detalle": "Los parámetros de consulta son requeridos"}]}
```

Esto es clave: la API **sí valida correctamente** el caso "cero parámetros" (400 limpio).
El bug es específicamente el caso intermedio — "solo paginación/orden, sin ningún filtro
real" — que cae en una ruta de código que no valida y truena con 500 en vez de devolver
un 400 análogo.

### 2.c Casos que SIEMPRE devuelven 200 (cualquier filtro real basta)

| Variante | Resultado |
|---|---|
| `cambio_desde` con `Z` (tz UTC) | 200 |
| `cambio_desde` sin tz, con microsegundos (formato que usa el cliente actual, `dt.isoformat()`) | 200 |
| `cambio_desde` sin tz, sin microsegundos | 200 |
| `cambio_desde` solo fecha `YYYY-MM-DD` | 200 |
| `cambio_desde` muy antiguo (`2020-01-01`) | 200 |
| `cambio_desde` futuro (sin resultados) | 200, `items: []`, `total_paginas: 0` |
| `publicado_desde` + `publicado_hasta` | 200 |
| `estado=publicada` (solo) | 200 |
| `region=13` (solo) | 200 |
| `q=informatica` (solo) | 200 |
| `estado=publicada,cerrada,proveedor_seleccionado` (los 3 válidos para la app) | 200 |

**Conclusión de formato:** el formato exacto de `cambio_desde` **no importa** — los 4
formatos probados (con tz, sin tz, sin microsegundos, solo fecha) funcionan igual. El
formato que ya genera el cliente actual (`datetime.isoformat()`, sin tz) es válido. El
problema nunca fue el formato de fecha; fue la **ausencia total de filtro**.

### 2.d Entitlement del ticket — descartado

```
GET /v2/compra-agil/3136-19-COT26   (detalle, código real obtenido del listado)
→ 200, payload completo (productos, institución, etc.)
```

El ticket tiene acceso completo a v2 (listado y detalle). No es un problema de permisos
disfrazado de 500.

---

## 3. Contrato oficial (Guía API Compra Ágil v2, v3.0, mayo 2026)

Se descargó y leyó la guía PDF oficial
(`chilecompra.cl/wp-content/uploads/2026/05/Documentacion_API_Compra_Agil.pdf`, ya
referenciada en `docs/01-analisis-api-mercado-publico.md`). Hallazgos relevantes:

- **Grupo 1 (Ventana de cambios)**: "Use opción A o B (no ambas a la vez)" —
  `ttl_cambio_ms` *o* `cambio_desde`/`cambio_hasta`. Ninguno es individualmente
  obligatorio según la tabla.
- **Los 6 ejemplos de uso (§8.1–8.6)** del documento oficial **siempre incluyen al menos
  un filtro real** (`ttl_cambio_ms`, `cambio_desde`/`cambio_hasta`, `publicado_desde`,
  `q`, `region` o `estado`). No hay ningún ejemplo de "listar todo sin filtro" en la guía.
- La tabla de errores documenta 500 genéricamente como "Error inesperado en el servidor
  ... intente nuevamente en unos minutos" — es decir, la documentación no advierte que
  el caso "sin filtro" sea persistente; lo trata como si fuera transitorio. **En la
  práctica, para este caso específico, es 100% persistente, no transitorio** (5/5).
- No se encontró ninguna nota de changelog (v2.0→v3.0) que mencione este comportamiento
  ni un cambio reciente de contrato. No hay evidencia de que el endpoint haya cambiado
  recientemente — el diseño "requiere al menos un filtro real" parece ser su
  comportamiento estable, simplemente nunca documentado explícitamente como
  obligatorio (y mal manejado del lado del servidor: 500 en vez de 400).
- El propio `docs/01-analisis-api-mercado-publico.md` (línea 161, escrito antes de este
  spike) ya recomendaba: *"Ingesta incremental de Compra Ágil: job cada N minutos con
  `ttl_cambio_ms` (o `cambio_desde` = último cursor) **+ `estado=publicada`**"* — es
  decir, el diseño original consideraba combinar la ventana de cambios con un filtro de
  estado. La implementación actual (`app/ingest/compra_agil.py`) se desvió de eso:
  filtra por estado **localmente** después de traer los datos (comentario en el código:
  *"spec: filtrar después del API"*) y no manda `estado` a la API. Esa desviación es la
  que deja la puerta abierta al caso "sin ningún filtro" cuando no hay cursor.

No fue necesario escalar a navegador real (la guía es un PDF descargable, no una SPA);
la regla 22 no aplica a esta fuente.

---

## 4. Mejora chica para diagnosticabilidad (no implementada aún)

`app/clients/base.py:_handle_response` descarta el cuerpo de la respuesta en los casos
401/429/5xx — solo guarda `status_code`. Si hubiera capturado `response.text` en el
`MPServerError`, este spike habría tomado minutos en vez de requerir reproducir
manualmente con curl/httpx. Recomendación para el fix: loguear (no solo levantar la
excepción) el cuerpo crudo de cualquier 5xx, truncado a ~500 caracteres, en
`_log.warning`/`_log.error` — nunca el header `ticket` (ya está fuera del body, así que
no hay riesgo de fuga ahí).

---

## 5. Fix recomendado (para implementar en un prompt aparte)

**No tocar el formato de `cambio_desde`** — ya es válido en cualquiera de las 4 variantes
probadas.

El fix real es en `app/ingest/compra_agil.py::sync_incremental`: cuando `cursor_dt is
None` (primera corrida o recuperación tras fallo total), **nunca llamar a
`listar_compra_agil` sin al menos un filtro real**. Dos opciones válidas, ambas
verificadas con 200 en este spike:

1. **Pasar `estados=list(_ESTADOS_VALIDOS)` a la API** en vez de (o además de) filtrar
   localmente, cuando no hay cursor. Esto además alinea con la recomendación original de
   `docs/01` y reduce páginas/cuota gastada en estados que de todos modos se descartan
   localmente (`desierta`, `cancelada`, `oc_emitida`).
2. **Usar una fecha sentinela antigua como `cambio_desde`** (p. ej. una constante tipo
   "inicio de la ingesta del proyecto") cuando `cursor_dt is None`, en vez de omitir el
   parámetro. Mantiene el diseño actual de filtrar estado localmente, cambia menos código.

Cualquiera de las dos rompe el bucle de fallo permanente. La opción 1 es la más barata en
cuota (la API ya filtra, no hay que paginar por estados irrelevantes) y la más fiel al
diseño original documentado; se recomienda esa salvo que haya una razón concreta para
seguir filtrando estado solo localmente.

Adicionalmente (independiente, baja prioridad): aplicar la mejora de §4 en `base.py` para
que el próximo incidente similar no requiera repetir este spike manual.

---

## 6. Qué NO se confirmó (para no propagar como hecho)

- El cursor de **prod** (Neon branch production) no se leyó directamente en este spike;
  solo se leyó el de **dev**. La hipótesis de que prod está en el mismo estado (`NULL`/
  nunca exitoso) es **inferida por consistencia de diseño**, no verificada en la fuente
  primaria de prod.
- No se probó el comportamiento bajo carga/concurrencia ni se descartó al 100% que el
  servidor tenga *además* algún problema de capacidad — pero la reproducción 5/5
  determinística ligada a un patrón de parámetros específico hace muy improbable que sea
  solo una caída transitoria del servicio.
