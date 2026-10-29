# Diagnóstico — `ca` fallando (29-oct-2026)

*Fuente: logs de GitHub Actions (corridas `ca`, `ciclo-match`, `nocturno`, 18–29 oct), código en `2aab073`.
[V] = visto en logs/código · [I] = inferido.*

## 1. Qué pasa (resumen)

La API devuelve un **429 nuevo, con `codigo: "429"`** y mensaje *"Se ha superado el límite de tasa de
solicitudes. Intente nuevamente más tarde."* [V]. Como no es 10500, `base.py` lo trata como **tope
diario** (regla 3): `marcar_agotado()` sube `quota_log` del día a 9.000 y **todos** los jobs del resto
del día Chile cortan en `check_budget` con *"quedan 0 requests"* sin emitir nada [V].

Resultado: `ca` trabaja ~10 min al día (00:05) y el resto del día falla en ~30 s. `ciclo-match` y
`nocturno` salen **verdes pero no hacen nada** (0 requests, cuota "agotada") [V] → falla silenciosa.

## 2. Evidencia

| Día (Chile) | Requests del día antes del 429 | Quién recibió el 429 |
|---|---|---|
| 23-oct 00:05 | **8** (primera corrida del día) | `ca` |
| 24-oct 00:05 | **21** (primera corrida del día) | `ca` |
| 27-oct 01:05 | ~165 (158 a las 00:05 + 7) | `ca` |
| 28-oct 08:50 | **~1.700** (ca ×5 + nocturno 898 + 13) | `ciclo-match` |
| 29-oct 01:05 | 162 (154 a las 00:05 + 8) | `ca` (#800) |

- El 429 llega con 8 requests en un día recién empezado y otro día no llega hasta ~1.700 → **no es el
  tope diario de 10.000** ni nuestro presupuesto de 9.000 [V].
- Ritmo al recibirlo: 2–7 s entre requests (RATE_LIMIT_RPS=0.5), secuenciales [V].
- Antes (20–21 oct) los fallos eran 504 *Endpoint request timed out* de la API, no 429 [V].
- Primer `marcado como agotado` en el rango revisado: 18-oct [V]. El código no cambió desde el 09-oct
  (último commit) → **cambió el comportamiento de la API**, no nuestro código [V].
- El ticket aparece enmascarado en los logs de `nocturno` (854 apariciones, todas `MASKED`) [V].

## 3. Consecuencias

- **Cursor de CA atrasado ~2,5 días**: la corrida de las 01:05 de hoy pedía la ventana
  `2026-10-26 17:47 → 18:47` [V]. Con p10 de 24 h entre publicación y cierre, buena parte de las CA
  nuevas se cierran antes de que la app las vea → feed y alertas de CA incompletos.
- `detalles-match` y licitaciones (`ciclo-activas`, `nocturno`) también quedan sin datos el resto del día.
- Neon: los jobs igual despiertan el compute cada hora (para leer `quota_log`), así que el consumo de
  CU-h sigue aunque no se ingiera nada. 89,7/100 CU-h al 29-oct [V, correo de Neon].

## 4. Hipótesis sobre el 429 "límite de tasa"

- **H1 [I, la más probable]**: limitador por **IP** de ChileCompra. Los runners de GitHub comparten IPs
  de Azure con otros clientes de la API; cuando otro le pega fuerte desde la misma IP, nos cae el
  429. Explica por qué a veces llega con 8 requests y otras con 1.700. Precedente: el host del PAC ya
  bloquea IPs de runners (D12).
- **H2 [I]**: limitador por ticket con ventana corta (minutos/hora) que ChileCompra activó ~18-oct.
- **H3 [I, poco probable]**: alguien más usa el mismo ticket. Contra: el ticket va enmascarado en logs.
- Cualquiera de las tres es **transitoria**; ninguna justifica bloquear el día entero.

## 5. Qué hacer

**A. Hoy, sin código (Boris, 2 min)** — destrabar y de paso verificar si el 429 era transitorio:
1. En Neon → SQL Editor (rama de producción):
   `UPDATE quota_log SET requests_usadas = 162 WHERE fecha = '2026-10-29';`
2. Disparar `ca` a mano (Run workflow). Si corre con 200 → confirmado que no era tope diario
   (8 h sin llamadas bastaron). Si vuelve el 429 al tiro → anotar hora y pasar a probar H1 con
   `python scripts/smoke_test.py` desde el PC (IP distinta) en ese mismo momento.
   Ojo: si vuelve el 429, la corrida vuelve a marcar el día; repetir el UPDATE solo una vez.

**B. Fase F-429-tasa (código, Opus por tocar regla dura 3)**:
- `codigo 429` / mensaje "límite de tasa" → **error transitorio nuevo** (`MPTasaError`): enfriar
  (p. ej. 5–15 min o `Retry-After` si viene), guardar el progreso y **cortar solo esa corrida**; no
  llamar a `marcar_agotado()`. La siguiente hora corre normal.
- Tope diario solo si aparece un código verificado de cuota diaria, o como red de seguridad tras N
  corridas seguidas con 429-tasa (p. ej. 3) — a decidir.
- Que `ciclo-match`/`nocturno` no salgan verdes con "quedan 0": warning visible o estado `omitido`.
- Actualizar CLAUDE.md regla 3, `decisiones.md` y `01-analisis-api` §10 con el hallazgo [V].

**C. Ponerse al día con CA**: con `CA_TAMANO_PAGINA=10` y ~150 requests por corrida, recuperar 2,5
días toma muchas horas. Opción: adelantar el cursor a "ahora − 2 h" para que las alertas vuelvan a
ser útiles y dejar el hueco 26→29-oct para el nocturno. Decidir después de A.

**D. Neon**: quedan ~10 CU-h para el cierre del período; al ritmo actual (~3/día) alcanza justo.
No hacer reruns masivos ni backfills grandes hasta el reinicio del contador.

## 6. Resultado de la prueba A (29-oct, 09:52 Chile) [V]

Tras el `UPDATE` de `quota_log`, `ca` disparado a mano:
- **113 requests con 200** entre 09:52:54 y 10:02:52 (~11/min), después **1 × 429 `codigo=429`**
  ("límite de tasa") en la request 114. El cursor avanzó de `26-oct 17:47` a `27-oct ~09:22` (~15 h).
- Conclusión [V]: tras 8 h sin llamadas la API volvió a atender el mismo día Chile → el 429 "límite de
  tasa" **es transitorio, no tope diario**. La regla 3 (cualquier 429 ≠ 10500 = tope diario) quedó
  desmentida para este código.
- Forma del límite [I]: llega tras ~110–190 requests seguidas, o con pocas si hubo ráfaga en la hora
  previa (01:05: 8 req, 51 min después de 154). Compatible con ventana deslizante de ~1 h o con
  limitador por IP compartida. No hay `Retry-After` ni cabeceras de cuota.
- El día volvió a quedar marcado como agotado (13:02:53 UTC).

*Fuente de los datos de dominio: Dirección ChileCompra.*
