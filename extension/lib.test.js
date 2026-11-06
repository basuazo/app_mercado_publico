// Pruebas de la lógica pura: `node --test extension/lib.test.js` (sin navegador, sin dependencias).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("./lib.js");

test("codigoDeUrl lee code= y descarta lo que no es un código de CA", () => {
  assert.equal(L.codigoDeUrl("https://buscador.mercadopublico.cl/ficha?code=3600-210-cot26"), "3600-210-COT26");
  assert.equal(L.codigoDeUrl("https://buscador.mercadopublico.cl/ficha?foo=1&code=1-2-COT26&x"), "1-2-COT26");
  assert.equal(L.codigoDeUrl("https://buscador.mercadopublico.cl/ficha"), null);
  assert.equal(L.codigoDeUrl("https://buscador.mercadopublico.cl/ficha?code=../../etc"), null);
  assert.equal(L.codigoDeUrl("no es una url"), null);
});

test("normalizarBase: https siempre, http solo en localhost, sin ruta", () => {
  assert.equal(L.normalizarBase("https://rastrillo.onrender.com/admin/"), "https://rastrillo.onrender.com");
  assert.equal(L.normalizarBase("http://localhost:8000/x"), "http://localhost:8000");
  assert.equal(L.normalizarBase("http://rastrillo.onrender.com"), null);
  assert.equal(L.normalizarBase("javascript:alert(1)"), null);
  assert.equal(L.normalizarBase(""), null);
});

test("nombreSubida antepone el código con __ y limpia rutas y controles", () => {
  assert.equal(L.nombreSubida("1-2-COT26", "Anexo 1.pdf"), "1-2-COT26__Anexo 1.pdf");
  assert.equal(L.nombreSubida("1-2-COT26", "C:\\x\\y\\bases.docx"), "1-2-COT26__bases.docx");
  assert.equal(L.nombreSubida("1-2-COT26", "../../a\u0000b.pdf"), "1-2-COT26__ab.pdf");
  assert.equal(L.nombreSubida("1-2-COT26", ""), "1-2-COT26__adjunto");
  assert.ok(L.nombreSubida("1-2-COT26", "x".repeat(500)).length <= 150);
});

test("armarRequest: URL con el código codificado y cabecera X-Anexos-Token", () => {
  const r = L.armarRequest("https://r.test", "1-2-COT26", "tok");
  assert.equal(r.url, "https://r.test/api/anexos/1-2-COT26");
  assert.deepEqual(r.headers, { "X-Anexos-Token": "tok" });
  assert.equal(L.armarRequest("https://r.test", "a/b", "t").url, "https://r.test/api/anexos/a%2Fb");
});

test("resultadoDeRespuesta distingue nuevo, ya existía, inválido y error", () => {
  assert.equal(L.resultadoDeRespuesta(201, { lineas: 3 }).tipo, "nuevo");
  assert.match(L.resultadoDeRespuesta(201, { lineas: 3 }).texto, /3 línea/);
  assert.equal(L.resultadoDeRespuesta(200, { duplicado: true }).tipo, "ya_existia");
  const inv = L.resultadoDeRespuesta(422, { detail: "Supera 10 MB" });
  assert.equal(inv.tipo, "invalido");
  assert.match(inv.texto, /Supera 10 MB/);
  for (const s of [401, 404, 429, 500]) assert.equal(L.resultadoDeRespuesta(s, null).tipo, "error");
});

test("pareceArchivo", () => {
  assert.ok(L.pareceArchivo("Anexo 1.pdf"));
  assert.ok(L.pareceArchivo("Bases (v2).xlsx"));
  assert.ok(!L.pareceArchivo("Descargar"));
  assert.ok(!L.pareceArchivo(""));
  assert.ok(!L.pareceArchivo(null));
});

test("base64 de ida y vuelta, incluso con archivos más grandes que un tramo", () => {
  const bytes = new Uint8Array(100000).map((_, i) => i % 251);
  assert.deepEqual(L.bytesDeBase64(L.base64DeBytes(bytes)), bytes);
});
