/* Контекстное обучение кабинета. Каждый раздел запоминается отдельно для
   текущего аккаунта; Turbo-навигация и прерванный тур обрабатываются явно. */
(function () {
  "use strict";

  var running = null;
  var attempted = {};
  var VERSIONS = {
    dashboard: 1,
    "dates-list": 1,
    "date-editor": 1,
    "categories-list": 1,
    "category-editor": 1
  };
  var ROUTES = {
    dashboard: "/admin/#tour=dashboard",
    "dates-list": "/admin/dates#tour=dates-list",
    "date-editor": "/admin/dates/new#tour=date-editor",
    "categories-list": "/admin/categories#tour=categories-list"
  };
  var DEFINITIONS = {
    dashboard: [
      { sel: '[data-tour="dashboard-feed"]', title: "Лента свиданий комьюнити",
        text: "Здесь находятся публичные идеи других людей. Понравившееся свидание можно сохранить себе." },
      { sel: '[data-tour="dashboard-create"]', title: "Создай своё свидание",
        text: "Открой редактор, добавь идею, время, место, фотографии и условия." },
      { sel: 'nav.glass-nav a[href="/admin/categories"]', title: "Собери подборку",
        text: "Категория объединяет свидания в одну секретную ссылку и задаёт правила голосования." },
      { sel: '.bell-link[aria-label="VPN"]', title: "Быстрый доступ к VPN",
        text: "Ссылка на VPN всегда находится в правой части шапки." }
    ],
    "dates-list": [
      { sel: '[data-tour="dates-create"]', title: "Создай свидание",
        text: "Кнопка открывает визуальный редактор новой идеи." },
      { sel: '[data-tour="dates-statuses"]', title: "Статусы свиданий",
        text: "Активные видны в категориях, неактивные ждут публикации или категории, прошедшие хранятся в архиве." },
      { sel: '[data-tour="dates-controls"]', title: "Найди нужное",
        text: "Сортируй и фильтруй свидания по категории, выборам гостей и наличию даты." },
      { sel: '[data-tour="dates-collection"]', title: "Управляй из списка",
        text: "Открой карточку для правки. В меню доступны ссылка, копирование, архив и удаление." }
    ],
    "date-editor": [
      { sel: '[data-tour="date-card-editor"]', title: "Карточка — это редактор",
        text: "Нажимай на название, место, описание и ссылки: изменения сразу выглядят так, как их увидит гость." },
      { sel: '[data-tour="date-media"]', title: "Фото и видео",
        text: "Добавляй медиа, листай и двигай фотографии, чтобы выбрать лучший кадр." },
      { sel: '[data-tour="date-when"]', title: "Когда и где",
        text: "Укажи начало, окончание и место. Поля можно оставить пустыми и договориться позже." },
      { sel: '[data-tour="date-modifiers"]', title: "Параметры свидания",
        text: "Здесь настраиваются условия оплаты и количество гостей. Создатель места не занимает." },
      { sel: '[data-tour="date-visibility"]', title: "Где показывать",
        text: "Добавь свидание в нужные категории и при желании оставь его в общей ленте." },
      { sel: '[data-tour="date-save"]', title: "Сохрани результат",
        text: "После сохранения свиданием можно делиться, управлять им из списка и следить за участниками." }
    ],
    "categories-list": [
      { sel: '[data-tour="category-create"]', title: "Создай категорию",
        text: "Категория — отдельная подборка со своей секретной ссылкой и дедлайном голосования." },
      { sel: '[data-tour="categories-collection"]', title: "Открой настройки",
        text: "Внутри категории можно оформить ссылку, добавить свидания и настроить правила выбора." },
      { sel: '[data-tour="categories-collection"] .menu-wrap, [data-tour="categories-collection"]',
        title: "Быстрые действия",
        text: "В меню можно открыть или скопировать ссылку, создать новую ссылку и удалить категорию." }
    ],
    "category-editor": [
      { sel: '[data-tour="category-description"]', title: "Название и описание",
        text: "Этот текст гости увидят в начале подборки." },
      { sel: '[data-tour="category-preview"]', title: "Превью ссылки",
        text: "Измени картинку и текст прямо здесь — так ссылка будет выглядеть в мессенджере." },
      { sel: '[data-tour="category-voting"]', title: "Правила голосования",
        text: "Выбери один или несколько вариантов на гостя и задай обязательный срок. После дедлайна система определит победителя." },
      { sel: '[data-tour="category-dates"]', title: "Наполни подборку",
        text: "Создай новое или добавь существующее свидание. Перетаскивание меняет порядок для гостей." },
      { sel: '[data-tour="category-actions"]', title: "Поделись ссылкой",
        text: "Скопируй гостевую ссылку, временно отключи её или замени новой." }
    ]
  };

  function screenId() {
    var p = location.pathname.replace(/\/+$/, "") || "/";
    if (p === "/admin") return "dashboard";
    if (p === "/admin/dates") return "dates-list";
    if (p === "/admin/dates/new" || /^\/admin\/dates\/\d+\/edit$/.test(p)) return "date-editor";
    if (p === "/admin/categories") return "categories-list";
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
        value.dashboard = Math.max(value.dashboard || 0, 1);
        localStorage.setItem(storageKey(), JSON.stringify(value));
        localStorage.removeItem("d4y_tour_v1");
      }
      return value;
    } catch (_) { return null; }
  }

  function markSeen(id) {
    var seen = readSeen();
    if (!seen) { attempted[id] = true; return; }
    seen[id] = VERSIONS[id] || 1;
    try { localStorage.setItem(storageKey(), JSON.stringify(seen)); }
    catch (_) { attempted[id] = true; }
  }

  function wasSeen(id) {
    var seen = readSeen();
    if (!seen) return !!attempted[id];
    return (seen[id] || 0) >= (VERSIONS[id] || 1);
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
      location.href = ROUTES[id] || "/admin/";
      return;
    }
    start(id, true);
  };

  function start(id, force) {
    if (running || !DEFINITIONS[id] || screenId() !== id) return;
    if (!force && wasSeen(id)) return;
    var steps = DEFINITIONS[id].filter(function (s) { return document.querySelector(s.sel); });
    if (!steps.length) { markSeen(id); return; }

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

    function continuation() {
      if (!force) return null;
      if (id === "dates-list") return "/admin/dates/new#tour=date-editor";
      if (id === "categories-list") {
        var first = document.querySelector("[data-tour-category-link]");
        return first ? first.getAttribute("href") + "#tour=category-editor" : null;
      }
      return null;
    }

    function layout() {
      if (!running || aborting) return;
      var step = steps[index];
      var target = document.querySelector(step.sel);
      if (!target) { advance(); return; }
      var r = target.getBoundingClientRect();
      var pad = innerWidth <= 720 ? 4 : 8;
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
      nextButton.textContent = last && continuation() ? "Продолжить" : (last ? "Готово" : "Далее");
      var popHeight = pop.offsetHeight || 160;
      pop.style.top = (r.bottom + 12 + popHeight <= innerHeight)
        ? (r.bottom + 12) + "px" : Math.max(12, r.top - popHeight - 12) + "px";
      pop.style.left = Math.min(Math.max(12, r.left), Math.max(12, innerWidth - pop.offsetWidth - 12)) + "px";
    }

    function place() {
      var target = document.querySelector(steps[index].sel);
      if (!target) { advance(); return; }
      document.body.classList.remove("tour-lock");
      target.scrollIntoView({ block: "center", behavior: "auto" });
      document.body.classList.add("tour-lock");
      requestAnimationFrame(function () { requestAnimationFrame(layout); });
    }

    function cleanup(completed) {
      if (aborting) return;
      aborting = true;
      if (completed) markSeen(id);
      window.removeEventListener("resize", layout);
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      document.body.classList.remove("tour-lock");
      running = null;
      if (previousFocus && previousFocus.focus) previousFocus.focus();
    }

    function advance() {
      if (index >= steps.length - 1) {
        var go = continuation();
        cleanup(true);
        if (go) location.href = go;
        return;
      }
      index += 1;
      place();
    }

    function onKey(e) {
      if (e.key === "Escape") { cleanup(true); return; }
      if (e.key !== "Tab") return;
      var controls = [overlay.querySelector(".tour-skip"), nextButton];
      var at = controls.indexOf(document.activeElement);
      e.preventDefault();
      controls[(at + (e.shiftKey ? 1 : 3)) % 2].focus();
    }

    nextButton.addEventListener("click", advance);
    overlay.querySelector(".tour-skip").addEventListener("click", function () { cleanup(true); });
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay || e.target === blur) cleanup(true);
    });
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", layout);
    running = { cancel: function () { cleanup(false); } };
    nextButton.focus();
    place();
  }

  function boot() {
    var id = forcedId();
    if (id) { clearForcedHash(); start(id, true); return; }
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
