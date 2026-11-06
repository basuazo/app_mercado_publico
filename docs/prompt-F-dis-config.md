# Prompt — F-dis-config (Configuración: preferencias, avisos, empresa, usuarios y salud) · 7-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit, **sin push**, **con migración** (una columna). Leer
> `docs/diseno/README.md` y la maqueta `Admin.dc.html`. Requiere F-dis-1 en `main`. Va **sola o en paralelo con una fase
> sin migración** (no con F-dis-docs ni F-dis-lote-datos).

## 1. Ruta y estructura
`/cuenta` pasa a ser **Configuración** con pestañas: **Mis preferencias** (todos), **Empresa**, **Usuarios**, **Salud
del sistema** (solo admin; reusan `/admin/empresas/{id}`, `/admin/usuarios` y `/salud`, que redirigen a la pestaña o
se renderizan dentro). El pie del menú lleva aquí.
## 2. Mis preferencias
- Apariencia: tema (Claro / Oscuro / Según el sistema), densidad (Cómoda / Compacta), pantalla de inicio: las columnas de
  F-dis-1, ahora con el diseño de la maqueta y cambio en vivo.
- Atajos de teclado (lista de `rastrillo.js`, desde una constante compartida para no duplicar).
- **Qué te avisamos y por dónde:** tabla de tipos de aviso × canal. La campana registra todo siempre. Canal **correo**
  por tipo: agregar `preferencias_alarma.canal` (`app|correo`, por defecto `app`) o una columna `por_correo` (bool), lo
  que encaje mejor con `PreferenciaAlarma` y `app/alerts/email.py`; tipos: mención, asignación, cierre próximo de
  oferta en armado, resultado, OC recibida, pago atrasado, documento por vencer. **WhatsApp no se ofrece** (no hay envío
  por WhatsApp; solo enlaces). Resumen diario por correo (`dias_resumen` existente). Silencio fuera de horario
  (20:00–8:00 y fines de semana) salvo cierres: constante documentada.
- Cambiar contraseña (lo que ya hay en `/cuenta`).
## 3. Empresa (admin)
Datos para la cotización (razón social, RUT, giro, dirección, contacto), **logo** (PNG/SVG ≤ 500 KB validado por firma,
se usa en la cotización; si no hay, la cotización va sin recuadro), **territorio por comunas** (las regiones y comunas
priorizadas que ya existen; reglas 25), token de anexos (generar/rotar, como hoy).
## 4. Usuarios y Salud (admin)
Usuarios con rol (los roles que existen hoy; **no** crear «Solo lectura» sin decisión de Boris: dejar la pregunta en el
informe), invitar, activar/desactivar, último acceso. Salud: el `/salud` actual vestido como lista de servicios con
estado (API, cuota, lectura de anexos, cruce de resultados y OC, respaldo, espacio en la base) — nunca claves.
## 5. Tests y cierre
Preferencias de avisos por canal respetadas por el envío de correo (test con el mailer mockeado), silencio fuera de
horario, logo inválido rechazado, no admin no ve Empresa/Usuarios/Salud, nada de secretos en el HTML.
Cierre según `docs/diseno/README.md` §13. Commit: `F-dis-config: Configuración con preferencias, avisos por correo,
empresa, usuarios y salud`. Changelog visible.
