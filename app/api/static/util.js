/* Utilidades compartidas por los widgets (rubros, organismos) y /perfiles.
   Sin red ni estado: todo vive en window.MP. */
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

  // "5000000" -> "5.000.000"; lo que no sean dígitos se descarta.
  function formatoMiles(texto) {
    const digitos = String(texto || "").replace(/\D/g, "");
    return digitos ? digitos.replace(/\B(?=(\d{3})+(?!\d))/g, ".") : "";
  }

  window.MP = { debounce, quitarChip, crearChip, formatoMiles };

  // Montos con formato de miles al salir del campo (el servidor acepta con o sin puntos).
  document.addEventListener("focusout", function (e) {
    const campo = e.target;
    if (campo && campo.classList && campo.classList.contains("js-monto")) {
      campo.value = formatoMiles(campo.value);
    }
  });

  // Un 422 de los formularios de perfil trae el formulario re-renderizado con sus
  // errores: HTMX no intercambia 4xx por defecto, así que se permite solo para eso.
  document.addEventListener("htmx:beforeSwap", function (e) {
    const xhr = e.detail && e.detail.xhr;
    const destino = e.detail && e.detail.target;
    if (xhr && xhr.status === 422 && destino && destino.hasAttribute("data-perfil-form")) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    }
  });
})();
