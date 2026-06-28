# Spike — Análisis de competencia desde datos abiertos (`lic-da`)

> Spike de investigación, NO ingiere datos ni toca la BD/modelos. Objetivo: confirmar
> si el archivo `lic-da` (mismo dataset que [docs/04-datos-abiertos.md](04-datos-abiertos.md))
> permite reconstruir el análisis de competencia — quién ofertó, montos, quién ganó —
> de una licitación ya **adjudicada**.
> Fuente leída solo en modo lectura: la BD dev (Neon) para obtener códigos reales, y
> los ZIP públicos de datos abiertos descargados a una carpeta temporal local, fuera
> del repo. No se imprimió ningún secreto (ni el `DATABASE_URL` ni el `MP_TICKET`).

---

## 0. Hallazgo previo: no se pudo usar `fecha_publicacion` para elegir el mes

El plan original era tomar `fecha_publicacion` de la licitación adjudicada para
saber qué archivo `lic-da/{año}-{mes}.zip` descargar. Al consultar:

```sql
SELECT codigo, fecha_publicacion, fecha_cierre
FROM licitaciones WHERE estado='adjudicada'
ORDER BY fecha_publicacion DESC LIMIT 5;
```

**las 3.050 licitaciones en estado `adjudicada` de la BD dev tienen
`fecha_publicacion` Y `fecha_cierre` en `NULL`, el 100 % de los casos.** Esto es
independiente del bug de formato de fechas ya corregido (parseo ISO en
`parse_fecha_v1`) — aquí el campo simplemente nunca se pobló: `detalle_obtenido=True`
en todas, pero sin fecha. Es un hallazgo a seguir por separado (posible gap en cómo
`upsert_detalle`/la API entrega fechas para licitaciones ya finalizadas hace tiempo);
**no se investigó ni se corrigió en este spike**, que es de solo lectura.

Como workaround para este spike: se tomaron 4 códigos `adjudicada` recientes
(por `creado_en`) y se probó el archivo `lic-da/2026-6.zip` (mes actual) directo,
sin necesitar el fallback al "mes de cierre" — 3 de los 4 códigos aparecieron ahí.
Esto SÍ confirma que el archivo del mes correcto trae la licitación aunque su estado
final (`adjudicada`) se haya alcanzado después de la publicación original.

---

## 1. Columnas de oferta pobladas para una licitación adjudicada

Verificado con `1002588-69-L126` (estado `Adjudicada`, 2 oferentes, 1 ítem) en
`lic-da/2026-6.zip`, y confirmado de nuevo con `4426-2-LE26` (9 oferentes, 113 ítems)
y `1057379-14-LE26` / `1031849-11-LE26` (2 oferentes c/u):

| Columna | Contenido | Poblada |
|---|---|---|
| `CodigoProveedor`, `RutProveedor`, `NombreProveedor`, `RazonSocialProveedor` | Identidad del oferente | Sí, siempre |
| `Nombre de la Oferta` | Texto libre que puso el proveedor | Sí |
| `Estado Oferta` | `Aceptada` / `Rechazada` — validez formal de la oferta | Sí |
| `Cantidad Ofertada` | Cantidad que ofertó ese proveedor para ese ítem | Sí |
| `MontoUnitarioOferta` | Precio unitario ofertado | Sí (ver gotcha de formato, §5) |
| `Valor Total Ofertado` | `MontoUnitarioOferta × Cantidad Ofertada` (consistente en los casos revisados) | Sí |
| `Monto Estimado Adjudicado` | Estimado de la licitación/ítem — **igual para todas las filas del mismo ítem**, no es el monto real adjudicado | Sí |
| `CantidadAdjudicada` | Cantidad adjudicada a ESTA oferta puntual — `0` si no ganó | Sí |
| `MontoLineaAdjudica` | Monto adjudicado a ESTA oferta puntual — `0` si no ganó | Sí |
| `FechaEnvioOferta` | Fecha en que el proveedor envió la oferta | Sí |
| `Oferta seleccionada` | **El flag de ganador** (ver §2) | Sí |
| `NumeroOferentes` | Cantidad total de oferentes de la licitación (constante para todas las filas de esa licitación) | Sí |

Todas estas columnas están pobladas tanto en el archivo del mes en curso (2026-6,
con la licitación aún reciente) como en un mes ya cerrado/histórico (2026-2, ver §4).

