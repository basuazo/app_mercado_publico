# Prompt de implementación — F-plat-3 (empresa, territorio priorizado, proveedores y costos) · 3-nov-2026

> Para Claude Code. **Modelo: Sonnet** (CRUD, importadores y permisos con contrato claro; sin API de MP).
> Una fase = un commit, **sin push**. **Con migración.** F-merc-1, F-merc-2 y F-merc-1c ya están en producción.
> Origen: `docs/17-plan-pivote-plataforma-ca.md` §2 C4, R-p2, R-p3.
> **Revisado contra el código el 4-nov-2026 (HEAD `7b0bae6`).** Rutas y símbolos marcados [V] están verificados.
> Cuando algo de este prompt no calce con el código, gana el código: anotarlo en el commit y no inventar.

## 0. Antes de escribir código
- Leer `CLAUDE.md` (reglas 10, 11, 16, 17, 18), `docs/17-plan-pivote-plataforma-ca.md` §0, §2 C4, §3,
  `docs/decisiones.md` (Pivote).
- Código [V]: `app/models/tables.py` (`Usuario`, `SyncState`; el tipo `JSONB` con variante para SQLite está al
  inicio del archivo), `app/api/deps.py` (`html_require_user`, `html_require_admin`, `check_csrf`),
  `app/api/routes/pages.py` (`/admin/usuarios` y `POST /admin/usuarios/nuevo` ~l. 2349–2430, `/cuenta`, patrón
  HTMX de `/perfiles`), `app/auth/csrf.py`, `app/core/settings.py` (`mercado_regiones`, l. 128),
  `app/ingest/mercado.py`, `app/ingest/mercado_oc.py`, `app/ingest/orchestrator.py`, `app/core/retencion.py`,
  `app/catalogos/mercado.py` (`GRUPOS`, `FAMILIAS_ALIMENTOS`, `norm`), `app/api/templates/base.html` (menú),
  `tests/conftest.py`, y la plantilla real de Boris `data/mercado/proveedores_plantilla.xlsx` (hojas `precios`,
  `condiciones`, `logistica`, `Instrucciones`).
- No tocar la ingesta de licitaciones ni reactivar jobs: **la cuota es solo para Compra Ágil (R-p1)**.

## 1. Cambios

### 1.1 Esquema (`tables.py` + migración)
- `empresas`: `id`, `nombre`, `rut` (nullable, único si no nulo), `comuna_base`, `regiones_prioritarias`
  (JSONB lista de int; default `[16]`), `comunas_prioritarias` (JSONB lista de str), `activa` (bool),
  `creado_en`, `actualizado_en`.
- `usuarios.empresa_id` FK nullable → `empresas` (los usuarios actuales quedan sin empresa).
- `proveedores`: `id`, `empresa_id` FK NOT NULL, `nombre`, `rut` (nullable), `comuna`, `direccion`, `lat`,
  `lon`, `contacto_nombre`, `telefono`, `email`, `rubros` (JSONB lista de str; se sugieren las familias de
  `app/catalogos/mercado.py` [V], pero se acepta texto libre como el de la plantilla: "lácteos, abarrotes"), `plazo_pago_dias`,
  `minimo_compra_clp`, `despacha` (bool), `notas` (Text), `activo`, timestamps. Índice `(empresa_id, nombre)`.
- `proveedor_productos`: `id`, `proveedor_id` FK, `producto_id` **nullable** (FK a `producto_canonico` cuando
  exista F-plat-1; en esta fase es `Integer` nullable **sin FK**, se agrega la FK en F-plat-1), `producto_texto`
  (lo que el proveedor llama al producto), `codigo_unspsc` (nullable), `formato_texto`, `factor_base`
  (Float nullable), `unidad_base` (`kg|L|u`, nullable), `precio_neto_actual`, `vigente_desde` (Date),
  `fuente` (`manual|planilla|lista_llm`), `disponible` (bool), timestamps.
  `UNIQUE (proveedor_id, producto_texto, formato_texto)`.
- `costo_historial` (append-only, nunca se edita): `id`, `proveedor_producto_id` FK, `precio_neto`,
  `vigente_desde`, `fuente`, `usuario_id` (nullable: importaciones por script), `registrado_en`. Crear un
  `proveedor_producto` inserta su primera fila; cada cambio de `precio_neto_actual` inserta otra (misma
  transacción). Precio igual = sin fila nueva.
