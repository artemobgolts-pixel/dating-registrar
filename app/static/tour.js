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
      { sel: '[data-tour="category-share-copy"]', title: "Поделись ссылкой",
        text: "Скопируй гостевую ссылку и отправь её друзьям." }
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
    document.body.classList.add("tour-lock");

    var spot = overlay.querySelector(".tour-spot");
    var blur = overlay.querySelector(".tour-blur");
    var pop = overlay.querySelector(".tour-pop");
    var nextButton = overlay.querySelector(".tour-next");
    var aborting = false;

    function stepRect(step, target) {
      var r = target.getBoundingClientRect();
      var extra = step.extra && document.querySelector(step.extra);
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

    function layout() {
      if (!running || aborting) return;
      var step = steps[index];
      var target = document.querySelector(step.sel);
      if (!target) { advance(); return; }
      var r = stepRect(step, target);
      var pad = typeof step.pad === "number" ? step.pad : (innerWidth <= 720 ? 4 : 8);
      var top = r.top - pad, left = r.left - pad;
      var width = r.width + pad * 2, height = r.height + pad * 2;
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
      overlay.querySelector("#tourStepNo").textContent = "Шаг " + (index + 1) + " из " + steps.length;
      overlay.querySelector("#tourTitle").textContent = step.title;
      overlay.querySelector("#tourText").textContent = step.text;
      var last = index === steps.length - 1;
      nextButton.textContent = last ? "Готово" : "Далее";
      var popHeight = pop.offsetHeight || 160;
      pop.style.top = (r.bottom + 12 + popHeight <= innerHeight)
        ? (r.bottom + 12) + "px" : Math.max(12, r.top - popHeight - 12) + "px";
      pop.style.left = Math.min(Math.max(12, r.left), Math.max(12, innerWidth - pop.offsetWidth - 12)) + "px";
    }

    function place() {
      var target = document.querySelector(steps[index].sel);
      if (!target) { advance(); return; }
      document.documentElement.classList.remove("tour-lock");
      document.body.classList.remove("tour-lock");
      target.scrollIntoView({ block: "center", behavior: "auto" });
      document.documentElement.classList.add("tour-lock");
      document.body.classList.add("tour-lock");
      requestAnimationFrame(function () { requestAnimationFrame(layout); });
    }

    function cleanup() {
      if (aborting) return;
      aborting = true;
      window.removeEventListener("resize", layout);
      document.removeEventListener("keydown", onKey);
      overlay.removeEventListener("wheel", preventScroll);
      overlay.removeEventListener("touchmove", preventScroll);
      overlay.remove();
      document.documentElement.classList.remove("tour-lock");
      document.body.classList.remove("tour-lock");
      running = null;
      if (previousFocus && previousFocus.focus) previousFocus.focus();
    }

    function advance() {
      if (index >= steps.length - 1) {
        cleanup();
        return;
      }
      index += 1;
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
    window.addEventListener("resize", layout);
    running = { cancel: cleanup };
    document.documentElement.classList.add("tour-lock");
    nextButton.focus();
    place();
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
