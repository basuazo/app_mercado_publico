# Despliegue — mp-oportunidades

Stack gratuito: **Render free** (web service) + **Neon free** (Postgres 0.5 GB).

---

## 0. Prerequisitos

- Cuenta en [neon.tech](https://neon.tech) y [render.com](https://render.com).
- Repositorio en GitHub/GitLab conectado a Render.
- Variables de entorno locales en `.env` (nunca commitear):

```
# Branch dev de Neon → desarrollo local y pytest
DATABASE_URL=postgresql://user:pw@host-dev.neon.host/neondb?sslmode=require

# Branch production de Neon → solo referencia; la usa Render
DATABASE_URL_PROD=postgresql://user:pw@host-prod.neon.host/neondb?sslmode=require

MP_TICKET=...
SECRET_KEY=...           # cadena aleatoria ≥ 32 chars (python -c "import secrets; print(secrets.token_hex(32))")
JOBS_TOKEN=...           # idem
ADMIN_EMAIL=admin@tuempresa.cl
ADMIN_PASSWORD=...       # contraseña segura; solo usada en el seed inicial
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM=alertas@tuempresa.cl
```

---

## 1. Neon — configuración de branches

Neon crea automáticamente la branch `main` (o `production`) al crear el proyecto.

```
production  ← branch principal; apunta Render y DATABASE_URL_PROD local
dev         ← branch para desarrollo local y pytest
```

**Pasos:**
1. En el dashboard de Neon → tu proyecto → **Branches** → **New branch**.
2. Nombre: `dev`, Partir de: `production`, Auto-delete: Never.
3. Copiar la connection string de cada branch con `?sslmode=require`.
4. Pegar en `.env` según la tabla anterior.

> **Regla:** nunca correr `alembic upgrade head` manualmente apuntando a la branch
> `production`. Render lo hace automáticamente en cada deploy.

---

## 2. Render — crear web service

1. **New → Web Service** → conectar repo.
2. **Runtime:** Python 3  
   **Build command:** `pip install -e ".[dev]"`  
   **Start command:** `alembic upgrade head && uvicorn app.api.main:_make_app --factory --host 0.0.0.0 --port $PORT`  
   **Health check path:** `/api/salud/ping`  
   **Plan:** Free
3. En **Environment** → agregar todas las variables de `render.yaml` (los `sync: false` son secretos que debes escribir a mano):

   | Variable | Valor |
   |---|---|
   | `MP_TICKET` | tu ticket de Mercado Público |
   | `DATABASE_URL` | connection string de la branch **production** de Neon |
   | `SECRET_KEY` | cadena aleatoria ≥ 32 chars |
   | `JOBS_TOKEN` | cadena aleatoria ≥ 32 chars |
   | `ADMIN_EMAIL` | email del primer admin |
   | `ADMIN_PASSWORD` | contraseña del primer admin |
   | `SMTP_HOST` | p.ej. `smtp-relay.brevo.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | usuario SMTP |
   | `SMTP_PASSWORD` | contraseña SMTP |
   | `SMTP_FROM` | dirección remitente |
   | `TASA_UF` | `37000` (ajustar mensualmente) |
   | `TASA_UTM` | `65000` |
   | `TASA_USD` | `950` |
   | `TASA_EUR` | `1030` |
   | `DIGEST_HOUR` | `8` |

4. **Deploy** → Render corre `alembic upgrade head` y luego inicia uvicorn.
5. En el primer arranque, el seed crea el usuario admin con `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

---

## 3. Pinger — mantener el servicio vivo

Render free duerme el proceso tras 15 min de inactividad. El scheduler interno muere al dormirse.

**Configurar en [cron-job.org](https://cron-job.org) o [UptimeRobot](https://uptimerobot.com):**

| Tarea | URL | Método | Cabecera | Frecuencia |
|---|---|---|---|---|
| Keepalive | `https://tu-app.onrender.com/api/salud/ping` | GET | — | cada 10 min |
| Job backup | `https://tu-app.onrender.com/api/jobs/run?job=ca` | POST | `X-Jobs-Token: <JOBS_TOKEN>` | cada 1 h |

> **Advertencia TZ:** los crons externos corren en UTC. La ventana nocturna de backfill
> (22:00–07:00) la valida la app internamente con `ZoneInfo("America/Santiago")`.
> No confíes en la hora del cron externo para eso.

---

## 4. Flujo de migraciones

```
Desarrollo local          Render (producción)
─────────────────         ──────────────────────────────────
DATABASE_URL → dev        DATABASE_URL → production
alembic upgrade head      alembic upgrade head   ← en startCommand
  ↓ aplica en dev           ↓ aplica en production automáticamente
```

Para crear una nueva migración:
```bash
alembic revision --autogenerate -m "descripción del cambio"
# editar el archivo generado si es necesario
alembic upgrade head   # aplica en tu branch dev local
# luego push → Render aplica en production
```

---

## 5. Verificación post-deploy

Checklist manual tras el primer deploy:

- [ ] `GET /api/salud/ping` → `{"status": "ok"}` (sin auth).
- [ ] Login con `ADMIN_EMAIL` / `ADMIN_PASSWORD` en `/login`.
- [ ] `POST /api/jobs/run?job=ca` con header `X-Jobs-Token` → `{"queued": true, "job": "ca"}`.
- [ ] Esperar ~2 min → `GET /salud` (admin) → ver `cuota_api.usadas_hoy > 0`.
- [ ] Verificar que `base_datos.porcentaje` < 80 %.
- [ ] Crear un perfil de búsqueda → esperar el próximo ciclo → revisar oportunidades en el dashboard.
- [ ] Confirmar que tras 2 h el servicio **no se durmió** (el pinger debe estar activo).

---

## 6. Smoke test manual (scripts/smoke_test.py)

Ejecutar localmente con `.env` apuntando a la branch `dev`:

```bash
python scripts/smoke_test.py
```

El script imprime cantidad de licitaciones activas, detalle de la primera,
total de resultados de Compra Ágil y cuota restante. No modifica datos.

---

## 7. Respaldo y monitoreo

- **Logs:** Render → tu servicio → Logs (en tiempo real).
- **BD:** Neon → Monitoring → Storage usage (alertar si > 400 MB).
- **Cuota API:** `/salud` → campo `cuota_api.usadas_hoy`; máximo 9 000/día.
- **Correos:** `/salud` → `correos.enviados_hoy`; límite 250/día (Brevo free = 300).
