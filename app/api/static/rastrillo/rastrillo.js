// Rastrillo (F-dis-1): atajos de teclado, paleta ⌘K, campana, menú móvil y avisos con «Deshacer».
// Sin dependencias; cargado con `defer`. Sin estado que importe (regla 10): lo que cuenta vive en
// el servidor. Ningún atajo se dispara con el foco en un campo de texto.
(function () {
  "use strict";

  var esMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");

  // «⌘K» en Mac, «Ctrl K» en lo demás.
  function rotularTeclas() {
    if (esMac) return;
    document.querySelectorAll("[data-tecla-mod]").forEach(function (el) {
      el.textContent = el.textContent.replace("⌘", "Ctrl ");
    });
  }

  function enCampo(el) {
    if (!el || el === document.body) return false;
    if (el.isContentEditable) return true;
    var tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select";
  }

  function visible(el) {
    return !!el && !el.hidden && el.getClientRects().length > 0;
  }

  // ------------------------------------------------------------------------------------------
  // Cerrables: panel, campana, modal y paleta. `Esc` cierra el último abierto; [data-cerrar]
  // cierra el que lo contiene.
  // ------------------------------------------------------------------------------------------
  function cerrar(el) {
    if (!el) return;
    if (el.tagName === "DIALOG") {
      if (el.open) el.close();
    } else {
      el.hidden = true;
      if (el.hasAttribute("data-panel")) el.dispatchEvent(new CustomEvent("rastrillo:cerrado", { bubbles: true }));
    }
    if (el.id) {
      document.querySelectorAll('[aria-controls="' + el.id + '"][aria-expanded]').forEach(function (b) {
        b.setAttribute("aria-expanded", "false");
      });
    }
  }

  function abiertos() {
    return Array.prototype.filter.call(document.querySelectorAll("[data-cerrable]"), function (el) {
      return el.tagName === "DIALOG" ? el.open : visible(el);
    });
  }

  document.addEventListener("click", function (evt) {
    var boton = evt.target.closest && evt.target.closest("[data-cerrar]");
    if (boton) {
      cerrar(boton.closest("[data-cerrable]"));
      return;
    }
    // Clic fuera de la campana abierta: se cierra.
    document.querySelectorAll(".m-campana__panel:not([hidden])").forEach(function (panel) {
      if (!panel.parentElement.contains(evt.target)) cerrar(panel);
    });
  });

  // ------------------------------------------------------------------------------------------
  // Campana: botón con aria-expanded y panel propio. Al abrir pide la lista (un solo GET).
  // ------------------------------------------------------------------------------------------
  document.addEventListener("click", function (evt) {
    var boton = evt.target.closest && evt.target.closest(".m-campana__boton");
    if (!boton) return;
    var panel = document.getElementById(boton.getAttribute("aria-controls"));
    if (!panel) return;
    if (panel.hidden) {
      panel.hidden = false;
      boton.setAttribute("aria-expanded", "true");
      if (window.htmx) window.htmx.trigger(boton, "abrir-campana");
    } else {
      cerrar(panel);
    }
  });

  // ------------------------------------------------------------------------------------------
  // Menú móvil: <dialog> con el mismo menú.
  // ------------------------------------------------------------------------------------------
  document.addEventListener("click", function (evt) {
    var boton = evt.target.closest && evt.target.closest("[data-abre-menu]");
    if (!boton) return;
    var dlg = document.getElementById(boton.getAttribute("aria-controls"));
    if (dlg && dlg.showModal && !dlg.open) {
      dlg.showModal();
      boton.setAttribute("aria-expanded", "true");
      dlg.addEventListener("close", function () { boton.setAttribute("aria-expanded", "false"); boton.focus(); }, { once: true });
    }
  });

  // ------------------------------------------------------------------------------------------
  // Paleta ⌘K: «Ir a» las pantallas del menú y filtro por texto (la búsqueda real llega en
  // F-dis-paleta-campana).
  // ------------------------------------------------------------------------------------------
  function normalizar(s) {
    return (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  }

  function paleta() { return document.getElementById("paleta"); }

  function opcionesVisibles(dlg) {
    return Array.prototype.filter.call(dlg.querySelectorAll("[data-paleta-op]"), function (li) { return !li.hidden; });
  }

  function marcar(dlg, idx) {
    var ops = opcionesVisibles(dlg);
    var q = dlg.querySelector("[data-paleta-q]");
    dlg.querySelectorAll("[data-paleta-op]").forEach(function (li) { li.setAttribute("aria-selected", "false"); });
    if (!ops.length) { q.removeAttribute("aria-activedescendant"); return; }
    idx = Math.max(0, Math.min(ops.length - 1, idx));
    ops[idx].setAttribute("aria-selected", "true");
    q.setAttribute("aria-activedescendant", ops[idx].id);
    ops[idx].scrollIntoView({ block: "nearest" });
    dlg.setAttribute("data-idx", String(idx));
  }

  function filtrar(dlg) {
    var q = normalizar(dlg.querySelector("[data-paleta-q]").value.trim());
    var n = 0;
    dlg.querySelectorAll("[data-paleta-op]").forEach(function (li) {
      var ok = q ? normalizar(li.getAttribute("data-texto")).indexOf(q) >= 0 : !li.hasAttribute("data-solo-busqueda");
      li.hidden = !ok;
      if (ok) n++;
    });
    var vacio = dlg.querySelector("[data-paleta-vacio]");
    if (vacio) {
      vacio.hidden = n > 0;
      var eco = vacio.querySelector("[data-paleta-eco]");
      if (eco) eco.textContent = dlg.querySelector("[data-paleta-q]").value.trim();
    }
    marcar(dlg, 0);
  }

  function abrirPaleta() {
    var dlg = paleta();
    if (!dlg || dlg.open) return;
    var q = dlg.querySelector("[data-paleta-q]");
    q.value = "";
    filtrar(dlg);
    dlg.showModal();
    q.focus();
  }

  document.addEventListener("click", function (evt) {
    if (evt.target.closest && evt.target.closest("[data-abre-paleta]")) {
      evt.preventDefault();
      var dlg = document.getElementById("menu-movil");
      if (dlg && dlg.open) dlg.close();
      abrirPaleta();
    }
  });

  document.addEventListener("input", function (evt) {
    if (evt.target.matches && evt.target.matches("[data-paleta-q]")) filtrar(paleta());
  });

  document.addEventListener("keydown", function (evt) {
    if (!(evt.target.matches && evt.target.matches("[data-paleta-q]"))) return;
    var dlg = paleta();
    var idx = parseInt(dlg.getAttribute("data-idx") || "0", 10) || 0;
    if (evt.key === "ArrowDown") { evt.preventDefault(); marcar(dlg, idx + 1); }
    else if (evt.key === "ArrowUp") { evt.preventDefault(); marcar(dlg, idx - 1); }
    else if (evt.key === "Enter") {
      evt.preventDefault();
      var op = opcionesVisibles(dlg)[idx];
      var a = op && op.querySelector("a");
      if (a) a.click();
    }
  });

  // ------------------------------------------------------------------------------------------
  // Filas de lista [data-fila]: J/K o ↑/↓ recorren y abren la elegida con su [data-abrir].
  // ------------------------------------------------------------------------------------------
  function moverFila(paso) {
    var filas = Array.prototype.filter.call(document.querySelectorAll("[data-fila]"), visible);
    if (!filas.length) return false;
    var actual = filas.findIndex(function (f) { return f.getAttribute("data-activa") === "true"; });
    if (actual < 0) {
      actual = filas.findIndex(function (f) { return f.contains(document.activeElement); });
    }
    var sig = actual < 0 ? (paso > 0 ? 0 : filas.length - 1) : Math.max(0, Math.min(filas.length - 1, actual + paso));
    filas.forEach(function (f) { f.removeAttribute("data-activa"); });
    var fila = filas[sig];
    fila.setAttribute("data-activa", "true");
    var abrir = fila.querySelector("[data-abrir]");
    if (abrir) { abrir.focus(); abrir.click(); }
    fila.scrollIntoView({ block: "nearest" });
    return true;
  }

  // ------------------------------------------------------------------------------------------
  // Atajos globales.
  // ------------------------------------------------------------------------------------------
  var IR = { h: "/?hoy=1", m: "/mesa", t: "/trabajo", v: "/ventas", d: "/catalogo" };
  var esperaG = 0;

  document.addEventListener("keydown", function (evt) {
    if (evt.defaultPrevented) return;
    var tecla = (evt.key || "").toLowerCase();

    if (tecla === "escape") {
      var lista = abiertos();
      var ultimo = lista[lista.length - 1];
      // Un <dialog> modal se cierra solo con Esc (nativo); lo demás, aquí.
      if (ultimo && !(ultimo.tagName === "DIALOG" && ultimo.open)) {
        cerrar(ultimo);
        evt.preventDefault();
      }
      return;
    }
    if (enCampo(evt.target)) return;

    if (tecla === "k" && (evt.metaKey || evt.ctrlKey)) {
      evt.preventDefault();
      abrirPaleta();
      return;
    }
    if (evt.metaKey || evt.ctrlKey || evt.altKey) return;
    if (document.querySelector("dialog[open]")) return;

    if (esperaG && Date.now() - esperaG < 1500 && IR[tecla]) {
      esperaG = 0;
      evt.preventDefault();
      window.location.href = IR[tecla];
      return;
    }
    esperaG = 0;
    if (tecla === "g") { esperaG = Date.now(); return; }

    if (evt.key === "/") {
      var buscar = document.querySelector("[data-buscar]");
      if (buscar) { evt.preventDefault(); buscar.focus(); if (buscar.select) buscar.select(); }
      return;
    }
    if (tecla === "j" || evt.key === "ArrowDown") {
      if (moverFila(1)) evt.preventDefault();
      return;
    }
    if (tecla === "k" || evt.key === "ArrowUp") {
      if (moverFila(-1)) evt.preventDefault();
    }
  });

  // ------------------------------------------------------------------------------------------
  // Avisos con «Deshacer» que llegan por HX-Trigger: {"aviso": {"texto": "…", "deshacer": "/url"}}.
  // ------------------------------------------------------------------------------------------
  function mostrarAviso(datos) {
    var cont = document.getElementById("avisos");
    if (!cont || !datos || !datos.texto) return;
    var el = document.createElement("div");
    el.className = "m-aviso";
    el.setAttribute("role", "status");
    var texto = document.createElement("span");
    texto.className = "m-aviso__texto";
    texto.textContent = datos.texto;
    el.appendChild(texto);
    if (datos.deshacer) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "r-btn r-btn--terciario r-btn--sm";
      b.setAttribute("hx-post", datos.deshacer);
      b.setAttribute("data-deshacer", "");
      var s = document.createElement("span");
      s.textContent = "Deshacer";
      b.appendChild(s);
      el.appendChild(b);
    }
    cont.appendChild(el);
    if (window.htmx) window.htmx.process(el);
    window.setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 8000);
  }
  window.rastrilloAviso = mostrarAviso;

  document.addEventListener("aviso", function (evt) {
    var d = evt.detail || {};
    mostrarAviso(d.value && typeof d.value === "object" ? d.value : d);
  });
  // Tras deshacer, el aviso se va.
  document.addEventListener("htmx:afterRequest", function (evt) {
    var elt = evt.detail && evt.detail.elt;
    if (elt && elt.hasAttribute && elt.hasAttribute("data-deshacer") && evt.detail.successful) {
      var aviso = elt.closest(".m-aviso");
      if (aviso && aviso.parentNode) aviso.parentNode.removeChild(aviso);
    }
  });

  // «Según el sistema»: Bootstrap (pantallas aún no migradas) sigue al sistema en vivo.
  function seguirSistema() {
    var html = document.documentElement;
    if (html.getAttribute("data-tema") !== "sistema" || !window.matchMedia) return;
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var aplicar = function () { html.setAttribute("data-bs-theme", mq.matches ? "dark" : "light"); };
    if (mq.addEventListener) mq.addEventListener("change", aplicar);
  }

  rotularTeclas();
  seguirSistema();
})();
