# Prompt de implementación — F-ca-presente (el listado deja de perseguir al reloj) · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet** (bug acotado, con test de reloj). Una fase = un commit, **sin push**. **Sin
> migración.** **Prioridad: antes que F-merc-3.** Origen: log de Actions de `mercado` del 4-nov (corrida de las 14:45
> Chile), revisado por Cowork. Lo marcado [V] está verificado en el log y en el código.

## 0. El bug [V]
`_sync_en_ventanas` (`app/ingest/compra_agil.py`) calcula en cada vuelta
`hasta = min(desde + ancho, ahora_utc() − 10 min)`, con `desde = cursor − 5 min`, y corta solo si `hasta <= cursor`.

Al llegar al presente, `cursor = hasta`. En la vuelta siguiente, el reloj ya avanzó los segundos que tardó el request,
así que `hasta` vuelve a quedar unos segundos por delante de `cursor` y se abre **otra ventana de 5 min + unos
segundos**. Eso se repite hasta el tope de `CA_MAX_REQUESTS_POR_CORRIDA` (150) o hasta un 429.

Evidencia del 4-nov, de 14:50 a 15:00 Chile (`mercado-ca`):
- 110 ventanas cerradas y 112 requests;
- cada ventana avanzó el cursor entre 4 y 15 s;
- el atraso real era de 0,17 h;
- terminó con un 429 `codigo=429` y una pausa hasta las 15:20.

Consecuencias:
1. Con cada `mercado-ca` alcanzando el presente, se gastan del orden de 100–150 requests cada 15 min solo en el
   listado. Eso basta para gatillar el "límite de tasa" (`15-diagnostico-ca-429.md`: llega tras ~110–190 requests
   seguidas). Es probable que sea la causa principal de los 429 diurnos [I].
2. Un listado cortado por `MPTasaError` hace `return` **antes de la cola de detalles** (`sync_mercado_ca`), así que
   esas corridas no bajan ganadores. Con F-merc-3, tampoco bajarían las vigentes.
3. El job `ca` (hoy apagado) usa la misma función desde F-ca-ventana (`3b6b9b9`, 22-sep). Con este fix queda
   corregido también.

## 1. Cambio
- Fijar el límite **una vez al inicio de la corrida**: `limite = ahora_utc() − _MARGEN_PRESENTE`, y en el loop usar
  `hasta = min(desde + ancho, limite)`. La condición de corte `hasta <= cursor` queda igual. Así, al alcanzar `limite`,
  la corrida termina, y lo que cambie después lo toma la siguiente (el solapamiento de 5 min cubre el borde).
- Además, una guarda: si una ventana avanzaría el cursor menos de `_AVANCE_MINIMO` (1 min) y no está partida por
  volumen, cortar con log INFO. Es una defensa por si vuelve a aparecer otra fuente de reloj móvil.
- No cambiar `_SOLAPAMIENTO`, `_MARGEN_PRESENTE`, la partición por volumen ni el contrato de retorno. Agregar al
  resumen `limite` (ISO).

## 2. Tests
- **Reloj que avanza 5 s por request** (freezegun con `tick` o un reloj inyectado). Con el cursor 1 h atrás: la corrida
  hace las ventanas necesarias para llegar a `limite` y termina. Que el número de requests sea pequeño y acotado, no
  ~150.
- Con el cursor ya en el presente: 0 ventanas y 0 requests.
- Un 429 a mitad de camino sigue guardando el avance, como hoy.
- Los tests actuales de `test_ca_ventana*.py` siguen en verde sin cambiar lo que esperan, salvo los que dependían del
  bug: anotar cuáles y por qué en el commit.

`ruff check .`, `python -m mypy app`, `python -m pytest -rs` contra `mp-test-pg`.

## 3. Cierre
Sin changelog (no es visible). `00-estado.md` y `15-diagnostico-ca-429.md`: anotar el hallazgo como [V], y como [I]
que puede explicar parte de los 429 desde el 18-oct. La sonda H1 sigue valiendo: dice si el límite es por ticket o por
IP. Commit: `F-ca-presente: el listado de CA termina al alcanzar el presente`.

## 4. Pasos de Boris
1. Push (sin migración: no hace falta pausar crons).
2. Si se usó la mitigación, borrar la variable de Actions `CA_MAX_REQUESTS_POR_CORRIDA` (o volverla a 150).
3. Al día siguiente, pegar `requests_usadas` de 3 corridas de `mercado` (esperado: 1–5 cada una) y cuántas pausas por
   429 hubo de día.
