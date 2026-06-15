# Prompts para Claude Code — Desarrollo y Auditoría
## v2 — Render + Neon (100 % gratis) · 3–10 usuarios con login

> Copia cada prompt en una **conversación nueva** de Claude Code (`claude` en la raíz del repo). El `CLAUDE.md` (plantilla al final) debe estar en la raíz antes de F0: Claude Code lo lee automáticamente, por eso los prompts no repiten todo el contexto.
>
> Convención: una fase por sesión → revisar diff → tests → commit → auditar en conversación limpia.

---

## PARTE 1 — PROMPTS DE DESARROLLO

### Prompt F0 — Bootstrap

```
Inicializa el proyecto "mp-oportunidades" según el CLAUDE.md de este repo.

1. Estructura: app/{clients,models,ingest,matching,alerts,auth,api,core}, tests/, scripts/, audits/, docs/.
2. pyproject.toml con Python 3.11+; deps: httpx, tenacity, sqlalchemy>=2, alembic, psycopg[binary], apscheduler, fastapi, uvicorn, jinja2, pydantic-settings, python-dotenv, passlib[bcrypt], itsdangerous; dev: pytest, pytest-cov, respx, ruff, mypy, pre-commit, freezegun.
3. app/core/settings.py (pydantic-settings): MP_TICKET (secreto, obligatorio), DATABASE_URL (Postgres/Neon, obligatorio; en local puede apuntar a la rama dev de Neon), SECRET_KEY (sesiones, obligatorio), JOBS_TOKEN (obligatorio), RATE_LIMIT_RPS=1.0, presupuesto API diario=9000, tope correos diario=250, SMTP_* opcionales, ADMIN_EMAIL/ADMIN_PASSWORD (solo seed), tasas de cambio UF/UTM/USD/EUR configurables. .env.example documentado; .env y data/ en .gitignore.
4. pre-commit con ruff (lint+format) y mypy.
5. app/core/logging.py: logging estructurado; filtro que enmascara los valores de MP_TICKET, SECRET_KEY y JOBS_TOKEN en cualquier mensaje.
6. README breve: qué es, instalación, configuración de Neon (crear proyecto, ramas main/dev, copiar DATABASE_URL con sslmode=require) y del ticket.
7. Test: Settings falla si falta cualquier secreto obligatorio.

No implementes clientes de API todavía. Al terminar: ruff, mypy, pytest, árbol del proyecto y commit inicial.
```

### Prompt F1 — Clientes de API

```
Implementa los clientes de la API de Mercado Público en app/clients/, según las reglas duras del CLAUDE.md.

1. app/clients/base.py:
   - RateLimiter (token bucket, RATE_LIMIT_RPS, con jitter).
   - QuotaTracker: contador de requests por día calendario PERSISTIDO EN POSTGRES (tabla quota_log o columna en sync_state; recuerda que el filesystem de Render es efímero). Expone remaining(); lanza QuotaExceededError si la siguiente request superaría el presupuesto local (settings, default 9000).
   - Excepciones: MPAuthError (401), MPRateLimitError (429, con retry_after_seconds calculado hasta las 00:01 del día siguiente en America/Santiago), MPServerError, MPParseError.
   - Retries con tenacity: solo 5xx y timeouts, backoff exponencial, máx 3 intentos. 401 y 429 NO se reintentan.

2. app/clients/mp_v1.py — MercadoPublicoV1Client (base https://api.mercadopublico.cl/servicios/v1/, ticket como query param):
   - licitaciones_por_fecha(fecha: date, estado=None, codigo_organismo=None, codigo_proveedor=None)
   - licitaciones_activas()
   - licitacion_detalle(codigo)
   - ordenes_por_fecha(...), orden_detalle(codigo)
   - buscar_proveedor(rut)  # RUT con puntos, guión y DV
   - listar_compradores()
   date→ddmmaaaa interno. Valida envelope (Cantidad, Listado). Retorna dataclasses/TypedDicts. Parseo defensivo: binarios pueden venir 0/1/2/"NO"/null.

3. app/clients/mp_v2.py — MercadoPublicoV2Client (base https://api2.mercadopublico.cl, ticket como HEADER "ticket"):
   - listar_compra_agil(cambio_desde=None, ttl_cambio_ms=None, publicado_desde=None, publicado_hasta=None, estados=None, regiones=None, q=None, tamano_pagina=50, numero_pagina=1, ordenar_por=None) con validación de exclusiones mutuas (ttl vs rango; id vs q).
   - iterar_compra_agil(...): generador que pagina con payload.paginacion.
   - detalle_compra_agil(codigo).
   Valida success == "OK"; normaliza errors[] a excepciones.

4. Tests con respx: éxito v1 y v2, 401, 429 (retry_after hasta cambio de día en TZ Chile, congela reloj con freezegun), 5xx con retry, JSON malformado, paginación multipágina, y un test que capture logs y verifique que el ticket NUNCA aparece.

5. scripts/smoke_test.py: con ticket real hace licitaciones_activas (cuenta), 1 detalle, 1 página de CA publicadas; imprime resumen y cuota consumida. No lo ejecutes tú; lo corro yo.

Al terminar: ruff, mypy, pytest (cobertura app/clients ≥85 %), commit.
```

### Prompt F2 — Modelo de datos, FTS, retención y usuarios

