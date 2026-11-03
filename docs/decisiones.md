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
  cortar. ~~Otro 429 = tope diario.~~ (reemplazado el 29-oct por F-429-tasa, ver abajo). Tras
  504/timeout, 60 s de enfriamiento.
- **429 código 429 ("límite de tasa") = transitorio; ningún 429 agota el día** (29-oct, F-429-tasa;
  reemplaza a "otro 429 = tope diario"). Evidencia [V, logs de Actions 18–29 oct]: llegó con 8
  requests (23-oct) y 21 (24-oct) en la primera corrida del día y otro día tras ~1.700; tras el
  429 de la 01:05 del 29-oct y un `UPDATE` manual de `quota_log`, `ca` hizo 113 requests con 200
  el mismo día. El tope diario de 10.000 nunca se ha visto en un 429 real. Política: sin
  reintento, pausa persistida en Postgres de 20 min (`PAUSA_TASA_MIN`; 60 min para un código no
  visto, `PAUSA_429_DESCONOCIDO_MIN`, con log ERROR), cortar la corrida guardando el avance.
  `marcar_agotado` queda sin uso hasta verificar un código de tope diario: un falso positivo
  cuesta un día entero de ingesta (`15-diagnostico-ca-429.md`).
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

## Pivote (31-oct)
- **Pausa de la ingesta actual (31-oct, Boris):** la app la usan Boris y Mario; el foco pasa al pivote de
  Compra Ágil en Ñuble (`16-pivote-compra-agil-nuble.md`). Se apagan en cron-job.org los disparos de `ca`,
  `ciclo-ca`, `ciclo-match`, `ciclo-activas`, `nocturno`, `resumen`, `catalogos` y `retencion` (retención
  también, para no purgar CA de Ñuble antes de F-merc-1 §1.7). La web en Render queda como está. Una sola
  key: `MP_TICKET` queda entera para el pivote.
- **Alimentos es el giro principal del pivote**; los demás rubros son expansión, no prioritarios.
- **Backfill diurno (2-nov-2026, Boris):** excepción a la regla 5 de `CLAUDE.md` mientras la ingesta de la
  app esté en pausa: `scripts/backfill_cot_nuble.py --diurno` puede correr fuera de 22:00–07:00 si no supera
  0,5 req/s (el ritmo de producción) ni 2.000 requests por corrida (el script baja con aviso un `--rps` o un
  `--max-requests` mayores). La espera tras un 429 y `--max-pausas` funcionan igual que de noche, y el modo se
  fija al arrancar: una corrida diurna que entra a la ventana nocturna sigue con los límites diurnos. Sin
  `--diurno` nada cambia (fuera de ventana no emite). Al retomar la ingesta de la app, la excepción deja de valer.
- **Plataforma de intermediación de CA (3-nov-2026, Boris confirma `17-plan-pivote-plataforma-ca.md`):**
  solo Compra Ágil y cuota solo para CA (regla 24); app actual congelada, el pivote vive al lado con el mismo
  login y base (`/mesa`, `/proveedores`, `/catalogo`, `/abastecimiento`); empresas con territorio priorizado y
  usuarios asignados por el admin (reglas 25 y 26); anexos: sonda a la API primero y, si no hay descarga, carga
  manual desde la ficha pública con bandera "CA sin detalle" y corrección en la plataforma; extracción de anexos y
  listas de precios con modelo de lenguaje vía OpenRouter (modelo elegido por set de evaluación de 30 anexos, el
  más barato con ≥ 95 % de líneas correctas; presupuesto mensual persistido en Postgres y caché); proveedores y
  costos se cargan desde cero. Orden de fases: F-merc-1 → F-merc-2 → F-plat-3 → F-plat-1 → F-plat-2 → F-plat-4 →
  F-plat-5 (4 semanas en sombra) → F-plat-6 → F-plat-7.
