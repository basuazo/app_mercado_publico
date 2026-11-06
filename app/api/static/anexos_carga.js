// Carga masiva de anexos (F-plat-anexos-2 §2.2).
// Varios archivos: la vista previa manda solo nombre, tamaño y sha256 (calculado acá); al
// procesar, cada archivo va en SU request y la fila se reemplaza con el resultado.
// ZIP: la vista previa lee solo el directorio en el servidor; al procesar, el ZIP se reenvía con
// `desde` y el servidor procesa un tramo y avisa el siguiente en `X-Siguiente`. El servidor no
// guarda nada entre requests: lo ya cargado vuelve como «Ya cargado» sin leerse de nuevo.
(function () {
  "use strict";
  var raiz = document.getElementById("carga-masiva");
  if (!raiz) return;
  var csrf = raiz.getAttribute("data-csrf") || "";
  var tbody = document.getElementById("carga-filas");
  var btnProcesar = document.getElementById("carga-procesar");
  var progreso = document.getElementById("carga-progreso");
  var inArchivos = document.getElementById("carga-archivos");
  var inZip = document.getElementById("carga-zip");
  var modo = null; // "archivos" | "zip"
  var ocupado = false;

  function aviso(texto) { progreso.textContent = texto; }

  function ocupar(si) {
    ocupado = si;
    ["carga-revisar-archivos", "carga-revisar-zip", "carga-procesar"].forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.disabled = si;
    });
  }

  function filasDe(html) {
    var t = document.createElement("tbody");
    t.innerHTML = html;
    return Array.prototype.slice.call(t.children);
  }

  async function enviar(url, fd) {
    fd.append("csrf_token", csrf);
    var r = await fetch(url, {
      method: "POST", body: fd, credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf },
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return { html: await r.text(), siguiente: r.headers.get("X-Siguiente") };
  }

  async function sha256(file) {
    if (!(window.crypto && window.crypto.subtle)) return "";
    try {
      var h = await window.crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      return Array.prototype.map.call(new Uint8Array(h), function (b) {
        return b.toString(16).padStart(2, "0");
      }).join("");
    } catch (e) { return ""; }
  }

  function mostrarVista(html) {
    tbody.innerHTML = html;
    var resumen = document.getElementById("carga-resumen");
    var n = resumen ? parseInt(resumen.getAttribute("data-procesables") || "0", 10) : 0;
    btnProcesar.hidden = n === 0;
    btnProcesar.textContent = "Procesar " + n;
    aviso(n ? "" : "No hay archivos para procesar.");
  }

  function reemplazar(fila, html) {
    var nuevas = filasDe(html);
    if (nuevas.length) fila.replaceWith.apply(fila, nuevas);
  }

  function marcarFallo(fila, err) {
    var celdas = fila.querySelectorAll("td");
    if (celdas.length) celdas[celdas.length - 1].textContent =
      "No se pudo enviar (" + err.message + "): vuelve a intentarlo.";
    fila.setAttribute("data-estado", "fallo");
  }

  document.getElementById("carga-revisar-archivos").addEventListener("click", async function () {
    if (ocupado || !inArchivos.files.length) return;
    ocupar(true); modo = "archivos"; aviso("Revisando…");
    try {
      var fd = new FormData();
      for (var i = 0; i < inArchivos.files.length; i++) {
        var f = inArchivos.files[i];
        fd.append("nombres", f.name);
        fd.append("tamanos", String(f.size));
        fd.append("sha256", f.size <= 10 * 1024 * 1024 ? await sha256(f) : "");
      }
      mostrarVista((await enviar("/catalogo/anexos/carga/vista-previa", fd)).html);
    } catch (e) { aviso("No se pudo revisar (" + e.message + ")."); }
    ocupar(false);
  });

  document.getElementById("carga-revisar-zip").addEventListener("click", async function () {
    if (ocupado || !inZip.files.length) return;
    ocupar(true); modo = "zip"; aviso("Revisando el ZIP…");
    try {
      var fd = new FormData();
      fd.append("zip", inZip.files[0]);
      fd.append("accion", "vista");
      mostrarVista((await enviar("/catalogo/anexos/carga/zip", fd)).html);
    } catch (e) { aviso("No se pudo revisar el ZIP (" + e.message + ")."); }
    ocupar(false);
  });

  async function procesarArchivos() {
    var filas = tbody.querySelectorAll('tr[data-modo="vista"][data-estado="nueva"]');
    for (var k = 0; k < filas.length; k++) {
      var fila = filas[k];
      var idx = parseInt(fila.getAttribute("data-indice"), 10);
      var archivo = inArchivos.files[idx];
      if (!archivo) continue;
      aviso("Procesando " + (k + 1) + " de " + filas.length + "… (cada archivo puede tardar un minuto)");
      try {
        var fd = new FormData();
        fd.append("archivo", archivo);
        fd.append("indice", String(idx));
        reemplazar(fila, (await enviar("/catalogo/anexos/carga/archivo", fd)).html);
      } catch (e) { marcarFallo(fila, e); }
    }
    aviso("Listo: " + filas.length + " archivo(s) procesados.");
  }

  async function procesarZip() {
    var desde = "0";
    var tramos = 0;
    while (desde !== null && desde !== "" && tramos < 500) {
      tramos += 1;
      aviso("Procesando el ZIP… (tramo " + tramos + ")");
      var fd = new FormData();
      fd.append("zip", inZip.files[0]);
      fd.append("accion", "procesar");
      fd.append("desde", desde);
      var r = await enviar("/catalogo/anexos/carga/zip", fd);
      filasDe(r.html).forEach(function (nueva) {
        var idx = nueva.getAttribute("data-indice");
        var vieja = idx !== null ? tbody.querySelector('tr[data-indice="' + idx + '"]') : null;
        if (vieja) vieja.replaceWith(nueva); else tbody.appendChild(nueva);
      });
      desde = r.siguiente;
    }
    aviso("Listo: ZIP procesado.");
  }

  btnProcesar.addEventListener("click", async function () {
    if (ocupado) return;
    ocupar(true);
    try {
      if (modo === "zip") await procesarZip(); else await procesarArchivos();
    } catch (e) { aviso("Se cortó el proceso (" + e.message + "): lo ya cargado quedó guardado; vuelve a intentarlo."); }
    btnProcesar.hidden = true;
    ocupar(false);
  });
})();