```
Implementa app/models/ con SQLAlchemy 2.x + Alembic contra Postgres (Neon), según CLAUDE.md.

Tablas:
- usuarios(id PK, email UNIQUE, password_hash, rol enum admin|usuario, activo bool, creado_en)
- organismos(codigo PK, nombre, rut nullable, actualizado_en)
- licitaciones(codigo PK, nombre, descripcion, estado_codigo int, estado enum propio, tipo, fecha_publicacion, fecha_cierre, moneda, monto_estimado, monto_clp, codigo_organismo nullable, raw_json JSONB NULLABLE, detalle_obtenido bool, creado_en, actualizado_en)
- licitacion_items(id PK, licitacion_codigo FK, codigo_producto, nombre, cantidad, unidad)
- compras_agiles(codigo PK, nombre, descripcion, estado, estado_convocatoria, fecha_publicacion, fecha_cierre, fecha_ultimo_cambio indexada, moneda, monto_disponible_clp, organismo_nombre, organismo_rut, region int, total_ofertas, id_orden_compra nullable, raw_json JSONB NULLABLE, creado_en, actualizado_en)
- ca_productos(id PK, ca_codigo FK, codigo_producto, nombre, descripcion, cantidad, unidad)
- ordenes_compra(codigo PK, análogo, tipo_oc, estado)
- perfiles_busqueda(id PK, owner_id FK usuarios, nombre, keywords json, keywords_excluir json, regiones json, monto_min_clp, monto_max_clp, fuentes json, frecuencia_alerta enum inmediata|digest, activo bool)
- oportunidades_match(id PK, perfil_id FK, fuente, codigo_oportunidad, score float, razones json, fecha_match, UNIQUE(perfil_id, fuente, codigo_oportunidad))
- alertas(id PK, match_id FK, tipo, enviada_en, canal, estado)
- sync_state(fuente PK, cursor, ultima_ejecucion, ultimo_ok, requests_usadas_hoy, fecha_contador, notas)

Full-text search:
- Migración que crea EXTENSION IF NOT EXISTS unaccent y una función IMMUTABLE inmutable_unaccent para usar en índices.
- Columnas generadas tsvector (config 'spanish' + unaccent) sobre nombre||descripcion en licitaciones y compras_agiles (y sobre nombre de productos vía vista o columna agregada), con índices GIN.

Retención (límite Neon 0.5 GB):
- Regla: raw_json se guarda SOLO cuando la oportunidad tiene al menos un match (el motor de F4 lo setea); por defecto NULL.
- core/retencion.py: purgar_terminales(dias=90) — para oportunidades en estado terminal (adjudicada/cancelada/desierta/revocada) con actualizado_en > 90 días: raw_json=NULL y borra items/productos; conserva la fila estructurada. Nunca toca vigentes ni matches con alertas pendientes.
- Función tamano_bd() (pg_database_size) para el panel de salud.

Más:
1. Enum EstadoOportunidad unificado y mapeos: licitaciones (5,6,7,8,18,19), OC (4,5,6,9,12,13,14,15), CA (publicada, cerrada, desierta, cancelada, proveedor_seleccionado). Desconocido → DESCONOCIDO + log, sin romper.
2. Seeds: regiones 1–16, tipos de licitación (L1, LE, LP, LS, A1, B1, J1, F1, E1, CO, B2, A2, D1, E2, C2, C1, F2, F3, G2, G1, R1, CA, SE), tipos de OC (1–14 incl. CM=9, AG=13, CC=14), y usuario admin desde ADMIN_EMAIL/ADMIN_PASSWORD (bcrypt) solo si la tabla usuarios está vacía.
3. core/montos.py: normalizar_clp(monto, moneda) con tasas de settings; interfaz lista para un provider real; documenta la limitación.
4. Tests (pueden usar la rama dev de Neon o Postgres local en CI): upsert no duplica; mapeo de estados; unique de match; FTS encuentra "electricos" en "Materiales Eléctricos"; retención purga lo correcto y respeta lo vigente; seed de admin idempotente.

Al terminar: alembic upgrade head desde BD vacía; ruff, mypy, pytest; commit.
```

### Prompt F3 — Ingesta y sincronización

```
Implementa app/ingest/ usando los clientes (F1) y modelos (F2). Reglas duras del CLAUDE.md: presupuesto de cuota, idempotencia, proceso desechable, backfill solo nocturno con TZ Chile, lotes acotados en memoria (Render 512 MB).

1. ingest/licitaciones.py:
   - sync_activas(): estado=activas → upsert básico por lotes (commit cada 200), nuevas con detalle_obtenido=False.
   - fetch_detalles_pendientes(max_requests): detalle por código SOLO de nuevas que pasan pre-filtro barato (keywords amplias opcionales de settings); respeta presupuesto.
   - sync_por_fecha(fecha): para backfill.
2. ingest/compra_agil.py:
   - sync_incremental(): cursor desde sync_state (máx fecha_ultimo_cambio procesada); listar con cambio_desde = cursor − 5 min (solapamiento), estados publicada,cerrada,proveedor_seleccionado, páginas de 50; upsert por lotes; cursor avanza SOLO si la corrida completa fue exitosa.
   - fetch_detalle(codigo) para candidatos.
3. ingest/lifecycle.py: refresh_estados(): re-consulta por código oportunidades en estados no terminales con fecha_cierre entre −7 y +3 días; prioriza por cercanía de cierre; respeta presupuesto.
4. ingest/catalogos.py: refresh_organismos() semanal.
5. ingest/orchestrator.py + APScheduler:
   - Adquiere pg_advisory_lock al iniciar cada ciclo; si otro proceso lo tiene, se salta el ciclo (Render puede levantar 2 instancias en un deploy).
   - Cada 30 min: sync_incremental CA; 3 veces/día: sync_activas; tras cada ingesta: fetch_detalles_pendientes con presupuesto repartido.
   - 23:30 Chile: lifecycle/backfill pesado. Guard: ventana 22:00–07:00 validada con ZoneInfo("America/Santiago") usando un now() inyectable; fuera de ventana, rehúsa y loguea.
   - Registra en sync_state: requests usadas, resultado, errores.
   - Ante MPRateLimitError: aborta limpio, persiste progreso, agenda reintento post-medianoche Chile.
6. CLI: python -m app.ingest run-once --job {activas|ca|detalles|lifecycle|catalogos|retencion} y run-scheduler.
7. Tests (HTTP mockeado, reloj congelable): idempotencia (misma página 2 veces); cursor solo avanza en éxito; 429 en página 3/5 → progreso guardado, cursor intacto; presupuesto respetado; backfill rechazado fuera de ventana; advisory lock impide doble ejecución (simula dos orquestadores).

Al terminar: ruff, mypy, pytest; docs/ingesta.md con flujo y estimación de requests/día en el peor caso (< 9.000); commit.
```