- `tarifas_logistica`: `id`, `empresa_id`, `ambito` (`comuna|provincia`), `nombre` (comuna o provincia de
  Ñuble: Diguillín, Itata, Punilla), `costo_fijo_clp`, `costo_por_kg_clp`, `dias`, `notas` (Text, nullable).
  `UNIQUE (empresa_id, ambito, nombre)`. Seed por empresa nueva: las 3 provincias con valores 0 (para que
  Boris los complete). No hay catálogo de comunas en el repo [V]: comuna = texto libre.
- Retención: ninguna de estas tablas se purga.

### 1.2 Territorio → ingesta (R-p2)
- `settings.mercado_regiones` ya existe [V] (default `[16]`) y queda como fallback.
- `app/mercado/territorio.py` (paquete nuevo `app/mercado/`, sin httpx): `regiones_activas(session, settings)
  -> list[int]` = unión ordenada de `regiones_prioritarias` de empresas **activas**; si vacía,
  `settings.mercado_regiones`. Test: empresas con `[16]` y `[16, 8]` → `[8, 16]`; sin empresas o todas
  inactivas → `[16]`.
- **Hay 5 consumidores del setting [V]** y los 5 pasan a `regiones_activas` (no solo `mercado-ca`):
  1. `sync_mercado_ca` (`app/ingest/mercado.py` ~l. 404);
  2. `Backfill.__init__` (`app/ingest/mercado.py` ~l. 626; hoy no recibe `session` en el constructor:
     resolver las regiones en `run_backfill` y pasarlas, sin cambiar la lógica de tramos de F-merc-1c);
  3. `mercado-oc` (`app/ingest/mercado_oc.py` ~l. 66);
  4. **la purga** (`app/ingest/orchestrator.py` ~l. 179, `regiones_protegidas`): si la ingesta usa las
     regiones de las empresas y la purga solo el setting, las CA de una región agregada se purgan a los 90
     días (regla 11 + excepción del mercado). Test: empresa con `[16, 8]` → una CA terminal vieja de la
     región 8 no se purga;
  5. `scripts/mercado/reabrir_ofertas_faltantes.py` (~l. 174, default cuando no viene `--region`).
  Al cerrar, `grep -rn mercado_regiones app scripts` solo debe mostrar `settings.py` y `territorio.py`.
- Agregar regiones aumenta el consumo de la cuota (regla 3) y el backfill no rellena hacia atrás los meses
  ya cerrados de la región nueva: el formulario de empresa lo advierte en una línea. Sin lógica extra.
- Comunas priorizadas no filtran la ingesta (la API no filtra por comuna; regla 7): se usan en F-plat-5.

### 1.3 Admin de empresas (`/admin/empresas`, solo admin)
- Lista, crear, editar (nombre, RUT, comuna base, regiones con selector múltiple de las 16 regiones, comunas
  de esas regiones como texto separado por coma, activa).
- Asignar y quitar usuarios (selector de usuarios sin empresa + lista de los de la empresa). Un usuario
  pertenece a una sola empresa.
- `POST /admin/usuarios/nuevo` (pages.py ~l. 2365 [V]) suma `empresa_id: int | None = Form(None)` y
  `admin_usuarios.html` el selector (opcional). Desactivar una empresa no borra nada: sus usuarios ven el
  aviso de "sin empresa" y su territorio deja de contar.

### 1.4 Proveedores (`/proveedores`, usuarios con empresa)
- Lista con búsqueda por nombre/rubro/comuna, ficha con contacto, condiciones, productos con precio actual,
  **variación histórica** por producto: tabla con % de variación respecto al precio anterior y al de hace
  90 días. `base.html` no trae Chart.js [V]: no agregar librerías JS en esta fase.
