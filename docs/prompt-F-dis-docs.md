# Prompt — F-dis-docs (Documentos de la empresa con archivo y vencimiento) · 7-nov-2026

> Para Claude Code. **Modelo: Opus**: guarda archivos en una base al 75 % de su cuota. Un commit, **sin push**,
> **con migración**. Leer `docs/diseno/README.md`, `docs/diseno/decisiones-diseno.md` (Paso 3 · Enviar, Documentos) y la
> maqueta `Documentos.dc.html`. Requiere F-dis-1 y F-dis-lote en `main` (usa el paso Enviar).
> Puede correr en paralelo con F-dis-mercado (worktree propio). **No** en paralelo con otra fase con migración.

## Decisión a confirmar por Boris antes de correr
Dónde viven los archivos. Por defecto (si Boris no dice otra cosa): **en Neon, tabla aparte con `bytea`**, porque el
stack no tiene almacenamiento de archivos y son pocos documentos. Límites duros: 5 MB por archivo, 30 MB por empresa,
solo PDF/PNG/JPG/DOCX validados por firma (como los anexos). Se guardan las 3 últimas versiones; las más viejas se borran.
`/salud` muestra el espacio usado por documentos. Si la purga del legado (F-plat-purga-legado) aún no corrió, avisar del
espacio disponible en el informe.

## 1. Modelo
- `empresa_documentos` (`id`, `empresa_id`, `tipo` (`resolucion_sanitaria|f30|vigencia_sociedad|registro_proveedores|
  declaracion_jurada|erut|ficha_tecnica|otro`), `nombre`, `emisor`, `vence_en` (date, nullable), `plantilla` (bool),
  `empresa_producto_id` (nullable, para fichas técnicas), `activo`).
- `empresa_documento_versiones` (`id`, `documento_id`, `archivo` bytea, `nombre_archivo`, `tipo_mime`, `bytes`,
  `sha256`, `subido_por_id`, `subido_en`). El binario **no** se carga al listar (columna diferida).
- Relación con requisitos: `empresa_requisitos` sigue siendo el texto que pide la CA; `ca_requisitos` puede apuntar a
  un `documento_id` para «Usar la guardada». Revisar `requisitos.py` y reutilizar.

## 2. Funciones (`app/mercado/documentos.py`)
`estado(doc, hoy)` → `vigente | por_vencer (≤ 30 días) | vencido | sin_vencimiento`; `listar`, `subir_version`,
`reemplazar`, `descargar` (con control de empresa), `uso(doc)` (ofertas que lo adjuntaron y CA abiertas que lo piden).
Evento `documento_por_vencer` 30 días antes y `documento_vencido` (job diario existente, único por documento y fecha),
que aparecen en la campana y en «Te necesita» (tipo `documentos`, si F-dis-datos lo permite sin reescribirlo; si no,
dejarlo anotado).

## 3. Pantalla `/documentos` (`documentos.html`)
Como `Documentos.dc.html`: cifras (vigentes, por vencer, vencidos, pedidos en CA abiertas), filtros (Todos, Por vencer o
vencidos, Sin vencimiento), lista con estado y señal explicada, panel con vista previa (PDF en `<iframe>` con
`Content-Disposition: inline` y `X-Frame-Options` acorde), fecha de vencimiento editable, «Se usa en», versiones,
«Reemplazar archivo» (primario si vence o venció), «Descargar». Tarjeta «Datos para la cotización» (lee
`empresas`; se edita en Configuración). Menú: «Documentos» deja de apuntar a `/cuenta`.
En el paso Enviar (F-dis-lote), «Usar la guardada» toma la versión vigente; un documento vencido bloquea «Marcar
enviada» si el requisito es obligatorio.

## 4. Tests y cierre
Estados por fecha (bordes 30 días, vencido hoy), límites de tamaño y tipo, empresa ajena (404), versiones (se guardan 3),
descarga con control, evento único, requisito obligatorio con documento vencido bloquea el envío.
Cierre según `docs/diseno/README.md` §13. Commit: `F-dis-docs: documentos de la empresa con vencimiento y versiones`.
Changelog visible.
