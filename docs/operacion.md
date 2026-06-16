# Runbook de Operación — mp-oportunidades

## 1. Instalación local

```bash
git clone <repo>
cd mp-oportunidades
python -m venv .venv && source .venv/bin/activate   # o .venv\Scripts\activate en Windows
pip install -e ".[dev]"
cp .env.example .env   # editar con tus credenciales
alembic upgrade head
pytest
```

`.env` mínimo para desarrollo:

```
DATABASE_URL=postgresql+psycopg://user:pw@host-dev.neon.host/neondb?sslmode=require
DATABASE_URL_PROD=postgresql+psycopg://user:pw@host-prod.neon.host/neondb?sslmode=require
MP_TICKET=tu_ticket_aqui
SECRET_KEY=cadena-aleatoria-32-chars-minimo
JOBS_TOKEN=otra-cadena-aleatoria-32-chars
ADMIN_EMAIL=admin@tuempresa.cl
ADMIN_PASSWORD=contraseña-segura
```

> **DATABASE_URL debe usar prefijo `postgresql+psycopg://`** (psycopg3), no `postgresql://`.
> La branch `dev` de Neon se usa localmente y en CI; `production` solo la usa Render.

---

## 2. Rotación del ticket MP_TICKET

El ticket vence periódicamente (ChileCompra no documenta el TTL; en la práctica dura meses).

Síntoma: respuestas 401 de la API v1 o v2.

**Procedimiento:**

