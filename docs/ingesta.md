# F3 — Ingesta incremental de Mercado Público

## Flujo de datos

```
API v1 / API v2
      │
      ▼
app/clients/          ← capa anti-corrupción (rate limit 1 req/s + cuota Postgres)
      │
      ▼
app/ingest/           ← lógica de sincronización
  ├── licitaciones.py     sync_activas · fetch_detalles_pendientes · sync_por_fecha
  ├── compra_agil.py      sync_incremental (cursor + solapamiento 5 min)
  ├── lifecycle.py        refresh_estados (ventana ±7/+3 días)
  ├── catalogos.py        refresh_organismos
  └── orchestrator.py     APScheduler + pg_advisory_lock
      │
      ▼
Postgres (Neon)       ← toda la persistencia; proceso es desechable
```

## Schedule de jobs

| Job               | Frecuencia              | Requests estimados      |
|-------------------|-------------------------|-------------------------|
| `ca_incremental`  | cada 30 min (48×/día)   | 1–3 req/ciclo = ~96     |
| `activas`         | 8h · 13h · 18h Chile    | 1 req/ciclo = 3         |
| `detalles`        | tras activas (3×/día)   | ≤200 req/ciclo = ≤600   |
| `nocturno`        | 23:30 Chile             | lifecycle ≤100 + backfill ≤200 |
| `retencion`       | 03:00 Chile (diario)    | 0 req (solo SQL)        |
| `catalogos`       | lunes 02:00 Chile       | 1 req/semana            |

**Peor caso diario:**
- CA incremental: 48 ciclos × 3 páginas × 50 items = 144 req
- Activas: 3 req
- Detalles: 3 × 200 = 600 req
- Nocturno: lifecycle 100 + backfill 200 = 300 req
- Catalogos: ≤1/7 ≈ 0 (semanal)

**Total peor caso: ~1.047 req/día → muy por debajo del presupuesto de 9.000**

El presupuesto (`api_daily_budget=9000`) se persiste en `quota_log` en Postgres.
Si se alcanza, `QuotaExceededError` se lanza antes de cualquier llamada HTTP.

## Cursor de Compra Ágil

```
sync_state.cursor = "2026-06-10T12:00:00"  ← UTC naive ISO-8601

Al leer:
  cursor_dt = datetime.fromisoformat(cursor).replace(tzinfo=UTC)
  cambio_desde = cursor_dt - timedelta(minutes=5)  ← solapamiento

Avance del cursor:
  - SOLO si exitoso=True (al terminar el while sin excepción)
  - Se guarda en finally: if exitoso and nuevo_cursor_dt is not None
  - En 429 a mitad de paginado: commit por página persiste, cursor NO avanza
```

## Advisory lock

```python
_LOCK_KEY = 7_891_011  # hash arbitrario de "mp_ingesta"

# En cada ciclo:
acquired = pg_try_advisory_lock(_LOCK_KEY)
if not acquired:
    return None  # otro proceso está corriendo (Render levanta 2 instancias en deploy)
try:
    resultado = job()
finally:
    pg_advisory_unlock(_LOCK_KEY)  # SIEMPRE liberado
```

## Backfill nocturno

```python
def _ciclo_nocturno(settings, engine, now_fn=None):
    if not en_ventana_nocturna(now_fn):   # validado con ZoneInfo("America/Santiago")
        _log.warning("fuera de ventana — abortando")
        return
    ...
```

La ventana 22:00–07:00 se valida en código con ZoneInfo, **no** se confía en el cron de Render (que corre en UTC).

## Reglas de filtrado

- **Compra Ágil**: la API v2 NO filtra por región ni estado. Se filtra **localmente** después de recibir la respuesta.  
  Estados válidos: `{"publicada", "cerrada", "proveedor_seleccionado"}`. El resto se descarta sin guardar.
- **Licitaciones**: pre-filtro barato por keywords en nombre antes de pedir detalle (configurable en `PREFILTER_KEYWORDS`).

## Comportamiento ante MPRateLimitError (429)

El spec original pedía "agenda reintento post-medianoche Chile". El comportamiento actual es:

- **CA incremental**: 429 lanza `MPRateLimitError` → capturado en `sync_incremental` → progreso de páginas anteriores ya está en Postgres (commit por página) → cursor no avanza → el scheduler reintentará en el próximo ciclo (30 min). Si el presupuesto diario ya está agotado, `QuotaTracker` lanzará `QuotaExceededError` en el siguiente ciclo y se saltará hasta el día siguiente en America/Santiago.
- No hay re-agenda explícita a las 00:01 Chile: el scheduler de 30 min es suficiente porque `QuotaTracker` bloquea los requests del mismo día; cuando el contador diario se resetea (nueva fecha en Santiago), el ciclo de las 00:30 ya funciona normalmente.

Esta decisión simplifica el scheduler y es correcta porque la cuota se persiste en Postgres (no en RAM).

## Idempotencia

Todos los jobs usan upsert (`session.get` + `session.add` si nuevo). Re-ejecutar un job nunca duplica ni corrompe datos. El cursor sólo avanza en éxito, por lo que un 429 o crash a mitad deja el progreso parcial y retoma desde el mismo cursor en el próximo ciclo.

## CLI

```bash
# Ejecutar un job una sola vez
python -m app.ingest run-once --job ca
python -m app.ingest run-once --job activas
python -m app.ingest run-once --job detalles
python -m app.ingest run-once --job lifecycle
python -m app.ingest run-once --job catalogos
python -m app.ingest run-once --job retencion

# Iniciar el scheduler (bloqueante, para producción)
python -m app.ingest run-scheduler
```
