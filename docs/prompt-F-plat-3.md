# Prompt de implementación — F-plat-3 (empresa, territorio priorizado, proveedores y costos) · 3-nov-2026

> Para Claude Code. **Modelo: Sonnet** (CRUD, importadores y permisos con contrato claro; sin API de MP).
> Una fase = un commit, **sin push**. **Con migración.** Va **después de F-merc-1** (usa `mercado_regiones`)
> y puede ir en paralelo con F-merc-2. Origen: `docs/17-plan-pivote-plataforma-ca.md` §2 C4, R-p2, R-p3.

## 0. Antes de escribir código
- Leer `CLAUDE.md` (reglas 10, 11, 16, 17, 18), `docs/17-plan-pivote-plataforma-ca.md` §0, §2 C4, §3,
  `docs/decisiones.md` (Pivote).
- Código: `app/models/tables.py` (`Usuario`, `SyncState`), `app/api/routes/auth.py` y `deps.py` (sesión,
  `require_admin`), `app/api/routes/pages.py` (`/admin/usuarios`, `/cuenta`, patrón HTMX de `/perfiles`),
  `app/auth/csrf.py`, `app/core/settings.py` (`mercado_regiones` de F-merc-1), `app/ingest/orchestrator.py`
  (`mercado-ca` lee regiones), `app/catalogos/unspsc.py`, `templates/base.html` (menú), `tests/conftest.py`.
- No tocar la ingesta de licitaciones ni reactivar jobs: **la cuota es solo para Compra Ágil (R-p1)**.

## 1. Cambios

### 1.1 Esquema (`tables.py` + migración)
- `empresas`: `id`, `nombre`, `rut` (nullable, único si no nulo), `comuna_base`, `regiones_prioritarias`
  (JSONB lista de int; default `[16]`), `comunas_prioritarias` (JSONB lista de str), `activa` (bool),
  `creado_en`, `actualizado_en`.
- `usuarios.empresa_id` FK nullable → `empresas` (los usuarios actuales quedan sin empresa).
- `proveedores`: `id`, `empresa_id` FK NOT NULL, `nombre`, `rut` (nullable), `comuna`, `direccion`, `lat`,
  `lon`, `contacto_nombre`, `telefono`, `email`, `rubros` (JSONB lista de familias del catálogo
  `app/catalogos/mercado.py` si ya existe por F-merc-2; si no, lista de str), `plazo_pago_dias`,
  `minimo_compra_clp`, `despacha` (bool), `notas` (Text), `activo`, timestamps. Índice `(empresa_id, nombre)`.
- `proveedor_productos`: `id`, `proveedor_id` FK, `producto_id` **nullable** (FK a `producto_canonico` cuando
  exista F-plat-1; en esta fase es `Integer` nullable **sin FK**, se agrega la FK en F-plat-1), `producto_texto`
  (lo que el proveedor llama al producto), `codigo_unspsc` (nullable), `formato_texto`, `factor_base`
  (Float nullable), `unidad_base` (`kg|L|u`, nullable), `precio_neto_actual`, `vigente_desde` (Date),
  `fuente` (`manual|planilla|lista_llm`), `disponible` (bool), timestamps.
  `UNIQUE (proveedor_id, producto_texto, formato_texto)`.
- `costo_historial` (append-only, nunca se edita): `id`, `proveedor_producto_id` FK, `precio_neto`,
  `vigente_desde`, `fuente`, `usuario_id`, `registrado_en`. Cada cambio de `precio_neto_actual` inserta una
  fila (en la misma transacción).
- `tarifas_logistica`: `id`, `empresa_id`, `ambito` (`comuna|provincia`), `nombre` (comuna o provincia de
  Ñuble: Diguillín, Itata, Punilla), `costo_fijo_clp`, `costo_por_kg_clp`, `dias`. Seed por empresa nueva:
  las 3 provincias con valores 0 (para que Boris los complete).
- Retención: ninguna de estas tablas se purga.

### 1.2 Territorio → ingesta (R-p2)
- **Ojo [V]:** la corrección del 31-oct a `prompt-F-merc-1.md` eliminó su §1.2, donde nacía
  `settings.mercado_regiones`; hoy no existe en `app/core/settings.py`. Si F-merc-1 no lo creó, crearlo en
  esta fase (`mercado_regiones: list[int] = [16]`) como fallback.
