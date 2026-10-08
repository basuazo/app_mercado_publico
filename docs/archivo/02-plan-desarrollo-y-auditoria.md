# Plan de desarrollo y auditoría — App de oportunidades en Mercado Público
## v2 — Despliegue 100 % gratuito (Render + Neon) · Equipo pequeño (3–10 usuarios)

> Plan pensado para ejecutarse con **Claude Code**, en fases pequeñas y verificables, cada una con su criterio de aceptación ("Definition of Done") y su checklist de auditoría. Los prompts exactos están en `03-prompts-claude-code.md`.
>
> **Cambios v2 respecto del plan original**: Postgres (Neon) desde el día uno en lugar de SQLite; full-text search nativo de Postgres en lugar de FTS5; política de retención de datos por el límite de 0.5 GB; autenticación liviana multiusuario (3–10 cuentas); perfiles y alertas por dueño; nueva fase F6.5 de despliegue en Render con keep-alive externo.

---

## 1. Definición del producto (MVP)

**Objetivo**: aplicación que consume la API oficial de Mercado Público, mantiene una base de datos en Neon con licitaciones y Compras Ágiles, permite a cada usuario definir **perfiles de búsqueda** (keywords, región, monto, organismo) y genera **alertas por email al dueño del perfil**, con un dashboard web protegido por login.

**Usuarios**: 3–10 cuentas creadas por el administrador (sin registro abierto). Roles: `admin` (gestiona usuarios y ve panel de salud) y `usuario` (gestiona sus perfiles, ve oportunidades).

**Fuera de alcance del MVP**: postulación automática, análisis predictivo, registro self-service, OAuth/SSO, scraping de HTML.

**Restricción rectora**: costo de operación = **$0**. Toda decisión técnica se subordina a los límites de las capas gratuitas (sección 3).

## 2. Stack (v2)

| Capa | Tecnología | Justificación |
|---|---|---|
| Lenguaje | Python 3.11+ | Ecosistema de datos, rápido de auditar |
| Cliente API | `httpx` + `tenacity` | Manejo robusto de 429/timeouts |
| **BD** | **PostgreSQL en Neon (free)** | Render free no tiene disco persistente → SQLite descartado. Neon: 0.5 GB, autosuspensión a los 5 min (cold start ~1 s, aceptable) |
| ORM/Migraciones | SQLAlchemy 2.x + Alembic | Esquema versionado, auditable |
| Búsqueda de texto | **Postgres FTS (`tsvector` config `spanish`) + extensión `unaccent`** | Mejor que FTS5 para español (tildes, stemming); disponible en Neon |
| Scheduler | APScheduler **dentro del web service** + pinger externo | Render free duerme a los 15 min sin tráfico; el pinger lo mantiene despierto (sección 3) |
| Backend/UI | FastAPI + Jinja2/HTMX | Server-rendered, liviano para 512 MB RAM |
| **Auth** | Sesión por cookie firmada + `bcrypt` (passlib) | Suficiente y auditable para ≤10 usuarios |
| Alertas | SMTP vía **Brevo free** (300 correos/día) o Gmail app password | Sobra para alertas de ≤10 usuarios |
| Hosting | **Render web service (free)** | 750 h/mes = 24/7 para 1 servicio |
| Keep-alive / cron | **cron-job.org o UptimeRobot (free)** | Ping a /api/salud cada 10 min + disparo de jobs |
| Config/secretos | Env vars de Render + `.env` local | El ticket jamás en código |
| Tests | pytest + respx | No quemar cuota en tests |
| Calidad | ruff, mypy, pre-commit | Apoya la auditoría |

## 3. Restricciones de capa gratuita y cómo se resuelven

