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

## 7. Error 429

Dos casos distintos (verificado el 22-sep; ver `01-analisis-api-mercado-publico.md` §10):

- **429 con `Codigo: 10500` = peticiones simultáneas** (concurrencia, no cuota). La app reintenta
  hasta 3 veces con 30/60/120 s y luego corta el job; el siguiente disparo corre normal.
- **Cualquier otro 429 = tope diario.** No reintentar hasta el cambio de día calendario en
  `America/Santiago`. Ojo: hoy ese bloqueo no se persiste entre corridas (auditoría 14, R1).

Revisar `/salud` → `cuota_api` (presupuesto local 9.000/día). Máximo medido: ~3.600/día.

---

## 8. API de Mercado Público caída (5xx, 504)

Tras un 504 o un timeout la app enfría 60 s antes de la siguiente request. `detalles-match` no
reintenta: cuenta el fallo en la oportunidad y la pone en espera tras varios fallos. El siguiente
disparo de Actions retoma solo; el cursor de CA recupera lo perdido por ventanas.

---

## 9. Neon suspendida o llena

### Neon suspendida (idle)

La base de datos free se suspende tras 5 minutos de inactividad. El pinger de `/api/salud/ping` **no** la mantiene activa, y no debe hacerlo: ese endpoint devuelve un dict literal y no toca la base. Lo único que mantiene despierto es el proceso web de Render.

> ⚠️ **No agregar una consulta a la base a `/api/salud/ping`.** Un ping que consultara Postgres cada pocos minutos mantendría Neon despierta 24/7 y agotaría las CU-horas del plan free. Que Neon se suspenda por idle es el comportamiento deseado: la despierta la primera consulta real, y si eso ocurre durante un arranque, `_wait_for_db()` en el startup de Render reintenta 5 veces con back-off.

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

## 10. Jobs atrasados o sin correr

Ya no hay pinger: los jobs corren en GitHub Actions disparados por cron-job.org.

1. `/salud` o `GET /api/salud/jobs` → ¿qué job está viejo?
2. GitHub → Actions: ¿hubo corrida? Si no, revisar cron-job.org (historial y notificaciones) y
   que el PAT no haya vencido (401). Ver `operacion-disparos.md`.
3. Si hubo corrida y falló: ver el log; para correrlo a mano, **Run workflow** en Actions o el CLI
   local (`02-arquitectura-y-operacion.md` §5).

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

---

## 14. Jobs y cadencia

La cadencia real y la composición de cada workflow están en `02-arquitectura-y-operacion.md` §2 y
en `operacion-disparos.md`. Todo job toma el mismo `pg_advisory_lock`; el CLI **espera** el lock
(`--esperar-lock-min`) en vez de omitir. `nocturno` es el único camino al backfill y valida por sí
mismo la ventana 22:00–07:00 de Chile. El scheduler en proceso y `POST /api/jobs/run` existen pero
no se usan (limpieza pendiente).
