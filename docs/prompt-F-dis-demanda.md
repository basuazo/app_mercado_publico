# Prompt — F-dis-demanda (Demanda MP: revisión de líneas, anexos con IA, productos, reglas e impuestos) · 7-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit, **sin push**, **sin migración**. Leer `docs/diseno/README.md`,
> `docs/diseno/decisiones-diseno.md` y la maqueta `Demanda.dc.html`. Leer también `docs/00-estado.md` (secciones
> F-plat-anexos-2, F-plat-auto-revision, F-plat-anexos-boton) y `docs/19-adjuntos-ca-explicacion-tecnica.md`.
> Requiere en `main`: F-dis-1 y el cierre de F-plat-impuestos. Puede correr en paralelo con F-dis-proveedores.
> **No cambia la lógica** de `anexos.py`, `anexos_carga.py`, `anexos_pendientes.py`, `auto_revision.py` ni
> `catalogo.py`: solo su presentación. Regla 9 vigente: los anexos llegan por subida manual, carga masiva o la extensión.

## 1. Estructura de `/catalogo` (Demanda MP)
Cinco pestañas-tarjeta (`m-tab`) con conteo: **Líneas por revisar** (`/catalogo/revision`), **Anexos**
(`/catalogo/anexos/pendientes` + carga), **Productos**, **Reglas por familia**, **Impuestos** (solo admin; la pestaña
que agregó F-plat-impuestos, vestida sin cambiar su lógica). La pestaña activa se recuerda (`usuario_vista('demanda')`).

## 2. Líneas por revisar (`catalogo_revision.html`, `_catalogo_linea.html`, `_catalogo_selector.html`)
- Orden: primero las que cierran antes. Cada fila: **texto del comprador** (`r-codigo`), CA · comprador · cierre,
  cantidad, **origen** (API, o anexo con archivo y página), producto propuesto (selector) con **confianza** y «Por qué»
  en el explicativo, cantidad en unidad base, «No es del catálogo» y «Confirmar». Confirmar muestra aviso «Rastrillo
  aprendió el alias» con **Deshacer** si la ruta existente lo permite; si no, sin Deshacer y anotarlo.
- «Confirmar las de confianza alta (N)» usa el endpoint existente de confirmar todas por CA, agrupado.
- **Auto-revisión:** líneas confirmadas por Rastrillo (`confirmada_por='auto'`) se ven con su puntaje. Las que el
  muestreo semanal mandó a **control** aparecen marcadas «Control» con «Está bien» / «Corregir» (funciones de
  `auto_revision.registrar_control`). Arriba: precisión de la semana, umbral actual y aviso si subió. Si
  `AUTO_CONFIRMAR_UMBRAL` está vacío: «Auto-revisión apagada» y nada de cifras inventadas.
- «Impuesto por clasificar» como señal en las líneas cuyo producto lo requiere.
## 3. Anexos (`catalogo_anexos_pendientes.html`, `catalogo_anexos_carga.html`, `_anexo_*.html`, `_anexos_carga_filas.html`)
- Estados de cada anexo: pendiente (la API no trae las líneas) → subido → **leyendo** (`r-brillo`) → leído con N líneas
  → resuelto / con páginas omitidas / no se pudo leer (motivo). Mapear desde `ca_anexos` y `ca_lineas` sin columnas nuevas.
- Pendientes: CA, comprador, cierre, «Abrir ficha» (la extensión sube el adjunto) y «Subir anexo»; «Abrir fichas» en
  bloque como hoy. Estado del token de la extensión: solo «configurado / no configurado», con enlace a Configuración.
- Carga masiva (archivos o ZIP) con la vista previa actual, vestida.
## 4. Productos y reglas
Productos del catálogo común (unidad base, familia, UNSPSC, n.º de alias, impuesto); ficha `catalogo_producto.html`
vestida. Reglas por familia (unidad e impuesto, «Por clasificar»): solo admin edita.
## 5. Salud (solo la parte de anexos)
En `salud.html`, bloque «Lectura de anexos (IA)»: anexos leídos y fallidos del mes, consumo de `llm_uso` del mes,
modelos configurados por nombre (nunca claves). El resto de Salud lo viste F-dis-config.
## 6. Tests y cierre
Render de cada pestaña con y sin datos; estados de anexo (uno por estado); control semanal con y sin umbral; admin vs no
admin en Impuestos y Reglas; nada de claves en el HTML. Cierre según `docs/diseno/README.md` §13.
Commit: `F-dis-demanda: Demanda MP con revisión de líneas, anexos e impuestos`. Changelog visible.