| Límite | Valor | Mitigación de diseño |
|---|---|---|
| Render: sin disco persistente | filesystem efímero | Todo estado en Neon (incl. `sync_state` y contador de cuota). Idempotencia total: un reinicio a mitad de ciclo no corrompe nada |
| Render: duerme a los 15 min sin tráfico | mata el APScheduler | Pinger externo gratuito a `/api/salud` cada 10 min. Respaldo: endpoint `POST /api/jobs/run` protegido por token propio (`JOBS_TOKEN`), invocable desde cron-job.org |
| Render: 512 MB RAM | OOM si se parsea JSON gigante | Paginación a 50 en v2; procesamiento por lotes en v1; nunca cargar un día completo en memoria de una vez |
| Neon: 0.5 GB | se llena con `raw_json` | **Política de retención** (sección 5, F2): raw_json solo de oportunidades con match; purga mensual de terminales >90 días |
| Neon: autosuspensión 5 min | cold start ~1 s | Pool con `pool_pre_ping=True` y reintento de conexión; imperceptible para usuarios |
| Brevo: 300 correos/día | — | Digest por usuario; con 10 usuarios el peor caso es ~30/día |
| GitHub Actions (si se usa como cron alterno) | 2.000 min/mes en repo privado | Solo como respaldo del pinger; frecuencia ≥2 h si se usa |
| API ChileCompra: 10.000 req/día | compartida por 1 ticket | **El nº de usuarios no afecta la cuota**: el dashboard lee solo de Neon; la API la consume únicamente la ingesta |
| Ventana nocturna backfill (22:00–07:00 Chile) | hora de Chile, no UTC | El orquestador valida con `zoneinfo("America/Santiago")` en código; nunca confiar solo en la expresión cron (UTC) |

## 4. Arquitectura (módulos)

```
app/
├── clients/          # mp_v1.py · mp_v2.py · base.py (rate limiter, retries, cuota)
├── models/           # SQLAlchemy: usuarios, licitaciones, compras_agiles, ordenes_compra,
│                     # organismos, items/productos, perfiles_busqueda (con owner),
│                     # oportunidades_match, alertas, sync_state
├── ingest/           # jobs idempotentes + orquestador con validación de TZ Chile
├── matching/         # FTS Postgres + scoring; corre por perfil/dueño
├── alerts/           # detección, deduplicación, digest por usuario, envío SMTP
├── auth/             # login/logout, sesiones, hashing, dependencia require_user/require_admin
├── api/              # FastAPI: dashboard protegido + /api + /api/jobs/run (token)
├── core/             # settings, logging (enmascara ticket), fechas, montos→CLP, retención
└── tests/
```

Principios rectores (sin cambios): **capa anti-corrupción** en `clients/`; estado externo mapeado a enums propios; jobs idempotentes; cursores en `sync_state`.

Nuevo principio v2: **el proceso web es desechable** — puede morir y reiniciarse en cualquier momento (Render free lo hará); ningún estado vive en memoria ni en disco local.

---

## 5. Fases de desarrollo (código)

Cada fase = una sesión de Claude Code = un commit/PR auditable.

### F0 — Bootstrap del proyecto
Estructura, `pyproject.toml`, `.env.example` (`MP_TICKET`, `DATABASE_URL` → Neon, `SECRET_KEY`, `JOBS_TOKEN`, `SMTP_*`, tasas de cambio), `.gitignore`, pre-commit, `CLAUDE.md`, README.
**DoD**: linters y pytest corren; settings falla si faltan secretos obligatorios; repo git inicializado.

### F1 — Cliente API v1 + v2
Sin cambios respecto del plan original (clientes tipados, rate limiter, QuotaTracker, excepciones, retries, ticket enmascarado en logs), **salvo**: el contador de cuota persiste en Neon (tabla `sync_state`/`quota_log`), no en archivo local.
**DoD**: tests mockeados (éxito/401/429/malformado/paginación); `scripts/smoke_test.py` para ejecución manual.

### F2 — Modelo de datos, FTS y retención
Tablas del plan original **más**:
- `usuarios(id, email UNIQUE, password_hash, rol admin|usuario, activo, creado_en)`.
- `perfiles_busqueda.owner_id FK usuarios` y `alertas` ligadas al dueño.
- Columnas generadas `tsvector` (config `spanish`, con `unaccent`) sobre nombre+descripción+productos, con índice GIN.
- `raw_json` **nullable** + regla de negocio: solo se persiste cuando la oportunidad tiene match.
- `core/retencion.py`: purga mensual de oportunidades en estado terminal con >90 días (conserva campos estructurados, elimina raw_json e items); job `vacuum`/métrica de tamaño de BD para el panel de salud.
- Seeds: regiones, tipos, estados + **usuario admin inicial** desde variables de entorno (`ADMIN_EMAIL`, `ADMIN_PASSWORD` solo para el seed).
**DoD**: `alembic upgrade head` contra Neon desde cero; FTS encuentra "electricos" en "eléctricos"; test de retención.