---

## 2. Cómo se identifica la oferta ganadora

**Columna `Oferta seleccionada`, valores `"Seleccionada"` / `"No Seleccionada"`.**
Es la señal explícita y la más confiable. Verificado en 3 licitaciones reales
distintas (`1002588-69-L126`, `1031849-11-LE26`, `1057379-14-LE26`): en cada una,
de las N filas (una por oferente) del mismo ítem, **exactamente una** queda
`"Seleccionada"`.

Corroborado por dos señales redundantes que SIEMPRE coinciden con `Oferta
seleccionada == "Seleccionada"` en todos los casos revisados:
- `CantidadAdjudicada > 0` (vs. `0` en las no seleccionadas).
- `MontoLineaAdjudica > 0`, y coincide con `MontoUnitarioOferta × CantidadAdjudicada`
  de esa misma fila (vs. `0` en las no seleccionadas).

Ejemplo real (`1002588-69-L126`, ítem único, 2 oferentes):

| Proveedor | Estado Oferta | MontoUnitarioOferta | CantidadAdjudicada | MontoLineaAdjudica | Oferta seleccionada |
|---|---|---|---|---|---|
| TRANSPORTE OBA | Aceptada | 5.700.000 | 0 | 0 | No Seleccionada |
| Servicio Tres de Maria | Aceptada | 5.500.000 | 1 | 5.500.000 | **Seleccionada** |

Nota: `Estado Oferta` (Aceptada/Rechazada) es una dimensión **distinta** — indica si
la oferta fue formalmente válida, no si ganó. Se confirmó con filas reales
`Estado Oferta = "Rechazada"`: siempre vienen con `Oferta seleccionada = "No
Seleccionada"` y `CantidadAdjudicada/MontoLineaAdjudica = 0` (una oferta rechazada
nunca gana, como se esperaría, pero el dato lo confirma explícitamente en vez de
asumirlo).

---

## 3. Estructura para reconstruir la competencia

Por **ítem** (`Codigoitem`) dentro de una licitación, las filas con ese mismo
`Codigoitem` son la lista `(proveedor, monto_ofertado)` de todos los que ofertaron
ese ítem; la fila con `Oferta seleccionada == "Seleccionada"` es la adjudicada.
Agrupar por `(CodigoExterno, Codigoitem)`, no asumir un orden fijo de filas.

**Total adjudicado por proveedor** (a nivel de toda la licitación): sumar
`MontoLineaAdjudica` de las filas con `Oferta seleccionada == "Seleccionada"`,
agrupado por `RutProveedor` (mejor que por nombre, que puede variar en
mayúsculas/espacios). Verificado en `4426-2-LE26` (9 oferentes, 113 ítems, 486
filas): exactamente 113 filas quedan `"Seleccionada"` (= 1 ganador por ítem, nunca
más ni menos) repartidas entre 7 de los 9 oferentes; se reconstruyó el total
adjudicado por proveedor sumando esas 113 filas agrupadas por proveedor sin
ambigüedad (de ~8,4M a ~380K CLP entre los 7 ganadores).

Pseudocódigo de la reconstrucción (ya con el patrón defensivo de `app/clients/datos_abiertos.py`):

```python
por_item: dict[str, list[dict]] = defaultdict(list)
for fila in filas_de_la_licitacion:
    por_item[fila["Codigoitem"]].append({
        "proveedor": fila["RutProveedor"],
        "nombre_proveedor": fila["NombreProveedor"],
        "monto_ofertado": parse_monto(fila["MontoUnitarioOferta"]),  # ver gotcha §5
        "ganador": fila["Oferta seleccionada"] == "Seleccionada",
    })

total_adjudicado_por_proveedor = defaultdict(float)
for fila in filas_de_la_licitacion:
    if fila["Oferta seleccionada"] == "Seleccionada":
        total_adjudicado_por_proveedor[fila["RutProveedor"]] += parse_monto(fila["MontoLineaAdjudica"])
```

---

## 4. Volumen — oferentes y filas por licitación

Muestreado sobre `lic-da/2026-6.zip` (79.915 filas analizadas de la corrida parcial)
y `lic-da/2026-2.zip` (125.814 filas, mes ya cerrado):