### Prompt F4 — Motor de búsqueda y scoring

```
Implementa app/matching/ según CLAUDE.md.

1. matching/text.py: normalización (minúsculas, unaccent en BD; frases entre comillas en keywords) y construcción segura de websearch_to_tsquery — SIEMPRE parametrizado, nunca interpolación.
2. matching/perfiles.py: CRUD de perfiles_busqueda con owner obligatorio; validación: al menos 1 keyword o 1 filtro estructurado.
3. matching/engine.py — match_perfil(perfil) y match_todos():
   - Candidatos: oportunidades PUBLICADA con fecha_cierre futura, de las fuentes del perfil.
   - Filtros estructurados: región (CA), monto_clp en rango (si la oportunidad no informa monto pasa, pero se anota en razones), organismo si está definido.
   - Texto: FTS Postgres (tsvector spanish + unaccent) sobre nombre/descripcion/productos; keywords_excluir descartan.
   - Score 0–100 = 60·relevancia_texto (proporción de keywords con hit, bonus en nombre) + 25·urgencia (máximo si cierra en 2–7 días; penaliza <24 h o >30 días) + 15·competencia (CA: menos ofertas → más puntos; licitaciones: neutro).
   - Upsert en oportunidades_match (unique) con razones explicables: {"keywords_hit": [...], "campo": ..., "dias_al_cierre": n, "ofertas": n}.
   - Al crear un match nuevo: si la oportunidad no tiene raw_json guardado, solicita/persiste el detalle (regla de retención) respetando presupuesto.
4. Integración: match_todos() corre tras cada ciclo de ingesta, iterando perfiles de usuarios activos.
5. tests/fixtures/dataset_matching.py: 12+ oportunidades sintéticas y 3 perfiles (de 2 dueños distintos) con resultados esperados explícitos: tilde vs sin tilde, keyword en producto, exclusión, monto fuera de rango, CA de otra región, y verificación de que los matches quedan asociados al perfil/dueño correcto. Tests de matches exactos y orden por score.

Al terminar: ruff, mypy, pytest; commit.
```

### Prompt F5 — Alertas por usuario

```
Implementa app/alerts/ según CLAUDE.md.

1. Eventos: (a) nuevo match; (b) cambio de estado de oportunidad ya matcheada (cerrada/adjudicada/cancelada/proveedor_seleccionado); (c) recordatorio cierre ≤48 h.
2. alerts/detector.py: compara estado actual vs último notificado; deduplicación vía tabla alertas (nunca dos veces el mismo (match, tipo)).
3. alerts/email.py: SMTP (settings; compatible con Brevo y Gmail). Plantillas Jinja2 texto+HTML: nombre, organismo, región, monto CLP, cierre, score y razones, enlace a la ficha en mercadopublico.cl, pie "Fuente de datos: Dirección ChileCompra — Mercado Público".
4. Entrega POR USUARIO: cada correo va únicamente al dueño del perfil; modo del perfil: inmediata o digest diario (hora configurable) que agrupa todos los perfiles del usuario en un solo correo.
5. Tope diario de correos (settings, default 250): si se alcanza, prioriza inmediatas, pospone digests y lo registra para el panel de salud.
6. Integración con el orquestador tras el matching.
7. Tests: SMTP falso; deduplicación; digest agrupa por usuario correcto; un usuario JAMÁS recibe alertas de perfiles ajenos; tope diario; la plantilla no contiene secretos.

Al terminar: ruff, mypy, pytest; commit.
```

### Prompt F6 — Autenticación, dashboard y API