### F3 — Ingesta y sincronización
Igual al plan original (sync activas, sync incremental CA con cursor, detalles con presupuesto, lifecycle, catálogos, CLI) con dos refuerzos v2:
- Validación de ventana nocturna con `ZoneInfo("America/Santiago")` inyectable en tests.
- Procesamiento por lotes (commit cada N registros) para no exceder 512 MB.
**DoD**: idempotencia probada; corte limpio por 429 sin perder progreso; cursor avanza solo en éxito; presupuesto respetado.

### F4 — Motor de búsqueda y scoring
Igual al original pero: matching vía FTS de Postgres (`websearch_to_tsquery` + `unaccent`), ejecutado **por perfil de cada usuario activo**; `oportunidades_match` conserva el `perfil_id` (y por tanto el dueño). Al crear un match se persiste el `raw_json` de la oportunidad (regla de retención).
**DoD**: dataset sintético con matches/no-matches esperados (tildes, exclusiones, montos, regiones) en tests.

### F5 — Alertas por usuario
Eventos: nuevo match, cambio de estado, cierre ≤48 h. Deduplicación por (match, tipo). **Digest por usuario** (un correo agrupa todos sus perfiles), frecuencia configurable por perfil (inmediata | digest diario). Plantillas con enlace a la ficha pública y atribución a ChileCompra. Presupuesto: nunca más de 250 correos/día (margen sobre Brevo); si se excede, prioriza inmediatas y pospone digests.
**DoD**: SMTP falso en tests; sin duplicados; el correo va solo al dueño del perfil.

### F6 — Autenticación + dashboard + API
- `auth/`: login (email+contraseña, bcrypt), cookie de sesión firmada (`SECRET_KEY`), expiración 7 días, logout, rate limit básico de intentos (5/15 min por IP), dependencias `require_user` / `require_admin`.
- Dashboard (todo tras login): oportunidades vigentes por score con filtros; detalle; **mis perfiles** (cada usuario solo ve/edita los suyos; admin ve todos); panel `/salud` (solo admin): última sync, cursor, cuota usada/presupuesto, tamaño BD, errores, correos enviados hoy.
- Admin: CRUD de usuarios (crear, desactivar, resetear contraseña).
- `/api/*` protegido por la misma sesión; `POST /api/jobs/run?job=...` protegido por header `X-Jobs-Token` (para el cron externo), nunca por sesión.
- Footer con atribución en todas las páginas.
**DoD**: sin sesión → redirect a login; usuario no accede a perfiles ajenos ni a /salud; tests de autorización.

### F6.5 — Despliegue en Render + Neon (nueva)
- `render.yaml` (web service free, `uvicorn`, health check `/api/salud/ping` sin auth que solo responde ok), env vars documentadas.
- Conexión a Neon con `sslmode=require`, `pool_pre_ping`, pool pequeño (≤5).
- Arranque: `alembic upgrade head` automático + seed de admin si no existe.
- APScheduler arranca con la app; guard para que **solo una instancia** ejecute jobs (lock en BD con `pg_advisory_lock`) por si Render levanta dos procesos durante un deploy.
- Configuración del pinger (cron-job.org/UptimeRobot): ping cada 10 min a `/api/salud/ping`; cron adicional que llama `POST /api/jobs/run` como respaldo.
- Docs: `docs/despliegue.md` paso a paso (crear proyecto Neon, rama dev/main, crear servicio Render, variables, pinger, verificación).
**DoD**: app desplegada y accesible; una sync real completa corre en Render; reinicio del servicio no duplica datos; el servicio no se durmió en una ventana de prueba de 2 h.

### F7 — Endurecimiento y entrega
Cobertura ≥80 % en `clients/`, `ingest/`, `matching/`, `auth/`; runbook de operación (incluye: rotar ticket, rotar SECRET_KEY, recuperar admin, qué pasa si Neon se llena, monitoreo del pinger); `docs/arquitectura.md` con diagrama; `pip-audit`; CHANGELOG; tag v0.1.0.

---

## 6. Plan de auditoría por fase

Misma mecánica del plan original: al cerrar cada fase, **sesión de auditoría en conversación nueva** → informe `audits/AUDIT-F{n}.md` con hallazgos Crítico/Alto/Medio/Bajo y remediación antes de continuar.

