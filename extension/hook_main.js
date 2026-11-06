/* Corre en el mundo MAIN de la ficha (document_start). NO llama a ningún servicio ni usa claves:
   solo observa el `Blob` que la propia ficha arma cuando se pide un adjunto (el mismo clic de una
   persona, disparado por el botón de la extensión) y lo entrega al content script ANTES de que se
   guarde en disco.

   Forma elegida [I, sin verificar en un navegador]: la ficha arma un Blob, un enlace temporal
   (`URL.createObjectURL`) y le hace «clic» [V, bundle]. Se registra cada Blob creado y, solo
   mientras el content script tiene la captura «armada», el clic sobre ese enlace se intercepta. */
(function () {
  "use strict";
  if (window.__rastrilloHook) return;
  window.__rastrilloHook = true;

  var blobs = new Map();
  var armado = false;
  var MAX_REGISTRADOS = 20;

  var crear = URL.createObjectURL;
  URL.createObjectURL = function (obj) {
    var url = crear.apply(this, arguments);
    try {
      if (obj instanceof Blob) {
        blobs.set(url, obj);
        if (blobs.size > MAX_REGISTRADOS) blobs.delete(blobs.keys().next().value);
      }
    } catch (e) { /* observar nunca rompe la ficha */ }
    return url;
  };

  function capturar(enlace) {
    if (!armado || !enlace || enlace.tagName !== "A") return false;
    var blob = blobs.get(enlace.href);
    if (!blob) return false;
    armado = false; // un archivo por armado
    window.postMessage(
      { origen: "rastrillo-hook", tipo: "archivo", blob: blob, nombre: enlace.download || "" },
      window.location.origin
    );
    return true; // se consume el clic: no se guarda en disco
  }

  var clic = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    if (capturar(this)) return;
    return clic.apply(this, arguments);
  };

  var despachar = EventTarget.prototype.dispatchEvent;
  EventTarget.prototype.dispatchEvent = function (ev) {
    if (ev && ev.type === "click" && this instanceof HTMLAnchorElement && capturar(this)) return true;
    return despachar.apply(this, arguments);
  };

  window.addEventListener("message", function (e) {
    var d = e.data;
    if (e.source !== window || !d || d.origen !== "rastrillo-content") return;
    if (d.tipo === "armar") armado = true;
    if (d.tipo === "desarmar") armado = false;
  });
})();