```
Implementa app/auth/ y app/api/ con FastAPI según CLAUDE.md. Escala: 3–10 usuarios; simple, server-rendered, seguro.

Auth:
1. Login email+contraseña (bcrypt/passlib), cookie de sesión firmada con SECRET_KEY (itsdangerous o SessionMiddleware), HttpOnly + Secure + SameSite=Lax, expiración 7 días, logout.
2. Rate limit de login: máx 5 intentos fallidos por IP/15 min (en memoria está bien; documenta la limitación con múltiples instancias).
3. Dependencias require_user y require_admin. Sin sesión → redirect a /login.
4. CSRF: token en formularios de mutación (login, perfiles, usuarios).
5. Admin: crear usuario, desactivar, resetear contraseña. Sin registro abierto.

Dashboard (Jinja2 + HTMX, autoescape activo; datos de la API tratados como NO confiables):
- / : oportunidades vigentes por score, filtros (fuente, región, texto, mis perfiles), badge días al cierre.
- /oportunidad/{fuente}/{codigo}: detalle + productos + razones de match + link externo.
- /perfiles: cada usuario ve y edita SOLO los suyos (verifica ownership en servidor, no por ocultamiento de UI); admin ve todos.
- /admin/usuarios (solo admin).
- /salud (solo admin): última sync por fuente, cursor, requests hoy vs presupuesto, correos hoy vs tope, tamaño BD vs 0.5 GB, errores recientes, estado del lock.
- Footer en todas las páginas: "Datos: Dirección ChileCompra — Mercado Público".

API:
- /api/oportunidades (filtros + paginación), /api/oportunidades/{fuente}/{codigo}, /api/perfiles (CRUD con ownership), /api/salud — todo protegido por sesión.
- GET /api/salud/ping: SIN auth, responde {"ok": true} y nada más (para el pinger externo).
- POST /api/jobs/run?job={ca|activas|detalles|lifecycle|catalogos|retencion}: protegido EXCLUSIVAMENTE por header X-Jobs-Token comparado en tiempo constante (secrets.compare_digest); ejecuta el job en background y responde de inmediato; nunca aparece el token en logs.
- Queries 100 % parametrizadas; paginación obligatoria; errores al cliente sin stack traces.

Tests: redirect sin sesión; IDOR (usuario A no puede ver/editar perfil de B por id directo, ni por API); /salud y /admin solo admin; CSRF; jobs/run rechaza token inválido; /api/salud no filtra secretos.

Al terminar: ruff, mypy, pytest; commit.
```

### Prompt F6.5 — Despliegue en Render + Neon

**Prerequisito (hacer manualmente en Neon antes de correr este prompt):**
- Crear proyecto en neon.tech → automáticamente crea la branch `production` (producción).
- Desde el dashboard, crear una branch adicional llamada `dev` a partir de `production` (auto-delete: Never).
- Copiar los dos `DATABASE_URL` (uno por branch) con `?sslmode=require` al final.
- Ambas URLs van en `.env` local:
  ```
  DATABASE_URL=postgresql://...@.../neondb?sslmode=require        # branch dev — desarrollo local y pytest
  DATABASE_URL_PROD=postgresql://...@.../neondb?sslmode=require   # branch production — solo referencia; la usa Render
  ```
- En Render, la variable `DATABASE_URL` apuntará a la branch `production`.

```
Prepara el despliegue 100 % gratuito según CLAUDE.md (sección "Despliegue").
El proyecto ya tiene dos branches en Neon: `production` (prod) y `dev` (local/tests).
La variable DATABASE_URL en el entorno local apunta a la branch `dev`.
En Render, DATABASE_URL apuntará a la branch `main`.

1. render.yaml: web service free, runtime python, startCommand "alembic upgrade head && uvicorn app.api.main:app --host 0.0.0.0 --port $PORT", healthCheckPath /api/salud/ping, y la lista de env vars requeridas (sync: false para secretos).

2. Separación dev/prod — ajusta en tres lugares:
   a. app/core/settings.py: asegura que DATABASE_URL se lee desde env var (ya debería estarlo). Sin hardcodeo.
   b. alembic/env.py: usa settings.database_url (no valor fijo). Confirma que sqlalchemy.url no está hardcodeado.
   c. pytest: los tests usan DATABASE_URL del entorno (.env / .env.test apuntando a branch dev). Añade fixture `db_url` que lee settings.database_url y falla explícitamente si apunta a la branch `main` (verificar que el hostname no contiene "-main-" o la lógica que Neon use en el hostname de cada branch).

3. Flujo de migraciones documentado en docs/despliegue.md:
   - Desarrollo: `alembic upgrade head` corre contra branch `dev` (DATABASE_URL local).
   - Deploy: Render corre `alembic upgrade head` automáticamente contra branch `main` (DATABASE_URL de Render).
   - Nunca correr alembic manualmente contra `main` desde local.

4. Conexión Neon: engine con sslmode=require, pool_pre_ping=True, pool_size≤5, max_overflow=0, pool_recycle=300; reintento de conexión al arrancar (Neon puede estar suspendida tras idle).

5. Arranque de la app: migraciones ya corren en startCommand; seed de admin idempotente (no falla si el admin ya existe); APScheduler arranca con la app PERO cada ciclo toma pg_advisory_lock — verifica que esto ya funciona (F3) y agrégalo si falta.

6. Lifespan de FastAPI: apagado limpio del scheduler en SIGTERM (Render reinicia servicios en deploys).

7. docs/despliegue.md paso a paso:
   a. Neon: ya tienes el proyecto con branches main y dev. Confirmar que DATABASE_URL local = dev.
   b. Render: crear web service desde el repo, setear env vars (MP_TICKET, DATABASE_URL=branch-main, SECRET_KEY, JOBS_TOKEN, ADMIN_EMAIL, ADMIN_PASSWORD, SMTP_*, tasas de cambio), deploy.
   c. Pinger: configurar cron-job.org o UptimeRobot → GET /api/salud/ping cada 10 min (mantiene el servicio despierto y el scheduler vivo).
   d. Respaldo de jobs: cron-job.org → POST /api/jobs/run?job=ca cada 1 h con header X-Jobs-Token (por si el scheduler interno falló).
   e. Verificación post-deploy: login admin, correr job manual, revisar /salud, confirmar que tras 2 h el servicio no se durmió.
   f. Advertencia TZ: los crons externos corren en UTC; la ventana nocturna la valida la app en America/Santiago, no el cron.

8. Smoke test de despliegue documentado (checklist manual).
9. Tests locales: lifespan apaga el scheduler; /api/salud/ping responde sin auth y sin datos.

Al terminar: ruff, mypy, pytest (contra branch dev); commit. Yo ejecuto el deploy real siguiendo docs/despliegue.md.
```

