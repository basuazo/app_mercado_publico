/* Service worker: sube cada archivo a Rastrillo. El token vive solo en `chrome.storage.local`
   y solo este script y la página de opciones lo leen (el content script nunca lo ve). */
importScripts("lib.js");

var CLAVES = ["rastrillo_url", "rastrillo_token"];

chrome.runtime.onMessage.addListener(function (msg, sender, responder) {
  if (!msg || msg.tipo !== "subir") return false;
  if (!sender.url || sender.url.indexOf("https://buscador.mercadopublico.cl/ficha") !== 0) return false;
  subir(msg).then(responder, function (e) {
    responder({ tipo: "error", texto: "No pude contactar a Rastrillo: " + (e && e.message ? e.message : e) });
  });
  return true; // respuesta asíncrona
});

async function subir(msg) {
  var cfg = await chrome.storage.local.get(CLAVES);
  var base = RastrilloLib.normalizarBase(cfg.rastrillo_url);
  var token = String(cfg.rastrillo_token || "").trim();
  if (!base || !token) {
    return { tipo: "error", texto: "Falta configurar la URL de Rastrillo y el token en las opciones de la extensión" };
  }
  var req = RastrilloLib.armarRequest(base, msg.codigo, token);
  var bytes = RastrilloLib.bytesDeBase64(msg.base64);
  var fd = new FormData();
  fd.append("archivo", new Blob([bytes], { type: msg.mime || "application/octet-stream" }),
            RastrilloLib.nombreSubida(msg.codigo, msg.nombre));
  // Un archivo, un intento: sin reintentos automáticos.
  var r = await fetch(req.url, { method: "POST", headers: req.headers, body: fd, credentials: "omit" });
  var cuerpo = null;
  try { cuerpo = await r.json(); } catch (e) { /* respuesta sin JSON */ }
  return RastrilloLib.resultadoDeRespuesta(r.status, cuerpo);
}
