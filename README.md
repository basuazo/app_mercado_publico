# mp-oportunidades

Aplicación que consume la [API oficial de Mercado Público](https://api.mercadopublico.cl) (ChileCompra), mantiene una base de datos en **Neon (Postgres)** con licitaciones y Compras Ágiles, y genera **alertas por email** para cada usuario según sus perfiles de búsqueda. Incluye un dashboard web protegido por login.

Costo de operación: **$0** (Render free tier + Neon free tier).

---

## Instalación local

### Requisitos
- Python 3.11+
- Una base de datos Postgres (ver sección Neon)
- Un ticket de la API de Mercado Público

### Pasos

```bash
# 1. Clonar y entrar al directorio
git clone <repo> mp-oportunidades
cd mp-oportunidades

# 2. Crear entorno virtual e instalar dependencias
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 3. Configurar variables de entorno
cp .env.example .env
# Editar .env con MP_TICKET, DATABASE_URL, SECRET_KEY y JOBS_TOKEN

# 4. Aplicar migraciones
alembic upgrade head

# 5. Ejecutar tests
pytest

# 6. Iniciar la aplicación
uvicorn app.api.main:app --reload
```

---

## Configuración de Neon (base de datos)

1. Crear una cuenta en [neon.tech](https://neon.tech) (plan gratuito).
2. Crear un nuevo **proyecto** (por ejemplo `mp-oportunidades`).
3. En el panel del proyecto, crear dos **ramas**:
   - `main` — producción (conectada a Render).
   - `dev` — desarrollo local.
4. En la rama `dev`, copiar la **Connection string** y añadir `?sslmode=require` al final si no aparece.
5. Pegar esa cadena en `.env` como valor de `DATABASE_URL`.

> La conexión requiere `sslmode=require`. Sin él, Neon rechaza la conexión.

---

## Configuración del ticket de la API

1. Acceder a [Mercado Público](https://www.mercadopublico.cl) con cuenta de proveedor o comprador.
2. Ir a **Mi cuenta → Configuración → API** y generar/copiar el ticket.
3. Pegar el valor en `.env` como `MP_TICKET`.

> El ticket **nunca** debe aparecer en código, logs ni commits. La app lo enmascara automáticamente en todos los mensajes de log.

---

## Variables de entorno requeridas

| Variable | Descripción |
|---|---|
| `MP_TICKET` | Ticket de acceso a la API de ChileCompra |
| `DATABASE_URL` | URL de Postgres con `sslmode=require` |
| `SECRET_KEY` | Clave para firmar cookies de sesión (32+ bytes aleatorios) |
| `JOBS_TOKEN` | Token para `POST /api/jobs/run` (cron externo) |

Ver `.env.example` para variables opcionales (SMTP, tasas de cambio, etc.).

---

*Datos provistos por la Dirección ChileCompra — Mercado Público.*
