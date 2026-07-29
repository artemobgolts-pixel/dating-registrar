/* Локальная тема date4you.
 *
 * Cookie, а не настройка профиля: выбор относится к этому браузеру и работает
 * на публичных страницах, до входа и в кабинетах. Скрипт подключается в <head>
 * перед CSS, поэтому сохранённая тёмная тема применяется без светлой вспышки.
 */
(function () {
  "use strict";

  var COOKIE = "d4y_theme";
  var MAX_AGE = 60 * 60 * 24 * 365;
  var root = document.documentElement;
  var transitionBusy = false;

  function savedTheme() {
    var match = document.cookie.match(new RegExp("(?:^|;\\s*)" + COOKIE + "=(light|dark)(?:;|$)"));
    return match ? match[1] : "light";
  }

  function updateChrome(theme) {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) return;
    var friends = root.dataset.skin === "friends";
    meta.setAttribute("content", theme === "dark"
      ? (friends ? "#15182a" : "#211a20")
      : (friends ? "#554eae" : "#b65f6f"));
  }

  function syncSkin() {
    // Turbo заменяет body, но оставляет корневой <html>. Дублируем скин в body
    // публичных страниц и после перехода переносим его на html, чтобы CSS,
    // theme-color и постоянный WebGL-canvas получили актуальную палитру.
    var bodySkin = document.body && document.body.dataset.skin;
    if (bodySkin !== "friends" && bodySkin !== "romantic") return;
    var changed = root.dataset.skin !== bodySkin;
    root.dataset.skin = bodySkin;
    if (changed) {
      document.dispatchEvent(new CustomEvent("d4y:skinchange", {
        detail: { skin: bodySkin }
      }));
    }
  }

  function updateButtons(theme) {
    var dark = theme === "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.setAttribute("aria-pressed", dark ? "true" : "false");
      button.setAttribute("aria-label", dark ? "Включить светлую тему" : "Включить тёмную тему");
      button.setAttribute("title", dark ? "Светлая тема" : "Тёмная тема");
      var icon = button.querySelector("[data-theme-icon]");
      if (icon) {
        icon.textContent = "";
        icon.classList.toggle("theme-sun-icon", dark);
        icon.classList.toggle("theme-moon-icon", !dark);
      }
      var label = button.querySelector("[data-theme-label]");
      if (label) label.textContent = dark ? "Светлая тема" : "Тёмная тема";
    });
    document.querySelectorAll("[data-theme-set]").forEach(function (button) {
      var active = button.getAttribute("data-theme-set") === theme;
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.classList.toggle("on", active);
    });
  }

  function applyTheme(theme, persist) {
    theme = theme === "dark" ? "dark" : "light";
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    if (persist) {
      document.cookie = COOKIE + "=" + theme + "; Path=/; Max-Age=" + MAX_AGE + "; SameSite=Lax";
    }
    updateChrome(theme);
    updateButtons(theme);
    document.dispatchEvent(new CustomEvent("d4y:themechange", { detail: { theme: theme } }));
  }

  function transitionOrigin(source, event) {
    var rect = source && source.getBoundingClientRect
      ? source.getBoundingClientRect() : null;
    // При управлении клавиатурой click приходит с координатами 0/0 — тогда
    // раскрываем тему из центра самой кнопки.
    var pointerClick = event && event.detail !== 0
      && Number.isFinite(event.clientX) && Number.isFinite(event.clientY);
    return {
      x: pointerClick ? event.clientX
        : (rect ? rect.left + rect.width / 2 : window.innerWidth / 2),
      y: pointerClick ? event.clientY
        : (rect ? rect.top + rect.height / 2 : window.innerHeight / 2)
    };
  }

  function canAnimateTheme() {
    return typeof document.startViewTransition === "function"
      && typeof root.animate === "function"
      && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function animateTheme(theme, source, event) {
    theme = theme === "dark" ? "dark" : "light";
    if (theme === root.dataset.theme) {
      applyTheme(theme, true);
      return;
    }
    if (!canAnimateTheme() || transitionBusy) {
      if (!transitionBusy) applyTheme(theme, true);
      return;
    }

    var point = transitionOrigin(source, event);
    var farX = Math.max(point.x, window.innerWidth - point.x);
    var farY = Math.max(point.y, window.innerHeight - point.y);
    var radius = Math.ceil(Math.hypot(farX, farY));
    var duration = Math.round(Math.max(520, Math.min(760, radius * 0.52)));
    transitionBusy = true;
    root.classList.add("d4y-theme-transition");

    var transition;
    try {
      transition = document.startViewTransition(function () {
        applyTheme(theme, true);
      });
    } catch (_) {
      root.classList.remove("d4y-theme-transition");
      transitionBusy = false;
      applyTheme(theme, true);
      return;
    }

    transition.ready.then(function () {
      var at = point.x + "px " + point.y + "px";
      root.animate(
        {
          clipPath: [
            "circle(0px at " + at + ")",
            "circle(" + radius + "px at " + at + ")"
          ]
        },
        {
          duration: duration,
          easing: "cubic-bezier(.22,.72,.24,1)",
          fill: "both",
          pseudoElement: "::view-transition-new(root)"
        }
      );
    }).catch(function () {
      // Тема уже применена в callback; браузер просто покажет её без волны.
    });

    transition.finished.finally(function () {
      root.classList.remove("d4y-theme-transition");
      transitionBusy = false;
    });
  }

  // Выполняется синхронно до CSS.
  applyTheme(savedTheme(), false);

  // Убираем стандартное перекрёстное растворение View Transition: старый
  // снимок остаётся снизу, а новый раскрывается над ним круглой волной.
  var transitionStyle = document.createElement("style");
  transitionStyle.id = "d4y-theme-transition-style";
  transitionStyle.textContent =
    ":root.d4y-theme-transition::view-transition-old(root)," +
    ":root.d4y-theme-transition::view-transition-new(root){" +
      "animation:none;mix-blend-mode:normal;}" +
    ":root.d4y-theme-transition::view-transition-old(root){z-index:1;}" +
    ":root.d4y-theme-transition::view-transition-new(root){z-index:2;}";
  document.head.appendChild(transitionStyle);

  document.addEventListener("click", function (event) {
    var choice = event.target.closest && event.target.closest("[data-theme-set]");
    if (choice) {
      event.preventDefault();
      animateTheme(choice.getAttribute("data-theme-set"), choice, event);
      return;
    }
    var button = event.target.closest && event.target.closest("[data-theme-toggle]");
    if (!button) return;
    event.preventDefault();
    animateTheme(root.dataset.theme === "dark" ? "light" : "dark", button, event);
  });

  document.addEventListener("DOMContentLoaded", function () {
    syncSkin();
    updateChrome(root.dataset.theme || "light");
    updateButtons(root.dataset.theme || "light");
  });
  document.addEventListener("d4y:skinchange", function () {
    updateChrome(root.dataset.theme || "light");
  });
  // Turbo заменяет body, но не перезапускает этот скрипт.
  document.addEventListener("turbo:load", function () {
    syncSkin();
    updateChrome(root.dataset.theme || "light");
    updateButtons(root.dataset.theme || "light");
  });
})();