### Prompt F7 — Endurecimiento y entrega

```
Cierra el proyecto para entrega según CLAUDE.md.

1. Cobertura: pytest --cov; lleva app/clients, app/ingest, app/matching y app/auth a ≥80 % priorizando ramas de error.
2. docs/operacion.md (runbook): instalación local; rotación del ticket; rotación de SECRET_KEY y JOBS_TOKEN (e impacto en sesiones); recuperar acceso admin; 401 persistente; 429 (esperar día calendario Chile); API caída; Neon suspendida o llena (qué purgar, cómo medir con /salud); pinger caído (síntoma: sync atrasada; remedio); respaldo/restore de la BD (pg_dump contra Neon); deploy y rollback en Render.
3. docs/arquitectura.md: diagrama mermaid de módulos y flujo de datos; decisiones (Render+Neon, retención, advisory lock, un ticket compartido) y limitaciones conocidas (gotchas API, tasas de cambio configuradas a mano, rate limit de login en memoria).
4. pip-audit; corrige lo razonable y documenta lo diferido.
5. README final con quickstart (local y producción) y CHANGELOG.

Commit final con tag v0.1.0.
```

---

## PARTE 2 — PROMPTS DE AUDITORÍA

> Cada auditoría en conversación nueva de Claude Code (auditor "fresco"). Entregable siempre en `audits/`.

### Prompt de auditoría por fase (genérico — reemplazar {N} y alcance)

```
Actúa como auditor técnico independiente. No modifiques código salvo aprobación mía: tu entregable es un informe.

Audita la fase F{N} ({alcance}) contra:
(a) el CLAUDE.md del repo,
(b) docs/02-plan-desarrollo-y-auditoria.md (sección 6: checklist transversal + checks de F{N}),
(c) docs/01-analisis-api-mercado-publico.md (cuota, 429 por día calendario, ticket secreto, ventana nocturna en TZ Chile, parseo defensivo),
(d) las restricciones de capa gratuita (Render efímero/512MB, Neon 0.5GB, tope de correos).

Procedimiento:
1. Lee documentos y código/diff de la fase.
2. Ejecuta ruff check, mypy, pytest --cov (resultados reales, no asumidos).
3. Busca secretos: grep de patrones de ticket/token/contraseña en código, tests, fixtures y logs versionados; git log -p para el historial.
4. Marca cada ítem del checklist: CUMPLE / NO CUMPLE / PARCIAL con evidencia (archivo:línea).
5. Riesgos no cubiertos por el checklist.

Entrega audits/AUDIT-F{N}.md: resumen ejecutivo, tabla del checklist con evidencia, hallazgos Crítico/Alto/Medio/Bajo (descripción, ubicación, impacto, remediación, esfuerzo), veredicto: APTO PARA CONTINUAR / REQUIERE REMEDIACIÓN.
```

### Prompt A1 — Auditoría final de seguridad

```
Actúa como auditor de seguridad. Entregable: audits/AUDIT-FINAL-A1-seguridad.md. No corrijas nada sin mi aprobación.

1. Secretos: busca MP_TICKET, SECRET_KEY, JOBS_TOKEN, contraseñas y DATABASE_URL en el árbol, en TODO el historial git (git log -p, git grep por commit), fixtures y logs versionados. Verifica con un test real el filtro de enmascaramiento de logs.
2. Autenticación y autorización (foco multiusuario):
   - IDOR: intenta acceder/editar perfiles de otro usuario por id en UI y API.
   - Sesiones: firma, expiración, HttpOnly/Secure/SameSite, fijación de sesión, invalidación al desactivar usuario.
   - Login: bcrypt con costo adecuado, rate limit de intentos, mensajes que no revelan si el email existe.
   - CSRF en todos los formularios de mutación.
   - /salud y /admin inaccesibles para rol usuario; /api/jobs/run: comparación en tiempo constante, token fuera de logs, ¿qué pasa con token vacío o ausente?
3. Web: inyección SQL (100 % parametrizado), XSS (autoescape Jinja2, datos de la API como no confiables — nombres de licitaciones pueden traer HTML), exposición en errores (sin stack traces), headers básicos.
4. Dependencias: pip-audit, versiones pinneadas.
5. Datos personales: raw_json puede contener RUT/razón social de proveedores (datos públicos de ChileCompra). Documenta tratamiento, retención y atribución.

Hallazgos clasificados con evidencia + remediación. Veredicto final.
```

### Prompt A2 — Auditoría final de cumplimiento e integridad de datos

```
Actúa como auditor de cumplimiento y calidad de datos. Entregable: audits/AUDIT-FINAL-A2-cumplimiento-datos.md.

Parte 1 — Términos de uso de ChileCompra (docs/01 sección 6), verificado en código con evidencia archivo:línea:
- Rate limiter activo por defecto; presupuesto local < 10.000/día persistido en BD; 429 espera al día calendario siguiente EN TZ CHILE; backfill bloqueado fuera de 22:00–07:00 validado con America/Santiago (no UTC); atribución visible en dashboard y correos; ticket no expuesto.

Parte 2 — Integridad de datos (requiere ticket real; PIDE MI CONFIRMACIÓN antes de gastar cuota; máx 30 requests):
- Compara 5 licitaciones y 5 Compras Ágiles de la BD contra la API en vivo: estado, fechas, montos, organismo.
- Normalizaciones: enum de estados, fechas UTC/ISO, montos→CLP con tasas configuradas, gotchas (id_orden_compra vs codigo_orden_compra null; binarios v1; slugs con erratas).
- Cursores de sync_state coherentes con lo más reciente en BD.

Parte 3 — Presupuestos free tier:
- Proyección de crecimiento de la BD a 12 meses con la retención activa (mide tamaño actual, estima por volumen diario observado) vs 0.5 GB de Neon.
- Peor caso de correos/día vs tope 250 y vs 300 de Brevo.

Hallazgos clasificados + veredicto.
```

