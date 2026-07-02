// Обучающий тур при первом заходе в кабинет (только на главной, один раз).
// Подсвечивает ключевые элементы и ведёт по шагам. CSP-safe: внешний файл,
// без inline. Флаг «пройдено» — в localStorage. Уважает prefers-reduced-motion.
(function () {
  "use strict";
  var SEEN_KEY = "d4y_tour_v1";

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn, { once: true });
  }

  function startTour() {
    // тур только на главной кабинета и только если есть лента (значит, дашборд)
    if (!document.getElementById("communityFeed")) return;
    try { if (localStorage.getItem(SEEN_KEY)) return; } catch (_) { return; }

    // Шаги: селектор целевого элемента + текст. Шаг без найденного элемента
    // пропускается (напр. блок «Поделиться» есть не у всех).
    var STEPS = [
      { sel: "#communityFeed",
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

    var spot = overlay.querySelector(".tour-spot");
    var pop = overlay.querySelector(".tour-pop");
    var elTitle = overlay.querySelector("#tourTitle");
    var elText = overlay.querySelector("#tourText");
    var elNo = overlay.querySelector("#tourStepNo");
    var elNext = overlay.querySelector("#tourNext");

    function place() {
      var step = steps[i];
      var target = document.querySelector(step.sel);
      if (!target) { next(); return; }
      target.scrollIntoView({ block: "center", behavior: "smooth" });
      // считаем позицию после возможного скролла
      requestAnimationFrame(function () {
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
        var below = r.bottom + 12;
        var popH = 180;
        if (below + popH > window.innerHeight && r.top - popH - 12 > 0) {
          pop.style.top = Math.max(12, r.top - popH - 12) + "px";
        } else {
          pop.style.top = Math.min(window.innerHeight - popH - 12, below) + "px";
        }
        var left = Math.min(Math.max(12, r.left), window.innerWidth - pop.offsetWidth - 12);
        pop.style.left = (isFinite(left) ? left : 12) + "px";
      });
    }

    function next() {
      i++;
      if (i >= steps.length) { finish(); return; }
      place();
    }
    function finish() { markSeen(); overlay.remove(); window.removeEventListener("resize", place); }

    elNext.addEventListener("click", next);
    overlay.querySelector("#tourSkip").addEventListener("click", finish);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) finish(); });
    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { finish(); document.removeEventListener("keydown", esc); }
    });
    window.addEventListener("resize", place);

    place();
  }

  function markSeen() { try { localStorage.setItem(SEEN_KEY, "1"); } catch (_) {} }

  ready(startTour);
  // под Turbo главная — новый DOM после перехода; пробуем снова (флаг не даст дубль)
  document.addEventListener("turbo:load", startTour);
})();