- Crear/editar proveedor y productos (formulario HTMX, mismo patrón que `_perfil_form.html`).
- Cambiar precio = nueva fila en `costo_historial`; nunca `UPDATE` sin historial.
- `/proveedores/plantilla.xlsx` (descargable) y `/proveedores/importar` (CSV o XLSX con `openpyxl`:
  **dependencia nueva**, justificar en el commit: lectura de planillas de proveedores; `pandas` no hace falta).
  **El formato es el de `data/mercado/proveedores_plantilla.xlsx` [V]** y la plantilla descargable se genera
  desde código con las mismas 4 hojas y encabezados:
  - `precios` (obligatoria): proveedor, rut, comuna, contacto, telefono, producto, formato, factor_base,
    unidad_base, precio_neto, vigente_desde. `precio_neto` es sin IVA y **por formato**, no por kg.
  - `condiciones` (opcional): proveedor, email, direccion, rubros (separados por coma), plazo_pago_dias,
    minimo_compra_clp, despacha (si/no), notas. Se importa por nombre de proveedor (para cumplir la meta
    de < 30 min de §4; corrige la nota de la hoja `Instrucciones` que dice que no se lee).
  - `logistica` (opcional): ambito, nombre, costo_fijo_clp, costo_por_kg_clp, dias, notas. Ignora filas
    con valores vacíos o con nombre entre paréntesis (ejemplo de la plantilla).
  - Filas cuyo proveedor empiece con "EJEMPLO" se ignoran y se informan en la vista previa.
  CSV: solo la hoja `precios`. Importación idempotente: misma fila dos veces no duplica ni genera historial
  si el precio no cambió; el proveedor se identifica por RUT si viene, si no por nombre normalizado dentro de
  la empresa. Vista previa antes de confirmar (n filas nuevas, n precios cambiados, errores por fila); la
  confirmación no relee el archivo del disco (regla 10): reenvía el archivo o guarda la vista previa en
  Postgres.
- "Mejor costo hoy" por `(producto_texto normalizado con app.catalogos.mercado.norm, unidad_base)` entre
  proveedores **activos** de la empresa, con `disponible=True`, comparando `precio_neto_actual / factor_base`
  (precio por kg, L o u); filas sin `factor_base`/`unidad_base` no compiten. Función
  `mejor_costo(session, empresa_id, producto_id | producto_texto)` en `app/mercado/proveedores.py` (la usa
  F-plat-4; en esta fase solo backend + test). Test: saco 25 kg a 27.500 gana a bolsa 1 kg a 1.300.
- Logística: `/proveedores/logistica` edita las tarifas de la empresa.

### 1.5 Permisos (R-p3, regla 17)
- Toda query de proveedores, productos, historial y tarifas filtra por `empresa_id` del usuario de sesión.
  Un usuario sin empresa ve `/proveedores` con un aviso ("pide al admin que te asigne a una empresa").
- Helper en `app/api/deps.py` (`html_require_empresa` o equivalente) que devuelve `(usuario, empresa_id)`.
- Tests: usuario de la empresa A no puede ver ni editar (GET y POST, 404 no 403) un proveedor de la B; admin sin
  empresa tampoco edita proveedores (solo administra empresas); admin **con** empresa ve y edita solo los de
  la suya (es el caso de Boris).
- CSRF en todas las mutaciones (regla 18).

### 1.6 Menú
Entrada "Mesa" en `base.html` con submenú Proveedores (y, más adelante, Mesa, Catálogo, Abastecimiento),
visible solo para usuarios con empresa. Las rutas antiguas no cambian.

## 2. Tests
Migración y modelos (SQLite y PG); `regiones_activas`; CRUD de empresa y asignación; ownership (1.5);
historial de costos (3 cambios → 3 filas, variación calculada); importación con planilla sintética (nueva,
repetida, precio cambiado, fila inválida); `mejor_costo`. `ruff check .`, `python -m mypy app`,
`python -m pytest -rs` (0 fallos, 0 errores, 0 saltados) contra Postgres local (`TEST_DATABASE_URL`,
contenedor `mp-test-pg`; `docs/operacion.md` §15). Tests que necesitan Postgres llevan el marcador `postgres`.

## 3. Cierre
Entrada en `app/changelog.py` ("Empresas, proveedores y costos con historial"). `docs/00-estado.md`.
Commit: `F-plat-3: empresa con territorio priorizado, proveedores y costos con historial`. `git add` solo de la fase.

## 4. Pasos de Boris
1. Migración (`docs/despliegue.md`; nunca `alembic upgrade` manual contra prod): fuera de 22:00–07:00,
   pausar `GH mercado` en cron-job.org → push → ver `Running upgrade` en los logs de Render → reactivar.
   Crear la empresa en `/admin/empresas` con región 16 y sus comunas prioritarias; asignarse y asignar a Mario.
2. Cargar 3 proveedores reales y ~40 productos con la plantilla (las 3 hojas); revisar la tarifa por provincia.
3. Medir: tiempo total (meta < 30 min) y cualquier campo que faltó → backlog de F-plat-5.
