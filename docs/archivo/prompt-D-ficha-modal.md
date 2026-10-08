# Prompt de diseño — Ficha en modal (D-ficha-modal) · 24-sep-2026

> Reemplaza al flujo genérico "Cowork → Design". La auditoría (Fase 1) ya existe:
> `docs/13-auditoria-ux.md`. Este prompt es la Fase 2, anclada a esa auditoría y a lo
> verificado EN VIVO el 24-sep en https://app-mercado-publico.onrender.com.
> Plantilla reutilizable para otros módulos: cambiar §2, §4 y §6; §1, §3 y §5 se mantienen.

---

## 1. Contexto de negocio (no cambia entre módulos)
mp-oportunidades: app interna para 3–10 personas de un mismo equipo que buscan en qué
compras públicas chilenas presentarse (Licitaciones v1 y Compras Ágiles v2 de la API
oficial de Mercado Público). La decisión que la UI debe servir es una sola:
**¿me presento o no, y antes de cuándo?** Lo que pesa, en orden: fecha y HORA de cierre
(hora de Chile), estado real del proceso, monto, organismo, qué piden (ítems/productos),
match con mi perfil y competencia (ofertas recibidas / quién ganó antes).

## 2. El problema de este módulo (verificado en vivo)
- Clic en título o "Ver ficha" navega a `/oportunidad/{fuente}/{codigo}`. "Volver" y la miga
  "Dashboard" apuntaban a `/` y perdían los filtros (parchado en F-ficha-volver; el botón
  Atrás del navegador sí conservaba filtros y scroll).
- La ficha quedó con el diseño viejo: estado como slug gris ("publicada"), match en badge
  amarillo sin rótulo, razones como chips grises indiferenciados, "Volver" metido entre las
  acciones primarias, atribución duplicada, acciones al final del scroll.
- CA con match cuyo detalle aún no se baja (286 de 1298 al 24-sep, doc 00): la ficha muestra solo organismo, región, publicación y
  "0 ofertas", sin avisar que falta descripción/productos.
- Licitaciones: organismo "—" y sin región (deuda de datos, ver §6).

## 3. Reglas fijas (no negociables)
- **Stack de salida**: el diseño se implementará en Jinja2 + HTMX 1.9 + Bootstrap 5.3 (CDN) +
  `app/api/static/app.css`. Nada de React, Tailwind ni librerías nuevas. El mockup puede
  ser HTML/CSS libre, pero cada pieza debe tener equivalente directo en clases Bootstrap o
  tokens existentes.
- **Tokens** (ya en `app.css`): superficies `--bg-app #F8FAFC`, `--bg-surface #FFF`,
  `--bg-subtle #F1F5F9`; bordes `#E2E8F0`/`#CBD5E1`; texto `#0F172A`/`#475569`/`#64748B`;
  marca `#1D4ED8`. Familias de estado (Abierta verde, En evaluación azul, Adjudicada
  violeta, Completada verde oscuro, Sin efecto rojo, Desconocido gris) con icono + texto.
  Urgencia ≤1d `#DC2626`, ≤3d `#EA580C`, ≤7d `#D97706`, >7d `#CBD5E1`. Match ≥60 verde,
  40–59 ámbar, <40 gris, rotulado "Match". Chips: match azul, oportunidad verde,
  advertencia ámbar.
- **Densidad**: la de la tarjeta actual del feed. Texto terciario nunca bajo 14px; objetivos
  táctiles ≥24px; filas de tabla 44px cómodo / 36px compacto. No "estilo dashboard
  financiero": ya está resuelto.
- **Datos**: montos `$12.500.000`; ausente → "No informado" escrito; cierre con día, hora y
  "hrs"; `tabular-nums` en números. "Fuente: Dirección ChileCompra" UNA vez, visible.
- **Accesibilidad**: estado nunca solo por color; `aria-pressed` en toggles; foco atrapado y
  devuelto; anuncio en la región `role="status"` existente.
- **Honestidad de datos**: si falta el detalle, decirlo ("La app todavía no descarga la
  descripción ni los productos de esta Compra Ágil"), no dejar huecos.

## 4. Qué diseñar
Modal (Bootstrap `modal-dialog-scrollable`, `modal-xl` en escritorio, pantalla completa bajo
`md`) que se abre sobre el feed sin perderlo:
1. **Cabecera fija**: tipo + código, título (máx. 2 líneas), fila de estado · cierre con
   urgencia · monto · anillo de match. Cerrar (×) arriba a la derecha.
2. **Barra de acciones fija bajo la cabecera** (no al final): primaria "Abrir en Mercado
   Público ↗" (o el texto de por qué no está disponible), luego Activar alertas / Me sirve /
   Descartar con los mismos estados que la tarjeta. Sin "Volver": cerrar es la × y Esc.
3. **Resumen** siempre visible: organismo, región, publicación, ofertas recibidas, razones del
   match tipificadas (máx. 3 + "+N"), aviso de detalle faltante si aplica.
4. **Pestañas**: Descripción · Ítems/Productos (N) · Competencia (solo si hay datos). Pestaña
   sin datos no se muestra.
5. **Navegación entre fichas**: "‹ Anterior / Siguiente ›" dentro del modal recorriendo el feed
   filtrado actual (evita cerrar y reabrir 20 veces).

Artboards: escritorio 1440 (feed atenuado detrás) con (a) licitación con 5 ítems, (b) CA sin
detalle, (c) licitación adjudicada con competencia; móvil 390 con (a).

## 5. Criterios de aceptación del diseño
- Las acciones están visibles sin scroll en los tres casos.
- En ≤3 s se lee: estado, cierre (fecha+hora), monto, organismo.
- Ningún dato aparece dos veces (cierre, atribución, estado).
- Cada elemento tiene su clase Bootstrap/token equivalente anotada.
- Se evalúa contra la matriz del §2 de `docs/13-auditoria-ux.md`, fila por fila.

## 6. Dependencias de datos (antes o en paralelo a implementar)
- **Organismo de licitaciones [V código + doc 10]**: `_parse_licitacion_detalle` lee
  `CodigoOrganismo` en el primer nivel, pero en el detalle v1 viene bajo
  `Comprador.CodigoOrganismo` (dump real en `docs/10-enlace-ficha.md` §2.a). Por eso queda
  NULL. Además la ficha muestra el código, no el nombre. [I] `Comprador` probablemente trae
  también nombre y región; confirmar con `scripts/smoke_test.py` (lo corre Boris).
- **Estado desactualizado [V en vivo + código]**: 1417913-96-L126 figura "Abierta" con "Cerró el
  22/07". `refresh_estados` solo re-consulta cierres en −7/+3 días y `sync_activas` no cierra
  lo que desaparece del listado de activas: lo que pasó la ventana queda "publicada" para
  siempre. [I] Cuántas hay: no se pudo contar (Neon no es alcanzable desde esta sesión).
  Mientras tanto, el modal y la tarjeta deben mostrar "Cierre vencido" en vez de "Abierta"
  cuando `fecha_cierre < ahora` y el estado sigue abierto.

*Fuente de los datos de dominio: Dirección ChileCompra.*
