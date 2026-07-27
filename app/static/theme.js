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

  function savedTheme() {
    var match = document.cookie.match(new RegExp("(?:^|;\\s*)" + COOKIE + "=(light|dark)(?:;|$)"));
    return match ? match[1] : "light";
  }

  function updateChrome(theme) {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#211a20" : "#b65f6f");
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

  // Выполняется синхронно до CSS.
  applyTheme(savedTheme(), false);

  document.addEventListener("click", function (event) {
    var choice = event.target.closest && event.target.closest("[data-theme-set]");
    if (choice) {
      event.preventDefault();
      applyTheme(choice.getAttribute("data-theme-set"), true);
      return;
    }
    var button = event.target.closest && event.target.closest("[data-theme-toggle]");
    if (!button) return;
    event.preventDefault();
    applyTheme(root.dataset.theme === "dark" ? "light" : "dark", true);
  });

  document.addEventListener("DOMContentLoaded", function () {
    updateChrome(root.dataset.theme || "light");
    updateButtons(root.dataset.theme || "light");
  });
  // Turbo заменяет body, но не перезапускает этот скрипт.
  document.addEventListener("turbo:load", function () {
    updateChrome(root.dataset.theme || "light");
    updateButtons(root.dataset.theme || "light");
  });
})();
