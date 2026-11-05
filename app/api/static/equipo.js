// F-equipo: campana, bloque de estado compartido y hilo de comentarios.
// Sin estado que importe: todo lo que cuenta vive en el servidor (regla 10).
(function () {
  "use strict";

  // --- 409 (otro cambió la CA antes) y 422 (falta el motivo / texto inválido) se pintan igual:
  // la respuesta trae el bloque con el mensaje y el estado actual.
  document.addEventListener("htmx:beforeSwap", function (evt) {
    var d = evt.detail;
    var s = d.xhr && d.xhr.status;
    if ((s === 409 || s === 422) && d.target && d.target.closest &&
        d.target.closest("[data-equipo], .hilo")) {
      d.shouldSwap = true;
      d.isError = false;
    }
  });

  // --- Título de la pestaña con el contador: «(3) Rastrillo».
  var tituloBase = document.title.replace(/^\(\d+\+?\)\s*/, "");
  function actualizarTitulo() {
    var n = document.getElementById("campana-n") || document.getElementById("campana-n-m");
    if (!n) return;
    var valor = parseInt(n.getAttribute("data-no-leidos") || "0", 10) || 0;
    document.title = valor > 0 ? "(" + (valor > 99 ? "99+" : valor) + ") " + tituloBase : tituloBase;
  }
  document.addEventListener("htmx:afterSettle", actualizarTitulo);
  document.addEventListener("htmx:oobAfterSwap", actualizarTitulo);

  // --- El sondeo del hilo no pisa una edición en curso.
  document.addEventListener("htmx:beforeRequest", function (evt) {
    var elt = evt.detail && evt.detail.elt;
    if (!elt || !elt.hasAttribute || !elt.hasAttribute("data-hilo-lista")) return;
    var hilo = elt.closest(".hilo");
    var escribiendo = hilo && hilo.querySelector("textarea") === document.activeElement &&
      hilo.querySelector("textarea").value.trim() !== "";
    if (elt.querySelector("details[open]") || escribiendo) evt.preventDefault();
  });

  // --- Panel lateral: al cerrarlo se vacía (y deja de sondear).
  document.addEventListener("hidden.bs.offcanvas", function (evt) {
    if (evt.target && evt.target.id === "panel-hilo") {
      var cuerpo = document.getElementById("panel-hilo-cuerpo");
      if (cuerpo) cuerpo.innerHTML = "";
    }
  });

  // --- Cuadro de texto: Enter envía, Shift+Enter salto de línea; autocompletar de @.
  function miembros(area) {
    try { return JSON.parse(area.getAttribute("data-miembros") || "[]"); } catch (e) { return []; }
  }

  function tokenArroba(area) {
    var antes = area.value.slice(0, area.selectionStart);
    var m = /(^|\s)@([^\s@]{0,30})$/.exec(antes);
    return m ? { inicio: antes.length - m[2].length - 1, texto: m[2] } : null;
  }

  function cerrarSugerencias(lista) {
    if (!lista) return;
    lista.classList.add("d-none");
    lista.innerHTML = "";
  }

  function insertar(area, nombre) {
    var t = tokenArroba(area);
    if (!t) return;
    var fin = area.selectionStart;
    area.value = area.value.slice(0, t.inicio) + "@" + nombre + " " + area.value.slice(fin);
    var pos = t.inicio + nombre.length + 2;
    area.setSelectionRange(pos, pos);
    area.focus();
  }

  document.addEventListener("input", function (evt) {
    var area = evt.target;
    if (!area.matches || !area.matches("[data-hilo-texto]")) return;
    var lista = area.parentElement.querySelector(".hilo__sugerencias");
    var t = tokenArroba(area);
    if (!t || !lista) { cerrarSugerencias(lista); return; }
    var q = t.texto.toLowerCase();
    var hallados = miembros(area).filter(function (n) { return n.toLowerCase().indexOf(q) === 0; }).slice(0, 6);
    if (!hallados.length) { cerrarSugerencias(lista); return; }
    lista.innerHTML = "";
    hallados.forEach(function (n, i) {
      var li = document.createElement("li");
      li.setAttribute("role", "option");
      li.className = "hilo__sugerencia" + (i === 0 ? " activa" : "");
      li.textContent = n;  // texto, nunca HTML
      li.addEventListener("mousedown", function (e) { e.preventDefault(); insertar(area, n); cerrarSugerencias(lista); });
      lista.appendChild(li);
    });
    lista.classList.remove("d-none");
  });

  document.addEventListener("keydown", function (evt) {
    var area = evt.target;
    if (!area.matches || !area.matches("[data-hilo-texto]")) return;
    var lista = area.parentElement.querySelector(".hilo__sugerencias");
    var abiertas = lista && !lista.classList.contains("d-none") ? lista.querySelectorAll("li") : [];
    if (abiertas.length && (evt.key === "ArrowDown" || evt.key === "ArrowUp")) {
      evt.preventDefault();
      var idx = 0;
      abiertas.forEach(function (li, i) { if (li.classList.contains("activa")) idx = i; });
      abiertas[idx].classList.remove("activa");
      idx = (idx + (evt.key === "ArrowDown" ? 1 : abiertas.length - 1)) % abiertas.length;
      abiertas[idx].classList.add("activa");
      return;
    }
    if (abiertas.length && (evt.key === "Enter" || evt.key === "Tab")) {
      evt.preventDefault();
      var activa = lista.querySelector("li.activa") || abiertas[0];
      insertar(area, activa.textContent);
      cerrarSugerencias(lista);
      return;
    }
    if (evt.key === "Escape") { cerrarSugerencias(lista); return; }
    if (evt.key === "Enter" && !evt.shiftKey && !evt.isComposing) {
      evt.preventDefault();
      if (area.value.trim() && area.form) {
        if (area.form.requestSubmit) area.form.requestSubmit(); else area.form.submit();
      }
    }
  });
})();