### Prompt A3 — Auditoría final de calidad de código y tests

```
Actúa como revisor senior de código. Entregable: audits/AUDIT-FINAL-A3-calidad.md.

1. Arquitectura: capa anti-corrupción (nada fuera de app/clients importa httpx ni conoce URLs de mercadopublico); solo app/models define esquema; ownership de perfiles verificado en servidor (no solo en plantillas). Violaciones con archivo:línea.
2. Ejecuta ruff, mypy --strict (delta vs config actual), pytest --cov. ¿La cobertura ≥80 % es real o inflada? Revisa críticamente 5 tests al azar.
3. Casos borde — verifica si existe test para: respuesta vacía (Cantidad=0), última página de paginación, oportunidad que cambia de región/monto entre syncs, keyword con caracteres especiales/comillas, fecha de cierre nula, moneda desconocida, usuario desactivado con sesión viva, dos jobs simultáneos (advisory lock).
4. Deuda: TODOs, código muerto, funciones >50 líneas, duplicación entre mp_v1/mp_v2 que debería vivir en base.py.
5. Top-5 de refactors con costo/beneficio.

Hallazgos clasificados + veredicto.
```

### Prompt A4 — Auditoría final de operación (game day)

```
Actúa como ingeniero de confiabilidad. Entregable: audits/AUDIT-FINAL-A4-operacion.md.

Simulacros controlados (mocks/env vars; NO gastes cuota real sin mi autorización):
1. Ticket inválido: ¿falla rápido y claro? ¿el scheduler evita loop abusivo de reintentos?
2. 429 en página 3/5 de una sync CA: ¿progreso persistido? ¿cursor intacto? ¿reintento agendado post-medianoche Chile?
3. API caída (timeouts): retries acotados; corrida termina registrada en sync_state sin tumbar el scheduler.
4. JSON malformado: MPParseError aislado al registro/página.
5. Neon suspendida: primera query tras idle → ¿pool_pre_ping reconecta sin error de usuario?
6. Reinicio de Render a mitad de ciclo (mata el proceso): al volver, idempotencia sin duplicados, cursor consistente, lock liberado.
7. Dos instancias simultáneas (deploy): advisory lock impide doble ingesta.
8. Pinger caído 24 h: el servicio durmió y la sync se atrasó — ¿/salud lo evidencia? ¿la recuperación es automática al volver el tráfico? ¿qué se perdió (nada, por cursor con solapamiento)?
9. BD al 90 % de 0.5 GB: ¿/salud alerta? ¿el runbook indica qué purgar?
10. Verifica que docs/operacion.md cubre cada escenario; señala vacíos.

Para cada simulacro: procedimiento, esperado, real, evidencia. Hallazgos clasificados + veredicto + mejoras de resiliencia priorizadas.
```

---

## PARTE 3 — Plantilla `CLAUDE.md` v2 (colocar en la raíz antes de F0)

```markdown
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

---

## PARTE 4 — Correcciones post-auditoría

### F6-fix — 4 bugs encontrados en auditoría

**Contexto:** Auditoría de F6 (auth + API + dashboard). 160 tests pasando antes de aplicar estas correcciones.

**Bugs encontrados:**

1. **CRÍTICO** `app/api/routes/api.py` — `api_actualizar_perfil` (l.176–185) y `api_eliminar_perfil` (l.199): ninguno llama `session.commit()` después de `actualizar_perfil()` / `eliminar_perfil()`. El contexto `with Session(engine) as session` hace rollback implícito al cerrar → los cambios se pierden silenciosamente. Solo `api_crear_perfil` (l.161) tiene commit.

2. **MENOR** `app/api/routes/auth.py` l.97 — `logout()` llama `validate_csrf_token(...)` pero ignora el valor retornado; no lanza excepción si el token es inválido. El logout se completa con cualquier token (o sin él).

3. **POTENCIAL** `app/api/routes/api.py` l.57–59 — `/api/salud` usa `assert` para verificar que la respuesta no contiene secretos. Los `assert` son eliminados con `python -O` (Render puede usar esta flag). Reemplazar con `if / raise`.

4. **MEDIO** `app/api/routes/api.py` l.207–237 — `/api/jobs/run` ignora el parámetro `job=` (siempre ejecuta ciclo completo) y usa `threading.Thread` en vez de FastAPI `BackgroundTasks`. El parámetro `job=` ya está mencionado en el prompt F6.5 (ítem d: `?job=ca` para respaldo de scheduler), por lo que debe implementarse.

---

**Prompt F6-fix para Claude Code:**

```
Aplica las siguientes correcciones en `app/api/routes/api.py` y `app/api/routes/auth.py`. No toques ningún otro archivo. Al terminar: ruff, mypy, pytest; commit "F6-fix: session.commit en PUT/DELETE, CSRF logout, assert→raise, BackgroundTasks".

### 1. `app/api/routes/api.py` — session.commit en actualizar y eliminar

En `api_actualizar_perfil`, añade `session.commit()` justo antes del `return`:

