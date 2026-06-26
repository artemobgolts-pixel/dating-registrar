// Вход/подключение Telegram. Две независимые поверхности:
//   1) Страница входа (/login): чекбокс согласия открывает Telegram Login Widget.
//   2) Кабинет: блоки «Подключить уведомления» (.tg-connect) — deep-link + поллинг.
//      Их может быть несколько на странице (баннер вверху + карточка в профиле),
//      поэтому работаем по классу-контейнеру, а не по одиночному id.
(function () {
  "use strict";

  // --- 1. Гейт согласия на странице входа: согласие → показываем виджет --------
  var consent = document.getElementById("tg-consent");
  if (consent) {
    var widgetWrap = document.getElementById("tg-widget-wrap");
    var widgetGate = document.getElementById("tg-widget-gate");
    // Виджет Telegram вставляем динамически при первом согласии. Если оставить
    // <script> статически внутри закрытого <dialog> (display:none), Telegram
    // подменяет его на iframe нулевого размера и кнопка не появляется — поэтому
    // грузим скрипт, когда контейнер уже видим (диалог открыт).
    var widgetLoaded = false;
    function loadWidget() {
      if (widgetLoaded || !widgetWrap) return;
      widgetLoaded = true;
      var s = document.createElement("script");
      s.async = true;
      s.src = "https://telegram.org/js/telegram-widget.js?22";
      s.setAttribute("data-telegram-login", widgetWrap.getAttribute("data-bot") || "");
      s.setAttribute("data-size", "large");
      s.setAttribute("data-auth-url", "/auth/widget");
      widgetWrap.appendChild(s);
    }
    var syncConsent = function () {
      var ok = consent.checked;
      if (widgetWrap) widgetWrap.hidden = !ok;
      if (widgetGate) widgetGate.hidden = ok;
      // дублируем согласие в сессию: серверный /auth/widget без флага вернёт 403,
      // даже если кто-то покажет виджет правкой DOM. На гостевой ссылке окно входа
      // передаёт next (data-next) — куда вернуться после входа; на /login next уже
      // сохранён сервером при заходе на страницу.
      if (ok) {
        loadWidget();
        var nxt = consent.getAttribute("data-next");
        var url = "/auth/consent" + (nxt ? "?next=" + encodeURIComponent(nxt) : "");
        fetch(url, { method: "POST", credentials: "same-origin" })
          .catch(function () { /* сеть моргнула — повторим при следующем change */ });
      }
    };
    consent.addEventListener("change", syncConsent);
    syncConsent();
  }

  // --- 2. Подключение бота в кабинете (deep-link + поллинг) --------------------
  function wire(box) {
    var btn = box.querySelector("[data-tg-connect]");
    if (!btn) return;
    var hint = box.querySelector("[data-tg-hint]");
    var link = box.querySelector("[data-tg-link]");
    var errBox = box.querySelector("[data-tg-error]");
    var label = btn.textContent;
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
        })
        .catch(function () { /* временная сетевая ошибка — продолжаем поллинг */ });
    }

    btn.addEventListener("click", function () {
      if (errBox) errBox.hidden = true;
      btn.disabled = true;
      btn.textContent = "Открываю Telegram…";
      fetch("/auth/start", { method: "POST", credentials: "same-origin" })
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
  document.addEventListener("turbo:load", wireAll);
})();
