// Интерактивность операторской панели без зависимостей.
(function () {
  "use strict";

  var body = document.body;
  var sidebar = document.getElementById("operatorSidebar");
  var menuButtons = document.querySelectorAll("[data-op-nav-toggle]");
  var overlay = document.querySelector("[data-op-nav-close]");
  var desktop = window.matchMedia("(min-width: 901px)");

  function setNav(open) {
    if (!sidebar) return;
    body.classList.toggle("op-nav-open", open);
    menuButtons.forEach(function (button) {
      button.setAttribute("aria-expanded", open ? "true" : "false");
    });
    if (!desktop.matches) {
      sidebar.setAttribute("aria-hidden", open ? "false" : "true");
      if ("inert" in sidebar) sidebar.inert = !open;
    } else {
      sidebar.removeAttribute("aria-hidden");
      if ("inert" in sidebar) sidebar.inert = false;
    }
  }

  menuButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      setNav(!body.classList.contains("op-nav-open"));
    });
  });

  if (overlay) {
    overlay.addEventListener("click", function () { setNav(false); });
  }

  if (sidebar) {
    sidebar.addEventListener("click", function (event) {
      if (!desktop.matches && event.target.closest("a")) setNav(false);
    });
  }

  function syncViewport() {
    if (desktop.matches) {
      body.classList.remove("op-nav-open");
      if (sidebar) sidebar.removeAttribute("aria-hidden");
      if (sidebar && "inert" in sidebar) sidebar.inert = false;
    } else if (sidebar && !body.classList.contains("op-nav-open")) {
      sidebar.setAttribute("aria-hidden", "true");
      if ("inert" in sidebar) sidebar.inert = true;
    }
  }

  if (desktop.addEventListener) desktop.addEventListener("change", syncViewport);
  else desktop.addListener(syncViewport);
  syncViewport();

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && body.classList.contains("op-nav-open")) {
      setNav(false);
      var button = document.querySelector("[data-op-nav-toggle]");
      if (button) button.focus();
      return;
    }

    // Быстрый переход в поиск: "/" вне поля ввода.
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey) {
      var target = event.target;
      var editing = target && (
        target.matches("input, textarea, select") ||
        target.isContentEditable
      );
      if (!editing) {
        var search = document.querySelector("[data-op-search]");
        if (search) {
          event.preventDefault();
          search.focus();
          search.select();
        }
      }
    }
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }

    // Даём понятную обратную связь, но не блокируем повторно формы, для которых
    // браузер остановил отправку из-за HTML-валидации.
    window.setTimeout(function () {
      if (!form.checkValidity()) return;
      var button = form.querySelector('button[type="submit"], button:not([type])');
      if (!button || button.disabled) return;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      if (!button.dataset.originalText) button.dataset.originalText = button.textContent;
      button.textContent = "Подождите…";
    }, 0);
  });

  document.addEventListener("click", function (event) {
    var close = event.target.closest("[data-flash-close]");
    if (close) {
      var flash = close.closest(".flash");
      if (flash) flash.remove();
    }
  });
})();
