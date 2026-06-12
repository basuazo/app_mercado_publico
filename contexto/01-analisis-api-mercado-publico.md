# Análisis técnico — API de Mercado Público (ChileCompra)

> Documento de estudio para guiar sesiones de desarrollo de una aplicación de búsqueda de oportunidades en compras públicas.
> Fuentes: https://www.chilecompra.cl/api/ y Guía de Uso API Compra Ágil v2 (v3.0, mayo 2026).
> Estado: actualizado a junio 2026.

---

## 1. Panorama general

ChileCompra expone hoy **dos generaciones de API**, que la aplicación deberá consumir en paralelo:

| API | Base URL | Autenticación | Formato | Cobertura |
|---|---|---|---|---|
| **API clásica (v1)** | `https://api.mercadopublico.cl/servicios/v1/publico/` | ticket como **query param** (`&ticket=...`) | JSON / JSONP / XML | Licitaciones, Órdenes de Compra, búsqueda de proveedores y organismos |
| **API Compra Ágil (v2)** | `https://api2.mercadopublico.cl` | ticket como **header HTTP** (`ticket: ...`) | JSON | Compras Ágiles (cotizaciones), con filtros modernos y paginación |

Punto clave de diseño: **un mismo ticket, dos esquemas de autenticación distintos**. El cliente HTTP de la app debe abstraer esta diferencia desde el día uno.

### 1.1 Naturaleza del servicio
- Datos públicos, gratuitos, en tiempo real (licitaciones, OC, compradores, proveedores).
- **No hay webhooks ni push**: toda la app se construye sobre **polling + sincronización incremental**.
- Las consultas de listado (por día/estado) entregan **información básica**; el **detalle completo solo se obtiene consultando por código**, una a una. Esto define el patrón de ingesta: *listar → filtrar → pedir detalle solo de lo relevante* (para no quemar la cuota).

---

## 2. API v1 — Licitaciones

Endpoint: `https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json`

### 2.1 Tipos de consulta
| Consulta | Parámetros | Resultado |
|---|---|---|
| Por código de licitación | `codigo=1509-5-L114` | Detalle completo (la fecha es irrelevante) |
| Del día actual (todos los estados) | solo `ticket` | Listado básico |
| Por fecha | `fecha=ddmmaaaa` (ej. `12062026`) | Listado básico del día |
| Activas | `estado=activas` | Todas las licitaciones publicadas al día de la consulta — **el endpoint central para "oportunidades abiertas"** |
| Por estado y fecha | `fecha=...&estado=adjudicada` | Estados: publicada, cerrada, desierta, adjudicada, revocada, suspendida, todos |
| Por organismo | `fecha=...&CodigoOrganismo=6945` | Licitaciones del organismo en esa fecha |
| Por proveedor | `fecha=...&CodigoProveedor=17793` | Licitaciones donde participa el proveedor |

### 2.2 Códigos de estado (vienen numéricos en la respuesta)
| Código | Estado |
|---|---|
| 5 | Publicada |
| 6 | Cerrada |
| 7 | Desierta |
| 8 | Adjudicada |
| 18 | Revocada |
| 19 | Suspendida |

### 2.3 Estructura de respuesta
```
Cantidad        → nº de licitaciones encontradas
FechaCreacion   → fecha de la consulta
Version         → versión de la API
Listado[]       → licitaciones (básico o detalle según consulta)
```

### 2.4 Catálogos relevantes (anexos)
- **Tipo de licitación**: L1 (<100 UTM), LE (100–1000 UTM), LP (>1000 UTM), LS (servicios personales), más tipos privados/trato directo (A1, B1, D1, C1/C2, F2/F3, G1/G2, R1, CA, SE, CO…). Ojo: la doc advierte que hay **tipologías obsoletas** que pueden aparecer en históricos.
- **Monedas**: CLP, CLF (UF), USD, UTM, EUR → la app debe **normalizar montos a CLP** para comparar/filtrar.
- **Monto estimado**: 1 = presupuesto disponible, 2 = precio referencial.
- **Modalidad de pago** (1–10), **unidades de tiempo** (1–5), **acto administrativo** (1–5).
- **Campos binarios** en el detalle: `Informada`, `CodigoTipo` (1 pública / 2 privada), `TomaRazon`, `EstadoPublicidadOfertas`, `Contrato`, `Obras`, `VisibilidadMonto`, `SubContratacion`, `ExtensionPlazo`, `EsBaseTipo`, `EsRenovable`. Atención: hay inconsistencias documentadas (p. ej., `Obras` declara "2=Sí 1=No" pero el ejemplo muestra `0`; `Contrato` ejemplifica el texto "NO"). **Regla de desarrollo: parsear estos campos de forma defensiva y validar contra datos reales.**

---

## 3. API v1 — Órdenes de Compra

Endpoint: `https://api.mercadopublico.cl/servicios/v1/publico/ordenesdecompra.json`

