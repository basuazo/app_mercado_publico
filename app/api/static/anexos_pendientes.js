/* «Abrir fichas» (F-plat-anexos-boton §2): abre las próximas N fichas públicas de la lista en
   pestañas nuevas. Todo ocurre en el navegador de la persona, en el mismo clic. */
(function () {
  "use strict";
  var raiz = document.getElementById("anexos-pendientes");
  var boton = document.getElementById("abrir-fichas");
  var campo = document.getElementById("abrir-n");
  var estado = document.getElementById("abrir-estado");
  if (!raiz || !boton || !campo) return;
  var CLAVE = "rastrillo.abrir_fichas_n";
  var max = parseInt(raiz.getAttribute("data-n-max"), 10) || 30;

  function leerGuardado() {
    try {
      var v = parseInt(window.localStorage.getItem(CLAVE), 10);
      if (v >= 1 && v <= max) campo.value = String(v);
    } catch (e) { /* sin almacenamiento: queda el valor por defecto */ }
  }
  function guardar(n) {
    try { window.localStorage.setItem(CLAVE, String(n)); } catch (e) { /* idem */ }
  }
  function cuantas() {
    var n = parseInt(campo.value, 10);
    if (!(n >= 1)) n = parseInt(raiz.getAttribute("data-n-defecto"), 10) || 10;
    return Math.min(n, max);
  }

  leerGuardado();
  boton.addEventListener("click", function () {
    var n = cuantas();
    campo.value = String(n);
    guardar(n);
    var enlaces = Array.prototype.slice.call(raiz.querySelectorAll("a[data-ficha]"), 0, n);
    enlaces.forEach(function (a) {
      window.open(a.href, "_blank", "noopener"); // con «noopener» no se puede saber si abrió
    });
    estado.textContent = enlaces.length
      ? "Abrí " + enlaces.length + " ficha(s). Pasa el botón de la extensión en cada pestaña."
      : "No hay fichas que abrir.";
  });
})();
