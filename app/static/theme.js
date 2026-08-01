/* Локальная тема date4you.
 *
 * Cookie, а не настройка профиля: выбор относится к этому браузеру и работает
 * на публичных страницах, до входа и в кабинетах. Скрипт подключается в <head>
 * перед CSS, поэтому сохранённая тёмная тема применяется без светлой вспышки.
 */
(function () {
  "use strict";

  var COOKIE = "d4y_theme";
  var SKIN_COOKIE = "d4y_skin";
  var MAX_AGE = 60 * 60 * 24 * 365;
  var root = document.documentElement;
  var transitionBusy = false;
  // View Transition держит снимок страницы до конца волны. Менять DOM второй
  // раз посреди этой волны нельзя: снимок и фактическая тема расходятся. Быстрые
  // клики поэтому схлопываем до последнего намерения и выполняем его после
  // текущего перехода, не меняя длительность самой анимации.
  var queuedAppearance = null;

  function savedTheme() {
    var match = document.cookie.match(new RegExp("(?:^|;\\s*)" + COOKIE + "=(light|dark)(?:;|$)"));
    return match ? match[1] : "light";
  }

  function savedSkin() {
    var match = document.cookie.match(new RegExp(
      "(?:^|;\\s*)" + SKIN_COOKIE + "=(friends|romantic)(?:;|$)"
    ));
    if (match) return match[1];
    return root.dataset.skin === "friends" ? "friends" : "romantic";
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

  function updateSkinButtons(skin) {
    document.querySelectorAll("[data-skin-set]").forEach(function (button) {
      var active = button.getAttribute("data-skin-set") === skin;
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.classList.toggle("on", active);
    });
  }

  function updateSkinAssets(skin) {
    var suffix = skin === "friends" ? "Friends" : "Romantic";
    document.querySelectorAll("[data-skin-asset]").forEach(function (node) {
      var href = node.dataset["href" + suffix];
      if (href) node.setAttribute("href", href);
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

  function applySkin(skin, persist) {
    skin = skin === "romantic" ? "romantic" : "friends";
    var changed = root.dataset.skin !== skin;
    root.dataset.skin = skin;
    if (document.body) document.body.dataset.skin = skin;
    if (persist) {
      document.cookie = SKIN_COOKIE + "=" + skin
        + "; Path=/; Max-Age=" + MAX_AGE + "; SameSite=Lax";
    }
    updateChrome(root.dataset.theme || "light");
    updateSkinButtons(skin);
    updateSkinAssets(skin);
    if (changed) {
      document.dispatchEvent(new CustomEvent("d4y:skinchange", {
        detail: { skin: skin }
      }));
    }
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

  function canAnimateAppearance() {
    return typeof document.startViewTransition === "function"
      && typeof root.animate === "function"
      && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function animateAppearance(change, source, event, timing) {
    if (!canAnimateAppearance()) {
      change();
      return;
    }
    if (transitionBusy) {
      queuedAppearance = function () {
        animateAppearance(change, source, event, timing);
      };
      return;
    }

    var point = transitionOrigin(source, event);
    var farX = Math.max(point.x, window.innerWidth - point.x);
    var farY = Math.max(point.y, window.innerHeight - point.y);
    var radius = Math.ceil(Math.hypot(farX, farY));
    timing = timing || {};
    var minDuration = timing.minDuration || 520;
    var maxDuration = timing.maxDuration || 760;
    var durationFactor = timing.durationFactor || 0.52;
    var duration = Math.round(Math.max(
      minDuration,
      Math.min(maxDuration, radius * durationFactor)
    ));
    transitionBusy = true;
    root.classList.add("d4y-theme-transition");

    var transition;
    try {
      transition = document.startViewTransition(function () {
        change();
      });
    } catch (_) {
      root.classList.remove("d4y-theme-transition");
      transitionBusy = false;
      change();
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
          easing: timing.easing || "cubic-bezier(.22,.72,.24,1)",
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
      var next = queuedAppearance;
      queuedAppearance = null;
      if (next) next();
    });
  }

  function animateTheme(theme, source, event) {
    theme = theme === "dark" ? "dark" : "light";
    if (transitionBusy) {
      queuedAppearance = function () {
        animateTheme(theme, source, event);
      };
      return;
    }
    if (theme === root.dataset.theme) {
      applyTheme(theme, true);
      return;
    }
    animateAppearance(function () {
      applyTheme(theme, true);
    }, source, event);
  }

  function animateSkin(skin, source, event, persist) {
    skin = skin === "romantic" ? "romantic" : "friends";
    if (transitionBusy) {
      queuedAppearance = function () {
        animateSkin(skin, source, event, persist);
      };
      return;
    }
    if (skin === root.dataset.skin) {
      applySkin(skin, persist);
      return;
    }
    animateAppearance(function () {
      applySkin(skin, persist);
    }, source, event, {
      // Оформление меняет всю визуальную систему страницы, поэтому волна
      // идёт заметно медленнее light/dark и читается от самой кнопки до углов.
      minDuration: 1050,
      maxDuration: 1550,
      durationFactor: 1.04,
      easing: "cubic-bezier(.18,.62,.2,1)"
    });
  }

  // Выполняется синхронно до CSS.
  // Cookie оформления читается только на явно переключаемой странице входа:
  // кабинет и публичные категории продолжают получать свой skin с сервера.
  if (root.hasAttribute("data-skin-switchable")) {
    applySkin(savedSkin(), false);
  }
  applyTheme(savedTheme(), false);

  // Профиль хранит skin на сервере, но использует тот же движок волны. API не
  // меняет cookie и не смешивает оформление с light/dark-настройкой браузера.
  window.d4yAppearance = {
    applySkin: function (skin) { applySkin(skin, false); },
    animateSkin: function (skin, source, event) {
      animateSkin(skin, source, event, false);
    }
  };

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
    var skinChoice = event.target.closest && event.target.closest("[data-skin-set]");
    if (skinChoice) {
      event.preventDefault();
      animateSkin(skinChoice.getAttribute("data-skin-set"), skinChoice, event, true);
      return;
    }
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
    updateSkinButtons(root.dataset.skin || "friends");
  });
  document.addEventListener("d4y:skinchange", function () {
    updateChrome(root.dataset.theme || "light");
    updateSkinButtons(root.dataset.skin || "friends");
  });
  // Turbo заменяет body, но не перезапускает этот скрипт.
  document.addEventListener("turbo:load", function () {
    syncSkin();
    updateChrome(root.dataset.theme || "light");
    updateButtons(root.dataset.theme || "light");
    updateSkinButtons(root.dataset.skin || "friends");
  });
})();
