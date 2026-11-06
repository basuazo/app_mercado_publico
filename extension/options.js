(function () {
  "use strict";
  var url = document.getElementById("url");
  var token = document.getElementById("token");
  var estado = document.getElementById("estado");

  chrome.storage.local.get(["rastrillo_url", "rastrillo_token"]).then(function (cfg) {
    url.value = cfg.rastrillo_url || "";
    token.value = cfg.rastrillo_token || "";
  });

  document.getElementById("guardar").addEventListener("click", async function () {
    var base = RastrilloLib.normalizarBase(url.value);
    if (!base) {
      estado.textContent = "La URL debe ser https (o http://localhost).";
      return;
    }
    // Permiso solo para el origen de Rastrillo, no para todos los sitios.
    var ok = await chrome.permissions.request({ origins: [base + "/*"] });
    if (!ok) {
      estado.textContent = "Sin permiso para hablar con " + base + ": no se guardó.";
      return;
    }
    await chrome.storage.local.set({ rastrillo_url: base, rastrillo_token: token.value.trim() });
    estado.textContent = "Guardado.";
  });
})();
