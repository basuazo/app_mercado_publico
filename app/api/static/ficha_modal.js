/* Ficha en modal (F-ficha-modal): lo usan el feed, Mi registro y Explorar CA.
 *
 * El contenido llega por HTMX (parcial de /oportunidad/{fuente}/{codigo}) dentro de
 * #modal-ficha .modal-content. Este script se ocupa de:
 *  - abrir: un listener delegado sobre los enlaces `a[data-abre-ficha]`. Con clic normal abre
 *    el modal; con Ctrl/Cmd/Shift/Alt o clic medio no hace nada y el navegador sigue el href
 *    (htmx cancelaría ese clic antes de evaluar un filtro, por eso los enlaces no llevan hx-get);
 *  - la URL: al abrir agrega ficha=<fuente>:<codigo> a la query ACTUAL (sin tocar los
 *    filtros); Atrás cierra el modal; cerrar con × / Esc hace history.back() si el modal
 *    agregó la entrada; cargar la página con ?ficha= lo abre al entrar;
 *  - Anterior / Siguiente sobre las tarjetas presentes en el DOM (sin estado en servidor);
 *  - si una acción saca la tarjeta de la lista, pasa a la que seguía (o cierra);
 *  - el foco (al título al abrir; a la tarjeta que lo abrió al cerrar) y el anuncio.
 */
