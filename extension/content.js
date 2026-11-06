/* Content script de la ficha pública de una Compra Ágil. Agrega «Enviar adjuntos a Rastrillo» junto
   a la sección «Adjuntos». Al pulsarlo, para cada adjunto hace el MISMO clic que haría una persona
   en la ficha y recoge el archivo que la ficha arma (ver hook_main.js). NO llama al servicio de
   adjuntos ni usa su clave. Un archivo a la vez, sin reintentos automáticos.

   [I] Los selectores de abajo (cómo se ubica la sección «Adjuntos» y sus botones de descarga) no se
   verificaron en el navegador: si la ficha cambia o no calzan, se ajusta SOLO `ubicarAdjuntos`. */
(function () {
  "use strict";
  var L = self.RastrilloLib;
  var ID = "rastrillo-adjuntos";
  var ESPERA_ARCHIVO_MS = 30000;
  var MAX_BYTES = 10 * 1024 * 1024; // el mismo tope que Rastrillo

  function codigoActual() {
    return L.codigoDeUrl(window.location.href);
  }

  /** El elemento que hace de título «Adjuntos» (texto exacto, sin contar nuestro panel). */
  function tituloAdjuntos() {
    var cand = document.querySelectorAll("h1,h2,h3,h4,h5,h6,legend,strong,span,p,div,label");
    for (var i = 0; i < cand.length; i++) {
      var el = cand[i];
      if (el.closest("#" + ID)) continue;
      if (el.children.length === 0 && /^adjuntos?:?$/i.test((el.textContent || "").trim())) return el;
    }
    return null;
  }

  /** Los controles de descarga de cada adjunto: dentro del bloque del título, los enlaces o
      botones cuyo texto (o el de su fila) parece un nombre de archivo. */
  function ubicarAdjuntos(titulo) {
    var bloque = titulo.parentElement;
    for (var nivel = 0; nivel < 4 && bloque; nivel++, bloque = bloque.parentElement) {
      var controles = Array.prototype.filter.call(
        bloque.querySelectorAll("a, button, [role='button']"),
        function (c) {
          if (c.closest("#" + ID)) return false;
          var fila = c.closest("li, tr, [class*='adjunto'], [class*='archivo']") || c.parentElement;
          return L.pareceArchivo(c.textContent) || L.pareceArchivo(c.getAttribute("title")) ||
                 L.pareceArchivo(c.getAttribute("aria-label")) ||
                 (fila && L.pareceArchivo(fila.textContent));
        }
      );
      // Un archivo por fila: si la fila tiene varios controles, solo el primero.
      var vistos = [];
      var unicos = controles.filter(function (c) {
        var fila = c.closest("li, tr, [class*='adjunto'], [class*='archivo']") || c;
        if (vistos.indexOf(fila) >= 0) return false;
        vistos.push(fila);
        return true;
      });
      if (unicos.length) return unicos;
    }
    return [];
  }

  function esperarArchivo() {
    return new Promise(function (resolver) {
      var t = setTimeout(function () {
        window.removeEventListener("message", alMensaje);
        window.postMessage({ origen: "rastrillo-content", tipo: "desarmar" }, window.location.origin);
        resolver(null);
      }, ESPERA_ARCHIVO_MS);
      function alMensaje(e) {
        var d = e.data;
        if (e.source !== window || !d || d.origen !== "rastrillo-hook" || d.tipo !== "archivo") return;
        clearTimeout(t);
        window.removeEventListener("message", alMensaje);
        resolver(d);
      }
      window.addEventListener("message", alMensaje);
      window.postMessage({ origen: "rastrillo-content", tipo: "armar" }, window.location.origin);
    });
  }

  function fila(lista, texto) {
    var li = document.createElement("li");
    li.textContent = texto;
    lista.appendChild(li);
    return li;
  }

  async function enviar(boton, lista) {
    var codigo = codigoActual();
    lista.textContent = "";
    if (!codigo) {
      fila(lista, "No pude leer el código de la Compra Ágil en la dirección de la página.");
      return;
    }
    var titulo = tituloAdjuntos();
    var controles = titulo ? ubicarAdjuntos(titulo) : [];
    if (!controles.length) {
      fila(lista, "No encontré adjuntos en esta ficha (¿terminó de cargar?).");
      return;
    }
    boton.disabled = true;
    try {
      for (var i = 0; i < controles.length; i++) {
        var etiqueta = (controles[i].textContent || "").trim().slice(0, 80) || "adjunto " + (i + 1);
        var li = fila(lista, etiqueta + ": descargando…");
        var espera = esperarArchivo();
        controles[i].click(); // el mismo clic de una persona
        var d = await espera;
        if (!d) {
          li.textContent = etiqueta + ": no capturé el archivo (la ficha no lo armó a tiempo).";
          continue;
        }
        var blob = d.blob;
        if (blob.size > MAX_BYTES) {
          li.textContent = etiqueta + ": inválido: supera 10 MB.";
          continue;
        }
        li.textContent = etiqueta + ": subiendo…";
        var bytes = new Uint8Array(await blob.arrayBuffer());
        var r = await chrome.runtime.sendMessage({
          tipo: "subir",
          codigo: codigo,
          nombre: d.nombre || etiqueta,
          mime: blob.type,
          base64: L.base64DeBytes(bytes),
        });
        li.textContent = etiqueta + ": " + (r && r.texto ? r.texto : "sin respuesta de la extensión");
      }
    } finally {
      boton.disabled = false;
    }
  }

  function inyectar() {
    if (document.getElementById(ID)) return;
    var titulo = tituloAdjuntos();
    if (!titulo) return;
    var caja = document.createElement("div");
    caja.id = ID;
    caja.style.cssText = "margin:8px 0;padding:8px;border:1px solid #888;border-radius:6px;font-size:14px";
    var boton = document.createElement("button");
    boton.type = "button";
    boton.textContent = "Enviar adjuntos a Rastrillo";
    boton.style.cssText = "padding:6px 12px;cursor:pointer";
    var lista = document.createElement("ul");
    lista.setAttribute("role", "status");
    lista.setAttribute("aria-live", "polite");
    lista.style.cssText = "margin:6px 0 0;padding-left:18px";
    var pie = document.createElement("div");
    pie.textContent = "Fuente: Dirección ChileCompra";
    pie.style.cssText = "margin-top:6px;font-size:12px;color:#555";
    boton.addEventListener("click", function () { enviar(boton, lista); });
    caja.appendChild(boton);
    caja.appendChild(lista);
    caja.appendChild(pie);
    titulo.insertAdjacentElement("afterend", caja);
  }

  // La ficha es una SPA: la sección «Adjuntos» aparece cuando termina de cargar.
  var pendiente = null;
  new MutationObserver(function () {
    if (pendiente) return;
    pendiente = setTimeout(function () { pendiente = null; inyectar(); }, 300);
  }).observe(document.documentElement, { childList: true, subtree: true });
  inyectar();
})();