- `app/mercado/territorio.py` (sin httpx): `regiones_activas(session) -> list[int]` = unión de
  `regiones_prioritarias` de empresas activas; si vacío, `settings.mercado_regiones`. `mercado-ca` y
  `mercado-backfill` la usan en vez del setting. Test: dos empresas con `[16]` y `[16, 8]` → `[8, 16]`;
  sin empresas → `[16]`.
- Comunas priorizadas no filtran la ingesta (la API no filtra por comuna; regla 7): se usan en F-plat-5.

### 1.3 Admin de empresas (`/admin/empresas`, solo admin)
- Lista, crear, editar (nombre, RUT, comuna base, regiones con selector múltiple de las 16 regiones, comunas
  de esas regiones como texto separado por coma, activa).
- Asignar y quitar usuarios (selector de usuarios sin empresa + lista de los de la empresa). Un usuario
  pertenece a una sola empresa.
- `/admin/usuarios/nuevo` suma el campo empresa (opcional).

### 1.4 Proveedores (`/proveedores`, usuarios con empresa)
- Lista con búsqueda por nombre/rubro/comuna, ficha con contacto, condiciones, productos con precio actual,
  **variación histórica** por producto (tabla y mini-gráfico inline SVG o Chart.js ya incluido si existe en
  `base.html`; si no, tabla con % de variación respecto al precio anterior y al de hace 90 días).
- Crear/editar proveedor y productos (formulario HTMX, mismo patrón que `_perfil_form.html`).
- Cambiar precio = nueva fila en `costo_historial`; nunca `UPDATE` sin historial.
- `/proveedores/plantilla.xlsx` (descargable) y `/proveedores/importar` (CSV o XLSX con `openpyxl`:
  **dependencia nueva**, justificar en el commit: lectura de planillas de proveedores; `pandas` no hace falta).
  Columnas: proveedor, rut, comuna, contacto, telefono, producto, formato, factor_base, unidad_base,
  precio_neto, vigente_desde. Importación idempotente: misma fila dos veces no duplica ni genera historial
  si el precio no cambió. Vista previa antes de confirmar (n filas nuevas, n precios cambiados, errores por fila).
- "Mejor costo hoy" por `(producto_texto normalizado, unidad_base)` entre proveedores de la empresa:
  función `mejor_costo(session, empresa_id, producto_id | producto_texto)` en `app/mercado/proveedores.py`
  (la usa F-plat-4; en esta fase solo backend + test).
- Logística: `/proveedores/logistica` edita las tarifas de la empresa.

### 1.5 Permisos (R-p3, regla 17)
- Toda query de proveedores, productos, historial y tarifas filtra por `empresa_id` del usuario de sesión.
  Un usuario sin empresa ve `/proveedores` con un aviso ("pide al admin que te asigne a una empresa").
- Tests: usuario de la empresa A no puede ver ni editar (GET y POST, 404 no 403) un proveedor de la B; admin sin
  empresa tampoco edita proveedores (solo administra empresas).
- CSRF en todas las mutaciones (regla 18).

### 1.6 Menú
Entrada "Mesa" en `base.html` con submenú Proveedores (y, más adelante, Mesa, Catálogo, Abastecimiento),
visible solo para usuarios con empresa. Las rutas antiguas no cambian.

## 2. Tests
Migración y modelos (SQLite y PG); `regiones_activas`; CRUD de empresa y asignación; ownership (1.5);
historial de costos (3 cambios → 3 filas, variación calculada); importación con planilla sintética (nueva,
repetida, precio cambiado, fila inválida); `mejor_costo`. `ruff check .`, `python -m mypy app`,
`python -m pytest -rs` (0 fallos, 0 errores, 0 saltados, contra Neon dev).

## 3. Cierre
Entrada en `app/changelog.py` ("Empresas, proveedores y costos con historial"). `docs/00-estado.md`.
Commit: `F-plat-3: empresa con territorio priorizado, proveedores y costos con historial`. `git add` solo de la fase.

## 4. Pasos de Boris
1. Aplicar la migración en Neon; crear la empresa en `/admin/empresas` con región 16 y sus comunas
   prioritarias; asignarse y asignar a Mario.
2. Cargar 3 proveedores reales y ~40 productos con la plantilla; completar la tarifa por provincia.
3. Medir: tiempo total (meta < 30 min) y cualquier campo que faltó → backlog de F-plat-5.