```python
    actualizar_perfil(
        session,
        perfil_id=perfil_id,
        owner_id=user.id,
        nombre=body.nombre,
        keywords=body.keywords,
        keywords_excluir=body.keywords_excluir,
        fuentes=body.fuentes,
        frecuencia_alerta=FrecuenciaAlerta(body.frecuencia_alerta),
    )
    session.commit()           # ← añadir
    return {"id": perfil_id, "nombre": body.nombre}
```

En `api_eliminar_perfil`, añade `session.commit()` tras `eliminar_perfil(...)`:

```python
    eliminar_perfil(session, perfil_id, user.id)
    session.commit()           # ← añadir
```

Añade tests que verifiquen que PUT y DELETE persisten (leer de nuevo con GET tras la operación confirma el cambio).

### 2. `app/api/routes/api.py` — assert → raise en /api/salud

Reemplaza las líneas con `assert` (que se eliminan con `python -O`) por:

```python
    data = get_salud_data(session, settings)
    # Nunca devolver secretos
    datos_str = str(data)
    for secreto in ("mp_ticket", "secret_key", "jobs_token"):
        if secreto in datos_str:
            raise RuntimeError(f"get_salud_data filtró el campo '{secreto}'")
    return data
```

### 3. `app/api/routes/api.py` — /api/jobs/run con job= y BackgroundTasks

Reemplaza toda la función `jobs_run` por esta implementación que:
- Acepta `job: str = "all"` como query param.
- Usa `BackgroundTasks` de FastAPI (no `threading.Thread`).
- Soporta valores: `"ca"`, `"activas"`, `"detalles"`, `"lifecycle"`, `"match"`, `"alerts"`, `"digest"`, `"all"`.
- Retorna `{"queued": true, "job": job}`.

```python
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

@router.post("/jobs/run")
async def jobs_run(
    request: Request,
    background_tasks: BackgroundTasks,
    job: str = "all",
    session: Session = Depends(get_db),
) -> dict[str, object]:
    settings = request.app.state.settings
    token = request.headers.get("X-Jobs-Token", "")
    if not secrets.compare_digest(token, settings.jobs_token):
        raise HTTPException(status_code=401, detail="Token inválido")

    from app.ingest.orchestrator import (
        run_alerts,
        run_detalles,
        run_digest,
        run_lifecycle,
        run_match,
        run_sync_activas,
    )

    engine = request.app.state.engine

    _jobs: dict[str, Any] = {
        "ca":        lambda: run_sync_activas(settings, engine),
        "activas":   lambda: run_sync_activas(settings, engine),
        "detalles":  lambda: run_detalles(settings, engine),
        "lifecycle": lambda: run_lifecycle(settings, engine),
        "match":     lambda: run_match(settings, engine),
        "alerts":    lambda: run_alerts(settings, engine),
        "digest":    lambda: run_digest(settings, engine),
    }

    def _full_cycle() -> None:
        run_sync_activas(settings, engine)
        run_detalles(settings, engine)
        run_lifecycle(settings, engine)
        run_match(settings, engine)
        run_alerts(settings, engine)

    if job == "all":
        background_tasks.add_task(_full_cycle)
    elif job in _jobs:
        background_tasks.add_task(_jobs[job])
    else:
        raise HTTPException(status_code=400, detail=f"job desconocido: {job!r}")

    return {"queued": True, "job": job}
```

Actualiza los tests existentes de `/api/jobs/run`:
- `test_jobs_run_token_correcto`: verificar `r.json()["queued"] is True`.
- Añadir `test_jobs_run_job_invalido`: `POST /api/jobs/run?job=xxx` con token válido → 400.
- Añadir `test_jobs_run_job_ca`: `POST /api/jobs/run?job=ca` con token válido → 200, `queued=True`.

### 4. `app/api/routes/auth.py` — CSRF en logout

En `logout()`, verifica el resultado de `validate_csrf_token` y responde con error si es inválido:

```python
        if user is not None:
            token = request.headers.get("X-CSRF-Token") or csrf_token
            if not validate_csrf_token(settings.secret_key, user.id, token):
                return RedirectResponse(url="/login?error=CSRF+inválido", status_code=303)
```