| Métrica | 2026-6 (mes en curso) | 2026-2 (mes cerrado) |
|---|---|---|
| Filas con `Oferta seleccionada = "Seleccionada"` | 2.803 (3,5 %) | 25.154 (20 %) |
| Filas con `Estado Oferta = "Rechazada"` | 394 (0,5 %) | — (no medido) |
| Licitaciones en estado `Adjudicada` en el archivo | 799 | 5.795 |

La proporción de `"Seleccionada"` es mucho más alta en el mes ya cerrado (20 % vs.
3,5 %): coherente con que en el mes en curso muchos procesos siguen abiertos
(`NumeroOferentes` filas sin que ninguna esté aún seleccionada).

Por licitación, el rango observado va de **2 oferentes / 2 filas** (caso simple, 1
ítem) a **9 oferentes / 486 filas** (113 ítems) en los ejemplos revisados; el
archivo completo tiene licitaciones de hasta ~1.600 filas (procesos grandes,
multi-ítem). Orden de magnitud típico: **decenas de filas por licitación mediana**,
con una cola larga de procesos grandes de cientos de ítems.

---

## 5. Gotcha de formato: `MontoUnitarioOferta` (y probablemente otras columnas monetarias)

Sobre 79.915 filas muestreadas: **97 % vienen como entero plano** (`"5500000"`),
pero **2 % vienen en notación científica** (`"5e+07"`, `"1e+08"`) y **1,1 % mezclan
coma decimal con notación científica** (`"9,9e+07"` = 9,9 × 10⁷ = 99.000.000). Un
`int(valor)` directo falla en ~3 % de las filas. Parseo defensivo recomendado:
intentar `float(valor)` directo (cubre enteros y notación científica estándar), y
si falla, reemplazar `,` por `.` antes de reintentar — mismo patrón que
`_parse_cantidad` en `app/clients/datos_abiertos.py` (regla 6: nunca romper la
ingesta por un formato inesperado). No se verificó si `Valor Total Ofertado`,
`Monto Estimado Adjudicado` y `MontoLineaAdjudica` tienen la misma mezcla de
formatos, pero al venir del mismo exportador es razonable asumir que sí y aplicar
el mismo parseo defensivo a las cuatro columnas monetarias.

---

## 6. Veredicto

**Sí, `lic-da` alcanza para el análisis de competencia de licitaciones adjudicadas**,
sin necesitar ningún archivo adicional (no se necesitó `oc-da` ni OCDS para esto):

- **Quién ofertó**: `RutProveedor`/`NombreProveedor` por fila, una fila por
  `(ítem, oferente)`.
- **Montos**: `MontoUnitarioOferta` y `Valor Total Ofertado` por oferta;
  `MontoLineaAdjudica` para el monto realmente adjudicado (solo en la fila ganadora).
- **Quién ganó**: columna `Oferta seleccionada` (`"Seleccionada"`/`"No
  Seleccionada"`), corroborada por `CantidadAdjudicada`/`MontoLineaAdjudica` > 0.
  Señal inequívoca, verificada en múltiples licitaciones reales con 2 y con 9
  oferentes.
- **Reconstrucción por ítem y total por proveedor**: directa agrupando por
  `Codigoitem` y por `RutProveedor` respectivamente (ver pseudocódigo §3).
- **Confirmado en mes congelado**: el archivo de un mes ya cerrado (`2026-2`) trae
  exactamente la misma estructura y, de hecho, una proporción más alta de
  ofertas ya resueltas (`"Seleccionada"`) que el mes en curso — el dato no se
  pierde ni se "limpia" con el tiempo.

**Lo único que falta — y no es de `lic-da`, es un problema de la BD propia**:
`fecha_publicacion`/`fecha_cierre` están en `NULL` para el 100 % de las licitaciones
`adjudicada` en la BD dev, lo que impide automatizar "qué mes descargar" a partir de
esos campos. Una futura ingesta de competencia tendría que resolver el mes con otra
señal (p. ej. `FechaAdjudicacion`/`FechaCreacion` del propio CSV de datos abiertos,
columnas 38/46, no exploradas a fondo en este spike pero presentes en el header) o
arreglar primero el gap de fechas en la ingesta de la API — **fuera de alcance de
este spike de solo lectura**.

---

*Fuente: Dirección ChileCompra — datos abiertos (https://datos-abiertos.chilecompra.cl/descargas).*
