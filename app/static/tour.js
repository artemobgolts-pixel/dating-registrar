/* Контекстное обучение кабинета. Каждый раздел запоминается отдельно для
   текущего аккаунта; Turbo-навигация и прерванный тур обрабатываются явно. */
(function () {
  "use strict";

  var running = null;
  var attempted = {};
  var REQUEST_KEY = "d4y_tour_request";
  var ROUTES = {
    dashboard: "/admin/",
    "date-editor": "/admin/dates/new"
  };
  var DEFINITIONS = {
    dashboard: [
      { sel: '[data-tour="dashboard-create"]', title: "Создай своё событие",
        text: "Открой редактор, добавь идею, время, место, фотографии и условия." },
      { sel: 'nav.glass-nav a[href="/admin/categories"]', title: "Собери подборку",
        text: "Категория объединяет события в одну секретную ссылку и задаёт правила голосования." },
      { sel: '[data-tour="dashboard-feed"]', extra: "#communityFeed .cfeed-card:first-child",
        title: "Лента встреч сообщества",
        text: "Здесь находятся публичные идеи других людей. Понравившееся событие можно сохранить себе." },
      { sel: '[data-tour="dashboard-share"]', title: "Ссылка",
        text: "Делись ссылкой с друзьями." },
      { sel: '.bell-link[aria-label="VPN"]', title: "Нужен VPN?",
        text: "Тогда жми сюда и забирай бесплатный пробный период." }
    ],
    "date-editor": [
      { sel: '[data-tour="date-card-editor"]', title: "Карточка — это редактор",
        text: "Нажимай на название, место, описание и ссылки: изменения сразу выглядят так, как их увидит гость." },
      { sel: '[data-tour="date-modifiers"]', title: "Параметры события",
        text: "Здесь настраиваются условия оплаты и количество гостей. Создатель места не занимает." },
      { sel: '[data-tour="date-visibility"]', title: "Где показывать",
        text: "Добавь событие в нужные категории и при желании оставь его в общей ленте." },
    ],
    "category-editor": [
      { sel: '[data-tour="category-description"]', title: "Название и описание", pad: 14,
        text: "Этот текст гости увидят в начале подборки." },
      { sel: '[data-tour="category-skin"]', title: "Оформление страницы", pad: 12,
        text: "Выбери дружеский или романтический дизайн. Тему увидят все, кто откроет ссылку." },
      { sel: '[data-tour="category-preview"]', title: "Превью ссылки",
        text: "Измени картинку и текст прямо здесь — так ссылка будет выглядеть в мессенджере." },
      { sel: '[data-tour="category-voting"]', title: "Правила голосования",
        text: "Выбери один или несколько вариантов на гостя и задай обязательный срок. После дедлайна система определит победителя." },
      { sel: '[data-tour="category-dates"]', title: "Наполни подборку",
        text: "Создай новое или добавь существующее событие. Перетаскивание меняет порядок для гостей." },
      { sel: '[data-tour="category-actions"]', title: "Поделись ссылкой",
        text: "Открой меню «⋯», скопируй гостевую ссылку и отправь её друзьям." }
    ]
  };

  function screenId() {
    var p = location.pathname.replace(/\/+$/, "") || "/";
    if (p === "/admin") return "dashboard";
    if (p === "/admin/dates/new" || /^\/admin\/dates\/\d+\/edit$/.test(p)) return "date-editor";
    if (/^\/admin\/categories\/\d+$/.test(p)) return "category-editor";
    return null;
  }

  function storageKey() {
    return "d4y_tours:" + (document.body.dataset.userId || "account");
  }

  function readSeen() {
    try {
      var raw = localStorage.getItem(storageKey());
      var value = raw ? JSON.parse(raw) : {};
      if (!value || typeof value !== "object" || Array.isArray(value)) value = {};
      if (localStorage.getItem("d4y_tour_v1")) {
        value.dashboard = true;
        localStorage.setItem(storageKey(), JSON.stringify(value));
        localStorage.removeItem("d4y_tour_v1");
      }
      return value;
    } catch (_) { return null; }
  }

  function markSeen(id) {
    var seen = readSeen();
    if (!seen) { attempted[id] = true; return; }
    seen[id] = true;
    try { localStorage.setItem(storageKey(), JSON.stringify(seen)); }
    catch (_) { attempted[id] = true; }
  }

  function wasSeen(id) {
    var seen = readSeen();
    if (!seen) return !!attempted[id];
    return !!seen[id];
  }

  function forcedId() {
    if (location.hash.indexOf("#tour=") !== 0) return null;
    var id = decodeURIComponent(location.hash.slice(6));
    return DEFINITIONS[id] ? id : null;
  }

  function clearForcedHash() {
    try { history.replaceState(null, "", location.pathname + location.search); } catch (_) {}
  }

  window.d4yStartTour = function (id) {
    id = DEFINITIONS[id] ? id : "dashboard";
    // Ручной повтор запускает только выбранную часть. В частности, «Основы»
    // не должны подхватывать оставшийся от другого курса переход.
    try { sessionStorage.removeItem(REQUEST_KEY); } catch (_) {}
    if (screenId() !== id) {
      try { sessionStorage.setItem(REQUEST_KEY, id); } catch (_) {}
      var route = ROUTES[id] || "/admin/";
      if (window.Turbo && window.Turbo.visit) window.Turbo.visit(route);
      else location.assign(route);
      return;
    }
    start(id, true);
  };

  function start(id, force) {
    if (running || !DEFINITIONS[id] || screenId() !== id) return;
    if (!force && wasSeen(id)) return;
    var steps = DEFINITIONS[id].filter(function (s) { return document.querySelector(s.sel); });
    if (!steps.length) { markSeen(id); return; }
    // Считаем обучение показанным сразу после фактического запуска. Поэтому
    // закрытие страницы или прерванный тур не вызовут повторный автозапуск.
    // Ручной запуск из профиля по-прежнему передаёт force=true.
    markSeen(id);

    var index = 0;
    var previousFocus = document.activeElement;
    var overlay = document.createElement("div");
    overlay.className = "tour-overlay";
    overlay.innerHTML =
      '<div class="tour-blur" aria-hidden="true"></div>' +
      '<div class="tour-spot" aria-hidden="true"></div>' +
      '<div class="tour-pop" role="dialog" aria-modal="true" aria-labelledby="tourTitle">' +
      '<div class="tour-step" id="tourStepNo"></div><h3 id="tourTitle"></h3>' +
      '<p id="tourText"></p><div class="tour-actions">' +
      '<button type="button" class="tour-skip">Пропустить</button>' +
      '<button type="button" class="tour-next btn primary"></button></div></div>';
    document.body.appendChild(overlay);

    var spot = overlay.querySelector(".tour-spot");
    var blur = overlay.querySelector(".tour-blur");
    var pop = overlay.querySelector(".tour-pop");
    var nextButton = overlay.querySelector(".tour-next");
    var aborting = false;
    var placing = false;
    var placeAgain = false;
    var placeFrame = 0;
    var placeToken = 0;
    var geometryFrame = 0;
    var geometryFramesLeft = 0;
    var lastGeometry = "";
    var resizeObserver = null;
    var mutationObserver = null;
    var observedTarget = null;
    var observedExtra = null;

    function setScrollLock(locked) {
      if (locked) {
        document.documentElement.classList.add("tour-lock");
        document.body.classList.add("tour-lock");
      } else {
        document.documentElement.classList.remove("tour-lock");
        document.body.classList.remove("tour-lock");
      }
    }

    function extraTarget(step) {
      return step.extra ? document.querySelector(step.extra) : null;
    }

    function stepRect(step, target) {
      var r = target.getBoundingClientRect();
      var extra = extraTarget(step);
      if (!extra) return r;
      var e = extra.getBoundingClientRect();
      return {
        top: Math.min(r.top, e.top),
        left: Math.min(r.left, e.left),
        right: Math.max(r.right, e.right),
        bottom: Math.max(r.bottom, e.bottom),
        width: Math.max(r.right, e.right) - Math.min(r.left, e.left),
        height: Math.max(r.bottom, e.bottom) - Math.min(r.top, e.top)
      };
    }

    function viewportSize() {
      return {
        width: Math.max(1, document.documentElement.clientWidth || innerWidth),
        height: Math.max(1, innerHeight || document.documentElement.clientHeight)
      };
    }

    function isViewportAnchored(target) {
      for (var node = target; node && node !== document.documentElement; node = node.parentElement) {
        if (getComputedStyle(node).position === "fixed") return true;
      }
      return false;
    }

    function updateCopy() {
      var step = steps[index];
      overlay.querySelector("#tourStepNo").textContent = "Шаг " + (index + 1) + " из " + steps.length;
      overlay.querySelector("#tourTitle").textContent = step.title;
      overlay.querySelector("#tourText").textContent = step.text;
      nextButton.textContent = index === steps.length - 1 ? "Готово" : "Далее";
    }

    function layout(measuredRect) {
      if (!running || aborting) return;
      var step = steps[index];
      var target = document.querySelector(step.sel);
      if (!target) { advance(); return; }
      var r = measuredRect && typeof measuredRect.top === "number"
        ? measuredRect : stepRect(step, target);
      var viewport = viewportSize();
      var pad = typeof step.pad === "number" ? step.pad : (innerWidth <= 720 ? 4 : 8);
      // Если цель выше экрана (например, большая карточка редактора), рамка
      // показывает её видимую часть и никогда не уезжает за границы viewport.
      var top = Math.max(4, r.top - pad);
      var left = Math.max(4, r.left - pad);
      var right = Math.min(viewport.width - 4, r.right + pad);
      var bottom = Math.min(viewport.height - 4, r.bottom + pad);
      var width = Math.max(1, right - left);
      var height = Math.max(1, bottom - top);
      spot.style.cssText = "top:" + top + "px;left:" + left + "px;width:" + width + "px;height:" + height + "px";
      if (blur) {
        var rr = Math.min(14, width / 2, height / 2), x1 = left + width, y1 = top + height;
        var hole = (left + rr) + "px " + top + "px," + (x1 - rr) + "px " + top + "px," +
          x1 + "px " + (top + rr) + "px," + x1 + "px " + (y1 - rr) + "px," +
          (x1 - rr) + "px " + y1 + "px," + (left + rr) + "px " + y1 + "px," +
          left + "px " + (y1 - rr) + "px," + left + "px " + (top + rr) + "px";
        blur.style.clipPath = "polygon(evenodd,0 0,100% 0,100% 100%,0 100%,0 0," + hole + ")";
        blur.style.webkitClipPath = blur.style.clipPath;
      }
      updateCopy();
      var popHeight = pop.offsetHeight || 160;
      var below = bottom + 12;
      var above = top - popHeight - 12;
      var popTop;
      if (below + popHeight <= viewport.height - 12) popTop = below;
      else if (above >= 12) popTop = above;
      else {
        // Высокая цель не может поместиться рядом с подсказкой. Закрепляем
        // подсказку у более далёкого края, сохраняя максимум цели открытым.
        popTop = (r.top + r.bottom) / 2 < viewport.height / 2
          ? viewport.height - popHeight - 12 : 12;
      }
      popTop = Math.min(Math.max(12, popTop), Math.max(12, viewport.height - popHeight - 12));
      pop.style.top = popTop + "px";
      pop.style.left = Math.min(Math.max(12, r.left), Math.max(12, viewport.width - pop.offsetWidth - 12)) + "px";
    }

    function refreshLayout() { layout(); }

    function onPageMotion(e) {
      // Анимация самой рамки тура не должна заново запускать измерения.
      if (!overlay.contains(e.target)) schedulePlace();
    }

    function observeStep(target, extra) {
      if (target === observedTarget && extra === observedExtra) return;
      observedTarget = target;
      observedExtra = extra;
      if (!resizeObserver) return;
      resizeObserver.disconnect();
      resizeObserver.observe(target);
      if (extra) resizeObserver.observe(extra);
    }

    function desiredScrollTop(step, target, r) {
      if (isViewportAnchored(target)) return window.pageYOffset || 0;
      var viewport = viewportSize();
      var margin = 12;
      var gap = 12;
      var usable = viewport.height - margin * 2;
      var popHeight = pop.offsetHeight || 160;
      var total = r.height + gap + popHeight;
      var desiredTop;
      if (total <= usable) {
        // Цель и подсказка помещаются вместе: центрируем весь комплект. Для
        // community-шага сюда входит и заголовок, и первая карточка целиком.
        desiredTop = margin + (usable - total) / 2;
      } else if (r.height <= usable) {
        desiredTop = margin + (usable - r.height) / 2;
      } else {
        desiredTop = margin;
      }
      var scrolling = document.scrollingElement || document.documentElement;
      var current = window.pageYOffset || scrolling.scrollTop || 0;
      var max = Math.max(0, scrolling.scrollHeight - viewport.height);
      return Math.min(max, Math.max(0, current + r.top - desiredTop));
    }

    function finishPlace(token) {
      if (aborting || token !== placeToken) return;
      setScrollLock(true);
      placing = false;
      lastGeometry = "";
      layout();
      if (placeAgain) {
        placeAgain = false;
        schedulePlace();
      }
    }

    function place() {
      if (aborting || !running) return;
      if (placing) { placeAgain = true; return; }
      var step = steps[index];
      var target = document.querySelector(step.sel);
      if (!target) { advance(); return; }
      var extra = extraTarget(step);
      observeStep(target, extra);
      updateCopy();
      var r = stepRect(step, target);
      var scrolling = document.scrollingElement || document.documentElement;
      var current = window.pageYOffset || scrolling.scrollTop || 0;
      var wanted = desiredScrollTop(step, target, r);
      var token = ++placeToken;
      placing = true;
      if (Math.abs(wanted - current) < 1) {
        finishPlace(token);
        return;
      }
      // На iOS программная прокрутка заблокированного html ненадёжна. Замок
      // снимается ровно на время синхронного scrollTo и двух кадров раскладки.
      setScrollLock(false);
      window.scrollTo(window.pageXOffset || 0, wanted);
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { finishPlace(token); });
      });
    }

    function schedulePlace() {
      if (aborting || !running) return;
      stabilizeGeometry(24);
      if (placing) { placeAgain = true; return; }
      if (placeFrame) return;
      placeFrame = requestAnimationFrame(function () {
        placeFrame = 0;
        place();
      });
    }

    function stabilizeGeometry(frames) {
      if (aborting || !running) return;
      geometryFramesLeft = Math.max(geometryFramesLeft, frames || 24);
      if (!geometryFrame) geometryFrame = requestAnimationFrame(watchGeometry);
    }

    function watchGeometry() {
      if (aborting || !running) return;
      geometryFrame = 0;
      var step = steps[index];
      var target = document.querySelector(step.sel);
      if (target) {
        var extra = extraTarget(step);
        if (target !== observedTarget || extra !== observedExtra) {
          observeStep(target, extra);
          schedulePlace();
        }
        var r = stepRect(step, target);
        var viewport = viewportSize();
        var signature = [r.top, r.left, r.width, r.height, viewport.width, viewport.height]
          .map(function (n) { return Math.round(n * 2) / 2; }).join(":");
        // Карточки и секции могут сдвигаться после загрузки изображений,
        // анимаций или изменений формы. Spotlight следует за реальной целью.
        if (signature !== lastGeometry && !placing) {
          lastGeometry = signature;
          layout(r);
        }
      }
      geometryFramesLeft -= 1;
      if (geometryFramesLeft > 0) geometryFrame = requestAnimationFrame(watchGeometry);
    }

    function cleanup() {
      if (aborting) return;
      aborting = true;
      placeToken += 1;
      if (placeFrame) cancelAnimationFrame(placeFrame);
      if (geometryFrame) cancelAnimationFrame(geometryFrame);
      geometryFrame = 0;
      geometryFramesLeft = 0;
      if (resizeObserver) resizeObserver.disconnect();
      if (mutationObserver) mutationObserver.disconnect();
      window.removeEventListener("resize", schedulePlace);
      window.removeEventListener("orientationchange", schedulePlace);
      window.removeEventListener("scroll", refreshLayout);
      if (window.visualViewport) {
        window.visualViewport.removeEventListener("resize", schedulePlace);
        window.visualViewport.removeEventListener("scroll", refreshLayout);
      }
      document.removeEventListener("load", schedulePlace, true);
      document.removeEventListener("transitionrun", onPageMotion, true);
      document.removeEventListener("transitionend", onPageMotion, true);
      document.removeEventListener("animationstart", onPageMotion, true);
      document.removeEventListener("animationend", onPageMotion, true);
      document.removeEventListener("keydown", onKey);
      overlay.removeEventListener("wheel", preventScroll);
      overlay.removeEventListener("touchmove", preventScroll);
      overlay.remove();
      setScrollLock(false);
      running = null;
      if (previousFocus && previousFocus.focus) previousFocus.focus();
    }

    function advance() {
      if (index >= steps.length - 1) {
        cleanup();
        return;
      }
      index += 1;
      lastGeometry = "";
      place();
    }

    function onKey(e) {
      if (e.key === "Escape") { cleanup(); return; }
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].indexOf(e.key) !== -1 ||
          (e.key === " " && !e.target.closest("button"))) {
        e.preventDefault();
        return;
      }
      if (e.key !== "Tab") return;
      var controls = [overlay.querySelector(".tour-skip"), nextButton];
      var at = controls.indexOf(document.activeElement);
      e.preventDefault();
      controls[(at + (e.shiftKey ? 1 : 3)) % 2].focus();
    }

    function preventScroll(e) { e.preventDefault(); }

    nextButton.addEventListener("click", advance);
    overlay.querySelector(".tour-skip").addEventListener("click", cleanup);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay || e.target === blur) cleanup();
    });
    overlay.addEventListener("wheel", preventScroll, { passive: false });
    overlay.addEventListener("touchmove", preventScroll, { passive: false });
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", schedulePlace);
    window.addEventListener("orientationchange", schedulePlace);
    window.addEventListener("scroll", refreshLayout, { passive: true });
    document.addEventListener("load", schedulePlace, true);
    document.addEventListener("transitionrun", onPageMotion, true);
    document.addEventListener("transitionend", onPageMotion, true);
    document.addEventListener("animationstart", onPageMotion, true);
    document.addEventListener("animationend", onPageMotion, true);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", schedulePlace);
      window.visualViewport.addEventListener("scroll", refreshLayout, { passive: true });
    }
    if ("ResizeObserver" in window) resizeObserver = new ResizeObserver(schedulePlace);
    if ("MutationObserver" in window) {
      mutationObserver = new MutationObserver(schedulePlace);
      var tourContent = document.querySelector("main.wrap") || document.body;
      mutationObserver.observe(tourContent, {
        childList: true, subtree: true, attributes: true,
        attributeFilter: ["class", "style", "hidden", "open"]
      });
    }
    running = { cancel: cleanup };
    setScrollLock(true);
    nextButton.focus();
    place();
    stabilizeGeometry(24);
  }

  function boot() {
    var id = forcedId();
    if (id) { clearForcedHash(); start(id, true); return; }
    try {
      id = sessionStorage.getItem(REQUEST_KEY);
      if (id) sessionStorage.removeItem(REQUEST_KEY);
    } catch (_) { id = null; }
    if (id && id === screenId() && DEFINITIONS[id]) { start(id, true); return; }
    id = screenId();
    if (id) start(id, false);
  }

  document.addEventListener("turbo:before-render", function () {
    if (running) running.cancel();
  });
  document.addEventListener("turbo:load", function () {
    requestAnimationFrame(function () { requestAnimationFrame(boot); });
  });
  if (!window.Turbo) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, { once: true });
    else boot();
  }
})();
