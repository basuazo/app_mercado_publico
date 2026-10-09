# Decisiones vigentes — mp-oportunidades

*Registro corto de decisiones que siguen mandando. Rescatadas de `archivo/12-free-vs-pago.md`,
`archivo/11-auditoria-ping-y-arquitectura.md`, los handoffs de sep y `archivo/00-estado-actual.md`.
Agregar al final con fecha; si una se revierte, tacharla y anotar por qué.*

## Infraestructura
- **Jobs en GitHub Actions, web en Render (Opción D, 22-sep).** Render free apaga el proceso 15 min
  después de la última request aunque haya un job corriendo, y Render puede suspender servicios
  free con mucho tráfico saliente. Actions da historial, logs y aviso de fallas gratis, y Neon
  despierta menos veces. [V] (`archivo/12-free-vs-pago.md` §2, §5)
- **Disparo externo con cron-job.org → `workflow_dispatch` (25-sep).** El `schedule` de GitHub
  llegó 2 a 5,5 h tarde y descartó 18 de 24 corridas horarias de `ca`. [V]
- **Render duerme a propósito (F-invertir-modelo).** No hay keepalive; `/api/salud/ping` no toca la
  base para no mantener Neon despierta.
- **Neon free mientras la base esté < 70 %** (27-sep). Launch: almacenamiento < US$1/mes; el costo
  real sería cómputo (~US$5–20/mes). Revisar al 70 % o si se agotan las CU-horas.
- **Correo por API REST de Brevo** (Render bloquea SMTP saliente).

## Datos y API
- **429 código 10500 = concurrencia, no cuota** (verificado 22-sep): 3 reintentos 30/60/120 s y
  cortar. Otro 429 = tope diario. Tras 504/timeout, 60 s de enfriamiento.
- **Detalle de CA inevitable:** el listado v2 no trae descripción ni productos [V]. Por eso
  `detalles-match` va aparte, con tope por tiempo (20 min día / 120 noche) y contador de fallos.
- **`ca` cada hora** (en punta hay ~2.000 cambios/h; la API entrega ~42 CA/min).
- **Plan Anual:** máximo una carga cada 28 días; búsqueda inversa por descripción (el
  `codigo_producto` del PAC no es UNSPSC), solo el año en curso.
- **FTS:** toda tsquery pasa por `inmutable_unaccent` (F-acentos).

## Producto
- **Dashboard solo vigente + "Mi registro"** (guardadas, cerradas, vencidas recientes con piso 30,
  descartadas, archivadas).
- **Vigente** = por fecha de cierre antes que por estado; **CA sin cierre = vigente solo si se
  publicó hace ≤ 7 días** (27-sep). Una sola definición en `app/core/vigencia.py` (F-ajustes).
- **Guardar** unifica "Me sirve" + "Activar alertas" (= `OportunidadSeguida`, F-guardar).
- **Match de CA por rubro no se automatiza:** explorador de CA + rubros favoritos; solo
  "confirmados" por defecto + palabras elegidas por la persona (F-ca-vocab). F-ca-rubro → próxima
  versión.
- **Ficha en modal** sobre el feed y Mi registro (URL con `ficha=`); la página `/oportunidad/...` se
  mantiene porque la enlazan los correos.
- **Exclusiones solo sobre el título** (09-oct, Boris): una palabra excluida saca la oportunidad
  solo si está en su nombre, no por la descripción ni por un ítem ("agua" en un ítem de una
  licitación de adulto mayor no la saca). Sin flag por perfil. Se implementa en F-match-1 §1.7.
- **Relevancia del match** (F-match-1, 09-oct, Boris tras simular contra producción): keyword en el título
  50; solo en ítem o descripción 35; cada keyword adicional distinta +10 (máx. +20); rubro +20;
  organismo +15; sin keyword, rubro u organismo 40 (55 ambos); tope 100. Urgencia y competencia no
  suman: ordenan. Piso del feed 40, "Alta" 60.
- **Descartar** tiene dos caminos: "Solo descartar" y "Descartar y excluir términos" (valida
  choques antes de descartar).
- Perfil 8 (30 rubros) es de prueba; los perfiles reales (5, 6, 7) son por keywords.

## Forma de trabajo
- Prompts de fase auditados en Cowork; Claude Code ejecuta; un commit por fase, sin push hasta
  auditar. Sonnet para fases acotadas; Opus para cuota/429/lock o borrado de datos.
- **Toda auditoría termina con un paso a paso de cómo seguir** (comando exacto, quién, qué esperar,
  qué hacer si falla).
- Investigación: no afirmar un negativo sin consultar la fuente primaria; marcar [V]/[I].
- **Plan Anual en pausa (09-oct):** el ZIP completo da 403 desde GitHub Actions (200 desde el PC de Boris).
  No se carga el PAC 2026; se retoma cuando se publique el PAC 2027 (en 1–2 meses), con carga
  mensual desde el PC de Boris. Mientras tanto `/plan-anual` muestra pocos datos (819 líneas).
- **Retención nunca borra lo que un usuario tocó (09-oct):** guardadas (también archivadas) y
  descartadas conservan fila, descripción, ítems y competencia para analizar perfiles. La purga de
  filas (F-retencion-filas) solo alcanza oportunidades terminales sin ninguna relación con usuarios.
