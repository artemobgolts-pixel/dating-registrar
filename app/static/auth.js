// Вход/подключение Telegram. Две независимые поверхности:
//   1) Страница входа (/login) и вход-модалки: официальный Telegram Login Widget
//      + обычные OAuth-ссылки. Согласие с документами показывается пассивным
//      текстом в шаблоне и не блокирует способы входа.
//   2) Кабинет: блоки «Подключить уведомления» (.tg-connect) — deep-link в бота.
(function () {
  "use strict";

  // --- 1. Telegram Login Widget ---------------------------------------------
  function loadLoginWidget(wrap) {
    if (!wrap || wrap.dataset.tgLoaded || !wrap.getAttribute("data-bot")) return;
    wrap.dataset.tgLoaded = "1";
    var s = document.createElement("script");
    s.async = true;
    s.src = "https://telegram.org/js/telegram-widget.js?22";
    s.setAttribute("data-telegram-login", wrap.getAttribute("data-bot"));
    s.setAttribute("data-size", "large");
    s.setAttribute("data-radius", "12");
    s.setAttribute("data-auth-url", wrap.getAttribute("data-auth-url") || "/auth/widget");
    s.setAttribute("data-request-access", "write");
    wrap.appendChild(s);
  }

  function prepareLoginWidget(wrap) {
    if (!wrap || wrap.dataset.tgPrepared) return;
    wrap.dataset.tgPrepared = "1";
    var dialog = wrap.closest("dialog");
    if (!dialog || dialog.open) {
      loadLoginWidget(wrap);
      return;
    }
    // Telegram превращает скрипт в iframe с нулевыми размерами, если выполнить
    // его внутри закрытого dialog. Ждём появления атрибута open и грузим один раз.
    var observer = new MutationObserver(function () {
      if (!dialog.open) return;
      observer.disconnect();
      loadLoginWidget(wrap);
    });
    observer.observe(dialog, { attributes: true, attributeFilter: ["open"] });
  }

  function prepareLoginWidgets() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-tg-widget]"),
      prepareLoginWidget);
  }
  prepareLoginWidgets();

  // --- 2. Подключение бота в кабинете (deep-link + поллинг) --------------------
  function wire(box) {
    var btn = box.querySelector("[data-tg-connect]");
    if (!btn) return;
    var hint = box.querySelector("[data-tg-hint]");
    var link = box.querySelector("[data-tg-link]");
    var errBox = box.querySelector("[data-tg-error]");
    var label = btn.textContent;
    var returnTo = box.getAttribute("data-return-to") || "";
    var timer = null;

    function showError(msg) {
      if (errBox) { errBox.textContent = msg; errBox.hidden = false; }
      btn.disabled = false;
      btn.textContent = label;
    }

    function poll(code) {
      fetch("/auth/poll?code=" + encodeURIComponent(code), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status === "ok") { window.location = d.redirect || "/admin/"; return; }
          if (d.status === "expired") { clearInterval(timer); showError("Код истёк — нажми кнопку ещё раз."); return; }
          if (d.status === "banned") { clearInterval(timer); showError("Доступ закрыт. Напиши в поддержку."); return; }
          if (d.status === "conflict") { clearInterval(timer); showError("Этот Telegram уже связан с другим аккаунтом."); return; }
          if (d.status === "forbidden") { clearInterval(timer); showError("Код создан в другой сессии. Начни подключение заново."); return; }
        })
        .catch(function () { /* временная сетевая ошибка — продолжаем поллинг */ });
    }

    btn.addEventListener("click", function () {
      if (errBox) errBox.hidden = true;
      btn.disabled = true;
      btn.textContent = "Открываю Telegram…";
      var startUrl = "/auth/start" + (returnTo
        ? "?return_to=" + encodeURIComponent(returnTo) : "");
      fetch(startUrl, { method: "POST", credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok) throw new Error("start failed");
          return r.json();
        })
        .then(function (d) {
          window.open(d.url, "_blank", "noopener");
          if (link) { link.href = d.url; link.hidden = false; }
          if (hint) hint.hidden = false;
          clearInterval(timer);
          timer = setInterval(function () { poll(d.code); }, 2000);
        })
        .catch(function () { showError("Не получилось начать. Попробуй ещё раз."); });
    });
  }

  function wireAll() {
    Array.prototype.forEach.call(document.querySelectorAll(".tg-connect"), function (box) {
      if (box.dataset.wired) return;     // не вешаем повторно (Turbo пере-рендер)
      box.dataset.wired = "1";
      wire(box);
    });
  }
  wireAll();
  // под Turbo баннер «подключить бота» — новый узел после перехода: переинициализируем
  document.addEventListener("turbo:load", function () {
    prepareLoginWidgets();
    wireAll();
  });
})();
