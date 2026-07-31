(function () {
  "use strict";

  var tg = window.Telegram && window.Telegram.WebApp;
  var root = document.body;

  function prepareTelegram() {
    if (!tg) return;
    document.documentElement.classList.add("tg-miniapp");
    try { tg.ready(); } catch (_) { /* old client */ }
    try { tg.expand(); } catch (_) { /* desktop/full-height */ }
    try { tg.setHeaderColor("secondary_bg_color"); } catch (_) { /* old client */ }
    try { tg.setBackgroundColor("bg_color"); } catch (_) { /* old client */ }
  }

  function updateBackButton() {
    if (!tg || !tg.BackButton || !document.body || document.body.dataset.miniappAuth) return;
    var atRoot = window.location.pathname === "/admin/";
    if (atRoot && typeof tg.BackButton.hide === "function") tg.BackButton.hide();
    else if (!atRoot && typeof tg.BackButton.show === "function") tg.BackButton.show();
  }

  function goBackToDashboard() {
    if (window.location.pathname !== "/admin/") window.location.assign("/admin/");
  }

  function initBridge() {
    prepareTelegram();
    if (!tg || !tg.BackButton) return;
    if (typeof tg.BackButton.offClick === "function") tg.BackButton.offClick(goBackToDashboard);
    if (typeof tg.BackButton.onClick === "function") tg.BackButton.onClick(goBackToDashboard);
    updateBackButton();
    document.addEventListener("turbo:load", updateBackButton);
  }

  function bootAuth() {
    if (!root || root.dataset.miniappAuth !== "1") return;
    var status = document.getElementById("miniappStatus");
    var spinner = document.getElementById("miniappSpinner");
    var actions = document.getElementById("miniappActions");
    var notifyButton = document.getElementById("miniappNotify");
    var continueButton = document.getElementById("miniappContinue");
    var browserLink = document.getElementById("miniappBrowserLink");

    function fail(message, canOpenTelegram) {
      status.textContent = message;
      if (spinner) spinner.hidden = true;
      if (actions) actions.hidden = true;
      if (browserLink && canOpenTelegram) browserLink.hidden = false;
    }

    function enter(url) { window.location.replace(url || "/admin/"); }

    if (!tg || !tg.initData) {
      fail("Откройте приложение из личного чата с ботом Telegram.", true);
      return;
    }
    prepareTelegram();
    fetch("/auth/miniapp", {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "Accept": "application/json"},
      body: JSON.stringify({
        init_data: tg.initData,
        nonce: root.dataset.miniappNonce || "",
        next: root.dataset.miniappNext || ""
      })
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.detail || "Не удалось подтвердить Telegram.");
        return data;
      });
    }).then(function (data) {
      if (data.notifications_enabled || typeof tg.requestWriteAccess !== "function") {
        enter(data.redirect);
        return;
      }
      spinner.hidden = true;
      status.textContent = "Вход выполнен. Разрешить боту присылать полезные уведомления?";
      actions.hidden = false;
      continueButton.addEventListener("click", function () { enter(data.redirect); }, {once: true});
      notifyButton.addEventListener("click", function () {
        notifyButton.disabled = true;
        var finished = false;
        function continueAfterPermission() {
          if (finished) return;
          finished = true;
          // Право подтвердит подписанный Telegram webhook service message.
          // Отказ или старый клиент никогда не отменяет уже выполненный вход.
          enter(data.redirect);
        }
        window.setTimeout(continueAfterPermission, 20000);
        try { tg.requestWriteAccess(continueAfterPermission); }
        catch (_) { continueAfterPermission(); }
      }, {once: true});
    }).catch(function (error) {
      fail(error.message || "Не удалось войти через Telegram.", true);
    });
  }

  initBridge();
  bootAuth();
}());