Mismos patrones de consulta que licitaciones: por `codigo` (ej. `2097-241-SE14`), por `fecha`, por `estado` (texto: `enviadaproveedor`, `aceptada`, `cancelada`, `recepcionconforme`, `pendienterecepcion`, `recepcionaceptadacialmente`, `recepecionconformeincompleta`, `todos` — **nótese que los dos últimos slugs vienen con erratas oficiales; usar tal cual**), por `CodigoOrganismo` y por `CodigoProveedor`.

### Estados (numéricos en respuesta)
| Código | Estado |
|---|---|
| 4 | Enviada a proveedor |
| 5 | En proceso |
| 6 | Aceptada |
| 9 | Cancelada |
| 12 | Recepción conforme |
| 13 | Pendiente de recepcionar |
| 14 | Recepcionada parcialmente |
| 15 | Recepción conforme incompleta |

### Catálogos
- **Tipo de OC** (1–14): OC automática, tratos directos (D1, C1, F3, G1), R1 (<3 UTM), CA, SE, **CM (Convenio Marco)**, FG, TL (obsoleto), MC (Microcompra), **AG (Compra Ágil)**, CC (Compra Coordinada). El tipo `AG` es el puente natural con la API v2.
- Tipos de despacho (7, 9, 12, 14, 20, 21, 22) y tipos de pago (1, 2, 39, 46–49).

Valor para el proyecto: las OC permiten **inteligencia de mercado** (qué compran los organismos, a quién, a qué precios), complementando la búsqueda de oportunidades abiertas.

---

## 4. API v1 — Códigos de organismos y proveedores

| Recurso | URL |
|---|---|
| Buscar proveedor por RUT | `.../servicios/v1/Publico/Empresas/BuscarProveedor?rutempresaproveedor=70.017.820-k&ticket=...` (RUT **con puntos, guión y DV**) |
| Listar todos los organismos | `.../servicios/v1/Publico/Empresas/BuscarComprador?ticket=...` |

El listado de compradores es un **catálogo maestro** que conviene descargar una vez y cachear localmente (tabla `organismos`), refrescándolo semanal o mensualmente.

---

## 5. API v2 — Compra Ágil (la más moderna y la mejor documentada)

Base: `https://api2.mercadopublico.cl` · Auth: header `ticket`.

### 5.1 Endpoints
| Endpoint | Uso |
|---|---|
| `GET /v2/compra-agil` | Listado/búsqueda con filtros y paginación |
| `GET /v2/compra-agil/{codigo}` | Detalle completo (productos, proveedores cotizando, montos, adjuntos) |

### 5.2 Filtros del listado
- **Ventana de cambios** (clave para sync incremental, usar A o B, no ambas):
  - A: `ttl_cambio_ms=300000` (cambios en los últimos 5 min)
  - B: `cambio_desde` / `cambio_hasta` (ISO-8601)
- **Fecha de publicación**: `publicado_desde` / `publicado_hasta` (ISO-8601).
- **Estado** (múltiples, separados por coma): `publicada`, `cerrada`, `desierta`, `cancelada`, `proveedor_seleccionado`. (`oc_emitida` existe en el modelo pero **no se usa en la práctica**; las CA con OC quedan en `proveedor_seleccionado`.)
- **Región**: `region=13,5` (códigos 1–16; 13 = Metropolitana).
- **Búsqueda**: `id` (código exacto) **o** `q` (palabras clave, URL-encoded) — mutuamente excluyentes.
- **Paginación**: `tamano_pagina` (default 15, **máx. 50**), `numero_pagina` (desde 1). Respuesta incluye `payload.paginacion` con `total_paginas` y `total_resultados`.
- **Orden**: `ordenar_por=FechaUltimaModificacion` (default) o `FechaPublicacion`.

### 5.3 Limitaciones y "gotchas" documentadas oficialmente
1. **No existe filtro `codigo_organismo`** en Compra Ágil. Workaround oficial: filtrar por `region` y luego filtrar localmente por `institucion.rut` u `organismo_comprador`.
2. `orden_compra.codigo_orden_compra` retorna **null aunque exista OC**. El indicador confiable es `orden_compra.id_orden_compra != null`; usar `id_oc`/`id_orden_compra` para cruzar con la API de OC.
3. `estado_cotizacion.{id,glosa}` está documentado pero **no confirmado** en respuestas reales → parseo defensivo.
4. Doble llamado: `estado_convocatoria` 1/2 (primer/segundo llamado). `proveedores_cotizando[]` siempre refleja **el llamado vigente** y es `[]` cuando no hay ofertas. El detalle completo de cotizaciones aparece desde "cerrada en segundo llamado" en adelante.
5. Envelope estándar: `{"success": "OK|NOK", "trace", "payload", "errors[]"}` → validar `success` antes de leer `payload`.

### 5.4 Campos más útiles para un buscador de oportunidades
`codigo`, `nombre`, `descripcion`, `estado.codigo`, `fechas.fecha_publicacion`, `fechas.fecha_cierre`, `fechas.fecha_ultimo_cambio` (cursor de sync), `montos.monto_disponible_clp` (ya normalizado a CLP), `institucion.{organismo_comprador, rut, region, nombre_region}`, `productos_solicitados[]` (código de catálogo, nombre, cantidad, unidad), `resumen.total_ofertas_recibidas` (proxy de competencia), `links.detalle`.

