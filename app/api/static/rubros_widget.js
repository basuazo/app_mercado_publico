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

  // ---- Palabras del rubro (F-ca-vocab): sugeridas o escritas, en cliente, sin red ----
  // Mismo criterio que el servidor (que igual las vuelve a validar).
  const PALABRA_RE = /^[a-záéíóúüñ]{3,60}$/;
  const MAX_PALABRAS = 20;

  function initPalabrasWidget(widget) {
    const form = widget.closest("form");
    const chipsBox = widget.querySelector(".js-palabras-chips");
    const inputsBox = widget.querySelector(".js-palabras-inputs");
    const campo = widget.querySelector(".js-palabra-nueva");
    const botonAgregar = widget.querySelector(".js-palabra-agregar");
    const sugeridas = Array.from(widget.querySelectorAll(".js-palabra-sugerida"));

    function elegidas() {
      return Array.from(inputsBox.querySelectorAll("input")).map((i) => i.value);
    }

    function render() {
      const actuales = elegidas();
      chipsBox.innerHTML = "";
      actuales.forEach((p) => {
        chipsBox.appendChild(crearChip(p, () => quitar(p)));
      });
      sugeridas.forEach((b) => {
        const ya = actuales.includes(b.dataset.palabra);
        b.disabled = ya;
        b.setAttribute("aria-pressed", ya ? "true" : "false");
      });
    }

    function avisarCambio() {
      // El formulario cuenta en vivo con HTMX al recibir `change`.
      if (form) form.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function agregar(texto) {
      let cambio = false;
      texto.split(",").forEach((parte) => {
        const p = parte.trim().toLowerCase();
        if (!PALABRA_RE.test(p) || elegidas().includes(p)) return;
        if (elegidas().length >= MAX_PALABRAS) return;
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "palabras_rubro";
        input.value = p;
        inputsBox.appendChild(input);
        cambio = true;
      });
      if (cambio) {
        render();
        avisarCambio();
      }
    }

    function quitar(palabra) {
      Array.from(inputsBox.querySelectorAll("input"))
        .filter((i) => i.value === palabra)
        .forEach((i) => i.remove());
      render();
      avisarCambio();
    }

    sugeridas.forEach((b) => b.addEventListener("click", () => agregar(b.dataset.palabra)));

    function agregarEscritas() {
      if (!campo) return;
      agregar(campo.value);
      campo.value = "";
    }
    if (botonAgregar) botonAgregar.addEventListener("click", agregarEscritas);
    if (campo) {
      campo.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          agregarEscritas();
        }
      });
    }
    // Lo escrito y no agregado también cuenta al aplicar los filtros.
    if (form) form.addEventListener("submit", agregarEscritas);

    render();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".js-rubros-widget").forEach(initRubrosWidget);
    document.querySelectorAll(".js-palabras-widget").forEach(initPalabrasWidget);
  });
})();