(function () {
  "use strict";

  var modalEl = document.getElementById("modal-ficha");
  if (!modalEl) return;
  var contenido = modalEl.querySelector(".modal-content");

  var abridor = null; // enlace de la tarjeta que abrió el modal
  var claveActual = null; // "fuente:codigo" de la ficha mostrada
  var modo = "push"; // cómo se está abriendo: push (clic), historial (Adelante) o inicial (?ficha=)
  var pusheado = false; // el modal agregó una entrada al historial
  var cierreSinHistorial = false; // se está cerrando por Atrás: no volver a llamar a history.back()
  var ignorarPopstate = false; // el popstate lo causó el propio cierre
  var accionPendiente = null; // lo que había en la lista ANTES de una acción hecha en el modal
  var recargar = false; // la lista de atrás quedó desactualizada: recargarla al cerrar

  function instancia() {
    return window.bootstrap ? window.bootstrap.Modal.getOrCreateInstance(modalEl) : null;
  }

  function abierto() {
    return modalEl.classList.contains("show");
  }

  function claveEnUrl() {
    return new URLSearchParams(window.location.search).get("ficha");
  }

  function urlConFicha(clave) {
    var u = new URL(window.location.href);
    if (clave) u.searchParams.set("ficha", clave);
    else u.searchParams.delete("ficha");
    return u.pathname + u.search + u.hash;
  }

  function rutaDe(clave) {
    var partes = clave.split(":");
    return "/oportunidad/" + partes[0] + "/" + partes.slice(1).join(":");
  }

  // De qué página viene la ficha: Mi registro sabe la pestaña (para sacar la tarjeta al
  // guardar o descartar) y Explorar CA actualiza los botones de su fila.
  function desdeActual() {
    if (window.location.pathname === "/compras-agiles") return "explorador";
    if (window.location.pathname !== "/registro") return "";
    var tab = new URLSearchParams(window.location.search).get("tab") || "guardadas";
    return "registro:" + tab;
  }

  function cargar(clave) {
    var url = rutaDe(clave);
    var desde = desdeActual();
    if (desde) url += "?desde=" + encodeURIComponent(desde);
    window.htmx.ajax("GET", url, { target: "#modal-ficha .modal-content", swap: "innerHTML" });
  }

  function anunciar(texto) {
    var region = document.getElementById("anuncios");
    if (region) region.textContent = texto;
  }

  // --- Abrir con clic normal; con modificador o clic medio navega al href -------------
  function claveDeEnlace(enlace) {
    var tarjeta = enlace.closest("[data-oportunidad-key]");
    if (tarjeta) return tarjeta.getAttribute("data-oportunidad-key");
    var m = /^\/oportunidad\/([^/]+)\/([^/?#]+)/.exec(enlace.getAttribute("href") || "");
    return m ? m[1] + ":" + decodeURIComponent(m[2]) : null;
  }

  document.addEventListener("click", function (evt) {
    var enlace = evt.target.closest ? evt.target.closest("a[data-abre-ficha]") : null;
    if (!enlace) return;
    if (evt.button !== 0 || evt.ctrlKey || evt.metaKey || evt.shiftKey || evt.altKey) return;
    var clave = claveDeEnlace(enlace);
    if (!clave) return;
    evt.preventDefault();
    abridor = enlace;
    cargar(clave);
  });

  // --- Anterior / Siguiente sobre las tarjetas del DOM -------------------------
  function claves() {
    var vistas = {};
    var salida = [];
    document.querySelectorAll("[data-oportunidad-key]").forEach(function (nodo) {
      // Un marcador oculto (fila descartada del explorador) no es una tarjeta.
      if (nodo.hidden || (nodo.closest && nodo.closest("[hidden]"))) return;
      var k = nodo.getAttribute("data-oportunidad-key");
      if (!vistas[k]) {
        vistas[k] = true;
        salida.push(k);
      }
    });
    return salida;
  }

  function actualizarNav() {
    var lista = claves();
    var i = lista.indexOf(claveActual);
    var prev = contenido.querySelector('[data-ficha-nav="prev"]');
    var next = contenido.querySelector('[data-ficha-nav="next"]');
    var pos = contenido.querySelector("[data-ficha-pos]");
    // La ficha no está entre las tarjetas de la lista (se abrió por enlace, o la tarjeta se
    // fue): no hay a dónde ir, así que no se muestra una navegación vacía.
    var fuera = i < 0 || recargar;
    [prev, next, pos].forEach(function (nodo) {
      if (nodo) nodo.hidden = fuera;
    });
    if (fuera) return;
    if (prev) prev.disabled = i === 0;
    if (next) next.disabled = i >= lista.length - 1;
    if (pos) pos.textContent = i + 1 + " de " + lista.length;
  }

  contenido.addEventListener("click", function (evt) {
    var cierraPanel = evt.target.closest ? evt.target.closest("[data-cierra-panel-excluir]") : null;
    if (cierraPanel) {
      var panel = contenido.querySelector("#ficha-panel-excluir");
      if (panel) panel.innerHTML = "";
      enfocarTitulo();
      return;
    }
    var boton = evt.target.closest ? evt.target.closest("[data-ficha-nav]") : null;
    if (!boton || boton.disabled) return;
    var lista = claves();
    var i = lista.indexOf(claveActual);
    var destino = lista[i + (boton.getAttribute("data-ficha-nav") === "next" ? 1 : -1)];
    if (destino) cargar(destino);
  });

  // --- Una acción hecha en el modal puede sacar la tarjeta de la lista ---------------
  // El índice se calcula ANTES de la acción: después la tarjeta ya no está.
  document.body.addEventListener("htmx:beforeRequest", function (evt) {
    var d = evt.detail || {};
    var elt = d.elt;
    var verbo = d.requestConfig && d.requestConfig.verb;
    if (!elt || !elt.closest || !elt.closest("#modal-ficha") || verbo !== "post") return;
    var lista = claves();
    var i = lista.indexOf(claveActual);
    accionPendiente = {
      clave: claveActual,
      enLista: i >= 0,
      siguiente: i >= 0 ? lista[i + 1] || lista[i - 1] || null : null,
    };
  });

  document.body.addEventListener("htmx:afterRequest", function (evt) {
    var p = accionPendiente;
    if (!p) return;
    accionPendiente = null;
    if (!evt.detail || !evt.detail.successful) return;
    // "Descartar y excluir términos" exitoso: el aviso con "Deshacer exclusión" debe quedar a
    // la vista (no se avanza sola) y la lista de atrás hay que recargarla al cerrar.
    if (contenido.querySelector("[data-recargar-lista]")) {
      recargar = true;
      actualizarNav();
      return;
    }
    if (!p.enLista) return;
    // En el tick siguiente: ya se aplicaron las salidas fuera de banda y el script del
    // dashboard ya quitó la tarjeta descartada.
    window.setTimeout(function () {
      if (claveActual !== p.clave) return;
      if (claves().indexOf(p.clave) >= 0) {
        actualizarNav();
        return;
      }
      if (p.siguiente && claves().indexOf(p.siguiente) >= 0) {
        cargar(p.siguiente);
      } else {
        var m = instancia();
        if (m) m.hide();
      }
    }, 0);
  });

  // --- Apertura: el contenido llegó ------------------------------------------------
  document.body.addEventListener("htmx:afterSwap", function (evt) {
    if (!evt.detail || evt.detail.target !== contenido) return;
    var raiz = contenido.querySelector("[data-ficha-key]");
    if (!raiz) return;
    var clave = raiz.getAttribute("data-ficha-key");
    var nombre = raiz.getAttribute("data-ficha-nombre") || "";
    var yaAbierto = abierto();
    claveActual = clave;

    if (!yaAbierto) {
      if (modo === "push") {
        window.history.pushState({ ficha: clave }, "", urlConFicha(clave));
        pusheado = true;
      } else {
        pusheado = modo === "historial";
      }
      modo = "push";
      var m = instancia();
      if (m) m.show();
      else
        window.addEventListener("load", function () {
          var tarde = instancia();
          if (tarde) tarde.show();
        });
    } else {
      // Anterior / Siguiente: la misma entrada del historial cambia de ficha.
      window.history.replaceState({ ficha: clave }, "", urlConFicha(clave));
      enfocarTitulo();
    }
    actualizarNav();
    anunciar("Ficha abierta: " + nombre);
  });

  function enfocarTitulo() {
    var titulo = contenido.querySelector("#ficha-titulo");
    if (titulo) titulo.focus();
  }

  modalEl.addEventListener("shown.bs.modal", enfocarTitulo);

  // --- Cierre ------------------------------------------------------------------
  modalEl.addEventListener("hide.bs.modal", function () {
    if (cierreSinHistorial) {
      cierreSinHistorial = false;
      pusheado = false;
      return;
    }
    if (pusheado) {
      // Deja el historial como estaba antes de abrir: sin entradas huérfanas.
      pusheado = false;
      ignorarPopstate = true;
      window.history.back();
    } else if (claveEnUrl()) {
      window.history.replaceState(null, "", urlConFicha(null));
    }
  });

  modalEl.addEventListener("hidden.bs.modal", function () {
    var destino = abridor && document.contains(abridor) ? abridor : null;
    if (!destino && claveActual) {
      destino = document.querySelector(
        '[data-oportunidad-key="' + claveActual + '"] [data-abre-ficha]'
      );
    }
    if (!destino) destino = document.querySelector("#toasts [data-deshacer]");
    if (destino) destino.focus();
    abridor = null;
    claveActual = null;
    // Se abrió por enlace (sin entrada propia en el historial): recargar sin ?ficha=.
    if (recargar && !ignorarPopstate) window.location.replace(urlConFicha(null));
    // Si todavía espera el popstate de history.back(), ese listener es el que recarga.
    if (!ignorarPopstate) recargar = false;
  });

  // --- Atrás / Adelante -----------------------------------------------------------
  window.addEventListener("popstate", function () {
    if (ignorarPopstate) {
      ignorarPopstate = false;
      // Cierre tras excluir términos: ya estamos en la URL sin ficha; se recarga la lista.
      if (recargar) window.location.replace(urlConFicha(null));
      return;
    }
    var clave = claveEnUrl();
    if (!clave) {
      if (abierto()) {
        cierreSinHistorial = true;
        var m = instancia();
        if (m) m.hide();
      }
    } else if (clave !== claveActual) {
      modo = "historial";
      cargar(clave);
    }
  });

  // --- Enlace compartible: ?ficha=<fuente>:<codigo> abre el modal al entrar ------------
  var inicial = claveEnUrl();
  if (inicial) {
    modo = "inicial";
    cargar(inicial);
  }
})();