Añade test `test_logout_csrf_invalido`: POST /logout con cookie válida pero csrf_token="" → no elimina cookie, redirige a /login con error.
```

---

## PARTE 4 — Correcciones post-auditoría

### Fix F1 (ya aplicado — commit `b0e3177`)

Las siguientes correcciones ya están en producción, no repetir:
- `httpx.request()` global → `httpx.Client` persistente en `BaseClient.__init__`
- `check_budget()` eliminó la segunda consulta redundante a Postgres
- `iterar_compra_agil` tipado correctamente como `Generator[CompraAgilBasica, None, None]`

---

### Prompt correcciones F2 — sesión Claude Code

**Contexto:** La auditoría de F2 (modelos + migración + FTS) identificó cuatro problemas.
Aplica todos en una sola sesión y cierra con `ruff check . && mypy app/ && pytest`.
Commit final: `"F2 fix: FTS productos, UTC aware, retención optimizada, tests"`.

---

**CORRECCIÓN 1 — ALTA: FTS no indexa nombres de productos/items**

Problema: `licitaciones.tsv` y `compras_agiles.tsv` son columnas GENERATED que solo
incluyen `nombre` y `descripcion` de la tabla padre. Los nombres de `licitacion_items`
y `ca_productos` no están indexados. El matching engine de F4 necesita buscar por
nombre de producto/ítem.

Solución adoptada (documentar en migración + TODO en migration, implementar en F4):
Los comentarios TODO ya existen en `alembic/versions/fde568616494_tablas_iniciales.py`
(líneas ~88 y ~150). Verificar que el comentario sea exactamente:

```sql
-- TODO(F4): el tsv de la tabla padre no incluye nombres de licitacion_items/ca_productos
-- porque las columnas GENERATED no pueden leer tablas hijas. En F4 (matching engine)
-- la query FTS debe combinar: parent.tsv @@ query OR EXISTS (
--   SELECT 1 FROM child_table WHERE fk = parent.codigo
--   AND to_tsvector('spanish', inmutable_unaccent(nombre)) @@ query
-- )
```

Agregar en `app/core/matching/` (aunque el módulo se crea en F4), un archivo
`README.md` o comentario en `__init__.py` que documente la estrategia de búsqueda
híbrida para no olvidarla al implementar F4.

Adicionalmente, agregar un índice GIN en `licitacion_items.nombre` y
`ca_productos.nombre` en una **nueva migración** (`F2b_indices_items`):

```python
# Nueva migración alembic: F2b — índices para búsqueda en items/productos
op.execute("""
    CREATE INDEX ix_licitacion_items_nombre_fts
    ON licitacion_items
    USING gin(to_tsvector('spanish', inmutable_unaccent(nombre)))
""")
op.execute("""
    CREATE INDEX ix_ca_productos_nombre_fts
    ON ca_productos
    USING gin(to_tsvector('spanish', inmutable_unaccent(nombre)))
""")
```

Downgrade de esa migración: `DROP INDEX IF EXISTS ix_licitacion_items_nombre_fts` y
`DROP INDEX IF EXISTS ix_ca_productos_nombre_fts`.

---

**CORRECCIÓN 2 — MEDIA: `datetime.utcnow()` deprecated (Python 3.12 warning)**

Archivos afectados: buscar con `grep -rn "utcnow" app/`.

Reemplazar toda ocurrencia de:
```python
datetime.utcnow()
```
por:
```python
datetime.now(UTC)
```
con el import:
```python
from datetime import UTC, datetime
```

Si `UTC` no existe en la versión (Python < 3.11), usar `timezone.utc`:
```python
from datetime import datetime, timezone
datetime.now(timezone.utc)
```
Como el proyecto requiere Python 3.11+, `UTC` es válido.

Verificar que mypy no reporte errores de tipo en ninguna función que reciba el resultado
(puede requerir `replace(tzinfo=None)` si alguna columna SQLAlchemy espera naive datetime
— en ese caso documentar el motivo con un comentario `# naive UTC para compatibilidad SQLAlchemy`).

---

**CORRECCIÓN 3 — MEDIA: `purgar_terminales` ejecuta la misma subquery dos veces**

Archivo: `app/core/retencion.py`

Problema: la función construye la subquery de IDs terminales dos veces (una para
licitaciones, otra para compras ágiles) repitiendo lógica idéntica. Además, la
subquery que excluye alertas pendientes se repite.

Solución: extraer la subquery como variable reutilizable o usar CTEs. Ejemplo
mínimo:

```python
# Antes (simplificado):
ids_lit = session.scalars(select(Licitacion.codigo).where(...)).all()
ids_ca  = session.scalars(select(CompraAgil.codigo).where(...)).all()

# Después: una sola consulta por tabla, sin repetir el filtro de alertas
cutoff = datetime.now(UTC) - timedelta(days=dias)
pendiente_lit = select(OportunidadMatch.codigo_oportunidad).join(Alerta).where(
    Alerta.estado == "pendiente", OportunidadMatch.fuente == "licitaciones"
)
terminales_lit = (
    select(Licitacion.codigo)
    .where(Licitacion.estado.in_(ESTADOS_TERMINALES))
    .where(Licitacion.actualizado_en < cutoff)
    .where(Licitacion.codigo.not_in(pendiente_lit))
)
# Usar terminales_lit en el UPDATE y luego en el DELETE sin reconstruirla
```

El objetivo es que cada tabla tenga una sola CTE/subquery que se reutiliza en el
UPDATE de raw_json y en el DELETE de items/productos.

---

**CORRECCIÓN 4 — BAJA: tests de retención incompletos**

Archivo: `tests/test_models.py`

Agregar al menos dos tests para `purgar_terminales`:

1. **Test protección por alerta pendiente**: crear una licitación terminal antigua con
   una `OportunidadMatch` asociada que tenga una `Alerta` en estado `"pendiente"`.
   Verificar que `purgar_terminales()` NO elimina su `raw_json`.

2. **Test purga efectiva**: crear una licitación terminal antigua SIN alertas pendientes.
   Verificar que `purgar_terminales()` SÍ pone `raw_json = None` y elimina sus items.

Usar SQLite en memoria (ya configurado en los otros tests) y `freezegun` para simular
fechas pasadas si es necesario.

---

**CORRECCIÓN 5 — BAJA: mojibake en comentarios de `app/models/tables.py`**

Buscar con `grep -n "\\\\x" app/models/tables.py` o revisar los comentarios que
contienen caracteres como `\xc3\xb3`, `\xc3\xba`, etc.

Reemplazar los comentarios afectados con texto UTF-8 legible. Solo son comentarios
(no afectan runtime), pero ensucian el diff y la legibilidad del código.

---

**Cierre de sesión F2-fix:**

```bash
ruff check . --fix
mypy app/
pytest -v
git add -A
git commit -m "F2 fix: FTS items indexados, UTC aware, retención optimizada, tests retención"
```
