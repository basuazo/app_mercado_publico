# Prompt — F-marca (la app pasa a llamarse «Rastrillo») · 4-nov-2026

> Para Claude Code. **Modelo: Sonnet** (reemplazo de texto). Un commit chico, **sin push**, **después** del commit de
> F-merc-3: no mezclar los dos. Sin migración. Decisión de Boris (4-nov).

## Cambio
- Reemplazar "MP Oportunidades" por **"Rastrillo"** en las 23 plantillas que lo nombran [V, `grep -rln "MP
  Oportunidades" app tests`]:
  - la marca de la barra lateral y de la barra móvil (`base.html`);
  - la tarjeta del login (`login.html`);
  - los `{% block title %}… — MP Oportunidades{% endblock %}` de cada página;
  - el enlace "Ver ficha en MP Oportunidades" del correo (`app/alerts/templates/alerta_seguimiento.html`).
- Revisar también los `.py` (título de FastAPI, asuntos de correo, `changelog.py` y `/salud`) con
  `grep -rn "MP Oportunidades\|mp-oportunidades" app`. El nombre del paquete, del repo (`app_mercado_publico`) y del
  servicio de Render **no** cambian.
- Tests que busquen el texto viejo: actualizarlos.
- Changelog: "La plataforma ahora se llama Rastrillo".
- `README.md` y `docs/00-estado.md`: una línea con el nombre nuevo. `CLAUDE.md` sigue titulado `mp-oportunidades`
  (nombre técnico); solo se agrega "(app: Rastrillo)".

`ruff check .`, `python -m mypy app` y `python -m pytest -rs` contra `mp-test-pg`. Commit:
`F-marca: la app se llama Rastrillo`.
