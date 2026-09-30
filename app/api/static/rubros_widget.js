/* Selector de rubros UNSPSC (F-ca-explorar): extraído de perfiles.html para
   compartirlo con el explorador de Compras Ágiles. Sin red: todo en cliente. */
(function () {
  "use strict";

  function debounce(fn, wait) {
    let temporizador;
    return function (...args) {
      clearTimeout(temporizador);
      temporizador = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  function quitarChip(boton, texto, alQuitar) {
    boton.type = "button";
    boton.className = "btn-close btn-close-white";
    boton.style.fontSize = "0.55rem";
    boton.setAttribute("aria-label", "Quitar " + texto);
    boton.addEventListener("click", alQuitar);
  }

  function crearChip(texto, alQuitar) {
    const chip = document.createElement("span");
    chip.className = "badge rounded-pill text-bg-primary d-inline-flex align-items-center gap-1";
    chip.textContent = texto;
    const boton = document.createElement("button");
    quitarChip(boton, texto, alQuitar);
    chip.appendChild(boton);
    return chip;
  }

  // ---- Rubros: acordeón + chips + buscador (en cliente, sin red) ----
  function initRubrosWidget(widget) {
    const buscador = widget.querySelector(".js-rubro-buscador");
    const chipsBox = widget.querySelector(".js-rubro-chips");
    const segmentos = Array.from(widget.querySelectorAll(".js-rubro-segmento"));
    const famChecks = Array.from(widget.querySelectorAll(".js-rubro-fam-check"));
    const segToggles = Array.from(widget.querySelectorAll(".js-rubro-seg-toggle"));

    function famsDeSegmento(segCodigo) {
      return famChecks.filter((c) => c.dataset.seg === segCodigo);
    }

    function actualizarSegToggle(segToggle) {
      if (!segToggle) return;
      const fams = famsDeSegmento(segToggle.dataset.seg);
      const marcados = fams.filter((c) => c.checked).length;
      segToggle.checked = fams.length > 0 && marcados === fams.length;
      segToggle.indeterminate = marcados > 0 && marcados < fams.length;
    }

    function renderChips() {
      chipsBox.innerHTML = "";
      famChecks.filter((c) => c.checked).forEach((c) => {
        chipsBox.appendChild(
          crearChip(c.dataset.nombre, () => {
            c.checked = false;
            actualizarSegToggle(segToggles.find((s) => s.dataset.seg === c.dataset.seg));
            renderChips();
          })
        );
      });
    }

    famChecks.forEach((c) => {
      c.addEventListener("change", () => {
        actualizarSegToggle(segToggles.find((s) => s.dataset.seg === c.dataset.seg));
        renderChips();
      });
    });

    segToggles.forEach((seg) => {
      seg.addEventListener("change", () => {
        famsDeSegmento(seg.dataset.seg).forEach((c) => {
          c.checked = seg.checked;
        });
        seg.indeterminate = false;
        renderChips();
      });
      actualizarSegToggle(seg);
    });

    if (buscador) {
      buscador.addEventListener(
        "input",
        debounce(() => {
          const q = buscador.value.trim().toLowerCase();
          segmentos.forEach((seg) => {
            const familias = Array.from(seg.querySelectorAll(".js-rubro-familia"));
            if (!q) {
              seg.classList.remove("d-none");
              familias.forEach((f) => f.classList.remove("d-none"));
              return;
            }
            const segCoincide = seg.dataset.buscar.includes(q);
            let algunaFamilia = false;
            familias.forEach((f) => {
              const coincide = segCoincide || f.dataset.buscar.includes(q);
              f.classList.toggle("d-none", !coincide);
              if (coincide) algunaFamilia = true;
            });
            seg.classList.toggle("d-none", !algunaFamilia);
            if (algunaFamilia && window.bootstrap) {
              const collapseEl = seg.querySelector(".accordion-collapse");
              if (collapseEl) {
                window.bootstrap.Collapse.getOrCreateInstance(collapseEl, { toggle: false }).show();
              }
            }
          });
        }, 150)
      );
    }

    renderChips();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".js-rubros-widget").forEach(initRubrosWidget);
  });
})();
