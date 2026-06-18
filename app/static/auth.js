// Вход через Telegram: получаем код, открываем бота, поллим подтверждение.
(function () {
  "use strict";
  var btn = document.getElementById("tg-login");
  if (!btn) return;
  var hint = document.getElementById("tg-hint");
  var link = document.getElementById("tg-link");
  var errBox = document.getElementById("tg-error");
  var timer = null;

  // Гейт согласия (только на странице входа): пока чекбокс не отмечен —
  // deeplink-кнопка заблокирована, а виджет скрыт. В кабинете чекбокса нет —
  // тогда кнопка активна сразу.
  var consent = document.getElementById("tg-consent");
  if (consent) {
    var widgetWrap = document.getElementById("tg-widget-wrap");
    var widgetGate = document.getElementById("tg-widget-gate");
    var syncConsent = function () {
      var ok = consent.checked;
      btn.disabled = !ok;
      if (widgetWrap) widgetWrap.hidden = !ok;
      if (widgetGate) widgetGate.hidden = ok;
    };
    consent.addEventListener("change", syncConsent);
    syncConsent();
  }

  function showError(msg) {
    if (errBox) { errBox.textContent = msg; errBox.hidden = false; }
    btn.disabled = false;
    btn.textContent = "Войти через Telegram";
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
        if (link) link.href = d.url;
        if (hint) hint.hidden = false;
        clearInterval(timer);
        timer = setInterval(function () { poll(d.code); }, 2000);
      })
      .catch(function () { showError("Не получилось начать вход. Попробуй ещё раз."); });
  });
})();
