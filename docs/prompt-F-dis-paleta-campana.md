# Prompt — F-dis-paleta-campana (búsqueda de la paleta ⌘K y campana que lleva a la acción) · 7-nov-2026

> Para Claude Code. **Modelo: Sonnet.** Un commit, **sin push**, **sin migración**.
> Leer `docs/diseno/README.md` y las maquetas `Paleta.dc.html` y `Campana.dc.html`. Requiere F-dis-1 en `main`.
> Puede correr en paralelo con F-dis-datos (worktree propio): no comparten archivos.

## 1. Búsqueda (`app/mercado/busqueda.py`, puro, filtrado por `empresa_id`)
`buscar(session, empresa_id, regiones, q, limite_por_grupo=6) -> dict[grupo, list[Resultado]]` con `Resultado(titulo,
sub, url, tipo)`. Grupos y fuentes:
- **Compras Ágiles:** código exacto o nombre (ILIKE escapado, como el buscador de la Mesa) de CA vigentes del
  territorio y de CA con trabajo de la empresa. `sub`: comprador · cierre · estado de trabajo.
- **Lotes:** lotes abiertos de la empresa por nombre o número. `sub`: N CA · estado.
- **Productos:** productos propios (`empresa_productos`) y de proveedores por nombre. `sub`: stock de bodega y el
  precio más barato con su antigüedad («Precio desactualizado · N días» si corresponde).
- **Proveedores:** por nombre o RUT.
- **Ir a:** las pantallas del menú (lista fija, la misma de F-dis-1).
Mínimo 2 caracteres; consultas en lote (regla 12); `limite_por_grupo` por grupo.
Sin `q`: «Ir a» + **Acciones sugeridas** (los 4 primeros de `hoy.te_necesita` si F-dis-datos ya está en `main`; si
no, omitir el grupo) + **CA recientes** (las 3 últimas abiertas por el usuario: `usuario_vista('reciente')`).

## 2. Ruta y paleta
`GET /buscar?q=` (`html_require_empresa`) devuelve el fragmento `_paleta_resultados.html` (grupos con cabecera,
opciones con `role="option"`, `aria-selected`, atajos visibles). `rastrillo.js` de F-dis-1: reemplazar la lista
fija por `hx-get` con `hx-trigger="input changed delay:150ms"`; `↑`/`↓` mueven la selección, `↵` abre, `Esc` cierra.
Registrar CA recientes al abrir una CA o su panel (`guardar_vista('reciente', …)`, máximo 10).

## 3. Campana con destino
- Cada evento de `ca_eventos` que muestra la campana lleva una **URL de destino** calculada en
  `equipo.destino_evento(evento) -> str` (función pura, sin cambiar el esquema):
  mención o comentario en un lote → `/lotes/{id}#hilo-del-lote`; en una CA con lote activo → `/lotes/{id}?ca={codigo}`;
  en una CA con venta → `/ventas/{id}`; cierre próximo de oferta en armado → el lote; oferta lista → paso Enviar del
  lote; asignación → `/mesa?ca={codigo}`; evento de venta → la venta; documento por vencer → `/documentos` (o
  `/cuenta` si F-dis-docs no está).
- `_campana_lista.html` con el diseño de `Campana.dc.html`: cada aviso muestra su destino («Ir al lote →»), se marca
  leído al abrirlo (`/actividad/leido` ya existe) y «Ver todo el registro» va a `/actividad`.

## 4. Tests y cierre
`buscar` por grupo, empresa ajena invisible, ILIKE escapado (`%`, `_`), límite, sin N+1 (contar consultas);
`destino_evento` con un caso por tipo de evento; render de la paleta y la campana en claro y oscuro.
Cierre según `docs/diseno/README.md` §13. Commit: `F-dis-paleta-campana: búsqueda ⌘K y campana con destino`.
Changelog: «La paleta ⌘K busca CA, lotes, productos y proveedores; cada aviso lleva a donde se actúa».
