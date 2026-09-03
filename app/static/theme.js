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
  var activeTransition = null;
  var activeRevealAnimation = null;
  var appearanceGeneration = 0;
  // Желаемое состояние хранится отдельно от DOM: если новая команда прерывает
  // активную волну, следующий callback применяет обе последние координаты
  // оформления вместе и старый callback уже не может откатить одну из них.
  var desiredTheme = null;
  var desiredThemePersist = false;
  var desiredSkin = null;
  var desiredSkinPersist = false;
  var SKIN_TIMING = {
    minDuration: 320,
    maxDuration: 480,
    durationFactor: 0.34,
    easing: "cubic-bezier(.18,.62,.2,1)"
  };
  // The landing presents appearance as part of the product story. Its wave is
  // deliberately slower and more legible, while authenticated screens keep the
  // compact timings above for everyday use.
  var LANDING_SKIN_TIMING = {
    minDuration: 1080,
    maxDuration: 1380,
    durationFactor: 0.78,
    easing: "cubic-bezier(.72,0,.18,1)"
  };
  var LANDING_THEME_TIMING = {
    minDuration: 760,
    maxDuration: 1040,
    durationFactor: 0.58,
    easing: "cubic-bezier(.68,0,.2,1)"
  };

  function isLandingAppearance() {
    return root.dataset.appearanceScope === "landing";
  }

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

  function applyDesiredAppearance() {
    var theme = desiredTheme || root.dataset.theme || "light";
    var skin = desiredSkin || root.dataset.skin || "friends";
    var themeChanged = theme !== root.dataset.theme;
    var skinChanged = skin !== root.dataset.skin;
    var persistTheme = desiredThemePersist;
    var persistSkin = desiredSkinPersist;
    desiredThemePersist = false;
    desiredSkinPersist = false;
    // Неизменившаяся координата оформления не должна повторно обновлять DOM,
    // favicon и живой фон во время второй половины волны.
    if (skinChanged || persistSkin) applySkin(skin, persistSkin);
    if (themeChanged || persistTheme) applyTheme(theme, persistTheme);
  }

  function announceAppearanceTransition(started) {
    document.dispatchEvent(new CustomEvent(
      started ? "d4y:appearance-transition-start" : "d4y:appearance-transition-end"
    ));
  }

  function finishAppearance(generation) {
    if (generation !== appearanceGeneration) return;
    activeRevealAnimation = null;
    activeTransition = null;
    transitionBusy = false;
    root.classList.remove("d4y-theme-transition");
    announceAppearanceTransition(false);
  }

  function animateAppearance(change, source, event, timing) {
    if (!canAnimateAppearance()) {
      change();
      return;
    }

    // Повторное намерение не ждёт старую волну: текущая View Transition
    // завершается, а новая сразу начинается из уже применённого состояния.
    // Это сохраняет agency даже при очень быстрых переключениях.
    if (transitionBusy) interruptAppearance();

    var point = transitionOrigin(source, event);
    var farX = Math.max(point.x, window.innerWidth - point.x);
    var farY = Math.max(point.y, window.innerHeight - point.y);
    var radius = Math.ceil(Math.hypot(farX, farY));
    timing = timing || {};
    var minDuration = timing.minDuration || 260;
    var maxDuration = timing.maxDuration || 420;
    var durationFactor = timing.durationFactor || 0.30;
    var duration = Math.round(Math.max(
      minDuration,
      Math.min(maxDuration, radius * durationFactor)
    ));
    transitionBusy = true;
    var generation = ++appearanceGeneration;
    root.classList.add("d4y-theme-transition");
    announceAppearanceTransition(true);

    var transition;
    try {
      transition = document.startViewTransition(function () {
        change();
      });
      activeTransition = transition;
    } catch (_) {
      change();
      finishAppearance(generation);
      return;
    }

    transition.ready.then(function () {
      var at = point.x + "px " + point.y + "px";
      activeRevealAnimation = root.animate(
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
      finishAppearance(generation);
    });
  }

  function interruptAppearance() {
    var wasBusy = transitionBusy;
    appearanceGeneration += 1;
    if (activeRevealAnimation) {
      try { activeRevealAnimation.cancel(); } catch (_) {}
    }
    if (activeTransition && typeof activeTransition.skipTransition === "function") {
      try { activeTransition.skipTransition(); } catch (_) {}
    }
    activeRevealAnimation = null;
    activeTransition = null;
    transitionBusy = false;
    root.classList.remove("d4y-theme-transition");
    if (wasBusy) announceAppearanceTransition(false);
  }

  function animateTheme(theme, source, event) {
    theme = theme === "dark" ? "dark" : "light";
    desiredTheme = theme;
    desiredThemePersist = true;
    if (transitionBusy) {
      interruptAppearance();
    }
    var skinChanged = desiredSkin !== root.dataset.skin;
    if (theme === root.dataset.theme && !skinChanged) {
      applyDesiredAppearance();
      return;
    }
    animateAppearance(
      applyDesiredAppearance,
      source,
      event,
      skinChanged
        ? (isLandingAppearance() ? LANDING_SKIN_TIMING : SKIN_TIMING)
        : (isLandingAppearance() ? LANDING_THEME_TIMING : null),
    );
  }

  function animateSkin(skin, source, event, persist) {
    skin = skin === "romantic" ? "romantic" : "friends";
    desiredSkin = skin;
    desiredSkinPersist = Boolean(persist);
    if (transitionBusy) {
      interruptAppearance();
    }
    var themeChanged = desiredTheme !== root.dataset.theme;
    if (skin === root.dataset.skin && !themeChanged) {
      applyDesiredAppearance();
      return;
    }
    animateAppearance(
      applyDesiredAppearance,
      source,
      event,
      isLandingAppearance() ? LANDING_SKIN_TIMING : SKIN_TIMING
    );
  }

  // Выполняется синхронно до CSS.
  // Cookie оформления читается только на явно переключаемой странице входа:
  // кабинет и публичные категории продолжают получать свой skin с сервера.
  if (root.hasAttribute("data-skin-switchable")) {
    applySkin(savedSkin(), false);
  }
  applyTheme(savedTheme(), false);
  desiredTheme = root.dataset.theme || "light";
  desiredSkin = root.dataset.skin || "friends";

  // Профиль хранит skin на сервере, но использует тот же движок волны. API не
  // меняет cookie и не смешивает оформление с light/dark-настройкой браузера.
  window.d4yAppearance = {
    applySkin: function (skin) {
      skin = skin === "romantic" ? "romantic" : "friends";
      desiredSkin = skin;
      desiredSkinPersist = false;
      applySkin(skin, false);
    },
    animateSkin: function (skin, source, event) {
      animateSkin(skin, source, event, false);
    }
  };

  // Убираем стандартное перекрёстное растворение View Transition: старый
  // снимок остаётся снизу, а новый раскрывается над ним круглой волной.
  var transitionStyle = document.createElement("style");
  transitionStyle.id = "d4y-theme-transition-style";
  transitionStyle.textContent =
    // Снимки View Transition лежат над DOM и без pointer-events:none
    // перехватывают второй click быстрого dblclick как click по <html>.
    ":root.d4y-theme-transition::view-transition{pointer-events:none;}" +
    ":root.d4y-theme-transition::view-transition-old(root)," +
    ":root.d4y-theme-transition::view-transition-new(root){" +
      "animation:none;mix-blend-mode:normal;pointer-events:none;}" +
    ":root.d4y-theme-transition::view-transition-old(root){z-index:1;}" +
    ":root.d4y-theme-transition::view-transition-new(root){z-index:2;}";
  document.head.appendChild(transitionStyle);

  function appearanceControlFromEvent(event) {
    var selector = "[data-skin-set],[data-theme-set],[data-theme-toggle]";
    var direct = event.target.closest && event.target.closest(selector);
    if (direct || !transitionBusy
        || !Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) {
      return direct;
    }

    // Chromium во время активной View Transition может отдать второй
    // click быстрого dblclick корневому <html>, хотя координаты всё ещё
    // лежат внутри кнопки. elementFromPoint в этот момент тоже возвращает
    // только <html>, поэтому восстанавливаем видимую цель по её DOM-геометрии.
    var controls = document.querySelectorAll(selector);
    for (var i = controls.length - 1; i >= 0; i -= 1) {
      var control = controls[i];
      var rect = control.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0
          && event.clientX >= rect.left && event.clientX <= rect.right
          && event.clientY >= rect.top && event.clientY <= rect.bottom) {
        return control;
      }
    }
    return null;
  }

  document.addEventListener("click", function (event) {
    var control = appearanceControlFromEvent(event);
    if (!control) return;
    if (control.hasAttribute("data-skin-set")) {
      event.preventDefault();
      animateSkin(control.getAttribute("data-skin-set"), control, event, true);
      return;
    }
    if (control.hasAttribute("data-theme-set")) {
      event.preventDefault();
      animateTheme(control.getAttribute("data-theme-set"), control, event);
      return;
    }
    event.preventDefault();
    animateTheme(desiredTheme === "dark" ? "light" : "dark", control, event);
  });

  document.addEventListener("DOMContentLoaded", function () {
    syncSkin();
    updateChrome(root.dataset.theme || "light");
    updateButtons(root.dataset.theme || "light");
    updateSkinButtons(root.dataset.skin || "friends");
    updateSkinAssets(root.dataset.skin || "friends");
  });
  document.addEventListener("d4y:skinchange", function () {
    updateChrome(root.dataset.theme || "light");
    updateSkinButtons(root.dataset.skin || "friends");
    updateSkinAssets(root.dataset.skin || "friends");
  });
  // Turbo заменяет body, но не перезапускает этот скрипт.
  document.addEventListener("turbo:load", function () {
    syncSkin();
    if (!transitionBusy) {
      desiredTheme = root.dataset.theme || "light";
      desiredSkin = root.dataset.skin || "friends";
    }
    updateChrome(root.dataset.theme || "light");
    updateButtons(root.dataset.theme || "light");
    updateSkinButtons(root.dataset.skin || "friends");
    // Turbo может заменить head-ссылки, не перезапуская theme.js. Каждый раз
    // доводим favicon/install-assets до фактического skin корневого документа.
    updateSkinAssets(root.dataset.skin || "friends");
  });
})();
