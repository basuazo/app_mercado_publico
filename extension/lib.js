/* Lógica pura de la extensión (sin DOM ni chrome.*): se prueba con `node --test extension/lib.test.js`.
   Se carga como script clásico (content, background con importScripts, options) y como módulo
   CommonJS en las pruebas. */
(function (raiz) {
  "use strict";

  var CODIGO_CA = /^[0-9]+-[0-9]+-[A-Z]{2,5}[0-9]{2}$/;
  var EXTENSION_ARCHIVO = /\.[A-Za-z0-9]{2,5}(?:\s|$|\))/;
  var MAX_NOMBRE = 150;

  /** Código de la CA desde la URL de la ficha (`?code=`), o null si no calza con el patrón. */
  function codigoDeUrl(url) {
    var u;
    try { u = new URL(url); } catch (e) { return null; }
    var c = (u.searchParams.get("code") || "").trim().toUpperCase();
    return CODIGO_CA.test(c) ? c : null;
  }

  /** Origen de Rastrillo (sin ruta ni barra final). Solo https; http solo en localhost. */
  function normalizarBase(texto) {
    var u;
    try { u = new URL(String(texto || "").trim()); } catch (e) { return null; }
    var local = u.hostname === "localhost" || u.hostname === "127.0.0.1";
    if (u.protocol !== "https:" && !(u.protocol === "http:" && local)) return null;
    return u.origin;
  }

  /** `CODIGO__nombre` sin rutas ni caracteres de control: así lo asocia Rastrillo (anexos-2 §2.2). */
  function nombreSubida(codigo, nombre) {
    var base = String(nombre || "").split(/[\\/]/).pop().replace(/[\u0000-\u001f\u007f]/g, "").trim();
    if (!base) base = "adjunto";
    return (codigo + "__" + base).slice(0, MAX_NOMBRE);
  }

  /** Dirección y cabecera del `POST /api/anexos/{codigo}`; el cuerpo (multipart) lo arma quien llama. */
  function armarRequest(base, codigo, token) {
    return {
      url: base + "/api/anexos/" + encodeURIComponent(codigo),
      headers: { "X-Anexos-Token": token },
    };
  }

  /** Texto y tipo para mostrar el resultado de un archivo (nuevo | ya_existia | invalido | error). */
  function resultadoDeRespuesta(status, cuerpo) {
    var c = cuerpo && typeof cuerpo === "object" ? cuerpo : {};
    if (status === 201) {
      var n = typeof c.lineas === "number" ? c.lineas : 0;
      return { tipo: "nuevo", texto: "Nuevo: subido" + (n ? " (" + n + " línea(s) por revisar)" : "") };
    }
    if (status === 200) return { tipo: "ya_existia", texto: "Ya existía: no se repite" };
    if (status === 422) return { tipo: "invalido", texto: "Inválido: " + (c.detail || "archivo no aceptado") };
    if (status === 404) return { tipo: "error", texto: "Rastrillo no tiene esta Compra Ágil en tu territorio" };
    if (status === 401) return { tipo: "error", texto: "Token rechazado: revisa las opciones de la extensión" };
    if (status === 429) return { tipo: "error", texto: "Demasiadas solicitudes: espera un minuto" };
    return { tipo: "error", texto: "Error " + status };
  }

  /** ¿El texto parece un nombre de archivo («Anexo 1.pdf»)? */
  function pareceArchivo(texto) {
    return EXTENSION_ARCHIVO.test(String(texto || "").trim() + " ");
  }

  function base64DeBytes(bytes) {
    var s = "";
    for (var i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s);
  }

  function bytesDeBase64(b64) {
    var s = atob(b64);
    var out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }

  var api = {
    codigoDeUrl: codigoDeUrl,
    normalizarBase: normalizarBase,
    nombreSubida: nombreSubida,
    armarRequest: armarRequest,
    resultadoDeRespuesta: resultadoDeRespuesta,
    pareceArchivo: pareceArchivo,
    base64DeBytes: base64DeBytes,
    bytesDeBase64: bytesDeBase64,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else raiz.RastrilloLib = api;
})(typeof self !== "undefined" ? self : this);