1. Ir a [api.mercadopublico.cl](https://api.mercadopublico.cl) → solicitar nuevo ticket.
2. En Render → Environment → `MP_TICKET` → actualizar valor → **Save Changes** → Render redeploya automáticamente.
3. Localmente: actualizar `.env`, reiniciar el proceso.
4. Verificar en `/salud` que `sync_state.licitaciones.ultima_sync` avanza en la próxima corrida.

El ticket **nunca** debe aparecer en logs, código ni commits (el logger lo enmascara).

---

## 3. Rotación de SECRET_KEY

`SECRET_KEY` firma las cookies de sesión. Al rotar:

- **Todas las sesiones activas se invalidan** — todos los usuarios deberán loguearse de nuevo.
- Los tokens CSRF (derivados de SECRET_KEY) también cambian.

**Procedimiento:**

1. Generar nueva clave: `python -c "import secrets; print(secrets.token_hex(32))"`.
2. En Render → Environment → `SECRET_KEY` → actualizar → redeploy.
3. Notificar a los usuarios que deberán iniciar sesión de nuevo.

## 4. Rotación de JOBS_TOKEN

`JOBS_TOKEN` protege el endpoint `POST /api/jobs/run`. No tiene impacto en sesiones de usuario.

**Procedimiento:**

1. Generar nuevo token: `python -c "import secrets; print(secrets.token_hex(32))"`.
2. Actualizar en Render → Environment → `JOBS_TOKEN`.
3. Actualizar el header `X-Jobs-Token` en cualquier cron externo (UptimeRobot, cron-job.org) que llame a `/api/jobs/run`.

---

## 5. Recuperar acceso admin

Si se pierde la contraseña del único admin:

```bash
# Apuntar DATABASE_URL a production en .env temporal (branch production de Neon)
python -c "
from app.core.settings import Settings
from app.api.main import make_engine
from sqlalchemy.orm import Session
from app.models.tables import Usuario
from app.auth.password import hash_password

s = Settings()
e = make_engine(s)
with Session(e) as session:
    u = session.execute(
        __import__('sqlalchemy').select(Usuario).where(Usuario.email == 'admin@tuempresa.cl')
    ).scalar_one()
    u.password_hash = hash_password('nueva-contraseña')
    session.commit()
    print('OK')
"
```

O directamente desde el SQL console de Neon:

```sql
UPDATE usuarios
SET password_hash = crypt('nueva-contraseña', gen_salt('bf'))
WHERE email = 'admin@tuempresa.cl';
```

> Alternativa: eliminar el usuario admin y dejar que el seed lo recree con `ADMIN_PASSWORD` actualizado en Render.

---

## 6. Error 401 persistente de la API

Síntoma: todos los jobs fallan con `MPAuthError` en los logs.

Causa más probable: ticket vencido o revocado.

**Pasos:**

1. Verificar con smoke_test manual: `python scripts/smoke_test.py` (apuntando a branch dev).
2. Si confirma 401: rotar `MP_TICKET` (ver sección 2).
3. Si el smoke_test pasa pero los jobs fallan: revisar logs en Render → puede ser problema de red temporal.

---

## 7. Error 429 — cuota de API agotada

Síntoma: `MPRateLimitError` en logs; `/salud` muestra `cuota_api.usadas_hoy ≥ 9000`.

**Regla crítica:** 429 significa cuota agotada hasta las **00:01 del día calendario siguiente en horario de Chile (America/Santiago)**. Jamás reintentar el mismo día.

La app ya maneja esto automáticamente (aborta el ciclo, no reintenta). El scheduler retomará al día siguiente.

**Si la cuota se agota frecuentemente:**

1. Revisar `/salud` → `cuota_api` para ver distribución de uso.
2. Considerar reducir la frecuencia de backfill o el tamaño de las ventanas.
3. El presupuesto por defecto es 9.000/día (techo: 10.000). No cambiar sin evaluar el impacto.

---

## 8. API de Mercado Público caída (5xx)

La app reintenta automáticamente con back-off exponencial (tenacity: 3 intentos, 2–30 s entre ellos). Si persiste, el job falla con `MPServerError` y el advisory lock se libera.

**No hay acción requerida** — el scheduler retomará en el siguiente intervalo (30 min para CA, cada 5 h para licitaciones activas).

Si la caída dura > 24 h, el cursor de CA puede quedar desactualizado. Al recuperarse, el sync incremental usa el cursor guardado y recupera los cambios perdidos.

---

## 9. Neon suspendida o llena

### Neon suspendida (idle)

La base de datos free se suspende tras 5 minutos de inactividad. El pinger en `/api/salud/ping` la mantiene activa. Si el pinger falla, `_wait_for_db()` en el startup de Render reintenta 5 veces con back-off.

Síntoma: primera request después de idle tarda 2–5 s (wake-up de Neon). Es normal.

### Neon llena (≥ 0.5 GB)

**Medir:** `GET /salud` (admin) → campo `base_datos.porcentaje`. Alertar si > 80 %.

**Qué purgar:**

1. **raw_json** de oportunidades terminales: la retención automática purga terminales > 90 días (`run_retencion` corre diario a las 03:00). Forzar manualmente:

   ```bash
   curl -X POST "https://tu-app.onrender.com/api/jobs/run?job=retencion" \
        -H "X-Jobs-Token: $JOBS_TOKEN"
   ```

2. **Tabla `oportunidades_match`**: registros de perfiles inactivos u obsoletos. Revisar si hay perfiles sin dueño activo.

3. **Tabla `quota_log`**: solo tiene un registro por día; máximo 365 filas al año → negligible.

Si el tamaño sigue creciendo después de purgar, revisar que `raw_json` no se guarda en licitaciones sin match (la regla: raw_json solo en oportunidades con al menos un match).

---

## 10. Pinger caído

**Síntoma:** Render duerme el proceso → sync atrasada. Se detecta porque `/salud` muestra `sync_state.ultima_sync` con > 2 h de antigüedad.

**Remedio:**

1. Verificar en UptimeRobot/cron-job.org que el monitor de `GET /api/salud/ping` está activo y sin errores.
2. Si estaba pausado, reactivarlo. Render despertará el proceso en la próxima request.
3. Verificar que la URL del monitor es correcta (`https://tu-app.onrender.com/api/salud/ping`).

El scheduler interno de APScheduler se detiene cuando Render duerme el proceso. Al despertar, los jobs retoman en el siguiente intervalo programado.

---

## 11. Respaldo y restore de la BD

### Backup desde Neon (branch production)

```bash
pg_dump "$DATABASE_URL_PROD" \
  --no-owner --no-acl \
  -f backup_$(date +%Y%m%d).sql
```

> `DATABASE_URL_PROD` debe tener el prefijo `postgresql://` (pg_dump usa psycopg2/libpq, no psycopg3).

### Restore

```bash
psql "$DATABASE_URL_DEV" < backup_20260615.sql
```

Neon también ofrece **branching instantáneo** desde el dashboard: crear una branch desde un point-in-time de production es el método más seguro para probar un restore.

---

## 12. Deploy y rollback en Render

### Deploy normal

1. Push a `main` → Render detecta el cambio y empieza el build automáticamente.
2. El startCommand ejecuta `alembic upgrade head` antes de iniciar uvicorn.
3. Verificar con el checklist de [despliegue.md](despliegue.md).

### Rollback

```bash
git revert HEAD   # crear commit de reversión
git push origin main
```

Render redeploya con el commit anterior. Si la migración de Alembic era destructiva (DROP COLUMN, etc.), hacer el downgrade antes de revertir el código:

```bash
# apuntando a la branch production
alembic downgrade -1
```

---

## 13. Nota: prefijo `postgresql+psycopg://`

psycopg3 (el driver que usa este proyecto) requiere que `DATABASE_URL` use el prefijo `postgresql+psycopg://` en SQLAlchemy 2. El prefijo `postgresql://` activa el dialecto psycopg2 que no está instalado.

```
# Correcto:
DATABASE_URL=postgresql+psycopg://user:pw@host/neondb?sslmode=require

# Incorrecto (activa psycopg2 → ModuleNotFoundError):
DATABASE_URL=postgresql://user:pw@host/neondb?sslmode=require
```

El archivo `render.yaml` ya define el prefijo correcto en los ejemplos de `docs/despliegue.md`.
