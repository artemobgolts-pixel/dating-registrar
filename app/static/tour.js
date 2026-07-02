// Обучающий тур при первом заходе в кабинет (только на главной, один раз).
// Подсвечивает ключевые элементы и ведёт по шагам. CSP-safe: внешний файл,
// без inline. Флаг «пройдено» — в localStorage. Уважает prefers-reduced-motion.
//
// Подсветка — «дырка» в затемнении: огромная box-shadow у .tour-spot гасит
// фон ВОКРУГ цели, а сама цель остаётся яркой и чёткой. Важно: overlay НЕ
// красит и НЕ блюрит весь экран (иначе цель тоже тускнела/размывалась — так
// и было в старой версии). На время тура блокируем прокрутку страницы, чтобы
// цель не «уезжала» из-под подсветки.
(function () {
  "use strict";
  var SEEN_KEY = "d4y_tour_v1";
  var running = false;          // защита от двойного запуска (ready + turbo:load)

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn, { once: true });
  }

  // Публичный запуск «повторить обучение» из профиля (кнопка «Обучение»):
  // снимаем флаг и стартуем принудительно (даже если уже пройдено).
  window.d4yStartTour = function () {
    try { localStorage.removeItem(SEEN_KEY); } catch (_) {}
    // тур живёт только на главной (там есть лента). С других страниц — уводим
    // на главную с якорем, тур сам стартует после перехода.
    if (!document.getElementById("communityFeed")) {
      location.href = "/admin/#tour";
      return;
    }
    startTour(true);
  };

  function startTour(force) {
    if (running) return;
    // тур только на главной кабинета и только если есть лента (значит, дашборд)
    if (!document.getElementById("communityFeed")) return;
    if (!force) {
      try { if (localStorage.getItem(SEEN_KEY)) return; } catch (_) { return; }
    }

    // Шаги: селектор целевого элемента + текст. Шаг без найденного элемента
    // пропускается (напр. блок «Поделиться» есть не у всех).
    var STEPS = [
      { sel: ".cfeed-card-wrap, #communityFeed",
        title: "Лента свиданий комьюнити",
        text: "Здесь — публичные свидания других людей. Листай и добавляй понравившиеся себе в коллекцию." },
      { sel: '.quick .btn.primary',
        title: "Создавай свои свидания",
        text: "Нажми «Создать свидание», опиши идею, добавь фото — и оно появится в твоей подборке." },
      { sel: 'nav.glass-nav a[href="/admin/categories"]',
        title: "Собирай их в категории",
        text: "Категории — это подборки-ссылки. Добавляй свидания в категорию и делись секретной ссылкой с тем, кого приглашаешь." },
      { sel: ".bell-link[aria-label='VPN'], a[aria-label='VPN']",
        title: "Нужен VPN?",
        text: "В правом верхнем углу — быстрый доступ к VPN, если какие-то сервисы не открываются." },
    ];

    var steps = STEPS.filter(function (s) { return document.querySelector(s.sel); });
    if (!steps.length) { markSeen(); return; }

    // ВАЖНО: помечаем «пройдено» сразу на старте, а не в finish(). Иначе второй
    // turbo:load (Turbo подменяет DOM) успевал запустить ДУБЛЬ тура до того, как
    // флаг выставлялся в finish() — отсюда «обучение повторилось 2 раза».
    running = true;
    markSeen();

    var i = 0;
    var overlay = document.createElement("div");
    overlay.className = "tour-overlay";
    overlay.innerHTML =
      '<div class="tour-spot" aria-hidden="true"></div>' +
      '<div class="tour-pop" role="dialog" aria-modal="true" aria-labelledby="tourTitle">' +
      '  <div class="tour-step" id="tourStepNo"></div>' +
      '  <h3 id="tourTitle"></h3>' +
      '  <p id="tourText"></p>' +
      '  <div class="tour-actions">' +
      '    <button type="button" class="tour-skip" id="tourSkip">Пропустить</button>' +
      '    <button type="button" class="tour-next btn primary" id="tourNext"></button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(overlay);
    document.body.classList.add("tour-lock");    // блокируем прокрутку страницы

    var spot = overlay.querySelector(".tour-spot");
    var pop = overlay.querySelector(".tour-pop");
    var elTitle = overlay.querySelector("#tourTitle");
    var elText = overlay.querySelector("#tourText");
    var elNo = overlay.querySelector("#tourStepNo");
    var elNext = overlay.querySelector("#tourNext");

    // Позиционируем подсветку и поповер по РЕАЛЬНОЙ геометрии цели. Отдельно от
    // прокрутки: сначала доводим цель в видимую область, ждём кадр — только потом
    // считаем прямоугольник (иначе на первом шаге rect ещё «схлопнут» — была
    // «просто линия» вместо рамки).
    function layout() {
      var step = steps[i];
      var target = document.querySelector(step.sel);
      if (!target) { next(); return; }
      var r = target.getBoundingClientRect();
      var pad = 8;
      spot.style.top = (r.top - pad) + "px";
      spot.style.left = (r.left - pad) + "px";
      spot.style.width = (r.width + pad * 2) + "px";
      spot.style.height = (r.height + pad * 2) + "px";
      elNo.textContent = "Шаг " + (i + 1) + " из " + steps.length;
      elTitle.textContent = step.title;
      elText.textContent = step.text;
      elNext.textContent = (i === steps.length - 1) ? "Готово" : "Далее";
      // поповер: под целью, либо над, если снизу нет места
      var popH = pop.offsetHeight || 180;
      var below = r.bottom + 12;
      if (below + popH > window.innerHeight && r.top - popH - 12 > 0) {
        pop.style.top = Math.max(12, r.top - popH - 12) + "px";
      } else {
        pop.style.top = Math.min(window.innerHeight - popH - 12, below) + "px";
      }
      var left = Math.min(Math.max(12, r.left), window.innerWidth - pop.offsetWidth - 12);
      pop.style.left = (isFinite(left) && left > 0 ? left : 12) + "px";
    }

    function place() {
      var step = steps[i];
      var target = document.querySelector(step.sel);
      if (!target) { next(); return; }
      // прокрутка страницы на время тура заблокирована, поэтому доводим цель в
      // зону видимости ВРЕМЕННО сняв замок, затем считаем геометрию после кадра.
      document.body.classList.remove("tour-lock");
      target.scrollIntoView({ block: "center", behavior: "auto" });
      document.body.classList.add("tour-lock");
      requestAnimationFrame(function () { requestAnimationFrame(layout); });
    }

    function next() {
      i++;
      if (i >= steps.length) { finish(); return; }
      place();
    }
    function finish() {
      markSeen();
      overlay.remove();
      document.body.classList.remove("tour-lock");
      window.removeEventListener("resize", layout);
      running = false;
    }

    elNext.addEventListener("click", next);
    overlay.querySelector("#tourSkip").addEventListener("click", finish);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) finish(); });
    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { finish(); document.removeEventListener("keydown", esc); }
    });
    window.addEventListener("resize", layout);

    place();
  }

  function markSeen() { try { localStorage.setItem(SEEN_KEY, "1"); } catch (_) {} }

  function boot() {
    // повтор обучения с другой страницы: /admin/#tour → стартуем принудительно
    if (location.hash === "#tour" && document.getElementById("communityFeed")) {
      try { history.replaceState(null, "", location.pathname + location.search); } catch (_) {}
      startTour(true);
      return;
    }
    startTour(false);
  }

  ready(boot);
  // под Turbo главная — новый DOM после перехода; пробуем снова (флаг не даст дубль)
  document.addEventListener("turbo:load", boot);
})();
