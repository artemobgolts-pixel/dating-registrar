// Вход/подключение Telegram. Две независимые поверхности:
//   1) Страница входа (/login) и вход-модалки: чекбокс согласия разблокирует
//      кнопки входа. Telegram-вход — по deep-link (start → бот → поллинг), а не
//      через iframe-виджет telegram.org (тот не грузится на части сетей/РФ-хостах
//      и оставлял пустое место вместо кнопки). OAuth-кнопки — обычные ссылки.
//   2) Кабинет: блоки «Подключить уведомления» (.tg-connect) — тот же deep-link.
(function () {
  "use strict";

  // --- 1. Гейт согласия + Telegram-вход по deep-link на странице/в модалке входа
  var consent = document.getElementById("tg-consent");
  if (consent) {
    var widgetGate = document.getElementById("tg-widget-gate");
    var methods = document.getElementById("loginMethods");
    var oauthLinks = methods ? methods.querySelectorAll("[data-oauth]") : [];
    var tgBtn = document.getElementById("tgLoginBtn");
    var tgLink = document.getElementById("tgLoginLink");
    var tgHint = document.getElementById("tgLoginHint");
    var tgErr = document.getElementById("tg-error");
    var tgTimer = null;

    var syncConsent = function () {
      var ok = consent.checked;
      if (methods) methods.classList.toggle("gated", !ok);
      if (widgetGate) widgetGate.hidden = ok;
      Array.prototype.forEach.call(oauthLinks, function (a) {
        a.setAttribute("aria-disabled", ok ? "false" : "true");
      });
      if (tgBtn) tgBtn.setAttribute("aria-disabled", ok ? "false" : "true");
      if (ok) {
        var nxt = consent.getAttribute("data-next");
        var url = "/auth/consent" + (nxt ? "?next=" + encodeURIComponent(nxt) : "");
        fetch(url, { method: "POST", credentials: "same-origin" })
          .catch(function () { /* сеть моргнула — повторим при следующем change */ });
      }
    };
    consent.addEventListener("change", syncConsent);

    // подсветка чекбокса при попытке войти без согласия
    function nudgeConsent() {
      if (widgetGate) { widgetGate.classList.remove("shake"); void widgetGate.offsetWidth; widgetGate.classList.add("shake"); }
      var box = consent.closest(".consent");
      if (box) { box.classList.remove("shake"); void box.offsetWidth; box.classList.add("shake"); }
    }
    function blockIfNoConsent(e) {
      if (consent.checked) return false;
      e.preventDefault();
      nudgeConsent();
      return true;
    }
    Array.prototype.forEach.call(oauthLinks, function (a) {
      a.addEventListener("click", blockIfNoConsent);
    });

    // Telegram-вход: deep-link + поллинг кода (тот же поток, что «Подключить бота»)
    if (tgBtn) {
      tgBtn.addEventListener("click", function (e) {
        if (blockIfNoConsent(e)) return;
        if (tgErr) tgErr.hidden = true;
        tgBtn.setAttribute("aria-busy", "true");
        var nxt = consent.getAttribute("data-next");
        var url = "/auth/start";
        fetch(url, { method: "POST", credentials: "same-origin" })
          .then(function (r) { if (!r.ok) throw new Error("start"); return r.json(); })
          .then(function (d) {
            window.open(d.url, "_blank", "noopener");
            if (tgLink) { tgLink.href = d.url; tgLink.hidden = false; }
            if (tgHint) tgHint.hidden = false;
            clearInterval(tgTimer);
            tgTimer = setInterval(function () {
              fetch("/auth/poll?code=" + encodeURIComponent(d.code), { credentials: "same-origin" })
                .then(function (r) { return r.json(); })
                .then(function (p) {
                  if (p.status === "ok") { clearInterval(tgTimer); window.location = p.redirect || "/admin/"; }
                  else if (p.status === "expired") { clearInterval(tgTimer); if (tgErr) { tgErr.textContent = "Код истёк — нажми кнопку ещё раз."; tgErr.hidden = false; } }
                  else if (p.status === "banned") { clearInterval(tgTimer); if (tgErr) { tgErr.textContent = "Доступ закрыт. Напиши в поддержку."; tgErr.hidden = false; } }
                })
                .catch(function () { /* временная ошибка сети — продолжаем поллинг */ });
            }, 2000);
          })
          .catch(function () {
            tgBtn.removeAttribute("aria-busy");
            if (tgErr) { tgErr.textContent = "Не получилось начать вход. Попробуй ещё раз."; tgErr.hidden = false; }
          });
      });
    }

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
