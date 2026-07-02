# Spike — El enlace "Ver ficha oficial en MP" no abre (licitaciones)

> **Estado: HECHO — fix implementado.** `_url_ficha` (`app/api/query.py`) ya usa
> `idlicitacion={codigo}` en vez de `qs={codigo}` (ver `docs/03-roadmap.md`, sección
> "Fix — Enlace 'Ver ficha oficial en MP' no abría"). Este documento queda como el spike
> que encontró la causa y verificó el fix antes de escribirlo.

> Spike de investigación. NO se tocó código de la app. Objetivo: entender por qué
> `_url_ficha` (`app/api/query.py`) arma un enlace que Mercado Público rechaza, y
> encontrar (o descartar) una forma de construir el enlace que sí abre.
> MP_TICKET: nunca impreso ni pegado en este documento (regla 1). No se scrapeó HTML
> para extraer datos de negocio (regla 9) — solo se verificó accesibilidad (status HTTP,
> destino final tras redirect, y el `<title>` genérico de la página) de URLs públicas de
> mercadopublico.cl, exactamente como autorizó el pedido.

**Caso de prueba (dado por Boris):** licitación `1300-31-LE26` (SENAMA, "ADQ. KITS
EDUCATIVOS PARA PERSONAS MAYORES", estado `Publicada`).
- Enlace que arma la app hoy (**NO abre**): `.../RFB/DetailsAcquisition.aspx?qs=1300-31-LE26`
- Enlace que sí abre (dado por Boris, **token real**): `.../RFB/DetailsAcquisition.aspx?qs=n6dWbcZtF4qwCLgOVuSBlg==`

---

## 1. Resumen ejecutivo (veredicto)

**Causa confirmada:** el parámetro `qs` de `DetailsAcquisition.aspx` NO acepta el
`CodigoExterno` en texto plano — espera un **token encriptado** (16 bytes binarios,
codificados en base64: `n6dWbcZtF4qwCLgOVuSBlg==` decodifica a
`9fa7566dc66d178ab008b80e56e48196`, alta entropía, un solo bloque de 128 bits — no es un
GUID ni un id numérico legible). Ninguna API oficial (v1 ni v2) entrega ese token ni un id
interno que permita derivarlo (§2).

**Fix encontrado y VERIFICADO (sin necesitar el token en absoluto):** la misma página
`DetailsAcquisition.aspx` acepta un parámetro alternativo, **`idlicitacion`**, al que
**si se le pasa el `CodigoExterno` en texto plano, el propio servidor de Mercado Público
resuelve y redirige (HTTP redirect real, seguido automáticamente) al `qs=` encriptado
correcto** — reproduciendo **byte a byte** el token que Boris confirmó que abre:

```
GET https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx?idlicitacion=1300-31-LE26
→ 200, redirige a
  https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx?qs=n6dWbcZtF4qwCLgOVuSBlg==
```

Ese es **exactamente** el token que Boris reportó como funcional. No es casualidad: el
servidor distingue códigos válidos de inválidos (§4) — un código inexistente redirige a
un token **fijo y distinto**, `qs=MwCbp3P2oB7qAAFjCgZyfw==` (visto igual para un código
claramente inventado y para el `Codigo` numérico interno de OTRA licitación real — ver
§4), nunca al token real de `1300-31-LE26`. Que nuestro código real dé un token distinto
a ese "fallback" y coincida carácter por carácter con el que Boris ya validó manualmente
es una confirmación fuerte, no un espejismo de caché.

**Recomendación (NO implementada en este spike):** cambiar `_url_ficha` en
`app/api/query.py` para licitaciones de

```python
f"https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx?qs={codigo}"
```
a
```python
f"https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx?idlicitacion={codigo}"
```
(con `urllib.parse.quote(codigo, safe="")` por prudencia, aunque todos los códigos vistos
hasta ahora solo usan dígitos/letras/guion — ver §6). `mostrar_ficha_oficial` (el gate que
restringe el botón a estado `PUBLICADA`) es un problema **distinto y no relacionado**
—MP igual bloquea la ficha a quien no es dueño de la unidad en procesos cerrados,
independiente de qué parámetro se use para llegar— y no hace falta tocarlo.

---

## 2. Qué entrega la API oficial — NADA útil para el enlace

### 2.a v1 (`GET .../licitaciones.json?codigo=1300-31-LE26&ticket=***`)

Una sola llamada real, con el `MP_TICKET` de `.env` (nunca impreso). Se volcaron **las 89
claves hoja** del objeto `Listado[0]` (recursivo, incluyendo `Comprador`, `Fechas`,
`Items`) buscando cualquier campo tipo `Link`/`Url`/`Guid`/`Token`/`*Id`/`*id`: **cero
coincidencias**. Los únicos identificadores presentes son:
- `CodigoExterno` = `"1300-31-LE26"` (el mismo código público que ya usamos).
- `Comprador.CodigoOrganismo`/`CodigoUnidad`/`CodigoUsuario` — ids del organismo/unidad/
  usuario comprador, no de la licitación.

Ningún campo del detalle v1 permite construir ni derivar el token `qs`. **Confianza:
VERIFICADO** (dump completo revisado campo por campo, no una muestra).

### 2.b v2 (`api2.mercadopublico.cl`)

**No aplica.** v2 es exclusivamente Compra Ágil (`app/clients/mp_v2.py` no define ningún
endpoint de licitaciones — confirmado leyendo el cliente, no hizo falta llamar a la red
para descartar esto).

---

## 3. Fuentes $0 alternativas (datos abiertos, sin ticket, sin cuota — regla 3 no aplica)

### 3.a `lic-da` (`transparenciachc.blob.core.windows.net/lic-da/{año}-{mes}.zip`)

**Hallazgo importante:** el CSV de `lic-da` tiene **110 columnas**, y las dos primeras son:

| Índice | Columna | Contenido observado |
|---|---|---|
| 0 | `Codigo` | Id numérico interno (ej. `9671223`) — **distinto** de `CodigoExterno` |
| 1 | `Link` | `http://www.mercadopublico.cl/fichaLicitacion.html?idLicitacion=<CodigoExterno>` |

(la documentación previa del proyecto, `docs/04-datos-abiertos.md`, solo había
catalogado 2 de las 110 columnas como "relevantes" — `CodigoExterno` y
`CodigoProductoONU` — nunca se había mirado `Codigo`/`Link`).

Verificado con 2 licitaciones reales del mismo organismo (`1300-24-LE26`,
`1300-28-LE26`, ambas `Estado=Cerrada`, junio 2026):
```
CodigoExterno=1300-24-LE26  Codigo=9671223  Link=http://www.mercadopublico.cl/fichaLicitacion.html?idLicitacion=1300-24-LE26
CodigoExterno=1300-28-LE26  Codigo=9677263  Link=http://www.mercadopublico.cl/fichaLicitacion.html?idLicitacion=1300-28-LE26
```

**Limitación real:** `1300-31-LE26` (nuestro caso de prueba) **NO aparece** en
`lic-da/2026-6.zip` (4.628 códigos únicos revisados, ninguno coincide) ni en
`lic-da/2026-7.zip` (archivo de julio recién iniciado, 870 bytes, prácticamente vacío).
La licitación se creó el 2026-06-24 y sigue `Publicada` (cierra 2026-07-07) — `lic-da`
parece no incluir licitaciones activas/recientes de forma inmediata, solo evidencia esto,
no fue objetivo de este spike explicar el rezago exacto. **Conclusión: `lic-da` NO sirve
como fuente en caliente para construir este enlace en el momento en que más importa (una
licitación recién publicada)** — pero el patrón `Link` (fórmula directa sobre
`CodigoExterno`, sin token) sí se pudo extraer de otras filas y probar de forma
independiente (§3.b), sin depender de que `lic-da` tenga el dato para cada código.

**Confianza: VERIFICADO** que la columna `Link` existe y su fórmula (para códigos que sí
están en el dataset); **INFERIDO** que la misma fórmula aplica a `1300-31-LE26` (no está
en los datos abiertos para confirmarlo ahí, pero es la misma fórmula que la app ya
tendría que construir de todos modos, sin depender de `lic-da` en absoluto).

### 3.b ¿Abre `fichaLicitacion.html`? — accesibilidad ambigua, NO se puede confirmar así

```
GET http://www.mercadopublico.cl/fichaLicitacion.html?idLicitacion=1300-24-LE26  (Link real del dataset)
→ 200, sin redirect, <title>Ficha Licitación</title>
GET .../fichaLicitacion.html?idLicitacion=1300-31-LE26  (fórmula aplicada a nuestro código)
→ 200, sin redirect, <title>Ficha Licitación</title>
```

**Problema:** el control (la URL ROTA que Boris reportó, `qs=1300-31-LE26`) **también**
devuelve `200` con el mismo `<title>Ficha Licitación</title>` genérico:

```
GET .../DetailsAcquisition.aspx?qs=1300-31-LE26   (ROTA, confirmada por Boris)
→ 200, <title>Ficha Licitación</title>   ← el mismo título que la que SÍ abre
```

Es decir: **el status HTTP y el `<title>` NO distinguen "ficha real" de "error"** en
ninguna de estas páginas (son SPA/ASPX que deciden el error del lado del cliente,
después de una llamada AJAX interna) — confirmar si `fichaLicitacion.html` realmente
renderiza la ficha exigiría inspeccionar contenido/JS de la página, que es exactamente lo
que la regla 9 pide NO hacer. **Por eso NO se recomienda `fichaLicitacion.html` como fix
principal** — queda anotado como alternativa a validar a mano en un navegador real antes
de usarla, no como hallazgo cerrado.

### 3.c OCDS (`ocds.blob.core.windows.net/ocds/{yyyymm}.zip`)

No se probó en este spike. `docs/04-datos-abiertos.md` ya había dejado registrado que
esta URL "no respondió 200" en pruebas anteriores y que `lic-da` resuelve lo que hacía
falta sin necesitar OCDS — con el hallazgo de `idlicitacion` (§4) ya verificado y
suficiente, no se justificó gastar otra ronda de red en re-probar una fuente ya anotada
como problemática para un dato que ya no hace falta. **Confianza: NO investigado en este
spike** (se hereda la nota previa de `docs/04`, no se verificó de nuevo).

---

## 4. El fix — `idlicitacion=` en `DetailsAcquisition.aspx` (VERIFICADO)

Probado contra `https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx`,
siempre siguiendo el redirect HTTP automáticamente (sin parsear contenido, regla 9):

| Request | Redirige a |
|---|---|
| `idlicitacion=1300-31-LE26` (nuestro caso, Publicada) | `qs=n6dWbcZtF4qwCLgOVuSBlg==` — **idéntico al token que Boris confirmó que abre** |
| `idlicitacion=1300-31-LE26` (repetido) | mismo resultado — determinístico |
| `idlicitacion=1300-24-LE26` (Cerrada) | `qs=9jAhDUxGJ1fJTtopIe9gCw==` |
| `idlicitacion=1300-28-LE26` (Cerrada) | `qs=TaHESS2Da+UJjzoDGFOHag==` |
| `idlicitacion=CODIGO-INEXISTENTE-XYZ` (inventado) | `qs=MwCbp3P2oB7qAAFjCgZyfw==` (repetido 3 veces — mismo resultado) |
| `idlicitacion=9671223` (el `Codigo` interno numérico de `1300-24-LE26`, NO su `CodigoExterno`) | `qs=MwCbp3P2oB7qAAFjCgZyfw==` — el MISMO "fallback" que el código inventado |
| `qs=9671223` (probar el id numérico interno directo, sin `idlicitacion`) | sin redirect, se queda en `qs=9671223` |

**Lectura de la tabla:**
- Cada `CodigoExterno` real produce un token `qs` **distinto y estable** — no es una
  encriptación ciega de cualquier string: el servidor **valida existencia** y hay un
  token "no encontrado" fijo (`MwCbp3P2oB7qAAFjCgZyfw==`) que aparece tanto para un
  código inventado como para el id numérico interno (que `idlicitacion` NO reconoce como
  formato válido — hay que pasarle el `CodigoExterno`, no el `Codigo` de `lic-da`).
- Funciona igual para `Publicada` y para `Cerrada` — el mecanismo de resolución del token
  no depende del estado. (Que MP después bloquee la ficha a un no-dueño en un proceso
  cerrado es el problema que `mostrar_ficha_oficial` ya maneja hoy, aparte de esto.)
- El id numérico interno (`Codigo` de `lic-da`) puesto directo en `qs=` **no** abre nada
  (no hay redirect, se queda en la URL rota) — descarta la hipótesis de "es solo ese id
  en base64" que planteaba el pedido original; el token realmente parece producirse
  server-side a partir del `CodigoExterno`, no ser una codificación simple de un id
  numérico que nosotros podamos reproducir de forma independiente.

**Confianza: VERIFICADO** para el caso de prueba dado (coincidencia exacta, carácter por
carácter, con el token que Boris validó manualmente abriendo la ficha) y reproducido para
2 licitaciones adicionales reales. **No se abrió un navegador real** para confirmar
visualmente que la página post-redirect renderiza la ficha completa (regla 22 no se
escaló porque no hizo falta: la comparación exacta de string con el token YA confirmado
por un humano es suficiente evidencia, más fuerte que inspeccionar el render).

---

## 5. Recomendación de fix (para un prompt aparte — NO implementado aquí)

En `app/api/query.py::_url_ficha`, para `fuente == "licitaciones"`:

```python
from urllib.parse import quote

f"https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx?idlicitacion={quote(codigo, safe='')}"
```

reemplazando el actual `qs={codigo}`. Sin cambios a:
- `mostrar_ficha_oficial` (gate por estado `PUBLICADA`) — problema distinto, ver §1.
- La rama de Compra Ágil de `_url_ficha` (`buscador.mercadopublico.cl/compra-agil`) —
  fuera de alcance de este spike, Boris no reportó problema ahí.

**Antes de escribir el fix real:** correr `scripts/smoke_test.py` (o un chequeo manual
puntual) contra 2-3 códigos reales adicionales de la BD (ideal: uno de cada `Tipo`, y uno
`Cerrada`/uno `Publicada`) para confirmar que el patrón se sostiene fuera de la muestra de
este spike, y — si el tiempo lo permite — abrir el resultado en un navegador real una vez
(no automatizado) para verificar visualmente el render, ya que este spike se apoyó en
comparar el token contra un valor ya validado por un humano, no en inspección visual
propia.

---

## 6. Qué NO se confirmó (para no propagar como hecho — regla 23)

- **No se probó el formato del token `qs` con ningún otro carácter especial en el
  código** (todos los códigos vistos usan solo dígitos/letras mayúsculas/guion, ej.
  `1300-31-LE26`) — si algún `CodigoExterno` tuviera caracteres URL-especiales, la
  recomendación de `quote(codigo, safe="")` en §5 es una precaución, no algo verificado
  con un caso real.
- **No se confirmó visualmente en un navegador real** que la página final (tras el
  redirect a `qs=n6dWbcZtF4qwCLgOVuSBlg==`) renderiza el contenido completo de la
  ficha — se confirmó por coincidencia exacta de string contra el token que Boris ya
  validó manualmente, que es un nivel de confianza distinto (más indirecto) a haberlo
  abierto nosotros mismos.
- **No se investigó por qué `lic-da` no incluye licitaciones recién publicadas/activas**
  (§3.a) — se documentó el síntoma (código ausente en el mes vigente y el anterior), no
  la causa; no bloquea la recomendación de §5 porque esa recomendación no depende de
  `lic-da` en absoluto.
- **No se re-probó OCDS** (§3.c) — se heredó la nota de `docs/04-datos-abiertos.md` sin
  verificar de nuevo, porque el hallazgo de `idlicitacion` ya cerraba la pregunta antes
  de necesitar esa fuente.
- **No se verificó el comportamiento de `idlicitacion` para Compra Ágil** (la ruta de
  `_url_ficha` para `compras_agiles` no pasó por `DetailsAcquisition.aspx` en ningún
  momento de este spike; Boris solo reportó el problema para licitaciones).
