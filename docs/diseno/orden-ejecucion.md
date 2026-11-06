# Orden de ejecución de las fases F-dis (7-nov-2026)

Reglas: una fase por sesión de Claude Code y un commit por fase, en su rama `F-dis-<nombre>`. Desde F-ci (6-nov) la
sesión **hace push de su rama y abre un PR** hacia `main` (nunca push a `main`); el CI tiene que quedar verde. Cuando dos
fases corren al mismo tiempo, cada una va en su propio `git worktree` (o su propia sesión en la nube) y con su propia base
de tests. **Nunca dos fases con migración al mismo tiempo.** Antes de pasar al paso siguiente, Boris revisa y fusiona el
PR, corre `migrar` → `prod` si la fase trae migración, y deploy.
Para correr una fase: «Lee `docs/prompt-F-dis-<nombre>.md` y ejecútalo».

| Paso | Sesión A | Modelo | Sesión B (en paralelo) | Modelo | Migración | Requiere |
|---|---|---|---|---|---|---|
| 0 | Push del arreglo de F-plat-impuestos, deploy y sembrar impuestos (Boris) | — | `F-plat-purga-legado` si sigue pendiente (se puede correr junto con el paso 1) | Opus | no | — |
| 1 | `F-dis-1` · fundamentos, menú nuevo, preferencias | **Opus** | — (o la purga) | — | **sí** | paso 0 |
| 2 | `F-dis-datos` · Hoy y panel de la CA (datos) | **Opus** | `F-dis-paleta-campana` · ⌘K y campana con destino | Sonnet | no | 1 |
| 3 | `F-dis-hoy-mesa` · vestir Hoy y Mesa | Sonnet | `F-dis-lote-datos` · hilo por lote, mover CA, recordar, presencia | **Opus** | **sí** (B) | 2 |
| 4 | `F-dis-lote` · En trabajo, modal por línea, Enviar | **Opus** | `F-dis-ventas` · estados de seguimiento, despacho sin OC | **Opus** | no | 3 |
| 5 | `F-dis-docs` · documentos con vencimiento | **Opus** | `F-dis-mercado` · Competencia, Estrategia, Abastecimiento, Actividad | Sonnet | **sí** (A) | 4 |
| 6 | `F-dis-proveedores` · vista por producto, inventario, logística | **Opus** | `F-dis-demanda` · líneas, anexos con IA, impuestos | Sonnet | no | 5 |
| 7 | `F-dis-config` · preferencias, avisos por correo, empresa, usuarios, salud | Sonnet | — (puede arrancar junto con el paso 6 si quieres tres sesiones) | — | **sí** | 1 (y 6 para Salud) |
| 8 | `F-dis-cierre` · sin Bootstrap, oscuro y accesibilidad | Sonnet | — | — | no | todas |

Por qué este orden:
- `F-dis-datos` refactoriza `ventas.condiciones_seguimiento`, por eso `F-dis-ventas` va después.
- `F-dis-hoy-mesa` viste `_equipo_hilo.html`, que después reutilizan En trabajo y Ventas.
- `F-dis-proveedores` usa `logistica.costo_despacho`, que crea `F-dis-ventas`.
- `F-dis-docs` conecta con el paso Enviar de `F-dis-lote`.
- Las pantallas que tocó F-plat-impuestos (Mesa, En trabajo, Proveedores, Demanda MP, Caja, Hoy) van después del paso 0.

Decisiones pendientes de Boris que algunos prompts dejan con un valor por defecto:
- `F-dis-docs`: guardar los archivos en Neon (por defecto; límite 5 MB por archivo y 30 MB por empresa) u otro lugar.
- `F-dis-config`: si hace falta el rol «Solo lectura».
- `F-dis-lote`: si la cotización debe salir también en PDF (exige una dependencia nueva).
