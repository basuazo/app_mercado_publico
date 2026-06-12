# CLAUDE.md — mp-oportunidades

App de búsqueda de oportunidades en compras públicas chilenas (API oficial de
Mercado Público / ChileCompra) para un equipo de 3–10 usuarios. Flujo: ingesta →
Postgres (Neon) → perfiles por usuario → matching con score → alertas email →
dashboard con login. Costo de operación: $0 (Render free + Neon free).

## Documentos de referencia (leer antes de tocar código)
- docs/01-analisis-api-mercado-publico.md  ← contrato y gotchas de la API
- docs/02-plan-desarrollo-y-auditoria.md   ← fases F0–F7, free tier, auditoría

## Stack
Python 3.11+, httpx+tenacity, SQLAlchemy 2 + Alembic sobre Postgres (Neon),
FTS nativo (tsvector spanish + unaccent), APScheduler, FastAPI + Jinja2/HTMX,
passlib[bcrypt] + cookies firmadas, pytest + respx, ruff + mypy + pre-commit.

## API de Mercado Público — reglas duras (NO negociables)
1. MP_TICKET solo en variable de entorno. Nunca en código, tests, fixtures,
   logs ni commits. El logger enmascara ticket, SECRET_KEY y JOBS_TOKEN.
2. v1 (api.mercadopublico.cl/servicios/v1/): ticket por query param; fechas ddmmaaaa.
   v2 (api2.mercadopublico.cl): ticket por HEADER "ticket"; ISO-8601;
   envelope {success, payload, errors}; paginación máx 50.
3. Cuota 10.000 req/día; presupuesto local 9.000, contado y PERSISTIDO EN POSTGRES
   (el disco de Render es efímero). 429 = agotado hasta el cambio de DÍA CALENDARIO
   en America/Santiago; jamás reintentar un 429 el mismo día.
4. Rate limit propio 1 req/s con jitter. Sin paralelismo agresivo.
5. Backfills masivos SOLO entre 22:00 y 07:00 hora de Chile, validado en código
   con ZoneInfo("America/Santiago") (los crons externos corren en UTC: no confiar en ellos).
6. Parseo SIEMPRE defensivo: binarios v1 inconsistentes; en v2 codigo_orden_compra
   es null aunque exista OC (usar id_orden_compra); slugs de estado de OC con
   erratas oficiales (usar tal cual); tipologías obsoletas; desconocido → enum
   DESCONOCIDO + log, nunca romper la ingesta.
7. Compra Ágil NO filtra por organismo: filtrar por region y luego localmente.
8. Toda publicación de datos lleva "Fuente: Dirección ChileCompra".
9. Prohibido scrapear HTML de mercadopublico.cl; solo la API oficial.

## Free tier — reglas duras
10. Cero estado en memoria o disco local que importe: el proceso es DESECHABLE
    (Render lo duerme/reinicia). Cursores, cuota y locks viven en Postgres.
11. Neon = 0.5 GB: raw_json SOLO en oportunidades con match; retención purga
    terminales >90 días; /salud muestra tamaño de BD.
12. Render = 512 MB RAM: ingesta por lotes, nunca un día completo en memoria.
13. Cada ciclo de ingesta toma pg_advisory_lock (deploys levantan 2 instancias).
14. Correos ≤250/día (Brevo free = 300); digest por usuario.
15. Conexión a Neon: sslmode=require, pool_pre_ping=True, pool_size≤5.

## Multiusuario (3–10 cuentas) — reglas
16. Sin registro abierto: el admin crea usuarios. Roles: admin | usuario.
17. Ownership SIEMPRE verificado en servidor: un usuario solo ve/edita sus
    perfiles y solo recibe alertas de sus perfiles. /salud y /admin: solo admin.
18. Cookies de sesión: firmadas, HttpOnly, Secure, SameSite=Lax. CSRF en mutaciones.
19. POST /api/jobs/run solo con header X-Jobs-Token (compare_digest);
    GET /api/salud/ping es público y no expone nada.

## Arquitectura — reglas
- Capa anti-corrupción: solo app/clients conoce httpx y las URLs/formatos de la API.
- Solo app/models define esquema. Queries 100 % parametrizadas (ORM/FTS incluido).
- Jobs idempotentes; re-ejecutar nunca duplica ni corrompe.

## Flujo de trabajo
- Una fase (F0–F7, incl. F6.5 despliegue) por sesión/commit. Antes de cerrar:
  ruff check, mypy, pytest.
- Tests de red SIEMPRE mockeados (respx). Llamadas reales solo en
  scripts/smoke_test.py y solo las ejecuta el humano.
- Commits en español con prefijo de fase: "F3: ingesta incremental de Compra Ágil".
- No agregar dependencias fuera del stack sin proponer y justificar primero.
```