// F-plat-lote: «Cotizar seleccionadas» en la mesa y «Copiar» del mensaje al proveedor.
// Sin estado que importe (regla 10): la selección vive en las casillas; el lote, en el servidor.
(function () {
  "use strict";

  function casillas() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-lote-check]"));
  }

  function actualizarBarra() {
    var barra = document.querySelector("[data-barra-lote]");
    if (!barra) return;
    var n = casillas().filter(function (c) { return c.checked; }).length;
    var num = barra.querySelector("[data-lote-n]");
    var plural = barra.querySelector("[data-lote-plural]");
    if (num) num.textContent = String(n);
    if (plural) plural.textContent = n === 1 ? "" : "s";
    barra.hidden = n === 0;
  }

  document.addEventListener("change", function (evt) {
    if (evt.target && evt.target.hasAttribute && evt.target.hasAttribute("data-lote-check")) {
      actualizarBarra();
    }
  });

  document.addEventListener("click", function (evt) {
    var t = evt.target;
    if (!t || !t.closest) return;
    if (t.closest("[data-lote-limpiar]")) {
      casillas().forEach(function (c) { c.checked = false; });
      actualizarBarra();
      return;
    }
    var copiar = t.closest("[data-copiar]");
    if (copiar) {
      var origen = document.getElementById(copiar.getAttribute("data-copiar"));
      if (!origen) return;
      var texto = origen.value || origen.textContent || "";
      var listo = function () {
        var antes = copiar.textContent;
        copiar.textContent = "Copiado";
        setTimeout(function () { copiar.textContent = antes; }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(texto).then(listo, function () { origen.select && origen.select(); });
      } else if (origen.select) {
        origen.select();
        try { document.execCommand("copy"); listo(); } catch (e) { /* el texto queda seleccionado */ }
      }
    }
  });

  document.addEventListener("DOMContentLoaded", actualizarBarra);
  // Volver con «atrás» puede restaurar casillas marcadas.
  window.addEventListener("pageshow", actualizarBarra);
})();