---

## 6. Cuotas, errores y condiciones de uso (restricciones de diseño)

| Restricción | Detalle | Implicancia en la app |
|---|---|---|
| **Cuota diaria** | 10.000 solicitudes/día por ticket (v1, no modificable). En v2 la cuota depende del tipo de ticket; `-1` = ilimitada | Presupuestar requests: priorizar listados con filtros, pedir detalle solo de candidatos; contador local de consumo |
| **Error 429** | "Se ha alcanzado el límite…"; la cuota se restablece **por día calendario**, no por ventana de 24 h | Backoff que espera al cambio de fecha; cola de pendientes que se retoma al día siguiente |
| **Error 401** | Ticket ausente/ inválido (v2 lo exige en header) | Validación temprana del ticket al arrancar |
| **Throttling por IP** | ChileCompra monitorea y puede restringir por volumen desde una misma IP | Rate limiting propio (p. ej. 1–2 req/s con jitter), no paralelizar agresivamente |
| **Descargas masivas** | Recomendado entre **22:00 y 07:00** | Programar backfills históricos en ventana nocturna |
| **Ticket personal** | Único por persona, asociado a RUT/Clave Única; no compartir ni subir a repos | Ticket SIEMPRE en variable de entorno/secreto; nunca en código ni en logs |
| **Atribución** | Si se publica información sin modificar, citar como fuente a la Dirección ChileCompra | Footer/leyenda de atribución en la UI y reportes |
| **Servicio voluntario** | ChileCompra puede modificar o suspender la API | Capa anti-corrupción (adaptadores) que aísle el resto de la app de cambios de contrato |

### Sobre "scraping"
La información necesaria está disponible vía API oficial y los términos exigen acceder "exclusivamente a través de los mecanismos descritos". **Recomendación firme: la app debe consumir la API, no scrapear el HTML de mercadopublico.cl.** Llamarla "app de búsqueda de oportunidades sobre la API de Mercado Público" es más preciso y evita riesgos de bloqueo del ticket/IP.

---

## 7. Estrategia de datos recomendada

1. **Catálogos** (una vez + refresh periódico): organismos (`BuscarComprador`), tablas de estados, tipos, monedas, regiones → seeds en la BD.
2. **Ingesta diaria de licitaciones**: `estado=activas` 1–3 veces al día → upsert básico → detalle por `codigo` **solo** para las que pasan los filtros de interés del usuario (rubro/keywords/monto/región).
3. **Ingesta incremental de Compra Ágil**: job cada N minutos con `ttl_cambio_ms` (o `cambio_desde` = último cursor) + `estado=publicada`; paginar a `tamano_pagina=50`; guardar `fecha_ultimo_cambio` máximo como cursor.
4. **Seguimiento de ciclo de vida**: re-consultar por código las oportunidades guardadas para detectar cambios de estado (cierre, adjudicación, OC emitida) y alimentar alertas.
5. **Normalización**: montos a CLP (CA ya lo trae; licitaciones requieren conversión UF/UTM/USD/EUR — definir fuente de tipos de cambio), fechas a ISO/UTC, estados a un enum propio unificado.
6. **Matching de oportunidades**: índice de texto (nombre + descripción + productos) con keywords/sinónimos del usuario + filtros estructurados (región, monto mín/máx, tipo, organismo) + score simple (relevancia keyword, días al cierre, nº de ofertas recibidas).

---

## 8. Riesgos técnicos identificados (para la auditoría)

- R1. Exposición del ticket (query param en v1 aparece en URLs → cuidar logs propios y de proxies).
- R2. Agotamiento de cuota por detalle-por-código indiscriminado.
- R3. Inconsistencias de datos (campos binarios v1, nulls inesperados en v2, tipologías obsoletas).
- R4. Cambios de contrato de la API (servicio voluntario, en evolución: consulta pública 2026 sobre nuevas APIs).
- R5. Formato de fecha v1 `ddmmaaaa` propenso a errores (vs ISO en v2).
- R6. Erratas oficiales en slugs de estado de OC (`recepcionaceptadacialmente`, `recepecionconformeincompleta`).
- R7. JSON potencialmente grande en días de alta actividad → parseo en streaming o paginación donde exista.

---

## 9. Referencias oficiales
- Portal API: https://www.chilecompra.cl/api/
- Solicitud de ticket: https://api.mercadopublico.cl/modules/IniciarSesion.aspx
- Guía Licitaciones (PDF): chilecompra.cl/wp-content/uploads/2026/03/Documentacion-API-Mercado-Publico-Licitaciones.pdf
- Guía Órdenes de Compra (PDF): chilecompra.cl/wp-content/uploads/2026/03/Documentacion-API-Mercado-Publico-oc.pdf
- Guía Compra Ágil v2 (PDF, v3.0 mayo 2026): chilecompra.cl/wp-content/uploads/2026/05/Documentacion_API_Compra_Agil.pdf
- Datos Abiertos (descargas masivas históricas, alternativa a la API para backfill): https://datos-abiertos.chilecompra.cl/
