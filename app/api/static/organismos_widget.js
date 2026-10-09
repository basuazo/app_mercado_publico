/* Selector de organismos de /perfiles: chips + búsqueda sin tildes sobre el catálogo
   JSON, que se pide UNA vez por página (GET /organismos/catalogo.json, cacheable). */
(function () {
  "use strict";

  const MAX_RESULTADOS = 80;
  const { debounce, crearChip } = window.MP;
  let catalogoPromesa = null;

  function sinTildes(texto) {
    return String(texto || "")
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase();
  }

  function pedirCatalogo() {
    if (!catalogoPromesa) {
      catalogoPromesa = fetch("/organismos/catalogo.json", { credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : []))
        .catch(() => [])
        .then((filas) =>
          filas.map((o) => ({ id: String(o.id), nombre: o.nombre, sector: o.sector, buscar: sinTildes(o.nombre + " " + (o.sector || "")) }))
        );
    }
    return catalogoPromesa;
  }

  function initWidget(widget, catalogo) {
    const buscador = widget.querySelector(".js-org-buscador");
    const chipsBox = widget.querySelector(".js-org-chips");
    const lista = widget.querySelector(".js-org-lista");
    const hidden = widget.querySelector(".js-org-hidden");
    const manual = widget.querySelector(".js-org-manual");

    if (catalogo.length === 0) {
      // Sin catálogo (aún no sincronizado): se escriben los códigos a mano.
      widget.querySelector(".js-org-con-catalogo").classList.add("d-none");
      manual.classList.remove("d-none");
      hidden.type = "text";
      hidden.className = "form-control";
      hidden.placeholder = "ej: 12345,76123456-7";
      return;
    }

    const porId = new Map(catalogo.map((o) => [o.id, o]));
    const seleccionados = new Map();
    (widget.dataset.preseleccion || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
      .forEach((codigo) => {
        seleccionados.set(codigo, porId.get(codigo) || { id: codigo, nombre: codigo, sector: null });
      });

    function actualizarHidden() {
      hidden.value = Array.from(seleccionados.keys()).join(",");
    }

    function renderChips() {
      chipsBox.innerHTML = "";
      seleccionados.forEach((org, codigo) => {
        chipsBox.appendChild(
          crearChip(org.nombre, () => {
            seleccionados.delete(codigo);
            actualizarHidden();
            renderChips();
            renderLista(buscador.value);
          })
        );
      });
    }

    function renderLista(query) {
      const q = sinTildes(query.trim());
      lista.innerHTML = "";
      if (!q) {
        lista.innerHTML = '<div class="text-muted px-1">Escribe para buscar entre ' + catalogo.length + " organismos…</div>";
        return;
      }
      let sectorActual = null;
      let mostrados = 0;
      let coinciden = 0;
      for (const org of catalogo) {
        if (seleccionados.has(org.id) || !org.buscar.includes(q)) continue;
        coinciden += 1;
        if (mostrados >= MAX_RESULTADOS) continue;
        if (org.sector !== sectorActual) {
          sectorActual = org.sector;
          const header = document.createElement("div");
          header.className = "text-muted fw-semibold mt-1";
          header.textContent = sectorActual;
          lista.appendChild(header);
        }
        const fila = document.createElement("button");
        fila.type = "button";
        fila.className = "btn btn-sm btn-light text-start w-100 mb-1";
        fila.textContent = org.nombre;
        fila.addEventListener("click", () => {
          seleccionados.set(org.id, org);
          actualizarHidden();
          renderChips();
          renderLista(buscador.value);
        });
        lista.appendChild(fila);
        mostrados += 1;
      }
      if (coinciden === 0) {
        lista.innerHTML = '<div class="text-muted px-1">No se encontraron organismos.</div>';
      } else if (coinciden > mostrados) {
        const aviso = document.createElement("div");
        aviso.className = "text-muted small px-1 pt-1";
        aviso.textContent = "Mostrando " + mostrados + " de " + coinciden + "; afina la búsqueda.";
        lista.appendChild(aviso);
      }
    }

    buscador.addEventListener("input", debounce(() => renderLista(buscador.value), 150));
    actualizarHidden();
    renderChips();
    renderLista("");
  }

  function iniciar(raiz) {
    const widgets = Array.from((raiz || document).querySelectorAll(".js-org-widget")).filter(
      (w) => !w.dataset.iniciado
    );
    if (widgets.length === 0) return;
    widgets.forEach((w) => {
      w.dataset.iniciado = "1";
    });
    pedirCatalogo().then((catalogo) => widgets.forEach((w) => initWidget(w, catalogo)));
  }

  document.addEventListener("DOMContentLoaded", () => iniciar(document));
  // Los formularios de perfil llegan por HTMX al abrirlos.
  document.addEventListener("htmx:load", (e) => iniciar(e.detail && e.detail.elt));
})();
