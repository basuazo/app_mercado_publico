# mp-oportunidades

Alertas de licitaciones y Compras Ágiles del sistema de compras públicas chilenas (Mercado Público / ChileCompra) para equipos de 3–10 usuarios. Costo de operación: **$0** (Render free + Neon free).

Fuente: Dirección ChileCompra

---

## Funcionalidades

- Ingesta incremental de licitaciones activas y Compras Ágiles (API oficial de Mercado Público).
- Matching con score por perfil de búsqueda: keywords, región, rango de monto.
- Alertas por email (inmediatas y digest diario) vía SMTP (Brevo free).
- Dashboard web con login por invitación (sin registro abierto).
- API REST para administración de perfiles y ejecución manual de jobs.

---

## Quickstart local

### Requisitos

- Python 3.11+
- Cuenta en [neon.tech](https://neon.tech) (Postgres gratuito)
- Ticket de API de Mercado Público ([api.mercadopublico.cl](https://api.mercadopublico.cl))

### Instalación

```bash
git clone <repo>
cd mp-oportunidades
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### Variables de entorno

Crear `.env` en la raíz del proyecto:

```env
# Branch DEV de Neon (para desarrollo local y pytest)
# IMPORTANTE: usar prefijo postgresql+psycopg:// (psycopg3), NO postgresql://
DATABASE_URL=postgresql+psycopg://user:pw@ep-xxx-dev.neon.host/neondb?sslmode=require

# Branch PRODUCTION de Neon (referencia para protección en tests)
DATABASE_URL_PROD=postgresql+psycopg://user:pw@ep-xxx-prod.neon.host/neondb?sslmode=require

MP_TICKET=tu_ticket_de_mercado_publico
SECRET_KEY=genera-con-python-c-import-secrets-print-secrets-token-hex-32
JOBS_TOKEN=otra-cadena-de-32-chars-aleatorios
ADMIN_EMAIL=admin@tuempresa.cl
ADMIN_PASSWORD=contraseña-segura-del-admin-inicial

# SMTP (Brevo recomendado, plan free = 300 correos/día)
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USER=tu_usuario_brevo
SMTP_PASSWORD=tu_clave_smtp_brevo
SMTP_FROM=alertas@tuempresa.cl
```

> **Nota:** `DATABASE_URL` debe usar la **branch `dev` de Neon** para desarrollo local. La branch `production` la usa solo Render. La suite de tests falla si ambas URLs son idénticas.

### Ejecutar

```bash
alembic upgrade head          # aplica migraciones en branch dev
uvicorn app.api.main:_make_app --factory --reload
```

La app queda disponible en `http://localhost:8000`. Login con `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

### Tests

```bash
pytest                        # todos los tests (SQLite en memoria)
pytest --cov=app              # con cobertura (actual: 87%)
```

Los tests usan SQLite en memoria y nunca llaman a la API real. Los tests que requieren Postgres se marcan con `@needs_postgres` y se omiten si `DATABASE_URL` no apunta a Postgres.

---

## Quickstart producción (Render + Neon)

Ver instrucciones completas en [docs/despliegue.md](docs/despliegue.md).

Resumen:

1. En Neon: crear proyecto → branches `production` (por defecto) y `dev`.
2. En Render: **New → Web Service** → conectar repo:
   - Start command: `alembic upgrade head && uvicorn app.api.main:_make_app --factory --host 0.0.0.0 --port $PORT`
   - Variables de entorno: igual que `.env` pero `DATABASE_URL` apunta a la branch **production** de Neon (con prefijo `postgresql+psycopg://`).
3. Configurar pinger en UptimeRobot o cron-job.org → `GET /api/salud/ping` cada 10 min.

---

## Documentación

| Documento | Contenido |
|---|---|
| [docs/01-analisis-api-mercado-publico.md](docs/01-analisis-api-mercado-publico.md) | Contrato y gotchas de la API |
| [docs/02-plan-desarrollo-y-auditoria.md](docs/02-plan-desarrollo-y-auditoria.md) | Fases F0–F7, decisiones de arquitectura |
| [docs/despliegue.md](docs/despliegue.md) | Guía paso a paso de despliegue en Render + Neon |
| [docs/operacion.md](docs/operacion.md) | Runbook: rotación de credenciales, incidentes, backup |
| [docs/arquitectura.md](docs/arquitectura.md) | Diagrama de módulos, decisiones y limitaciones |

---

## Stack

| Capa | Tecnología |
|---|---|
| HTTP cliente | httpx + tenacity |
| Base de datos | PostgreSQL (Neon) con SQLAlchemy 2 + Alembic |
| FTS | tsvector spanish + unaccent (nativo Postgres) |
| Scheduler | APScheduler (BackgroundScheduler en FastAPI) |
| Web | FastAPI + Jinja2 + HTMX + Bootstrap 5 |
| Auth | itsdangerous (cookies firmadas) + passlib bcrypt |
| Tests | pytest + respx + freezegun |
| Linting | ruff + mypy + pre-commit |

---

## Seguridad de auditoría (pip-audit)

Ejecutado el 2026-06-16. **Sin vulnerabilidades en deps directas o transitivas del proyecto.**

Vulnerabilidades diferidas (paquetes del entorno del sistema, no deps del proyecto):

| Paquete | Requerido por | Acción |
|---|---|---|
| `cryptography` | `google-auth` (herramienta del sistema) | Diferido — no es dep del proyecto |
| `pillow`, `fonttools` | `matplotlib` (herramienta del sistema) | Diferido — no es dep del proyecto |
| `pip`, `uv` | herramientas del sistema | Diferido — actualizar con `pip install --upgrade pip` |
| `requests`, `urllib3` | `pip-audit` y herramientas del sistema | Diferido — no es dep directa del proyecto |

Acción tomada: se añadió `idna>=3.15` como dep directa (transitiva de httpx) para fijar la versión corregida del CVE-2026-45409.

---

## CHANGELOG

### v0.1.0 (2026-06-16)

- **F0**: configuración base, settings, logging con enmascaramiento de secretos.
- **F1**: clientes API v1 (licitaciones, OC, proveedores) y v2 (Compra Ágil) con rate limiter, quota persistida en Postgres y retry con tenacity.
- **F2**: modelos SQLAlchemy (Licitacion, CompraAgil, PerfilBusqueda, OportunidadMatch, Usuario, Organismo, SyncState), migraciones Alembic, FTS con tsvector spanish.
- **F3**: ingesta incremental de licitaciones activas y Compra Ágil con cursor, lifecycle de estados, catálogos, orquestador con pg_advisory_lock y APScheduler, retención de datos.
- **F4**: motor de matching con score texto/urgencia/competencia, FTS nativo, filtros de región y monto, CRUD de perfiles con ownership.
- **F5**: detección de eventos (nuevo match, cambio de estado, recordatorios) y envío de alertas por email (inmediatas y digest diario, ≤250/día).
- **F6**: autenticación con cookies firmadas (HttpOnly, Secure, SameSite=Lax), rate limiting de login, CSRF HMAC-SHA256, dashboard HTML con Jinja2+HTMX+Bootstrap 5, API REST con ownership verificado.
- **F6.5**: despliegue en Render free + Neon free: `render.yaml`, `make_engine` con parámetros de pool para Neon, lifespan con BackgroundScheduler, seed idempotente del admin, `_make_app()` factory para uvicorn `--factory`, docs de despliegue.
- **F7 (cierre)**: cobertura ≥80 % en módulos críticos (87 % total), runbook de operación, diagrama de arquitectura, pip-audit, README final, tag v0.1.0.