Checklist transversal (todas las fases):
1. **Secretos**: `MP_TICKET`, `SECRET_KEY`, `JOBS_TOKEN`, `ADMIN_PASSWORD`, `DATABASE_URL` solo en entorno; ausentes de código, logs, tests e historial git.
2. **Cumplimiento ToS ChileCompra**: rate limit activo; cuota contabilizada en BD; backfill solo en ventana nocturna validada con TZ Chile; atribución visible.
3. **Robustez**: 401/429/5xx/timeouts/JSON inválido; parseo defensivo; reconexión a Neon tras autosuspensión.
4. **Idempotencia y proceso desechable**: matar el proceso en cualquier punto no corrompe datos ni pierde cursores.
5. **Calidad**: ruff/mypy limpios; tests de la fase pasan.
6. **Presupuestos free tier**: requests API < 9.000/día; correos < 250/día; crecimiento de BD proyectado < 0.5 GB/año con retención activa.

Checks específicos por fase:
| Fase | Focos adicionales |
|---|---|
| F0 | `.gitignore` cubre `.env`; CLAUDE.md refleja reglas v2 |
| F1 | Ticket nunca en logs; 429 espera cambio de día calendario; cuota persistida en BD (no en disco local) |
| F2 | Migraciones reversibles; FTS con unaccent probado; retención no borra oportunidades vigentes ni con match activo; hash bcrypt con costo adecuado |
| F3 | Peor caso diario < 9.000 requests; lotes acotados en memoria; validación TZ inyectable y testeada |
| F4 | Sin falsos negativos en dataset; raw_json solo en matches; queries FTS parametrizadas |
| F5 | Correo solo al dueño; tope diario de correos; sin datos sensibles innecesarios |
| F6 | **Autorización**: IDOR (acceder a perfil ajeno por id), sesión expira, cookies `HttpOnly/Secure/SameSite`, rate limit de login, /salud solo admin, JOBS_TOKEN no adivinable ni logueado |
| F6.5 | Advisory lock evita doble scheduler; health check no expone datos; env vars completas en Render; sslmode=require |
| F7 | Auditoría final completa (sección 7) |

## 7. Auditoría final (app completa) — 4 sesiones

**A1 — Seguridad** (ampliada v2): secretos en repo/historial/imagen; dependencias (`pip-audit`); superficie web: inyección SQL, XSS, **IDOR entre usuarios**, fijación de sesión, CSRF en formularios de mutación, fuerza bruta de login, exposición en mensajes de error; cookies y headers; `JOBS_TOKEN`.

**A2 — Cumplimiento e integridad de datos**: obligaciones ToS verificadas en código con evidencia; validación cruzada de registros BD vs API en vivo (máx. 30 requests, con autorización previa del humano); normalizaciones (monedas, fechas TZ, estados, gotchas); coherencia de cursores; **proyección de almacenamiento vs 0.5 GB**.

**A3 — Calidad de código y tests**: capa anti-corrupción respetada; cobertura real (no inflada); casos borde; deuda; top-5 refactors.

**A4 — Operación (game day)** (ampliada v2): ticket inválido; 429 a mitad de sync; API caída; JSON malformado; **Neon suspendida/reconexión**; **reinicio de Render a mitad de ciclo**; **pinger caído 24 h** (¿qué se pierde y cómo se recupera?); **dos instancias simultáneas** (advisory lock); BD llena al 90 %. Verificar runbook contra cada escenario.

Entregable: `audits/AUDIT-FINAL.md` + matriz de riesgos con estado de mitigación.

## 8. Estimación de sesiones

| Sesión | Contenido |
|---|---|
| 1 | F0 + auditoría F0 (rápida) |
| 2 | F1 |
| 3 | Auditoría F1 + F2 |
| 4 | Auditoría F2 + F3 |
| 5 | Auditoría F3 + F4 |
| 6 | Auditoría F4 + F5 |
| 7 | F6 + auditoría F5/F6 |
| 8 | F6.5 (despliegue) + auditoría F6.5 |
| 9 | F7 + auditorías finales A1–A4 |

Regla operativa: una fase por conversación de Claude Code, commit al cerrar, auditoría en conversación limpia.
