# Prompt — F-dis-ventas (Ventas › Ofertas enviadas con estados de seguimiento) · 7-nov-2026

> Para Claude Code. **Modelo: Opus**: toca la máquina de estados de `ventas.py`. Un commit, **sin push**,
> **sin migración** (si resulta imprescindible, justificarla y avisar: no puede correr en paralelo con otra con
> migración). Leer `docs/diseno/README.md`, `docs/diseno/decisiones-diseno.md` (Ventas) y la maqueta `Ventas.dc.html`.
> Requiere en `main`: F-dis-1 y F-dis-datos (que refactorizó `ventas.condiciones_seguimiento`).
> Puede correr en paralelo con F-dis-lote (worktree propio).

## 1. Estados de seguimiento (derivados, sin columna nueva)
`ventas.estado_seguimiento(trabajo, venta, ahora) -> EstadoSeg` (función pura) que traduce `ca_trabajo` + `ventas` a:
- **Por resolver:** `enviada` (CA abierta) · `en_evaluacion` (cerró y no hay resultado).
- **Adjudicadas:** `pendiente_oc` (venta `adjudicada` sin OC) · `pendiente_despacho` (`oc_recibida`/`comprando`) ·
  `pendiente_factura` (`entregada`) · `pendiente_pago` (`facturada`).
- **Cerradas:** `completa` (`pagada`) · `no_ganada` (resultado perdida, desierta o revocada; guardar el motivo).
Con etiqueta, grupo y los 7 pasos de la barra. Tabla de verdad completa en el test.
`ventas.resumen_estados(session, empresa_id, ahora)`: conteo y monto bruto por estado, en una consulta (regla 12).

## 2. «Despachar sin OC» y factura que referencia la OC
- Decisión de Boris: a veces la OC no llega y hay que despachar igual. Permitir pasar de `adjudicada` a la entrega sin
  OC registrada; la OC queda **pendiente aparte** (`oc_numero` NULL) y la venta muestra la señal «Sin OC · ya
  despachada». Ajustar `_exigir_estado` solo para este camino; los demás no cambian.
- «Registrar OC» (N.º, fecha, monto bruto) se puede hacer en cualquier momento antes de facturar.
- **Registrar factura exige OC registrada** («Registra la OC primero»): validar en `ventas.py`, no solo en la vista.
  Formulario: N.º, fecha de emisión, monto bruto, plazo 30/45/60 → vencimiento; «Referencia: OC N.º …».
- «Cambiar a mano» el estado (para corregir o marcar «No ganada» con motivo), con evento y `version`.

## 3. Pantallas (`ventas.html`, `venta.html`, `caja.html`)
- Cifras: ganado 30 días, esperando resultado, tasa de adjudicación, por cobrar (bruto).
- **Barra de estados** con los tres grupos; cada estado filtra (conteo + monto), el nombre del grupo filtra el grupo,
  «Con algo pendiente», «Mostrando: X · Ver todas». Filtro recordado en `usuario_vista('ventas')`.
- Fila: enviada (fecha, quién), CA, ofertado, margen, estado + barra de 7 pasos, responsable, comentarios, señales con
  explicativo. Panel lateral (patrón de la Mesa): estado, ofertado, margen, P(ganar) al enviar, resultado si se perdió,
  **línea de tiempo** desde `ca_eventos`, conversación de la CA, documentos enviados, **un primario según el estado**,
  enlace al portal.
- **Planificar entrega** en el panel: plazo de la OC, modo (ruta propia / transportista / retira el comprador) con el
  costo calculado desde `tarifas_logistica` (función pura `logistica.costo_despacho(tarifas, comuna, provincia, kg)`,
  la misma que usará F-dis-proveedores), fecha (aviso si queda fuera de plazo), quién recibe, **margen tras el
  despacho**, guía opcional, «Marcar entregada».
- Registrar pago (fecha, monto recibido bruto). `/caja`: montos rotulados neto o bruto en cada cifra.

## 4. Tests y cierre
Tabla de verdad de `estado_seguimiento`; despachar sin OC; factura bloqueada sin OC (función y ruta); cambio manual con
conflicto de versión; `costo_despacho` (comuna manda sobre provincia; sin tarifa); resumen sin N+1.
Cierre según `docs/diseno/README.md` §13. Commit: `F-dis-ventas: estados de seguimiento, despacho sin OC y
planificación de entrega`. Changelog visible.
